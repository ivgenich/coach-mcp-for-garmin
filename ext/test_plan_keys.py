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
        args = {**tpt.ARGS, **getattr(self, "extra", {})}
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


class WeekGrid(Rebuilds):
    """A rebuild of an existing plan keeps the plan's week grid (week 1 from the day it was first built) and its
    days up to today; a new plan still starts on next Monday with a week 0 before it (rule 1). The history moves
    by whole weeks with the clock (activities too), so every preview starts from the same weekly km, and Garmin
    reports no training load, so the planned-ACWR cap (which reads the recorded days up to today) stays off."""

    def setUp(self):
        super().setUp()
        self.garmin.get_training_status = lambda d: {}

    def go_to(self, day):
        self.clock.advance(days=(day - self.today).days)
        self.today = day
        shift = pe.monday(day) - pe.monday(BUILT)
        self.garmin.move_to(BUILT + shift)
        acts = copy.deepcopy(self.garmin.reads.data["activities"])
        for a in acts:
            when = dt.datetime.fromisoformat(a["startTimeLocal"]) + shift
            a["startTimeLocal"] = when.strftime("%Y-%m-%d %H:%M:%S")
        self.garmin.reads.data["activities"] = acts

    @staticmethod
    def week_of(out, day):
        return next(w for w in out["weeks"] if w["start"] <= day.isoformat() <= w["end"])

    @staticmethod
    def sessions_of(out, first, last):
        return [s for w in out["weeks"] for s in w["sessions"] if first.isoformat() <= s["date"] <= last.isoformat()]

    def test_a_mid_week_rebuild_keeps_the_planned_week_and_writes_nothing(self):
        self.extra = {"long_run_day": "fri", "run_days": "tue,wed,thu,fri,sun", "rest_days": "mon,sat"}
        sunday, wednesday = dt.date(2026, 11, 29), dt.date(2026, 12, 2)
        self.go_to(sunday)
        first = self.preview(days=14)
        self.assertEqual(self.week_of(first, wednesday)["week"], 1)
        self.commit(first)
        self.go_to(wednesday)
        second = self.preview()
        week = self.week_of(second, wednesday)
        self.assertEqual((week["week"], week["kind"]), (1, self.week_of(first, wednesday)["kind"]))
        thu, sun = wednesday + dt.timedelta(days=1), wednesday + dt.timedelta(days=4)
        ops = second["commit"].get("ops") or []
        self.assertEqual([o for o in ops if thu.isoformat() <= o["date"] <= sun.isoformat()], [], ops)
        self.assertEqual(ops, [], "nothing at all changed")
        friday = [s for s in self.sessions_of(second, thu, sun) if s["role"] == "long"]
        self.assertEqual([s["date"] for s in friday], ["2026-12-04"], "the long run on Friday stays")
        # the days up to today are the stored version's, and every later day of the week is as planned
        self.assertEqual(self.sessions_of(second, sunday, sun), self.sessions_of(first, sunday, sun))

    def test_a_plan_built_on_a_monday_rebuilt_the_next_day_writes_nothing(self):
        self.extra = {"b_races": ""}
        monday = dt.date(2027, 2, 1)
        self.go_to(monday)
        first = self.preview(days=7)
        self.assertEqual(self.week_of(first, monday)["week"], 1)
        self.commit(first)
        self.go_to(monday + dt.timedelta(days=1))
        second = self.preview(days=7)
        self.assertEqual(second["commit"].get("ops") or [], [])
        self.assertEqual(self.sessions_of(second, monday, monday + dt.timedelta(days=7)),
                         self.sessions_of(first, monday, monday + dt.timedelta(days=7)))

    def test_the_new_version_keeps_the_past_weeks_and_todays_session(self):
        first = self.preview(days=7)   # a Wednesday: week 0, then week 1 from Monday 30 Nov
        self.commit(first)
        day = dt.date(2026, 12, 3)
        self.go_to(day)
        second = self.preview()
        old = self.sessions_of(first, BUILT, day)
        self.assertTrue(any(s["date"] == day.isoformat() for s in old))
        self.assertEqual(self.sessions_of(second, BUILT, day), old)
        self.assertEqual([w["week"] for w in second["weeks"][:2]], [0, 1])
        self.assertEqual(second["weeks"][0], first["weeks"][0], "week 0 is history, kept whole")

    def test_a_new_plan_built_mid_week_starts_with_week_0(self):
        self.go_to(dt.date(2026, 12, 2))
        out = self.preview()
        self.assertEqual([(w["week"], w["kind"], w["start"]) for w in out["weeks"][:2]],
                         [(0, "week0", "2026-11-30"), (1, out["weeks"][1]["kind"], "2026-12-07")])


