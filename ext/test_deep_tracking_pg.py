"""Deep tracking tests on PgPlanStore (COACH_TEST_DATABASE_URL): links, settling, feedback, plan_get and the
daily status's plan section against real Postgres. The database is shared with other suites: every test uses
its own account (a random profile id, so its own fingerprint and plans) and never drops or forgets anything.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import logging
import os
import secrets
import unittest
from unittest import mock

import plan_store as ps
import plan_tools as pt
import plan_tracking as tr

DB_URL = os.environ.get("COACH_TEST_DATABASE_URL", "").strip()
HAVE_PSYCOPG = importlib.util.find_spec("psycopg") is not None
PG_SKIP = "needs psycopg and COACH_TEST_DATABASE_URL (Postgres with pgvector)"
UTC = dt.timezone.utc
RACE = dt.date(2027, 4, 4)
MON = dt.date(2026, 11, 30)


def day(n):
    return MON + dt.timedelta(days=n)


def session(key, when, role="easy", km=8.0, minutes=48):
    return {"key": key, "date": when.isoformat(), "role": role, "km": km, "minutes": minutes}


SESSIONS = [session("w01-e1", day(1)), session("w01-q1", day(2), role="threshold", km=10.0, minutes=55),
            session("w01-e2", day(4), minutes=36), session("w01-l", day(6), role="long", km=20.0, minutes=120),
            session("w02-e1", day(8))]


def body():
    weeks = []
    for n in range(2):
        s = MON + dt.timedelta(days=7 * n)
        e = s + dt.timedelta(days=6)
        weeks.append({"week": n + 1, "start": s.isoformat(), "end": e.isoformat(),
                      "sessions": [x for x in SESSIONS if s.isoformat() <= x["date"] <= e.isoformat()]})
    return {"weeks": weeks, "content_hash": "p"}


def run(aid, when, hour=7, type_key="running", km=8.0, minutes=48.0):
    return {"activityId": aid, "activityType": {"typeKey": type_key},
            "startTimeLocal": f"{when.isoformat()} {hour:02d}:00:00", "distance": km * 1000.0 if km else None,
            "duration": minutes * 60.0}


class OwnAccount:
    """The store as one account sees it: history_account() is this test's fingerprint (the shared meta row is
    never written)."""

    def __init__(self, store, fp):
        self._store, self._fp = store, fp

    def history_account(self):
        return self._fp

    def __getattr__(self, name):
        return getattr(self._store, name)


@unittest.skipUnless(DB_URL and HAVE_PSYCOPG, PG_SKIP)
class PgTracking(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import history_store as hs

        hs.PgHistoryStore(DB_URL).migrate()   # idempotent; never forget_all on the shared database

    def setUp(self):
        self.store = ps.PgPlanStore(DB_URL)
        self.profile = str(10 ** 12 + secrets.randbelow(10 ** 12))
        self.fp = self.store.account_fp(self.profile)
        self.pid, _, _ = self.store.save_preview(self.fp, None, RACE, 1, {}, body(), secrets.token_hex(32))
        self.store.move_plan(self.pid, "active")   # tracking follows the active plan only
        # The plan is created and activated now(); these tests' runs are in Nov/Dec 2026, so date both before them.
        with self.store.transaction() as conn:
            conn.execute("UPDATE history.plans SET created_at = %s, activated_at = %s WHERE plan_id = %s",
                         (dt.datetime(2026, 11, 1, tzinfo=UTC), dt.datetime(2026, 11, 1, tzinfo=UTC), self.pid))
        logger = logging.getLogger("garmin_ext.plans")
        saved = (logger.handlers[:], logger.propagate)
        logger.handlers, logger.propagate = [logging.NullHandler()], False
        self.addCleanup(lambda: setattr(logger, "handlers", saved[0]) or setattr(logger, "propagate", saved[1]))

    def links(self):
        return {r["workout_key"]: (r["link_status"], r["linked_activity_id"]) for r in self.store.links(self.pid)}

    def weekly(self, acts, end, today=None, scheduled=()):
        return tr.refresh_links(self.store, self.fp, end, acts, list(scheduled), first=end - dt.timedelta(days=13),
                                last=end, tz=UTC, today=today or end + dt.timedelta(days=1))

    def test_links_settle_and_count(self):
        acts = [run(1, day(1)), run(2, day(3), km=10.0, minutes=55.0),
                run(3, day(4), type_key="cycling", km=20.0, minutes=40.0), run(4, day(6), km=12.0, minutes=75.0)]
        state = self.weekly(acts, day(7))
        expected = {"w01-e1": ("done", 1), "w01-q1": ("done_late", 2), "w01-e2": ("replaced", 3),
                    "w01-l": ("partial", 4)}
        self.assertEqual(self.links(), expected)
        self.assertEqual(tr.week_counts(self.store, self.fp, MON),
                         {"planned": 4, "done": 2, "partial": 1, "replaced": 1, "missed": 0, "open": 0, "moved": 1})
        self.assertEqual(tr.week_counts(self.store, self.fp, MON, state=state), tr.week_counts(self.store, self.fp,
                                                                                               MON))
        rows = {r["workout_key"]: r for r in self.store.links(self.pid)}
        self.assertEqual(rows["w01-e1"]["state"], ps.LOCAL_STATE)
        self.assertIsNotNone(rows["w01-e1"]["link_checked_at"])
        # A refresh with fewer days keeps every counted link.
        tr.refresh_links(self.store, self.fp, day(8), [], [], tz=UTC, today=day(8))
        self.assertEqual(self.links(), expected)

    def test_missed_waits_then_upgrades(self):
        self.weekly([], day(7), today=day(7))
        self.assertNotIn("w01-l", self.links())                      # Monday, its late day, is not over
        self.weekly([], day(7), today=day(8))
        self.assertEqual(self.links()["w01-l"], ("missed", None))
        self.weekly([run(9, day(7), km=20.0, minutes=120.0)], day(7), today=day(8))
        self.assertEqual(self.links()["w01-l"], ("done_late", 9))

    def test_garmin_row_keeps_its_state_and_link_wins(self):
        self.store.put_workout(self.pid, {"workout_key": "w01-e1", "version": 1, "date_local": day(1), "role": "easy",
                                          "dto_hash": "0" * 64, "state": "scheduled", "garmin_workout_id": 990001,
                                          "garmin_schedule_id": 77})
        entries = [{"workoutId": 990001, "scheduleDate": day(1).isoformat(), "associatedActivityId": 4242}]
        self.weekly([run(4242, day(3))], day(6), scheduled=entries)
        rows = {r["workout_key"]: r for r in self.store.links(self.pid)}
        self.assertEqual((rows["w01-e1"]["link_status"], rows["w01-e1"]["linked_activity_id"]), ("done", 4242))
        self.assertEqual((rows["w01-e1"]["state"], rows["w01-e1"]["garmin_workout_id"]), ("scheduled", 990001))
        self.assertEqual(tr.ours_by_garmin_id(self.store, self.fp, [990001, 5]),
                         {990001: {"workout_key": "w01-e1", "role": "easy"}})

    def test_activation_keeps_a_counted_link(self):
        tr.refresh_links(self.store, self.fp, day(1), [run(1, day(1))], [], tz=UTC, today=day(1))
        with self.store.transaction() as conn:     # activation moved to Wednesday noon, between the two runs
            conn.execute("UPDATE history.plans SET activated_at = %s WHERE plan_id = %s",
                         (dt.datetime(2026, 12, 2, 12, 0, tzinfo=UTC), self.pid))
        self.weekly([run(1, day(1)), run(2, day(2))], day(6))
        self.assertEqual(self.links()["w01-e1"], ("done", 1))
        self.assertEqual(self.links()["w01-q1"], ("missed", None))   # Wednesday's run predates the activation

    def test_today_plan_and_feedback(self):
        tr.refresh_links(self.store, self.fp, day(1), [run(1, day(1))], [], tz=UTC, today=day(1))
        today = tr.today_plan(self.store, self.fp, day(1))
        self.assertEqual((today["workout_key"], today["link_status"], today["feedback_due"], today["feedback_for"]),
                         ("w01-e1", "done", True, "w01-e1"))
        svc = pt.PlanService(lambda: None, lambda: OwnAccount(self.store, self.fp), lambda: day(1))
        first = json.loads(svc.feedback("w01-e1", 4, 0, "none", False))
        second = json.loads(svc.feedback("w01-e1", 7, 4, "achilles", True))
        self.assertEqual((first["updated"], second["updated"], second["progression_hold"]), (False, True, True))
        rows = self.store.feedback(self.pid)
        self.assertEqual([(r["rpe"], r["pain"], r["pain_location"], r["sharp"]) for r in rows],
                         [(7, 4, "achilles", True)])
        self.assertLessEqual(rows[0]["created_at"], rows[0]["updated_at"])
        self.assertFalse(tr.today_plan(self.store, self.fp, day(1))["feedback_due"])
        self.assertTrue(svc.feedback("w01-e1", 0, 0, "none", False).startswith("Error: Invalid input: "))
        self.assertIn("after tomorrow", svc.feedback("w02-e1", 4, 0, "none", False))
        other = self.store.account_fp(str(int(self.profile) + 1))
        foreign, _, _ = self.store.save_preview(other, None, RACE, 1, {}, body(), secrets.token_hex(32))
        self.assertEqual(svc.feedback("w01-e1", 4, 0, "none", False, foreign), pt.TEXT_NOT_FOUND)
        self.assertEqual(self.store.feedback(foreign), [])
        with mock.patch.dict(os.environ, {"REPORT_TZ": "Asia/Jerusalem"}):
            got = json.loads(svc.get(""))
        self.assertEqual(got["tracking"]["links"][0]["key"], "w01-e1")
        self.assertRegex(got["tracking"]["links"][0]["as_of"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$")
        self.assertEqual(got["tracking"]["feedback"][0]["pain_location"], "achilles")

    def test_daily_status_plan_section(self):
        import daily_status as ds
        import test_deep_tracking_reports as rep

        self.addCleanup(tr.configure, None)
        self.addCleanup(rep.join_plan_threads)
        tr.configure(lambda: self.store)
        data = rep.daily_data(day(1), acts_today=[run(31, day(1))])
        data[f"stats:{day(1).isoformat()}"] = {"userProfileId": int(self.profile)}
        quiet = logging.getLogger(ds.log.name)
        saved = (quiet.handlers[:], quiet.propagate)
        quiet.handlers, quiet.propagate = [logging.NullHandler()], False
        self.addCleanup(lambda: setattr(quiet, "handlers", saved[0]) or setattr(quiet, "propagate", saved[1]))
        out = rep.build_daily(data, text_format="markdown")
        self.assertEqual((out["plan"]["workout_key"], out["plan"]["link_status"], out["plan"]["feedback_for"]),
                         ("w01-e1", "done", "w01-e1"))
        self.assertIn("Plan: easy run 8 km 48 min (done); feedback due (effort and pain)", out["text"])


if __name__ == "__main__":
    unittest.main()
