"""Tests for history_store on the in-memory store, plus the shared contract run against Postgres when
COACH_TEST_DATABASE_URL is set and psycopg is installed (CI's pgvector service). Standard library only:

    cd ext && python3 -m unittest test_history_store -v
"""

from __future__ import annotations

import datetime as dt
import hmac
import hashlib
import importlib.util
import json
import os
import unittest

import history_docs
import history_extract as hx
import history_store as hs

FP = "a" * 64
OTHER = "b" * 64
D = dt.date(2026, 3, 18)
MONDAY = dt.date(2026, 3, 16)
DB_URL = os.environ.get("COACH_TEST_DATABASE_URL", "").strip()
HAVE_PSYCOPG = importlib.util.find_spec("psycopg") is not None
PG_SKIP = "needs psycopg and COACH_TEST_DATABASE_URL (Postgres with pgvector)"


def day(**kw):
    n = {"mode": "full", "sleep_seconds": 27000, "hrv": 60, "sessions": 1, "run_sessions": 1,
         "run_seconds": 3600.0, "run_meters": 10000.0, "missing": []}
    n.update(kw)
    return n


def empty_day():
    return {"mode": "key", "sessions": 0, "missing": []}


def week(**kw):
    n = {"mode": "full", "rules_version": 1, "level": "green", "flags": [], "missing": ["plan"], "total_hours": 5.0}
    n.update(kw)
    return n