class UnscheduleFirst(tpw.Base):
    """Deleting a workout is not known to remove its calendar entries (docs: Garmin facts); the fakes keep them.
    Every delete unschedules the workout's entries first (from the calendar when no entry id is stored), and
    nothing is deleted while the calendar cannot be read."""

    def committed(self, sessions=None):
        pid, commit = self.preview(sessions=sessions)
        self.assertEqual(self.commit(pid, commit["token"])["status"], "done")
        return pid

    def forget_entry_ids(self, pid):
        for key in list(self.rows(pid)):
            self.store.workout_rows[(pid, key)]["garmin_schedule_id"] = None

    def assert_calendar_clean(self):
        alive = set(self.garmin.library)
        self.assertEqual([e for e in self.garmin.calendar if e["workoutId"] not in alive], [],
                         "a calendar entry of a deleted workout")

    def test_cleanup_reads_the_entry_when_no_id_is_stored(self):
        pid = self.committed()
        self.forget_entry_ids(pid)
        counts = pw.cleanup(self.store, self.garmin, tpw.TODAY, dry_run=False, sleep=lambda s: None, join_s=2)
        self.assertEqual((counts["deleted"], counts["failed"]), (3, 0), counts)
        self.assertEqual(self.garmin.calendar, [])
        self.assertEqual(self.garmin.library, {})

    def test_cleanup_deletes_nothing_when_the_calendar_cannot_be_read(self):
        pid = self.committed()
        self.forget_entry_ids(pid)
        self.garmin.read_faults["calendar"] = ConnectionError("down")
        mark = len(self.garmin.writes())
        with self.assertRaises(ConnectionError):
            pw.cleanup(self.store, self.garmin, tpw.TODAY, dry_run=False, sleep=lambda s: None, join_s=2)
        self.assertEqual(len(self.garmin.writes()), mark)
        self.assertEqual({r["state"] for r in self.rows(pid).values()}, {"scheduled"})

    def test_cleanup_takes_rows_a_crash_left_mid_request(self):
        pid = self.committed()
        keys = sorted(self.rows(pid))
        self.store.workout_rows[(pid, keys[0])]["state"] = "uploading"
        self.store.workout_rows[(pid, keys[1])]["state"] = "scheduling"
        self.forget_entry_ids(pid)
        counts = pw.cleanup(self.store, self.garmin, tpw.TODAY, dry_run=False, sleep=lambda s: None, join_s=2)
        self.assertEqual(counts["deleted"], 3, counts)
        self.assertEqual((self.garmin.library, self.garmin.calendar), ({}, []))

    def test_orphans_are_unscheduled_before_they_are_deleted(self):
        pid = self.committed()
        tag = self.store.install_tag()
        later = tpw.TODAY + dt.timedelta(days=4)
        with self.garmin.lock:
            self.garmin.library[701] = {"workoutId": 701, "workoutName": "Coach Easy",
                                        "description": f"coach-mcp {tag} {pid} 20261205-run h12345678"}
            self.garmin.calendar.append({"workoutId": 701, "scheduleDate": later.isoformat(),
                                         "scheduledWorkoutId": 7011, "workoutName": "Coach Easy",
                                         "workoutType": "running"})
        counts = pw.cleanup(self.store, self.garmin, tpw.TODAY, dry_run=False, orphans=True, sleep=lambda s: None,
                            join_s=2)
        self.assertEqual(counts["deleted"], 4, counts)
        writes = self.garmin.writes()
        self.assertLess(writes.index(("DELETE", "workout-service/schedule/7011")),
                        writes.index(("DELETE", "workout-service/workout/701")))
        self.assert_calendar_clean()

    def executor_reading_only_the_first_day(self, pid, first):
        ex = pw.Executor(self.garmin, self.store, self.store.get_plan(self.fp, pid), tpw.TODAY, sleep=lambda s: None,
                         join_s=2)
        ex.prepare([first.isoformat()])   # the calendar around the first day only
        return ex

    def test_a_row_outside_the_read_range_is_unscheduled_from_a_read_around_its_day(self):
        days = [tpw.TODAY + dt.timedelta(days=n) for n in (1, 9)]
        pid = self.committed([tpw.session(pe.session_key(d), d) for d in days])
        far = pe.session_key(days[1])
        self.forget_entry_ids(pid)
        wid = self.rows(pid)[far]["garmin_workout_id"]
        ex = self.executor_reading_only_the_first_day(pid, days[0])
        ex.delete({"op": "delete", "key": far, "workout_id": wid, "reason": "removed"})
        self.assertEqual((ex.remaining, self.rows(pid)[far]["state"]), (0, "deleted"))
        writes = self.garmin.writes()
        self.assertEqual(writes[-2][1].rsplit("/", 2)[-2], "schedule", writes[-2:])
        self.assertEqual(writes[-1], ("DELETE", f"workout-service/workout/{wid}"))
        self.assert_calendar_clean()

    def test_a_delete_waits_when_the_calendar_around_its_day_cannot_be_read(self):
        days = [tpw.TODAY + dt.timedelta(days=n) for n in (1, 9)]
        pid = self.committed([tpw.session(pe.session_key(d), d) for d in days])
        far = pe.session_key(days[1])
        self.forget_entry_ids(pid)
        ex = self.executor_reading_only_the_first_day(pid, days[0])
        self.garmin.read_faults["calendar"] = ConnectionError("down")
        ex.delete({"op": "delete", "key": far, "workout_id": self.rows(pid)[far]["garmin_workout_id"],
                   "reason": "removed"})
        self.assertEqual(ex.remaining, 1)
        self.assertEqual(self.rows(pid)[far]["state"], "scheduled")
        self.assertIn(self.rows(pid)[far]["garmin_workout_id"], self.garmin.library)


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


