"""Deep tests of log_feedback and plan_get's tracking view (PLAN_TRACKING=on) on MemoryPlanStore. Standard
library only.

Every bound of rpe and pain, each pain location and invalid ones, the sharp flag's types, the upsert, unknown
keys, another account's plan, no account recorded, a session after tomorrow, and that no free text reaches an
answer, a log line or a stored row (docs/training-plan.md "Feedback").
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import logging
import os
import unittest
from unittest import mock

import garmin_paths
import plan_store as ps
import plan_tools as pt
import plan_tracking as tr

UTC = dt.timezone.utc
RACE = dt.date(2027, 4, 4)
MON = dt.date(2026, 11, 30)
FREE_TEXT = "my left knee hurts SECRET-77"


def day(n):
    return MON + dt.timedelta(days=n)


def body():
    sessions = [{"key": "20261202-run", "date": day(1).isoformat(), "role": "easy", "km": 8.0, "minutes": 48},
                {"key": "20261203-run", "date": day(2).isoformat(), "role": "threshold", "km": 10.0, "minutes": 55},
                {"key": "20261205-run", "date": day(4).isoformat(), "role": "easy", "km": 6.0, "minutes": 36},
                {"key": "20300224-b", "date": day(5).isoformat(), "role": "b_race", "km": 0.0, "minutes": None},
                {"key": "20300220-run", "date": day(6).isoformat(), "role": "long", "km": 20.0, "minutes": 120}]
    return {"weeks": [{"week": 1, "start": MON.isoformat(), "end": day(6).isoformat(), "sessions": sessions}],
            "content_hash": "f"}


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


class Base(unittest.TestCase):
    def setUp(self):
        self.clock = Clock(dt.datetime(2026, 11, 28, 9, 0, tzinfo=UTC))
        self.store = ps.MemoryPlanStore(clock=self.clock)
        self.fp = self.store.account_fp("424242")
        self.store.meta[ps.META_ACCOUNT] = self.fp
        self.pid, _, _ = self.store.save_preview(self.fp, None, RACE, 1, {}, body(), hashlib.sha256(b"a").hexdigest())
        self.store.move_plan(self.pid, "active")   # feedback goes to the active plan only
        self.today = day(1)
        self.svc = pt.PlanService(lambda: None, lambda: self.store, lambda: self.today)
        logger = logging.getLogger("garmin_ext.plans")
        saved = (logger.handlers[:], logger.propagate)
        logger.handlers, logger.propagate = [logging.NullHandler()], False
        self.addCleanup(lambda: setattr(logger, "handlers", saved[0]) or setattr(logger, "propagate", saved[1]))

    def log(self, **kw):
        args = {"workout_key": "20261202-run", "rpe": 5, "pain": 0, "pain_location": "none", "sharp": False, **kw}
        return self.svc.feedback(**args)

    def ok(self, **kw):
        text = self.log(**kw)
        self.assertFalse(text.startswith("Error"), text)
        return json.loads(text)

    def refused(self, **kw):
        text = self.log(**kw)
        self.assertTrue(text.startswith("Error: Invalid input: "), text)
        return text


class Bounds(Base):
    def test_rpe_bounds(self):
        # None is "not given" (1.1.1): a session's first feedback needs rpe, a later call keeps the stored one
        self.assertIn("the first feedback for a session needs rpe", self.refused(rpe=None))
        for rpe in (1, 10):
            self.assertEqual(self.ok(rpe=rpe)["rpe"], rpe)
        for rpe in (0, 11, -1, 100, 5.0, 5.5, "5", True, False):
            with self.subTest(rpe=rpe):
                self.assertIn("rpe must be a whole number from 1 to 10", self.refused(rpe=rpe))
        self.assertEqual(self.ok(rpe=None)["rpe"], 10)

    def test_pain_bounds(self):
        self.assertEqual(self.ok(pain=0, pain_location="none")["pain"], 0)
        self.assertEqual(self.ok(pain=10, pain_location="knee")["pain"], 10)
        for pain in (-1, 11, 3.0, "2", True):
            with self.subTest(pain=pain):
                self.assertIn("pain must be a whole number from 0 to 10", self.refused(pain=pain, pain_location="knee"))
        kept = self.ok(rpe=6, pain=None, pain_location=None, sharp=None)   # not given: the stored pain stays
        self.assertEqual((kept["rpe"], kept["pain"], kept["pain_location"]), (6, 10, "knee"))

    def test_every_pain_location(self):
        for location in ps.PAIN_LOCATIONS:
            with self.subTest(location=location):
                pain = 0 if location == "none" else 2
                self.assertEqual(self.ok(pain=pain, pain_location=location)["pain_location"], location)
        self.assertEqual(len(ps.PAIN_LOCATIONS), 13)
        self.assertEqual(self.ok(pain=0, pain_location="")["pain_location"], "none")
        self.assertEqual(self.ok(pain=0, pain_location=None)["pain_location"], "none")
        self.assertEqual(self.ok(pain=2, pain_location=" Lower_Back ")["pain_location"], "lower_back")

    def test_invalid_pain_locations(self):
        for location in ("elbow", "lower back", "lowerback", "knee,calf", FREE_TEXT, "kn ee", "none;drop", 5, ["knee"]):
            with self.subTest(location=location):
                text = self.refused(pain=2, pain_location=location)
                self.assertIn("pain_location must be one of: none, foot", text)
                self.assertNotIn("SECRET", text)

    def test_pain_and_location_go_together(self):
        self.assertIn("pain_location must be none", self.refused(pain=0, pain_location="calf"))
        self.assertIn("give pain_location", self.refused(pain=1, pain_location="none"))
        self.assertIn("give pain_location", self.refused(pain=1, pain_location=""))

    def test_sharp_types(self):
        self.assertTrue(self.ok(pain=1, pain_location="shin", sharp=True)["sharp"])
        self.assertFalse(self.ok(pain=1, pain_location="shin", sharp=False)["sharp"])
        for sharp in (1, 0, "true", "false", "yes"):
            with self.subTest(sharp=sharp):
                self.assertIn("sharp must be true or false", self.refused(pain=1, pain_location="shin", sharp=sharp))
        self.assertTrue(self.ok(pain=None, pain_location=None, sharp=True)["sharp"])
        self.assertTrue(self.ok(pain=None, pain_location=None, sharp=None, rpe=3)["sharp"])   # not given: kept
        self.assertIn("sharp false", self.refused(pain=0, pain_location="none", sharp=True))

    def test_progression_hold_bounds(self):
        cases = [(3, False, False), (4, False, True), (10, False, True), (1, True, True), (0, False, False)]
        for pain, sharp, hold in cases:
            with self.subTest(pain=pain, sharp=sharp):
                out = self.ok(pain=pain, pain_location="none" if pain == 0 else "calf", sharp=sharp)
                self.assertEqual(out["progression_hold"], hold)
                self.assertEqual("physiotherapist" in out["note"], hold)

    def test_bad_keys_and_plan_ids(self):
        for key in ("", "w1-e1", "W01-E1", "w01-", "w01-toolongkey", "../w01", FREE_TEXT, "20261202-run ", None, 5,
                    "20261202-run~abcdef01", "20261302-run", "20261202-long", "2026-12-02-run", "w01-e1"):
            with self.subTest(key=key):
                if key == "20261202-run ":
                    self.assertTrue(self.ok(workout_key=key)["stored"])   # stripped, like every text argument
                    continue
                self.assertNotIn("SECRET", self.refused(workout_key=key))
        for plan_id in ("x", "A" * 32, "a" * 31, FREE_TEXT):
            with self.subTest(plan_id=plan_id):
                self.assertNotIn("SECRET", self.refused(plan_id=plan_id))

    def test_the_server_guard_agrees(self):
        ok = {"workout_key": "20261202-run", "rpe": 5, "pain": 0, "pain_location": "none", "sharp": False, "plan_id": ""}
        self.assertIsNone(garmin_paths.refused_argument("log_feedback", ok))
        for name, value in (("rpe", 0), ("rpe", 11), ("pain", -1), ("pain", 11), ("pain_location", "elbow"),
                            ("sharp", 1), ("sharp", "true"), ("workout_key", FREE_TEXT), ("plan_id", FREE_TEXT)):
            with self.subTest(name=name, value=value):
                refused = garmin_paths.refused_argument("log_feedback", {**ok, name: value})
                self.assertEqual(refused[0], name)
                self.assertNotIn("SECRET", json.dumps(refused, default=str))
        for name, value in (("rpe", 1), ("rpe", 10), ("pain", 0), ("pain", 10), ("pain_location", "")):
            with self.subTest(name=name, value=value):
                self.assertIsNone(garmin_paths.refused_argument("log_feedback", {**ok, name: value}))


class Storage(Base):
    def test_upsert_twice_keeps_one_row(self):
        first = self.ok(rpe=4)
        self.assertEqual((first["stored"], first["updated"]), (True, False))
        created = self.store.feedback(self.pid)[0]["created_at"]
        self.clock.now += dt.timedelta(hours=2)
        second = self.ok(rpe=7, pain=3, pain_location="calf", sharp=True)
        self.assertEqual((second["stored"], second["updated"]), (True, True))
        rows = self.store.feedback(self.pid)
        self.assertEqual(len(rows), 1)
        self.assertEqual({k: rows[0][k] for k in ("workout_key", "rpe", "pain", "pain_location", "sharp")},
                         {"workout_key": "20261202-run", "rpe": 7, "pain": 3, "pain_location": "calf", "sharp": True})
        self.assertEqual(rows[0]["created_at"], created)
        self.assertGreater(rows[0]["updated_at"], created)

    def test_answer_shape(self):
        out = self.ok(rpe=6, pain=2, pain_location="hip")
        self.assertEqual(set(out), {"stored", "updated", "plan_id", "workout_key", "date", "role", "rpe", "pain",
                                    "pain_location", "sharp", "progression_hold", "note"})
        self.assertEqual((out["plan_id"], out["date"], out["role"]), (self.pid, "2026-12-01", "easy"))

    def test_unknown_key_and_session_dates(self):
        self.assertIn("not a session of the plan", self.refused(workout_key="20310118-run"))
        self.assertTrue(self.ok(workout_key="20261203-run")["stored"])                # tomorrow
        self.assertIn("after tomorrow", self.refused(workout_key="20261205-run"))    # Friday, today Tuesday
        self.today = day(4)
        self.assertTrue(self.ok(workout_key="20261205-run")["stored"])               # on its day
        self.assertTrue(self.ok(workout_key="20300224-b")["stored"])                # tomorrow's B race (not tracked)
        self.today = day(30)
        self.assertTrue(self.ok(workout_key="20300220-run")["stored"])             # long after
        self.assertEqual(len(self.store.feedback(self.pid)), 4)

    def test_other_account_and_no_account(self):
        other = self.store.account_fp("999999")
        foreign, _, _ = self.store.save_preview(other, None, RACE, 1, {}, body(), hashlib.sha256(b"b").hexdigest())
        self.assertEqual(self.log(plan_id=foreign), pt.TEXT_NOT_FOUND)
        self.assertEqual(self.store.feedback(foreign), [])
        self.assertTrue(self.ok(plan_id=self.pid)["stored"])
        del self.store.meta[ps.META_ACCOUNT]
        self.assertEqual(self.log(), pt.TEXT_NO_ACCOUNT)
        self.store.meta[ps.META_ACCOUNT] = other
        self.assertEqual(self.log(plan_id=self.pid), pt.TEXT_NOT_FOUND)     # our plan, but not this account's
        self.store.meta[ps.META_ACCOUNT] = self.fp
        self.store.meta[ps.META_MISMATCH] = "yes"                             # Garmin signed in to another
        self.assertEqual(self.log(), pt.TEXT_NO_ACCOUNT)

    def test_cancelled_and_finished_plans(self):
        for status in ("cancelled", "finished"):
            with self.subTest(status=status):
                self.store.set_status(self.pid, status)
                self.assertEqual(self.log(), pt.TEXT_NO_PLAN)
                self.assertEqual(self.log(plan_id=self.pid), pt.TEXT_NOT_FOUND)
        self.assertEqual(self.store.feedback(self.pid), [])

    def test_the_tracked_plan_is_the_active_one(self):
        draft, _, _ = self.store.save_preview(self.fp, None, RACE, 1, {}, body(), hashlib.sha256(b"c").hexdigest())
        self.clock.now += dt.timedelta(hours=1)
        self.assertEqual(self.ok()["plan_id"], self.pid)                       # the active plan, not the newer draft
        self.assertEqual(self.log(plan_id=draft), pt.TEXT_DRAFT)              # a draft is refused
        self.assertEqual(self.store.feedback(draft), [])

    def test_database_failures(self):
        class Broken(ps.MemoryPlanStore):
            def save_feedback(self, *a, **k):
                raise ps.StoreError("plans: database error SECRET-ROW")

        broken = Broken()
        broken.meta.update(self.store.meta)
        broken.plans, broken.versions = self.store.plans, self.store.versions
        svc = pt.PlanService(lambda: None, lambda: broken, lambda: self.today)
        with self.assertLogs("garmin_ext.plans", "WARNING") as logs:
            text = svc.feedback("20261202-run", 5, 0, "none", False)
        self.assertEqual(text, pt.TEXT_DB)
        self.assertEqual(logs.output, ["WARNING:garmin_ext.plans:plans: log_feedback failed (StoreError)"])
        broken.is_ready = False
        with self.assertLogs("garmin_ext.plans", "WARNING"):
            self.assertEqual(svc.feedback("20261202-run", 5, 0, "none", False), pt.TEXT_STARTING)

        def no_store():
            raise ps.StoreError("plans: cannot connect to the database")
        svc = pt.PlanService(lambda: None, no_store, lambda: self.today)
        with self.assertLogs("garmin_ext.plans", "WARNING"):
            self.assertEqual(svc.feedback("20261202-run", 5, 0, "none", False), pt.TEXT_DB)


class NoFreeText(Base):
    def test_no_free_text_in_answers_logs_or_rows(self):
        root = logging.getLogger()
        records = []

        class Keep(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        keep = Keep(level=logging.DEBUG)
        plans = logging.getLogger("garmin_ext.plans")
        plans.addHandler(keep)
        old_level = plans.level
        plans.setLevel(logging.DEBUG)
        self.addCleanup(plans.setLevel, old_level)
        self.addCleanup(plans.removeHandler, keep)
        answers = []
        for kw in (dict(workout_key=FREE_TEXT), dict(pain=2, pain_location=FREE_TEXT), dict(rpe=FREE_TEXT),
                   dict(pain=FREE_TEXT), dict(sharp=FREE_TEXT), dict(plan_id=FREE_TEXT),
                   dict(workout_key="20261202-run", pain=2, pain_location="calf")):
            answers.append(self.log(**kw))
        self.assertTrue(all("SECRET" not in a and "knee hurts" not in a for a in answers), answers)
        self.assertFalse(any("SECRET" in r for r in records), records)
        self.assertNotIn(keep, root.handlers)
        for row in self.store.feedback(self.pid):
            self.assertEqual(set(row), {"plan_id", "workout_key", "rpe", "pain", "pain_location", "sharp",
                                        "created_at", "updated_at"})
            self.assertIn(row["pain_location"], ps.PAIN_LOCATIONS)

    def test_the_store_refuses_free_text_too(self):
        for args in (("20261202-run", 5, 2, FREE_TEXT, False), ("20261202-run", 5, 2, "Calf", False),
                     (FREE_TEXT, 5, 0, "none", False), ("20261202-run", 11, 0, "none", False),
                     ("20261202-run", 5, 0, "none", "yes")):
            with self.subTest(args=args):
                with self.assertRaises(ValueError) as cm:
                    self.store.save_feedback(self.pid, *args)
                self.assertNotIn("SECRET", str(cm.exception))
                self.assertNotIn("Calf", str(cm.exception))

    def test_registered_tool(self):
        added = {}

        def adder(fn, name=None, description=None, **kwargs):
            added[name] = fn
            return fn

        class Cfg:
            plan_tracking = True

        self.addCleanup(tr.configure, None)
        pt.register(adder, Cfg(), lambda: None, store=self.store, today=lambda: self.today)
        tool = added["log_feedback"]
        out = json.loads(asyncio.run(tool(workout_key="20261202-run", rpe=3)))
        self.assertEqual((out["rpe"], out["pain"], out["pain_location"], out["sharp"]), (3, 0, "none", False))
        self.assertTrue(asyncio.run(tool(workout_key="20261202-run")).startswith("Error: Invalid input: give rpe"))
        out = json.loads(asyncio.run(tool(workout_key="20261202-run", pain=5, pain_location="knee")))
        self.assertEqual((out["rpe"], out["pain"]), (3, 5))
        out = json.loads(asyncio.run(tool(workout_key="20261202-run", rpe=8)))   # a correction keeps the pain
        self.assertEqual((out["rpe"], out["pain"], out["pain_location"]), (8, 5, "knee"))


class PlanGet(Base):
    def test_tracking_view_in_the_report_zone(self):
        self.clock.now = dt.datetime(2026, 12, 1, 21, 30, tzinfo=UTC)          # 23:30 in Jerusalem
        run = {"activityId": 5, "activityType": {"typeKey": "running"}, "startTimeLocal": "2026-12-01 07:00:00",
               "distance": 8000.0, "duration": 2880.0}
        tr.refresh_links(self.store, self.fp, day(1), [run], [], tz=UTC, today=day(1))
        self.ok(rpe=4, pain=1, pain_location="foot")
        with mock.patch.dict(os.environ, {"REPORT_TZ": "Asia/Jerusalem"}):
            got = json.loads(self.svc.get(""))
        self.assertEqual(got["tracking"]["links"], [{"key": "20261202-run", "date": "2026-12-01", "status": "done",
                                                     "as_of": "2026-12-01T23:30"}])
        self.assertEqual(got["tracking"]["feedback"], [{"key": "20261202-run", "rpe": 4, "pain": 1, "pain_location": "foot",
                                                        "sharp": False}])
        self.assertIn(pt.NOTE_LINKS, got["note"])
        with mock.patch.dict(os.environ, {"REPORT_TZ": "Europe/London"}):
            got = json.loads(self.svc.get(""))
        self.assertEqual(got["tracking"]["links"][0]["as_of"], "2026-12-01T21:30")

    def test_rows_never_checked_are_not_shown(self):
        self.store.put_workout(self.pid, {"workout_key": "20261202-run", "version": 1, "date_local": day(1), "role": "easy",
                                          "dto_hash": "0" * 64, "state": "scheduled", "garmin_workout_id": 7001})
        got = json.loads(self.svc.get(""))
        self.assertNotIn("tracking", got)

    def test_only_this_accounts_plans(self):
        other = self.store.account_fp("999999")
        foreign, _, _ = self.store.save_preview(other, None, RACE, 1, {}, body(), hashlib.sha256(b"b").hexdigest())
        self.store.save_feedback(foreign, "20261202-run", 5, 0, "none", False)
        self.assertEqual(self.svc.get(foreign), pt.TEXT_NOT_FOUND)
        self.assertNotIn("tracking", json.loads(self.svc.get("")))


if __name__ == "__main__":
    unittest.main()
