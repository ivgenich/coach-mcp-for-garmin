"""Tests for history_retrieval: query safety, the numbers channel (robust z-scores, MAD 0, 8-week minimum,
exclusion window), RRF, the LangChain composition (EnsembleRetriever over vector, keyword and numbers),
LangSmith forced off, the keyword SQL, the embed_doc job, forget and the worker's vector deleter.

    cd ext && python3 -m unittest test_history_retrieval -v

The pure parts are standard library. The LangChain parts need the history packages
(config/requirements-history.txt); the Postgres tests need COACH_TEST_DATABASE_URL (CI's pgvector service).
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
import types
import unittest
import uuid
from unittest import mock

import history_retrieval as hr

HAVE_LC = all(importlib.util.find_spec(m) is not None
              for m in ("langchain_core", "langchain_classic", "langsmith", "numpy"))
LC_SKIP = "needs the history packages (config/requirements-history.txt)"
DB_URL = os.environ.get("COACH_TEST_DATABASE_URL", "")
HAVE_PG = HAVE_LC and importlib.util.find_spec("psycopg") is not None and \
    importlib.util.find_spec("langchain_postgres") is not None
PG_SKIP = "needs COACH_TEST_DATABASE_URL (a Postgres with pgvector) and the history packages"
FP = "b" * 64
MONDAY = dt.date(2026, 1, 5)
CANARY = '"NEAR(x) text:canary* -- ; DROP TABLE x; \'quote\''


def weeks(n=12, start=MONDAY, override=None):
    override = override or {}
    out = []
    for i in range(n):
        s = start + dt.timedelta(days=7 * i)
        nums = {"hrv_avg_7d": 60 + (i % 3), "hrv_delta_ms": (i % 4) - 2, "sleep_avg_h": 7.0 + (i % 5) / 10,
                "nights_under_6h": 0, "sleep_avg_score": 78 + i % 4, "rhr_avg": 47 + (i % 2),
                "stress_avg": 27 + (i % 3), "total_hours": 8 + (i % 4), "change_pct": (i % 5) * 5 - 10,
                "sessions": 6 + (i % 2), "run_hours": 3 + (i % 3) / 2, "bike_hours": 4 + (i % 2),
                "swim_hours": 0.7 + (i % 2) / 4, "other_hours": 0.0, "level": "green", "flags": []}
        nums.update(override.get(i, {}))
        out.append({"start": s, "end": s + dt.timedelta(days=6), "numbers": nums, "final": True})
    return out


class QuerySafety(unittest.TestCase):
    def test_tokens_are_only_lowercase_words_and_digits(self):
        toks = hr.query_tokens(CANARY)
        for t in toks:
            self.assertRegex(t, r"^[a-z0-9]{1,32}$")
        self.assertIn("canary", toks)
        self.assertEqual(hr.query_tokens("A" * 100), ["a" * 32])
        self.assertEqual(len(hr.query_tokens(" ".join(f"w{i}" for i in range(40)))), hr.MAX_TOKENS)
        self.assertEqual(hr.query_tokens("bike bike BIKE"), ["bike"])
        self.assertEqual(hr.query_tokens("\u05e9\u05d1\u05d5\u05e2 \u05e8\u05db\u05d9\u05d1\u05d4 \u05db\u05d1\u05d3"), [], "Hebrew leaves no keyword tokens")
        self.assertEqual(hr.query_tokens(None), [])

    def test_tsquery_text_only_joins_safe_tokens(self):
        self.assertEqual(hr.tsquery_text(["bike", "heavy"]), "bike | heavy")
        self.assertEqual(hr.tsquery_text(["x'y", "ok", "a:*"]), "ok")
        q = hr.tsquery_text(hr.query_tokens(CANARY))
        self.assertRegex(q, r"^[a-z0-9 |]+$")


class NumbersChannel(unittest.TestCase):
    def test_robust_scales_drop_mad_zero(self):
        scales = hr.robust_scales([{"a": 1, "b": 5}, {"a": 2, "b": 5}, {"a": 3, "b": 5}])
        self.assertIn("a", scales)
        self.assertNotIn("b", scales, "a feature with MAD 0 is dropped, never divided by")
        med, scale = scales["a"]
        self.assertEqual(med, 2)
        self.assertAlmostEqual(scale, 1.4826)

    def test_fewer_than_8_weeks_is_not_enough_history(self):
        rows = weeks(5)
        with self.assertRaises(hr.NotEnoughHistory) as cm:
            hr.numeric_rank(rows[-1], rows)
        self.assertEqual(cm.exception.usable, 5)

    def test_nearest_week_wins_and_window_is_excluded(self):
        odd = {"total_hours": 20, "run_hours": 1, "bike_hours": 17, "sessions": 9, "change_pct": 80}
        rows = weeks(14, override={2: odd, 12: odd, 11: odd})
        matches = hr.numeric_rank(rows[12], rows, "training")
        starts = [m.row["start"] for m in matches]
        self.assertEqual(starts[0], rows[2]["start"])
        for m in matches:
            self.assertGreater(abs((m.row["start"] - rows[12]["start"]).days), 14, "reference +-2 weeks excluded")
        self.assertTrue(matches[0].why and all(" vs " in w for w in matches[0].why))
        self.assertGreater(matches[0].similarity, matches[-1].similarity)

    def test_masked_features_never_count_as_zero(self):
        rows = weeks(12)
        for r in rows[8:]:
            r["numbers"]["acwr"] = None  # key mode
        for r in rows[:8]:
            r["numbers"]["acwr"] = 1.0 + (r["start"].toordinal() % 3) / 10
        self.assertTrue(hr.numeric_rank(rows[-1], rows))
        self.assertNotIn("acwr", hr.week_features(rows[-1]["numbers"]))

    def test_date_filter_and_bad_focus(self):
        rows = weeks(14)
        cut = rows[6]["start"].isoformat()
        for m in hr.numeric_rank(rows[-1], rows, date_to=cut):
            self.assertLessEqual(m.row["start"].isoformat(), cut)
        with self.assertRaises(ValueError):
            hr.numeric_rank(rows[-1], rows, "fitness")

    def test_feature_names_are_stored_week_fields(self):
        import history_extract as hx

        for name in hr.RECOVERY_FEATURES + hr.TRAINING_FEATURES:
            self.assertIn(name, hx.WEEK_FIELDS)

    def test_rrf(self):
        self.assertEqual(hr.rrf([["a", "b"], ["b", "c"]]), ["b", "a", "c"])
        self.assertEqual(hr.rrf([["a"], []]), ["a"])


class Metadata(unittest.TestCase):
    def test_normalise(self):
        m = hr.normalise_metadata({"kind": "week", "start_date": "2026-01-05", "end_date": "2026-01-11",
                                   "langchain_metadata": json.dumps({"tags": ["taper"]})})
        self.assertEqual((m["start"], m["end"], m["doc_id"], m["tags"]),
                         ("2026-01-05", "2026-01-11", "week:2026-01-05", ["taper"]))
        self.assertEqual(hr.doc_uuid(FP, "week:2026-01-05"), hr.doc_uuid(FP, "week:2026-01-05"))

    def test_dsn_forms(self):
        self.assertEqual(hr._pg_dsn("postgres://u@h/db"), "postgresql://u@h/db")
        self.assertEqual(hr._sqlalchemy_url("postgresql://u@h/db"), "postgresql+psycopg://u@h/db")


class FakeConn:
    def __init__(self, rows=(), fail=False):
        self.rows, self.fail, self.calls = list(rows), fail, []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        if self.fail:
            raise RuntimeError(f"syntax error near {params}")
        rows = self.rows
        return types.SimpleNamespace(fetchall=lambda: rows, fetchone=lambda: ("history.doc_vectors",), rowcount=2)


@unittest.skipUnless(HAVE_LC, LC_SKIP)
class KeywordSQL(unittest.TestCase):
    def test_query_is_bound_never_in_sql(self):
        # The row layout of the real table: kind and the dates live in their own columns, not in the JSON.
        conn = FakeConn(rows=[("week:2026-01-05", "week", "2026-01-05", "2026-01-11", "Week text",
                               json.dumps({"tags": ["bike"]}), 0.5)])
        backend = hr.PgKeywordBackend("postgres://x", connect=lambda dsn: conn)
        hits = backend.search(hr.query_tokens(CANARY), kind="week", account_fp=FP, date_from="0001-01-01",
                              date_to="9999-12-31", k=5)
        sql, params = conn.calls[0]
        self.assertNotIn("canary", sql)
        self.assertEqual(sql, hr._KEYWORD_SQL)
        self.assertRegex(params["q"], r"^[a-z0-9 |]+$")
        self.assertEqual(hits[0][0].metadata["doc_id"], "week:2026-01-05")
        meta = hits[0][0].metadata
        self.assertEqual((meta["kind"], meta["start"], meta["end"]), ("week", "2026-01-05", "2026-01-11"))
        self.assertEqual(meta["tags"], ["bike"])

    def test_no_token_no_query(self):
        conn = FakeConn()
        backend = hr.PgKeywordBackend("postgres://x", connect=lambda dsn: conn)
        self.assertEqual(backend.search([], kind="week", account_fp=FP, date_from="a", date_to="b", k=5), [])
        self.assertEqual(conn.calls, [])

    def test_database_error_is_fixed_and_logs_no_query(self):
        backend = hr.PgKeywordBackend("postgres://x", connect=lambda dsn: FakeConn(fail=True))
        with self.assertLogs("garmin_ext.history", "WARNING") as logs:
            with self.assertRaises(hr.HistoryUnavailable) as cm:
                backend.search(["canary"], kind="week", account_fp=FP, date_from="a", date_to="b", k=5)
        self.assertEqual(str(cm.exception), "database")
        self.assertIsNone(cm.exception.__cause__)
        self.assertTrue(cm.exception.__suppress_context__)
        self.assertNotIn("canary", "\n".join(logs.output))


def docs_for(rows):
    from langchain_core.documents import Document

    tags = {0: "bike_heavy", 5: "taper", 9: "bike_heavy"}
    out = []
    for i, r in enumerate(rows):
        s = r["start"].isoformat()
        out.append(Document(page_content=f"Week {s}. Training {'heavy bike' if i in (0, 9) else 'moderate'}. "
                                         f"Tags: {tags.get(i, 'none')}.",
                            metadata={"kind": "week", "start": s, "end": r["end"].isoformat(),
                                      "tags": [tags[i]] if i in tags else []}))
    return out


@unittest.skipUnless(HAVE_LC, LC_SKIP)
class Composition(unittest.TestCase):
    def setUp(self):
        import history_embed

        self.rows = weeks(14)
        self.index = hr.MemoryDocIndex(history_embed.hash_embeddings(), FP)
        self.index.add(docs_for(self.rows))

    def test_ensemble_fuses_and_records_channels(self):
        req = hr.SearchRequest(text="heavy bike", k=5)
        found, used = hr.search(self.index, self.rows, req)
        self.assertEqual(used, ["vector", "keyword"])
        self.assertIn(found[0].metadata["start"], (self.rows[0]["start"].isoformat(),
                                                   self.rows[9]["start"].isoformat()))
        self.assertTrue(set(found[0].metadata["matched_on"]) <= {"vector", "keyword"})
        retriever, _ = hr.build_retriever(self.index, self.rows, req)
        from langchain_classic.retrievers import EnsembleRetriever

        self.assertIsInstance(retriever, EnsembleRetriever)
        self.assertEqual(retriever.c, hr.RRF_K)

    def test_numbers_reference_excludes_window(self):
        ref = {**self.rows[10], "start": self.rows[10]["start"].isoformat()}
        req = hr.SearchRequest(text="", reference=ref, channels=("numbers",), k=10)
        found, used = hr.search(self.index, self.rows, req)
        self.assertEqual(used, ["numbers"])
        for d in found:
            self.assertGreater(abs((dt.date.fromisoformat(d.metadata["start"]) - self.rows[10]["start"]).days), 14)
            self.assertEqual(d.metadata["matched_on"], ["numbers"])
            self.assertIn("why", d.metadata)

    def test_hebrew_runs_vector_only(self):
        _, used = hr.search(self.index, self.rows, hr.SearchRequest(text="\u05e9\u05d1\u05d5\u05e2 \u05e8\u05db\u05d9\u05d1\u05d4 \u05db\u05d1\u05d3"))
        self.assertEqual(used, ["vector"])

    def test_nothing_runnable(self):
        self.assertEqual(hr.search(hr.MemoryDocIndex(None, FP), self.rows, hr.SearchRequest(text="")), ([], []))

    def test_date_filter_in_every_text_channel(self):
        lo = self.rows[5]["start"].isoformat()
        found, _ = hr.search(self.index, self.rows, hr.SearchRequest(text="heavy bike", date_from=lo, k=10))
        self.assertTrue(found)
        self.assertTrue(all(d.metadata["start"] >= lo for d in found))

    def test_reranker_wraps_in_contextual_compression(self):
        from langchain_classic.retrievers import ContextualCompressionRetriever

        class CE:
            def score(self, pairs):
                return [1.0 if "taper" in d else 0.0 for _q, d in pairs]

        from langchain_core.cross_encoders import BaseCrossEncoder

        ce = type("FakeCE", (BaseCrossEncoder,), {"score": CE.score})()
        req = hr.SearchRequest(text="heavy bike", rerank=True)
        retriever, _ = hr.build_retriever(self.index, self.rows, req, ce)
        self.assertIsInstance(retriever, ContextualCompressionRetriever)
        found, _ = hr.search(self.index, self.rows, req, ce)
        self.assertIn("taper", found[0].page_content)

    def test_delete(self):
        key = f"week:{self.rows[0]['start'].isoformat()}"
        self.index.delete([key])
        found, _ = hr.search(self.index, self.rows, hr.SearchRequest(text="heavy bike", k=14))
        self.assertNotIn(key, [d.metadata["doc_id"] for d in found])


@unittest.skipUnless(HAVE_LC, LC_SKIP)
class LangSmithOff(unittest.TestCase):
    def test_tracing_forced_off_even_when_env_says_on(self):
        import history_embed
        import langsmith
        import langsmith.utils as ls_utils
        from langchain_core.tracers.context import _tracing_v2_is_enabled

        env = {"LANGSMITH_TRACING": "true", "LANGCHAIN_TRACING_V2": "true", "LANGSMITH_API_KEY": "lsv2-canary"}
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(langsmith.Client, "__init__", side_effect=AssertionError("LangSmith client")):
            ls_utils.get_env_var.cache_clear()
            rows = weeks(12)
            index = hr.MemoryDocIndex(history_embed.hash_embeddings(), FP)
            index.add(docs_for(rows))
            hr.search(index, rows, hr.SearchRequest(text="heavy bike"))
            self.assertFalse(_tracing_v2_is_enabled())
            self.assertFalse(ls_utils.tracing_is_enabled())
            self.assertEqual(os.environ["LANGSMITH_TRACING"], "false")
            self.assertEqual(os.environ["LANGCHAIN_TRACING_V2"], "false")


class FakeStoreCtx:
    pass


@unittest.skipUnless(HAVE_LC, LC_SKIP)
class EmbedDocJob(unittest.TestCase):
    def setUp(self):
        import history_embed
        import history_extract as hx
        import history_store as hs

        self.store = hs.MemoryHistoryStore()
        self.store.meta_set(hs.META_ACCOUNT, FP)
        day = {"mode": "full", "sleep_seconds": 7 * 3600, "hrv": 60, "hrv_7d": 60, "rhr": 47, "rhr_7d": 47,
               "stress_avg": 25, "sessions": 1, "run_sessions": 1, "run_seconds": 3600.0, "run_meters": 10000.0,
               "missing": []}
        for i in range(35):
            self.store.upsert_day(FP, MONDAY - dt.timedelta(days=28) + dt.timedelta(days=i), day, True)
        days = {r["date"]: r["numbers"] for r in self.store.days(FP, MONDAY - dt.timedelta(days=28), MONDAY
                                                                    + dt.timedelta(days=6))}
        self.store.upsert_week(FP, MONDAY, hx.week_numbers(MONDAY, days, mode="full"), True)
        self.index = hr.MemoryDocIndex(history_embed.hash_embeddings(), FP)
        self.ctx = types.SimpleNamespace(store=self.store, conn=None, doc_index=self.index)

    def job(self, **payload):
        return types.SimpleNamespace(payload=payload)

    def test_embeds_week_then_reruns_idempotently(self):
        hr.embed_doc(self.job(kind="week", start=MONDAY.isoformat()), self.ctx)
        hr.embed_doc(self.job(kind="week", start=MONDAY.isoformat()), self.ctx)
        self.assertEqual(list(self.index.keyword.docs), [f"week:{MONDAY.isoformat()}"])
        self.assertEqual(len(self.index.store.store), 1)

    def test_missing_row_deletes(self):
        import history_docs

        hr.embed_doc(self.job(kind="day", start=MONDAY.isoformat()), self.ctx)
        self.assertEqual(len(self.index.keyword.docs), 1)
        self.index.delete = mock.Mock()
        hr.embed_doc(self.job(kind="day", start="2030-01-01"), self.ctx)
        self.index.delete.assert_called_once_with([history_docs.doc_id("day", dt.date(2030, 1, 1))])

    def test_bad_payload_and_account_mismatch_are_permanent(self):
        import history_store as hs
        import worker

        for payload in ({"kind": "month", "start": "2026-01-01"}, {"kind": "week", "start": "soon"}):
            with self.assertRaises(worker.Permanent) as cm:
                hr.embed_doc(self.job(**payload), self.ctx)
            self.assertEqual(cm.exception.group, "bad_input")
        self.store.meta_set(hs.META_MISMATCH, "yes")
        with self.assertRaises(worker.Permanent) as cm:
            hr.embed_doc(self.job(kind="week", start=MONDAY.isoformat()), self.ctx)
        self.assertEqual(cm.exception.group, "account_mismatch")

    def test_model_unavailable_defers(self):
        import history_embed
        import jobqueue

        self.index.add = mock.Mock(side_effect=history_embed.ModelUnavailable("backoff"))
        with self.assertRaises(jobqueue.Defer) as cm:
            hr.embed_doc(self.job(kind="week", start=MONDAY.isoformat()), self.ctx)
        self.assertEqual(cm.exception.group, "model_unavailable")

    def test_registers_embed_doc_without_garmin(self):
        calls = []
        hr.register_jobs(lambda kind, fn, garmin=True: calls.append((kind, fn, garmin)))
        self.assertEqual(calls, [("embed_doc", hr.embed_doc, False)])


class ForgetAndDeleter(unittest.TestCase):
    def test_forget_drops_the_table(self):
        conn = FakeConn()
        out = hr.forget(types.SimpleNamespace(database_url="postgres://x"), connect=lambda dsn: conn)
        self.assertEqual(conn.calls[0][0], f"DROP TABLE IF EXISTS {hr.SCHEMA}.{hr.DOC_TABLE}")
        self.assertIn("doc_vectors", out)
        self.assertEqual(hr._INDEXES, {})

    def test_forget_without_database(self):
        with mock.patch.dict(os.environ, {"DATABASE_URL": ""}):
            self.assertEqual(hr.forget(types.SimpleNamespace(database_url=None)), "no database configured")

    def test_worker_vectors_delete_by_uuid(self):
        conn = FakeConn()
        rows = hr.VectorRows("postgres://x", connect=lambda dsn: conn)
        ids = [str(uuid.uuid4()), str(uuid.uuid4())]
        self.assertEqual(rows.delete(ids=ids), 2)
        self.assertEqual(conn.calls[-1][1], (ids,))
        self.assertEqual(rows.delete(ids=[]), 0)
        self.assertNotIn("postgres", repr(rows))
        with self.assertRaises(ValueError):
            rows.delete(ids=["not-a-uuid"])

    def test_worker_vectors_error_is_fixed(self):
        rows = hr.VectorRows("postgres://x", connect=lambda dsn: FakeConn(fail=True))
        with self.assertLogs("garmin_ext.history", "WARNING") as logs, self.assertRaises(hr.HistoryUnavailable):
            rows.delete(ids=[str(uuid.uuid4())])
        self.assertNotIn("syntax", "\n".join(logs.output))


@unittest.skipUnless(DB_URL and HAVE_PG, PG_SKIP)
class Postgres(unittest.TestCase):
    """Real pgvector: table DDL, PGVectorStore upsert, keyword FTS, filters, delete, forget."""

    def setUp(self):
        import history_embed

        self.fp = uuid.uuid4().hex * 2
        hr.ensure_doc_table(DB_URL)
        hr.ensure_doc_table(DB_URL)  # idempotent
        self.index = hr.PgDocIndex(DB_URL, history_embed.hash_embeddings(), self.fp, embed_model="test")
        self.rows = weeks(14)
        self.docs = docs_for(self.rows)
        for d in self.docs:
            d.id = str(uuid.uuid4())
        self.index.add(self.docs)
        self.addCleanup(lambda: self.index.delete([d.id for d in self.docs]))

    def test_search_all_channels(self):
        found, used = hr.search(self.index, self.rows, hr.SearchRequest(text="heavy bike", k=5))
        self.assertEqual(used, ["vector", "keyword"])
        self.assertIn("keyword", found[0].metadata["matched_on"])
        self.assertIn("bike", found[0].page_content)

    def test_upsert_is_idempotent_and_canary_query_is_safe(self):
        self.index.add(self.docs)
        found, _ = hr.search(self.index, self.rows, hr.SearchRequest(text=CANARY, k=14))
        self.assertLessEqual(len(found), 14)
        import psycopg

        with psycopg.connect(hr._pg_dsn(DB_URL)) as c:
            n = c.execute(f"SELECT count(*) FROM {hr.SCHEMA}.{hr.DOC_TABLE} WHERE account_fp = %s",
                          (self.fp,)).fetchone()[0]
        self.assertEqual(n, len(self.docs))

    def test_filters_and_delete(self):
        lo = self.rows[7]["start"].isoformat()
        found, _ = hr.search(self.index, self.rows, hr.SearchRequest(text="heavy bike", date_from=lo, k=14))
        self.assertTrue(all(d.metadata["start"] >= lo for d in found))
        self.assertEqual(hr.VectorRows(DB_URL).delete([self.docs[0].id]), 1)


if __name__ == "__main__":
    unittest.main()
