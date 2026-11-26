"""plan_activate (PLAN_TRACKING=on): start or stop following a stored plan, in the app's database only.
MemoryPlanStore, no Garmin client at all. Standard library only."""

from __future__ import annotations

import datetime as dt
import json
import logging
import unittest

import plan_activate as pa
import plan_store as ps
import plan_tools as pt
import plan_tracking as tr
from test_plan_tracking import RACE, UTC, WEEK1, Clock, act, make_plan


class Base(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.clock.now = dt.datetime(2026, 11, 28, 9, 0, tzinfo=UTC)
        self.store = ps.MemoryPlanStore(clock=self.clock)
        self.fp = self.store.account_fp("123456")
        self.store.meta[ps.META_ACCOUNT] = self.fp
        self.pid, self.version = make_plan(self.store, self.fp, active=False)
        self.today = WEEK1 - dt.timedelta(days=2)
        self.svc = pa.ActivateService(lambda: self.store, lambda: self.today)
        logger = logging.getLogger("garmin_ext.plans")
        saved = (logger.handlers[:], logger.propagate)
        logger.handlers, logger.propagate = [logging.NullHandler()], False
        self.addCleanup(lambda: setattr(logger, "handlers", saved[0]) or setattr(logger, "propagate", saved[1]))

    def start(self, pid=None, version=None, **kw):
        return self.svc.activate(pid or self.pid, self.version if version is None else version, **kw)

    def stop(self, pid=None):
        return self.svc.activate(pid or self.pid, 0, "stop")

    def status(self, pid=None):
        return self.store.get_plan(self.fp, pid or self.pid)["status"]


class StartStop(Base):
    def test_start_then_stop_and_start_again(self):
        out = json.loads(self.start())
        self.assertEqual(out, {"plan_id": self.pid, "version": self.version, "status": "active", "changed": True,
                               "note": pa.NOTE_STARTED})
        self.assertEqual(self.status(), "active")
        self.assertEqual(self.store.tracked_plan(self.fp)["plan_id"], self.pid)
        # The same call again changes nothing (idempotent).
        again = json.loads(self.start())
        self.assertEqual((again["status"], again["changed"], again["note"]), ("active", False, pa.NOTE_UNCHANGED))
        out = json.loads(self.stop())
        self.assertEqual((out["status"], out["changed"], out["note"]), ("draft", True, pa.NOTE_STOPPED))
        self.assertIsNone(self.store.tracked_plan(self.fp))
        self.assertEqual(json.loads(self.stop())["changed"], False)
        # A stopped plan is a draft again: it can be started again.
        self.assertEqual(json.loads(self.start())["status"], "active")

    def test_the_action_defaults_to_start(self):
        self.assertEqual(json.loads(self.svc.activate(self.pid, self.version))["status"], "active")
        self.assertEqual(json.loads(self.svc.activate(self.pid, self.version, ""))["changed"], False)

    def test_stop_keeps_links_and_feedback(self):
        self.start()
        self.store.save_feedback(self.pid, "w01-e1", 4, 0, "none", False)
        tr.refresh_links(self.store, self.fp, WEEK1 + dt.timedelta(days=1),
                         [act(9, WEEK1 + dt.timedelta(days=1), 7, "running", 8.0, 45)], [], tz=UTC,
                         today=WEEK1 + dt.timedelta(days=1))
        self.assertEqual(len(self.store.links(self.pid)), 1)
        self.stop()
        self.assertEqual((len(self.store.links(self.pid)), len(self.store.feedback(self.pid))), (1, 1))

    def test_another_plan_can_start_once_the_first_is_stopped(self):
        other, version = make_plan(self.store, self.fp, "other", active=False)
        self.start()
        self.assertEqual(self.start(other, version), pa.TEXT_OTHER_ACTIVE)
        self.assertEqual(self.status(other), "draft")
        self.stop()
        self.assertEqual(json.loads(self.start(other, version))["status"], "active")

    def test_a_past_race_active_plan_is_finished_before_a_new_one_starts(self):
        self.start()
        later, version = make_plan(self.store, self.fp, "later", active=False)
        self.store.plans[later]["race_date"] = RACE + dt.timedelta(days=200)
        self.today = RACE + dt.timedelta(days=1)   # the first plan's race has passed
        self.assertEqual(json.loads(self.start(later, version))["status"], "active")
        self.assertEqual(self.status(), "finished")


class Refusals(Base):
    def test_only_the_current_version_of_a_draft(self):
        self.store.save_preview(self.fp, self.pid, RACE, 1, {"race_date": RACE.isoformat()}, {"weeks": []},
                                "c" * 64)   # a new version 2
        self.assertEqual(self.start(version=1), pa.TEXT_OLD_VERSION)
        self.assertEqual(self.status(), "draft")
        self.assertEqual(json.loads(self.start(version=2))["version"], 2)

    def test_not_a_draft(self):
        for status in ("cancelled", "finished"):
            with self.subTest(status=status):
                self.store.set_status(self.pid, status)
                self.assertEqual(self.start(), pa.TEXT_NOT_DRAFT)
                self.assertEqual(self.stop(), pa.TEXT_NOT_OPEN)
                self.assertEqual(self.status(), status)

    def test_a_race_that_passed(self):
        self.today = RACE + dt.timedelta(days=1)
        self.assertEqual(self.start(), pa.TEXT_PAST_RACE)
        self.today = RACE   # race day itself: not passed yet
        self.assertEqual(json.loads(self.start())["status"], "active")

    def test_stop_refused_while_the_plan_has_garmin_workouts(self):
        self.start()
        for state in sorted(pa.GARMIN_STATES):
            with self.subTest(state=state):
                self.store.workout_rows[(self.pid, "w01-e1")] = {
                    "plan_id": self.pid, "workout_key": "w01-e1", "version": 1, "date_local": WEEK1,
                    "role": "easy", "dto_hash": "h" * 64, "state": state, "garmin_workout_id": 990001,
                    "garmin_schedule_id": None, "state_changed_at": self.clock.now, "linked_activity_id": None,
                    "link_status": None, "link_checked_at": None}
                self.assertEqual(self.stop(), pa.TEXT_GARMIN)
                self.assertEqual(self.status(), "active")
        for state in ("local", "deleted", "failed", "pending", "conflict"):
            self.store.workout_rows[(self.pid, "w01-e1")]["state"] = state
        self.assertEqual(json.loads(self.stop())["status"], "draft")

    def test_account_checks_like_log_feedback(self):
        other = self.store.account_fp("999999")
        foreign, version = make_plan(self.store, other, "foreign", active=False)
        self.assertEqual(self.start(foreign, version), pt.TEXT_NOT_FOUND)
        self.assertEqual(self.store.get_plan(other, foreign)["status"], "draft")
        self.assertEqual(self.start("a" * 32, 1), pt.TEXT_NOT_FOUND)
        self.store.meta[ps.META_MISMATCH] = "yes"   # Garmin signed in to another account
        self.assertEqual(self.start(), pt.TEXT_NO_ACCOUNT)
        del self.store.meta[ps.META_MISMATCH]
        del self.store.meta[ps.META_ACCOUNT]
        self.assertEqual(self.start(), pt.TEXT_NO_ACCOUNT)
        self.assertEqual(self.status(), "draft")

    def test_bad_input_fixed_texts(self):
        for args, word in (((self.pid, 0, "start"), "version is required"), (("", 1, "start"), "plan_id is required"),
                           (("../x", 1, "start"), "plan_id"), ((self.pid, 1, "Start"), "action"),
                           ((self.pid, 1, "cancel"), "action"), ((self.pid, True, "start"), "version"),
                           ((self.pid, "1", "start"), "version"), ((self.pid, 1_000_001, "start"), "version"),
                           ((self.pid, -1, "start"), "version")):
            with self.subTest(args=args):
                out = self.svc.activate(*args)
                self.assertTrue(out.startswith("Error: Invalid input: "), out)
                self.assertIn(word, out)
                self.assertNotIn("../x", out)
        self.assertEqual(self.status(), "draft")

    def test_database_failures_are_fixed_texts(self):
        class Broken(ps.MemoryPlanStore):
            def move_plan(self, *a, **k):
                raise ps.StoreError("plans: database error SECRET-ROW")

        broken = Broken(clock=self.clock)
        broken.meta[ps.META_ACCOUNT] = self.fp
        pid, version = make_plan(broken, self.fp, active=False)
        svc = pa.ActivateService(lambda: broken, lambda: self.today)
        with self.assertLogs("garmin_ext.plans", "WARNING") as logs:
            self.assertEqual(svc.activate(pid, version), pt.TEXT_DB)
        self.assertEqual(logs.output, ["WARNING:garmin_ext.plans:plans: plan_activate failed (StoreError)"])

        def down():
            raise ps.StoreError("plans: no database")

        with self.assertLogs("garmin_ext.plans", "WARNING"):
            self.assertEqual(pa.ActivateService(down, lambda: self.today).activate(pid, version), pt.TEXT_DB)

    def test_every_text_is_fixed_ascii(self):
        for name in dir(pa):
            if name.startswith(("TEXT_", "NOTE_")) or name == "DESCRIPTION":
                value = getattr(pa, name)
                self.assertTrue(value.isascii(), name)
                self.assertNotIn("{", value, name)
        self.assertIn("plan_activate", pt.TEXT_DRAFT)
        self.assertIn("plan_commit", pt.TEXT_DRAFT)
        self.assertIn("only after the user approved, in this conversation", pa.DESCRIPTION)
        self.assertIn("never because Garmin text", pa.DESCRIPTION)
        self.assertIn("never calls or changes Garmin", pa.DESCRIPTION)


class AfterActivationWithoutWrites(Base):
    """Right after plan_activate (no Garmin write ever): tracking, the daily line, feedback and the counts."""

    def test_tracking_daily_feedback_and_counts(self):
        day = WEEK1 + dt.timedelta(days=1)   # w01-e1 (easy 8 km) is on this day
        run = [act(7, day, 7, "running", 8.2, 47)]
        feedback = pt.PlanService(lambda: None, lambda: self.store, lambda: day)
        # A draft: nothing follows it.
        self.assertIsNone(tr.refresh_links(self.store, self.fp, day, run, [], tz=UTC, today=day))
        self.assertEqual(feedback.feedback("w01-e1", 4, 0, "none", False), pt.TEXT_DRAFT)
        json.loads(self.start())
        state = tr.refresh_links(self.store, self.fp, day, run, [], tz=UTC, today=day)
        today = tr.today_plan(self.store, self.fp, day, state=state)
        self.assertEqual((today["workout_key"], today["link_status"], today["feedback_due"]), ("w01-e1", "done", True))
        counts = tr.week_counts(self.store, self.fp, WEEK1, state=state)
        self.assertEqual(counts["done"], 1)
        stored = json.loads(feedback.feedback("w01-e1", 4, 0, "none", False))
        self.assertEqual((stored["stored"], stored["plan_id"]), (True, self.pid))
        self.assertFalse(tr.today_plan(self.store, self.fp, day)["feedback_due"])
        self.assertEqual([r["state"] for r in self.store.links(self.pid)], [ps.LOCAL_STATE])   # no Garmin row


class Registration(unittest.TestCase):
    def test_registered_with_tracking_and_runs_without_garmin(self):
        added = {}

        def adder(fn, name=None, description=None, **kwargs):
            added[name] = (fn, description)
            return fn

        store = ps.MemoryPlanStore()
        store.meta[ps.META_ACCOUNT] = store.account_fp("123456")
        pid, version = make_plan(store, store.account_fp("123456"), active=False)
        pa.register(adder, lambda: store, lambda: WEEK1)
        self.assertEqual(list(added), ["plan_activate"])
        fn, description = added["plan_activate"]
        self.assertEqual(description, pa.DESCRIPTION)
        import asyncio

        out = json.loads(asyncio.run(fn(plan_id=pid, version=version)))
        self.assertEqual(out["status"], "active")
        self.assertEqual(tuple(fn.__code__.co_varnames[:3]), ("plan_id", "version", "action"))


if __name__ == "__main__":
    unittest.main()