class StoreContract:
    """Every HistoryStore must pass these (memory always, Postgres in CI)."""

    def make(self):
        raise NotImplementedError

    def setUp(self):
        self.store = self.make()
        self.store.migrate()

    def test_upsert_day_is_idempotent(self):
        s = self.store
        self.assertTrue(s.upsert_day(FP, D, day(), False))
        self.assertFalse(s.upsert_day(FP, D, day(), False))
        rows = s.days(FP, D, D)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["numbers"]["hrv"], 60)
        self.assertEqual(rows[0]["date"], D)
        self.assertTrue(s.upsert_day(FP, D, day(hrv=61), False))
        self.assertEqual(s.days(FP, D, D)[0]["numbers"]["hrv"], 61)

    def test_final_and_rechecked_never_go_back(self):
        s = self.store
        s.upsert_day(FP, D, day(), True, rechecked=True)
        s.upsert_day(FP, D, day(), False)
        row = s.days(FP, D, D)[0]
        self.assertTrue(row["final"])
        self.assertTrue(row["rechecked"])

    def test_recheck_with_a_missing_source_keeps_stored_values(self):
        s = self.store
        s.upsert_day(FP, D, day(hrv=60, hrv_7d=58, rhr=48), True)
        # Monday recheck: hrv failed (None, listed missing), stats came back with a new value.
        s.upsert_day(FP, D, day(hrv=None, hrv_7d=None, rhr=47, missing=["hrv"]), True, rechecked=True)
        row = s.days(FP, D, D)[0]
        self.assertEqual(row["numbers"]["hrv"], 60)
        self.assertEqual(row["numbers"]["hrv_7d"], 58)
        self.assertEqual(row["numbers"]["rhr"], 47)
        self.assertEqual(row["numbers"]["missing"], [])
        self.assertTrue(row["final"] and row["rechecked"])
        # A recheck with new values updates them.
        s.upsert_day(FP, D, day(hrv=63, hrv_7d=59, rhr=47), True, rechecked=True)
        row = s.days(FP, D, D)[0]
        self.assertEqual((row["numbers"]["hrv"], row["numbers"]["hrv_7d"]), (63, 59))

    def test_an_answered_source_replaces_stored_values_with_null(self):
        s = self.store
        s.upsert_day(FP, D, day(hrv=60, hrv_7d=58, rhr=48, stress_avg=30), True)
        # hrv answered with no reading this time: its fields become null; stats failed: its fields stay.
        s.upsert_day(FP, D, day(hrv=None, hrv_7d=None, rhr=None, stress_avg=None, missing=["stats"]), True,
                     rechecked=True)
        n = s.days(FP, D, D)[0]["numbers"]
        self.assertEqual((n["hrv"], n["hrv_7d"]), (None, None))
        self.assertEqual((n["rhr"], n["stress_avg"]), (48, 30))
        self.assertEqual(n["missing"], [])
        # stats answered with nothing: now its fields are null too.
        s.upsert_day(FP, D, day(hrv=None, hrv_7d=None, rhr=None, stress_avg=None), True, rechecked=True)
        n = s.days(FP, D, D)[0]["numbers"]
        self.assertEqual((n["rhr"], n["stress_avg"]), (None, None))
        self.assertEqual(n["sleep_seconds"], 27000)

    def test_open_days_are_the_stored_days_not_final(self):
        s = self.store
        s.upsert_day(FP, D, day(), False)
        s.upsert_day(FP, D - dt.timedelta(days=1), day(), True)
        s.upsert_day(FP, D - dt.timedelta(days=3), day(), False)
        s.upsert_day(OTHER, D - dt.timedelta(days=2), day(), False)
        self.assertEqual(s.open_days(FP, D - dt.timedelta(days=5), D), [D - dt.timedelta(days=3), D])
        self.assertEqual(s.open_days(FP, D - dt.timedelta(days=2), D - dt.timedelta(days=1)), [])

    def test_a_source_missing_twice_stays_missing(self):
        s = self.store
        s.upsert_day(FP, D, day(hrv=None, missing=["hrv"]), False)
        s.upsert_day(FP, D, day(hrv=None, missing=["hrv", "sleep"], sleep_seconds=None), False)
        row = s.days(FP, D, D)[0]
        self.assertEqual(row["numbers"]["missing"], ["hrv"])
        self.assertEqual(row["numbers"]["sleep_seconds"], 27000)

    def test_a_missing_source_does_not_make_a_stored_day_empty(self):
        s = self.store
        s.upsert_day(FP, D, day(), True)
        s.upsert_day(FP, D, {"mode": "full", "missing": list(hx.SOURCES)}, True, rechecked=True)
        row = s.days(FP, D, D)[0]
        self.assertFalse(row["empty"])
        self.assertEqual(row["numbers"]["hrv"], 60)

    def test_empty_is_derived(self):
        self.store.upsert_day(FP, D, empty_day(), True)
        self.assertTrue(self.store.days(FP, D, D)[0]["empty"])

    def test_allowlist_and_keys_are_enforced(self):
        s = self.store
        with self.assertRaises(hx.NotAllowed):
            s.upsert_day(FP, D, {"activityName": "x"}, False)
        with self.assertRaises(ValueError):
            s.upsert_day("not-a-fingerprint", D, day(), False)
        with self.assertRaises(ValueError):
            s.upsert_week(FP, D, week(), False)          # not a Monday
        with self.assertRaises(hx.NotAllowed):
            s.upsert_week(FP, MONDAY, {"flags": ["made_up"]}, False)
        self.assertEqual(s.days(FP, D, D), [])

    def test_accounts_are_separate(self):
        self.store.upsert_day(FP, D, day(), False)
        self.assertEqual(self.store.days(OTHER, D, D), [])
        self.assertEqual(self.store.missing_days(OTHER, D, D), [D])

    def test_missing_days_oldest_first(self):
        s = self.store
        s.upsert_day(FP, D, day(), False)
        got = s.missing_days(FP, D - dt.timedelta(days=2), D + dt.timedelta(days=1))
        self.assertEqual(got, [D - dt.timedelta(days=2), D - dt.timedelta(days=1), D + dt.timedelta(days=1)])

    def test_weeks(self):
        s = self.store
        self.assertTrue(s.upsert_week(FP, MONDAY, week(), True))
        self.assertFalse(s.upsert_week(FP, MONDAY, week(), True))
        rows = s.weeks(FP, MONDAY, MONDAY)
        self.assertEqual((rows[0]["start"], rows[0]["end"], rows[0]["final"]),
                         (MONDAY, MONDAY + dt.timedelta(days=6), True))
        self.assertEqual(rows[0]["numbers"]["level"], "green")

    def test_meta(self):
        s = self.store
        self.assertIsNone(s.meta_get("x"))
        self.assertEqual(s.meta_get("x", "d"), "d")
        s.meta_set("x", "1")
        s.meta_set("x", "2")
        self.assertEqual(s.meta_get("x"), "2")
        for bad_key in ("Bad Key", "", "k" * 65):
            with self.assertRaises(ValueError):
                s.meta_set(bad_key, "1")
        with self.assertRaises(ValueError):
            s.meta_set("x", "v" * 300)
        self.assertEqual(s.meta_setdefault("y", "first"), "first")
        self.assertEqual(s.meta_setdefault("y", "second"), "first")
        self.assertEqual(s.meta_get("y"), "first")

    def test_account_fingerprint(self):
        s = self.store
        fp, status = hs.account_check(s, 123456789)
        self.assertEqual(status, hs.ACCOUNT_NEW)
        self.assertRegex(fp, r"^[0-9a-f]{64}$")
        self.assertNotIn("123456789", fp)
        salt = s.meta_get(hs.META_SALT)
        self.assertEqual(fp, hmac.new(bytes.fromhex(salt), b"123456789", hashlib.sha256).hexdigest())
        self.assertEqual(hs.account_check(s, "123456789"), (fp, hs.ACCOUNT_OK))
        with self.assertLogs("garmin_ext.history", "WARNING") as logs:
            self.assertEqual(hs.account_check(s, 42), (fp, hs.ACCOUNT_MISMATCH))
        self.assertNotIn("42", "".join(logs.output))
        self.assertEqual(s.meta_get(hs.META_MISMATCH), "yes")
        self.assertEqual(s.meta_get(hs.META_ACCOUNT), fp)

    def test_retention(self):
        s = self.store
        old = MONDAY - dt.timedelta(days=7)
        s.upsert_day(FP, old, day(), True)
        s.upsert_day(FP, D, day(), True)
        s.upsert_week(FP, old, week(), True)
        s.upsert_week(FP, MONDAY, week(), True)
        out = s.retention(FP, MONDAY)
        self.assertEqual((out["days"], out["weeks"]), (1, 1))
        self.assertEqual(set(out["doc_ids"]), {history_docs.doc_id("day", old), history_docs.doc_id("week", old)})
        self.assertEqual([r["date"] for r in s.days(FP, old, D)], [D])
        self.assertEqual([r["start"] for r in s.weeks(FP, old, MONDAY)], [MONDAY])

    def test_coverage(self):
        s = self.store
        s.meta_set(hs.META_ACCOUNT, FP)
        self.assertEqual(s.coverage()["days"], 0)
        s.upsert_day(FP, D - dt.timedelta(days=4), day(), True)
        s.upsert_day(FP, D - dt.timedelta(days=3), empty_day(), True)
        s.upsert_day(FP, D, day(), False)
        s.upsert_week(FP, MONDAY - dt.timedelta(days=7), week(), True)
        c = s.coverage()
        self.assertEqual((c["days"], c["days_with_data"], c["empty_days"], c["final_days"], c["weeks"]),
                         (3, 2, 1, 2, 1))
        self.assertEqual((c["from"], c["to"], c["last_final_day"]), ("2026-03-14", "2026-03-18", "2026-03-15"))
        self.assertEqual(c["weeks_to"], "2026-03-15")

    def test_forget_all(self):
        s = self.store
        s.upsert_day(FP, D, day(hrv=4242), True)
        s.upsert_week(FP, MONDAY, week(), True)
        s.meta_set(hs.META_ACCOUNT, FP)
        out = s.forget_all()
        self.assertTrue(out["schema_dropped"])
        self.assertTrue(out["vacuum"])
        self.assertEqual((out["days"], out["weeks"]), (1, 1))
        s.migrate()
        self.assertEqual(s.days(FP, D, D), [])
        self.assertIsNone(s.meta_get(hs.META_ACCOUNT))

    def test_transaction_rolls_back(self):
        s = self.store
        with self.assertRaises(RuntimeError):
            with s.transaction() as conn:
                s.upsert_day(FP, D, day(), False, conn=conn)
                s.meta_set("x", "1", conn=conn)
                raise RuntimeError("boom")
        self.assertEqual(s.days(FP, D, D), [])
        self.assertIsNone(s.meta_get("x"))
        with s.transaction() as conn:
            s.upsert_day(FP, D, day(), False, conn=conn)
        self.assertEqual(len(s.days(FP, D, D)), 1)