class OursById(unittest.TestCase):
    """A calendar workout is the plan's own by its Garmin workout id, never by its name: the user's own hard
    session named "Coach ..." counts for spacing and calendar_changed."""

    ENTRY = {"scheduleDate": "2026-12-16", "workoutName": "Coach VO2 intervals", "workoutType": "running",
             "estimatedDurationInSecs": 3600, "isRestDay": False, "workoutId": 4711}

    def test_fixed_sessions_count_a_users_workout_named_coach(self):
        athlete = {"lt_speed_mps": 3.85, "lt_hr": None, "ftp_w": None}
        ours = dict(self.ENTRY, workoutId=4712, scheduleDate="2026-12-18")
        fixed = pt.fixed_sessions([self.ENTRY, ours], athlete, own_ids={4712})
        self.assertEqual([f.date.isoformat() for f in fixed], ["2026-12-16"])

    def test_calendar_changed_sees_a_users_workout_named_coach(self):
        ops = [{"op": "create", "date": "2026-12-17", "hard_or_long": True}]
        payload = {"fixed_hard": [], "lt_speed_mps": None}
        entry = dict(self.ENTRY, workoutName="Coach Long run", estimatedDistanceInMeters=30000)
        self.assertTrue(pw._calendar_changed(ops, [entry], set(), payload))
        self.assertFalse(pw._calendar_changed(ops, [entry], {4711}, payload))


def _session(out, day):
    return copy.deepcopy(next(s for w in out["weeks"] for s in w["sessions"] if s["date"] == day))


if __name__ == "__main__":
    unittest.main()
