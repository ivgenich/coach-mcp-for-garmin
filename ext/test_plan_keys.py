"""Session keys across rebuilds, and the commit ops and deletes that rely on them, end to end on one fake Garmin
(the reads of test_plan_tools, the writes of test_plan_writes) and MemoryPlanStore. Standard library only.

A plan is rebuilt with the real today at every plan_preview, and the normal flow previews again every few days to
commit the next ones: the same planned session must keep its key, so that the stored rows (matched by key) still
name the same day. Every test joins the write helper's threads in its cleanup (ci-unittest fails on leaked
threads)."""

from __future__ import annotations

import asyncio
import copy
import datetime as dt
import json
import unittest

import garmin_paths
import plan_engine as pe
import plan_store as ps
import plan_tools as pt
import plan_writes as pw
import test_plan_tools as tpt
import test_plan_writes as tpw

BUILT = tpt.TODAY                       # 2026-11-25, a Wednesday
T0 = dt.datetime(2026, 11, 25, 7, 0, tzinfo=dt.timezone.utc)


class Garmin(tpw.FakeGarmin):
    """Writes and the calendar as test_plan_writes' fake, every other read as test_plan_tools' fake (whose
    day-keyed answers move with `today`, so a later preview reads the same numbers)."""

    garmin_connect_activities = tpt.ACTIVITIES_PATH

    def __init__(self):
        super().__init__()
        self.reads = tpt.FakeClient()
        self.profile = 123456

    def move_to(self, day):
        data = tpt.default_data()
        shift = {}
        for name, value in data.items():
            if ":" in name and name.split(":", 1)[1][:4].isdigit() and name.split(":", 1)[0] != "course":
                kind, when = name.split(":", 1)
                moved = dt.date.fromisoformat(when) + (day - BUILT)
                shift[f"{kind}:{moved.isoformat()}"] = value
            else:
                shift[name] = value
        self.reads.data = shift

    def get_stats(self, day):
        self._read("stats")
        return {"userProfileId": self.profile}

    def connectapi(self, path, params=None):
        return self.reads.connectapi(path, params)

    def get_training_status(self, d):
        return self.reads.get_training_status(d)

    def get_lactate_threshold(self):
        return self.reads.get_lactate_threshold()

    def get_race_predictions(self):
        return self.reads.get_race_predictions()

    def get_max_metrics(self, d):
        return self.reads.get_max_metrics(d)


def run(coro):
    return asyncio.run(coro)


async def _unsupported(ctx, **summary):
    return "unsupported"


