"""Deep write tests on PgPlanStore (COACH_TEST_DATABASE_URL): the same executor and tools on Postgres rows, a
"restart" (a new store instance and WriteService on the same data) resuming a partial token and settling a row
a crash left mid-request, late landings with the quarantine (state_changed_at moved back in SQL), the token
windows (expires_at moved in SQL), the replace and cancel flows, and --plan-cleanup.

The database is shared: every test uses its own random Garmin profile (so its account fingerprint and plans are
its own), never drops a table, and deletes only its own plans at the end. history_account() is the test's
account (the meta row is shared, so the store subclass answers it).
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import os
import secrets
import threading
import unittest

import history_store as hs
import plan_store as ps
import plan_writes as pw
import test_deep_writes as tdw
import test_deep_writes_base as b
from test_deep_writes_base import BLOCK, after, before

DB_URL = os.environ.get("COACH_TEST_DATABASE_URL", "").strip()
HAVE_PSYCOPG = importlib.util.find_spec("psycopg") is not None
PG_SKIP = "needs psycopg and COACH_TEST_DATABASE_URL (Postgres with pgvector)"
SCHEMA = ps.SCHEMA


class OwnAccountStore(ps.PgPlanStore if HAVE_PSYCOPG else object):
    history_fp = None

    def history_account(self):
        return self.history_fp


@unittest.skipUnless(DB_URL and HAVE_PSYCOPG, PG_SKIP)
class PgDeep(b.DeepBase):
    _migrated = False
    _lock = threading.Lock()

    def setUp(self):
        with PgDeep._lock:
            if not PgDeep._migrated:
                hs.PgHistoryStore(DB_URL).migrate()
                PgDeep._migrated = True
        self.plan_ids = []
        self.addCleanup(self._drop_own_plans)
        super().setUp()

    def _drop_own_plans(self):
        if self.plan_ids:
            self.sql(f"DELETE FROM {SCHEMA}.plans WHERE plan_id = ANY(%s)", (list(self.plan_ids),))

    def make_store(self):
        return OwnAccountStore(DB_URL)

    def make_profile(self):
        return 700000000 + secrets.randbelow(99999999)

    def set_history_account(self, fp):
        self.store.history_fp = fp

    def new_store(self):
        store = OwnAccountStore(DB_URL)
        store.history_fp = self.fp
        return store

    def sql(self, query, params=()):
        with self.store.transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(query, params)
                return cur.fetchall() if cur.description else []

    def preview(self, *args, **kwargs):
        pid, commit = super().preview(*args, **kwargs)
        if pid not in self.plan_ids:
            self.plan_ids.append(pid)
        return pid, commit

    def token_status(self, token):
        return self.sql(f"SELECT status FROM {SCHEMA}.plan_tokens WHERE token_hash = %s", (ps.token_hash(token),))[0][0]

    def age_rows(self, pid, minutes=3):
        self.sql(f"UPDATE {SCHEMA}.plan_workouts SET state_changed_at = state_changed_at - %s WHERE plan_id = %s",
                 (dt.timedelta(minutes=minutes), pid))

    def plan_status(self, pid):
        return self.store.get_plan(self.fp, pid)["status"]

    # --- the flows ---------------------------------------------------------------------------------------------

    def test_commit_replay_cancel(self):
        pid, c = self.preview()
        out = self.commit(pid, c["token"])
        self.assertEqual(out["status"], "done", out)
        self.assertEqual(self.plan_status(pid), "active")
        self.assertEqual(self.token_status(c["token"]), "done")
        calls = len(self.garmin.calls)
        self.assertTrue(self.commit(pid, c["token"])["replayed"])
        self.assertEqual(len(self.garmin.calls), calls)
        rows = self.rows(pid)
        self.assertEqual({r["state"] for r in rows.values()}, {"scheduled"})
        self.assertTrue(all(r["garmin_schedule_id"] for r in rows.values()))
        prev = self.cancel(pid)
        out = self.cancel(pid, prev["cancel"]["token"])
        self.assertEqual(out["status"], "done", out)
        self.assertEqual(self.plan_status(pid), "cancelled")
        self.assertEqual(self.garmin.library, {})

    def test_a_restart_resumes_a_partial_token(self):
        pid, c = self.preview()
        self.garmin.faults["schedule"] = {2: before(b.GarminConnectTooManyRequestsError("x"))}
        out = self.commit(pid, c["token"])
        self.assertEqual((out["status"], out["retry_after_s"]), ("partial", 900), out)
        self.assertEqual(self.token_status(c["token"]), "partial")
        self.assertEqual(self.rows(pid)["20261203-run"]["state"], "uploaded")
        store2 = self.new_store()
        svc2 = self.service(store=store2)
        out = self.commit(pid, c["token"], svc=svc2)
        self.assertEqual(out["status"], "done", out)
        self.assert_no_double_writes()
        self.assertEqual(len(self.garmin.uploads()), 3)

    def test_a_late_upload_is_adopted_after_the_quarantine(self):
        pid, c = self.preview()
        self.garmin.faults["upload"] = {2: BLOCK}
        self.assertEqual(self.commit(pid, c["token"])["status"], "unknown")
        self.garmin.blocked.set()
        self.assertTrue(pw.join_write_threads(10))
        out = self.commit(pid, c["token"])
        self.assertEqual((out["status"], out["retry_after_s"]), ("partial", pw.RETRY_AFTER_UNKNOWN_S), out)
        self.assertEqual(self.rows(pid)["20261203-run"]["state"], "unknown")
        self.age_rows(pid)
        out = self.commit(pid, c["token"])
        self.assertEqual(out["status"], "done", out)
        self.assertEqual(len(self.garmin.uploads()), 3)
        self.assert_no_double_writes()

    def test_a_late_schedule_is_settled_from_the_calendar(self):
        pid, c = self.preview()
        self.garmin.faults["schedule"] = {1: after(b.ReadTimeout("r"))}
        self.assertEqual(self.commit(pid, c["token"])["status"], "unknown")
        self.age_rows(pid)
        out = self.commit(pid, c["token"])
        self.assertEqual(out["status"], "done", out)
        self.assertEqual(len([w for w in self.garmin.write_log if w[0] == "schedule"]), 3)
        self.assert_no_double_writes()
        self.assertTrue(self.rows(pid)["20261202-run"]["garmin_schedule_id"])

    def test_a_crash_mid_request_is_settled_after_a_restart(self):
        pid, c = self.preview(sessions=[b.session("20261202-run", b.TODAY + dt.timedelta(days=1))])
        real = self.store.put_workout

        def crash(plan_id, row):
            if row["state"] == "uploaded":
                raise RuntimeError("TEST-ONLY crash")
            return real(plan_id, row)

        self.store.put_workout = crash
        with self.assertLogs("garmin_ext.plans", "ERROR"):
            self.assertEqual(self.commit(pid, c["token"])["status"], "unknown")
        self.assertEqual(self.rows(pid)["20261202-run"]["state"], "uploading")
        store2 = self.new_store()
        svc2 = self.service(store=store2)
        self.assertEqual(self.commit(pid, c["token"], svc=svc2)["status"], "partial", "quarantine first")
        self.age_rows(pid)
        out = self.commit(pid, c["token"], svc=svc2)
        self.assertEqual(out["status"], "done", out)
        self.assertEqual(len(self.garmin.uploads()), 1)
        self.assert_no_double_writes(1)

    def test_token_windows(self):
        pid, c = self.preview()
        h = ps.token_hash(c["token"])
        self.sql(f"UPDATE {SCHEMA}.plan_tokens SET expires_at = now() - interval '1 second' WHERE token_hash = %s",
                 (h,))
        self.assertEqual(self.commit(pid, c["token"])["reason"], "expired")
        self.assertEqual(self.garmin.calls, [])
        self.sql(f"UPDATE {SCHEMA}.plan_tokens SET expires_at = now() + interval '1 minute' WHERE token_hash = %s",
                 (h,))
        self.garmin.faults["upload"] = {1: before(b.GarminConnectTooManyRequestsError("x"))}
        self.assertEqual(self.commit(pid, c["token"])["status"], "partial")
        first, expires = self.sql(f"SELECT first_used_at, expires_at FROM {SCHEMA}.plan_tokens WHERE token_hash = %s",
                                  (h,))[0]
        self.assertEqual(expires - first, ps.TOKEN_RESUME)
        # Started almost a day ago: still resumes; a day and a second ago: expired.
        self.sql(f"UPDATE {SCHEMA}.plan_tokens SET first_used_at = now() - interval '23 hours 59 minutes', "
                 "expires_at = now() + interval '1 minute' WHERE token_hash = %s", (h,))
        self.garmin.faults["upload"] = {2: before(b.GarminConnectTooManyRequestsError("x"))}
        self.assertEqual(self.commit(pid, c["token"])["status"], "partial")
        expires2 = self.sql(f"SELECT expires_at FROM {SCHEMA}.plan_tokens WHERE token_hash = %s", (h,))[0][0]
        self.assertLess(abs((expires2 - dt.datetime.now(dt.timezone.utc)).total_seconds() - 60), 30,
                        "a resume keeps the first use's window")
        self.sql(f"UPDATE {SCHEMA}.plan_tokens SET expires_at = now() - interval '1 second' WHERE token_hash = %s",
                 (h,))
        calls = len(self.garmin.calls)
        self.assertEqual(self.commit(pid, c["token"])["reason"], "expired")
        self.assertEqual(len(self.garmin.calls), calls)

    def test_a_done_token_replays_after_its_window(self):
        pid, c = self.preview()
        self.assertEqual(self.commit(pid, c["token"])["status"], "done")
        self.sql(f"UPDATE {SCHEMA}.plan_tokens SET expires_at = now() - interval '3 days' WHERE token_hash = %s",
                 (ps.token_hash(c["token"]),))
        calls = len(self.garmin.calls)
        self.assertTrue(self.commit(pid, c["token"])["replayed"])
        self.assertEqual(len(self.garmin.calls), calls)

    def test_tokens_bind_plan_and_action(self):
        pid, c = self.preview()
        other, _c = self.preview(sessions=[b.session("20300721-run", b.TODAY + dt.timedelta(days=10))], content="e")
        self.assertEqual(self.commit(other, c["token"])["reason"], "unknown_token")
        self.assertEqual(self.cancel(pid, "xt1." + c["token"][4:])["reason"], "unknown_token")
        self.preview(plan_id=pid, content="b")
        self.assertEqual(self.commit(pid, c["token"])["reason"], "superseded")
        self.assertEqual(self.garmin.calls, [])

    def test_replace_then_cancel_with_a_refused_delete(self):
        pid, _c = self.committed()
        old = self.rows(pid)["20261205-run"]
        changed = b.default_sessions()
        changed[2] = b.session("20261205-run", b.TODAY + dt.timedelta(days=4), km=12.0)
        _p, c2 = self.preview(sessions=changed, plan_id=pid, content="b")
        out = self.commit(pid, c2["token"])
        self.assertEqual((out["status"], out["replaced"]), ("done", [{"key": "20261205-run",
                                                                      "date": old["date_local"].isoformat()}]))
        retired = f"20261205-run~{old['dto_hash'][:8]}"
        self.assertEqual(self.rows(pid)[retired]["state"], "deleted")
        self.assert_no_double_writes()
        wid = self.rows(pid)["20261203-run"]["garmin_workout_id"]
        self.garmin.reject_delete_ids.add(wid)
        prev = self.cancel(pid)
        out = self.cancel(pid, prev["cancel"]["token"])
        self.assertEqual(out["status"], "partial", out)
        self.assertEqual(self.plan_status(pid), "active")
        self.garmin.reject_delete_ids.clear()
        self.assertEqual(self.cancel(pid, prev["cancel"]["token"])["status"], "done")
        self.assertEqual(self.plan_status(pid), "cancelled")

    def test_two_calls_same_token_one_is_busy(self):
        pid, c = self.preview()
        svc = self.service(join_s=10)
        self.garmin.faults["upload"] = {1: BLOCK}
        box = {}

        def target():
            box["out"] = self.commit(pid, c["token"], svc=svc)

        t = threading.Thread(target=target, name="deep-writes-pg-call")
        t.start()
        self.addCleanup(t.join, 10)
        self.assertTrue(tdw.wait_for(lambda: len(self.garmin.uploads()) == 1))
        self.assertEqual(self.commit(pid, c["token"], svc=svc)["reason"], "busy")
        self.garmin.blocked.set()
        t.join(10)
        self.assertEqual(box["out"]["status"], "done")
        self.assert_no_double_writes()

    def test_cleanup_dry_run_then_real_run(self):
        pid, _c = self.committed()
        # A second plan of another (random) account with a row on Garmin: kept.
        other_fp = self.store.account_fp(self.profile + 1)
        opid, _v, _ch = self.store.save_preview(other_fp, None, dt.date(2027, 3, 1), 1, {}, {}, "e" * 64)
        self.plan_ids.append(opid)
        self.store.put_workout(opid, {"workout_key": "20261202-run", "version": 1, "date_local": b.TODAY + dt.timedelta(
            days=2), "role": "easy", "dto_hash": "d" * 64, "state": "scheduled", "garmin_workout_id": 950,
            "garmin_schedule_id": 951})
        before_rows = {k: (r["state"], r["state_changed_at"]) for k, r in self.rows(pid).items()}
        writes = len(self.garmin.writes())
        counts = pw.cleanup(self.store, self.garmin, b.TODAY, dry_run=True, orphans=True, sleep=lambda s: None)
        self.assertGreaterEqual(counts["rows"], 3)
        self.assertGreaterEqual(counts["other_account"], 1)
        self.assertEqual(len(self.garmin.writes()), writes)
        self.assertEqual({k: (r["state"], r["state_changed_at"]) for k, r in self.rows(pid).items()}, before_rows)
        mine = {r["garmin_workout_id"] for r in self.rows(pid).values()}
        # The shared database may hold other tests' rows of other accounts; only this account's are deleted.
        counts = pw.cleanup(self.store, self.garmin, b.TODAY, dry_run=False, sleep=lambda s: None, join_s=2)
        self.assertEqual(counts["failed"], 0, counts)
        self.assertEqual({r["state"] for r in self.rows(pid).values()}, {"deleted"})
        self.assertEqual(self.rows(opid)["20261202-run"]["state"], "scheduled")
        self.assertFalse(mine & set(self.garmin.library))
        self.assertFalse([w for w in self.garmin.write_log if w[1].endswith(("/950", "/951"))])


if __name__ == "__main__":
    unittest.main()