class DocVectorsMigration(unittest.TestCase):
    def test_002_is_history_retrieval_ddl(self):
        files = {name: path for _v, name, path in hs.migration_files()}
        self.assertIn("002_doc_vectors.sql", files)
        with open(files["002_doc_vectors.sql"], encoding="utf-8") as fh:
            text = fh.read()
        body = [s.strip() for s in "\n".join(line for line in text.splitlines()
                                             if not line.startswith("--")).split(";") if s.strip()]
        try:
            import history_retrieval
        except ImportError as exc:  # history_retrieval imports only the standard library at module level
            self.fail(f"history_retrieval not importable: {type(exc).__name__}")
        self.assertEqual(body, [s.strip() for s in history_retrieval.DOC_TABLE_SQL])
        self.assertEqual(hs.DOC_VECTORS, f"{history_retrieval.SCHEMA}.{history_retrieval.DOC_TABLE}")


class MemoryStore(StoreContract, unittest.TestCase):
    def make(self):
        return hs.MemoryHistoryStore()

    def test_migrate_once(self):
        s = hs.MemoryHistoryStore()
        self.assertEqual(s.migrate(), [1, 2])
        self.assertEqual(s.migrate(), [])


class Migrations(unittest.TestCase):
    def test_files_are_versioned_and_unique(self):
        files = hs.migration_files()
        self.assertEqual(files[0][:2], (1, "001_history_store.sql"))
        self.assertEqual(len({v for v, _n, _p in files}), len(files))
        for _v, _n, path in files:
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
            text.encode("ascii")
            self.assertNotIn("DROP ", text.upper().replace("-- ", ""))
            self.assertIn("history.", text)

    def test_duplicate_versions_are_refused(self):
        import tempfile

        with tempfile.TemporaryDirectory() as folder:
            for name in ("001_a.sql", "001_b.sql"):
                with open(os.path.join(folder, name), "w") as fh:
                    fh.write("--\n")
            with self.assertRaises(hs.StoreError):
                hs.migration_files(folder)

    def test_pg_store_needs_a_url_and_hides_it(self):
        with self.assertRaises(ValueError):
            hs.PgHistoryStore("")
        if HAVE_PSYCOPG:
            self.assertEqual(repr(hs.PgHistoryStore("postgresql://u:secret@h/db")), "PgHistoryStore()")