class Rebuilds(unittest.TestCase):
    def setUp(self):
        garmin_paths.allow_plan_writes(set(garmin_paths.WRITE_PATHS))
        self.addCleanup(garmin_paths.allow_plan_writes, ())
        self.garmin = Garmin()
        self.addCleanup(self._release)
        self.clock = tpw.Clock(T0)
        self.today = BUILT
        self.store = ps.MemoryPlanStore(clock=self.clock)
        self.store.meta[ps.META_ACCOUNT] = self.store.account_fp("123456")
        self.plans = pt.PlanService(lambda: self.garmin, lambda: self.store, lambda: self.today)
        self.plans.commit_hook = pw.commit_section
        self.writes = pw.WriteService(lambda: self.garmin, lambda: self.store, lambda: self.today,
                                      may_write=lambda: True, confirm=_unsupported,
                                      executor_options={"sleep": lambda s: None, "join_s": 0.3})

    def _release(self):
        self.garmin.blocked.set()
        self.assertTrue(pw.join_write_threads(10))

    def preview(self, days=None):
        args = dict(tpt.ARGS)
        if days is not None:
            args["commit_days"] = days
        if getattr(self, "pid", None):
            args["plan_id"] = self.pid
        out = json.loads(run(self.plans.preview(**args)))
        self.assertTrue(out["invariants"]["ok"], out["invariants"]["failed"])
        self.pid = out["stored"]["plan_id"]
        return out

    def commit(self, out):
        pid = out["stored"]["plan_id"]
        for _ in range(8):   # at most 8 writes per call
            done = json.loads(run(self.writes.commit(plan_id=pid, commit_token=out["commit"]["token"])))
            if done["status"] != "partial":
                break
        self.assertEqual(done["status"], "done", done)
        return done

    def go_to(self, day):
        self.clock.advance(days=(day - self.today).days)
        self.today = day
        self.garmin.move_to(day)

    @staticmethod
    def runs(out):
        return {s["date"]: s["key"] for w in out["weeks"] for s in w["sessions"]
                if s["role"] not in ("race", "b_race")}

    def calendar_days(self):
        return sorted(e["scheduleDate"] for e in self.garmin.calendar)

    def test_a_session_keeps_its_key_when_the_plan_is_rebuilt_on_a_later_day(self):
        first = self.preview()
        self.go_to(BUILT + dt.timedelta(days=1))
        second = self.preview()
        before, after = self.runs(first), self.runs(second)
        common = sorted(set(before) & set(after))
        self.assertTrue(common)
        self.assertEqual({d: before[d] for d in common}, {d: after[d] for d in common})
        self.go_to(dt.date(2026, 12, 1))   # a Tuesday: the rest of this week is week 0 now
        third = self.preview()
        again = self.runs(third)
        keys = [s["key"] for w in third["weeks"] for s in w["sessions"]]
        self.assertEqual(len(keys), len(set(keys)), "two sessions with one key")
        for day in set(before) & set(again):
            same_session = {k: v for k, v in _session(first, day).items() if k != "key"} == {
                k: v for k, v in _session(third, day).items() if k != "key"}
            if same_session:
                self.assertEqual(before[day], again[day], day)

    def test_the_next_day_preview_leaves_todays_run_and_the_committed_days_alone(self):
        first = self.preview(days=7)
        self.commit(first)
        on_garmin = self.calendar_days()
        self.assertIn(BUILT.isoformat() if BUILT.isoformat() in self.runs(first) else on_garmin[0], on_garmin)
        tomorrow = BUILT + dt.timedelta(days=1)
        self.go_to(tomorrow)
        second = self.preview()
        ops = second["commit"].get("ops") or []
        self.assertEqual([o for o in ops if o["op"] == "delete"], [], ops)
        self.assertEqual([o for o in ops if o["date"] == tomorrow.isoformat()], [], ops)
        # Only the day that came into the 7-day window is added.
        last = (tomorrow + dt.timedelta(days=6)).isoformat()
        self.assertEqual(sorted(o["date"] for o in ops), [d for d in sorted(self.runs(second)) if d == last])
        if ops:
            self.commit(second)
        self.assertEqual(self.calendar_days(), sorted(set(on_garmin) | {o["date"] for o in ops}))

    def test_a_week_later_with_past_sessions_linked_every_new_day_is_written(self):
        first = self.preview(days=7)
        self.commit(first)
        pid = first["stored"]["plan_id"]
        today = dt.date(2026, 12, 1)
        for n, row in enumerate(self.store.write_rows(pid)):
            if row["date_local"] < today:   # done: tracking linked each past session to its run
                self.store.put_workout(pid, {**row, "linked_activity_id": 5000 + n})
        self.go_to(today)
        second = self.preview()
        ops = second["commit"]["ops"]
        todays = [o for o in ops if o["date"] == today.isoformat()]
        self.assertEqual(todays, [], "a rebuilt plan never plans today: today's workout stays")
        window = [d for d in sorted(self.runs(second)) if today < dt.date.fromisoformat(d) <= today + dt.timedelta(6)]
        self.assertTrue(window)
        self.assertEqual(sorted(o["date"] for o in ops if o["op"] == "create"), window)
        self.commit(second)
        cal = self.calendar_days()
        for day in window:
            self.assertIn(day, cal)
        self.assertIn(today.isoformat(), cal) if today.isoformat() in self.runs(first) else None

    def test_a_shorter_window_after_a_longer_commit_only_replaces_changed_days(self):
        first = self.preview(days=14)
        self.commit(first)
        cal = self.calendar_days()
        self.go_to(BUILT + dt.timedelta(days=1))
        second = self.preview()   # the default 7 days
        ops = second["commit"].get("ops") or []
        created = {o["key"] for o in ops if o["op"] == "create"}
        for o in ops:
            if o["op"] == "delete":   # only a replaced workout goes, never one whose session is still planned as is
                self.assertIn(o["key"].split("~")[0], created, o)
        if ops:
            self.commit(second)
        self.assertEqual(self.calendar_days(), cal, "every committed day is still on Garmin")