@unittest.skipUnless(DB_URL and HAVE_PSYCOPG, PG_SKIP)
class PostgresStore(StoreContract, unittest.TestCase):
    def make(self):
        store = hs.PgHistoryStore(DB_URL)
        store.forget_all()
        return store

    def tearDown(self):
        self.store.forget_all()

    def test_migrate_is_recorded_once(self):
        self.assertEqual(self.store.migrate(), [])
        with self.store.transaction() as c:
            rows = c.execute("SELECT version FROM history.schema_migrations ORDER BY 1").fetchall()
        self.assertEqual([r[0] for r in rows], [v for v, _n, _p in hs.migration_files()])

    def test_forget_leaves_no_canary_in_the_database(self):
        canary = 98761234
        self.store.upsert_day(FP, D, day(hrv=canary % 200, sleep_seconds=canary), True)
        self.store.meta_set("canary", str(canary))
        with self.store.transaction() as c:
            c.execute("CREATE TABLE IF NOT EXISTS langchain_pg_collection (uuid uuid PRIMARY KEY, name text, "
                      "cmetadata jsonb)")
            c.execute("CREATE TABLE IF NOT EXISTS langchain_pg_embedding (id text PRIMARY KEY, collection_id uuid, "
                      "document text, cmetadata jsonb)")
            c.execute("INSERT INTO langchain_pg_collection VALUES "
                      "('00000000-0000-0000-0000-000000000001', %s, '{}') ON CONFLICT DO NOTHING",
                      (hs.COLLECTION_NAME,))
            c.execute("INSERT INTO langchain_pg_embedding VALUES ('x', '00000000-0000-0000-0000-000000000001', "
                      "%s, '{}') ON CONFLICT DO NOTHING", (f"sleep {canary}",))
        out = self.store.forget_all()
        self.assertEqual(out["collection_rows"], 1)
        with self.store.transaction() as c:
            self.assertIsNone(c.execute("SELECT to_regnamespace('history')").fetchone()[0])
            hits = c.execute("SELECT count(*) FROM langchain_pg_embedding WHERE document LIKE %s",
                             (f"%{canary}%",)).fetchone()[0]
            self.assertEqual(hits, 0)
            c.execute("DROP TABLE langchain_pg_embedding, langchain_pg_collection")

    def test_retention_deletes_old_vectors(self):
        old = MONDAY - dt.timedelta(days=7)
        self.store.upsert_day(FP, old, day(), True)
        old_id, keep_id = history_docs.doc_id("day", old), history_docs.doc_id("day", D)
        with self.store.transaction() as c:
            for lid in (old_id, keep_id):
                c.execute("INSERT INTO history.doc_vectors (langchain_id, content, embedding, doc_key, kind, "
                          "start_date, end_date, account_fp, embed_model) VALUES (%s, 'x', "
                          "array_fill(0.1::real, ARRAY[384])::vector, 'k', 'day', 's', 'e', %s, 'm')", (lid, FP))
        out = self.store.retention(FP, MONDAY)
        self.assertEqual(out["vectors"], 1)
        with self.store.transaction() as c:
            left = [str(r[0]) for r in c.execute("SELECT langchain_id FROM history.doc_vectors").fetchall()]
        self.assertEqual(left, [keep_id])

    def test_jsonb_round_trip_keeps_types(self):
        self.store.upsert_day(FP, D, day(resp_sleep_avg=14.5), False)
        n = self.store.days(FP, D, D)[0]["numbers"]
        self.assertEqual(json.dumps(n, sort_keys=True), json.dumps(hx.check_numbers("day", day(resp_sleep_avg=14.5)),
                                                                   sort_keys=True))


if __name__ == "__main__":
    unittest.main()