class KeyShape(unittest.TestCase):
    def test_every_golden_plan_keys_each_session_by_its_day(self):
        import glob
        import os

        for path in sorted(glob.glob(os.path.join(os.path.dirname(__file__), "fixtures", "plan", "g*.json"))):
            with open(path) as f:
                plan = json.load(f)["plan"]
            sessions = [s for w in plan["weeks"] for s in w["sessions"]]
            with self.subTest(plan=os.path.basename(path)):
                keys = [s["key"] for s in sessions]
                self.assertEqual(len(keys), len(set(keys)))
                for s in sessions:   # no overrides in the golden plans: every session is on its planned day
                    slot = {"race": "race", "b_race": "b"}.get(s["role"], "run")
                    self.assertEqual(s["key"], pe.session_key(dt.date.fromisoformat(s["date"]), slot))
                    self.assertTrue(ps.key_ok(s["key"], retired=False))

    def test_key_checks(self):
        for good in ("20261206-run", "20270226-race", "20270129-b", "20240229-run"):
            self.assertTrue(ps.key_ok(good, retired=False), good)
        self.assertTrue(ps.key_ok("20261206-run~0123abcd"))
        self.assertTrue(ps.key_ok("20261206-run~0123abcd-2"))
        for bad in ("20261206-run~0123abcd", "20261306-run", "20250229-run", "20261206-long", "w03-long", "",
                    "2026-12-06-run", None, 20261206):
            self.assertFalse(ps.key_ok(bad, retired=False), bad)
        with self.assertRaises(ValueError):
            pe.session_key(dt.date(2026, 12, 6), "long")


class Windows(tpw.Base):
    def commit_all(self, pid, token):
        for _ in range(8):   # at most 8 writes per call
            out = self.commit(pid, token)
            if out["status"] != "partial":
                break
        self.assertEqual(out["status"], "done", out)

    def test_a_shorter_window_deletes_nothing_beyond_it_and_issues_no_token(self):
        days = [tpw.TODAY + dt.timedelta(days=n) for n in range(1, 14)]
        sessions = [tpw.session(pe.session_key(d), d) for d in days]
        pid, commit = self.preview(sessions=sessions, days=14)
        self.commit_all(pid, commit["token"])
        _pid, again = self.preview(sessions=sessions, plan_id=pid, days=7)
        self.assertEqual(again["ops"], [], again)
        self.assertIsNone(again["token"])

    def test_a_changed_day_beyond_the_window_is_replaced_and_a_dropped_one_removed(self):
        days = [tpw.TODAY + dt.timedelta(days=n) for n in range(1, 14)]
        sessions = [tpw.session(pe.session_key(d), d) for d in days]
        pid, commit = self.preview(sessions=sessions, days=14)
        self.commit_all(pid, commit["token"])
        changed = [dict(s) for s in sessions]
        changed[10] = tpw.session(changed[10]["key"], days[10], km=6.0)
        del changed[11]
        _pid, again = self.preview(sessions=changed, plan_id=pid, days=7, content="b")
        got = sorted((o["op"], o["key"].split("~")[0]) for o in again["ops"])
        self.assertEqual(got, sorted([("create", sessions[10]["key"]), ("delete", sessions[10]["key"]),
                                      ("delete", sessions[11]["key"])]))

    def test_todays_workout_stays_when_the_rebuilt_plan_has_no_session_today(self):
        today = tpw.TODAY
        sessions = [tpw.session(pe.session_key(today + dt.timedelta(days=n)), today + dt.timedelta(days=n))
                    for n in (0, 1, 2)]
        pid, commit = self.preview(sessions=sessions)
        self.assertEqual(self.commit(pid, commit["token"])["status"], "done")
        _pid, again = self.preview(sessions=sessions[1:], plan_id=pid, content="b")   # plans from tomorrow on
        self.assertEqual(again["ops"], [])


def _session(out, day):
    return copy.deepcopy(next(s for w in out["weeks"] for s in w["sessions"] if s["date"] == day))


if __name__ == "__main__":
    unittest.main()
