"""plan_tracking and log_feedback (PLAN_TRACKING=on, Stage 3) on MemoryPlanStore. Standard library only.

The fixture is a made-up two-week block in the shape of a real one (no personal data): sessions done on their
day, a session done a day late and one a day early, a partial long run, a ride that replaced an easy run, a
day with two candidate runs, a missed long run, a Garmin-linked workout and sessions without any Garmin link.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import unittest

import garmin_paths
import plan_store as ps
import plan_tools as pt
import plan_tracking as tr

UTC = dt.timezone.utc
RACE = dt.date(2027, 2, 26)
WEEK1 = dt.date(2026, 11, 30)
WEEK2 = dt.date(2026, 12, 7)
WEEK3 = dt.date(2026, 12, 14)


def session(key, day, role, km, minutes):
    return {"key": key, "date": day.isoformat(), "role": role, "km": km, "minutes": minutes,
            "structure": f"{km} km {role}"}


def body():
    d = lambda week, n: week + dt.timedelta(days=n)  # noqa: E731
    weeks = [
        (WEEK1, [session("w01-e1", d(WEEK1, 1), "easy", 8.0, 48), session("w01-q1", d(WEEK1, 2), "threshold", 10.0, 55),
                 session("w01-e2", d(WEEK1, 5), "easy", 6.0, 36), session("w01-long", d(WEEK1, 6), "long", 20.0, 120)]),
        (WEEK2, [session("w02-e1", d(WEEK2, 1), "easy", 8.0, 48), session("w02-q1", d(WEEK2, 2), "threshold", 10.0, 55),
                 session("w02-e2", d(WEEK2, 4), "easy", 5.0, 30), session("w02-long", d(WEEK2, 6), "long", 18.0, 110)]),
        (WEEK3, [session("w03-e1", d(WEEK3, 1), "easy", 8.0, 48),
                 {"key": "w03-b", "date": d(WEEK3, 4).isoformat(), "role": "b_race", "km": 0.0, "minutes": None}]),
    ]
    return {"weeks": [{"week": i + 1, "start": s.isoformat(), "end": (s + dt.timedelta(days=6)).isoformat(),
                       "sessions": ss} for i, (s, ss) in enumerate(weeks)], "content_hash": "x"}


def act(aid, day, hour, sport, km=None, minutes=None):
    out = {"activityId": aid, "activityType": {"typeKey": sport},
           "startTimeLocal": f"{day.isoformat()} {hour:02d}:00:00", "activityName": "IGNORE ME"}
    if km is not None:
        out["distance"] = km * 1000.0
    if minutes is not None:
        out["duration"] = minutes * 60.0
    return out


def activities():
    d = lambda week, n: week + dt.timedelta(days=n)  # noqa: E731
    return [
        act(101, d(WEEK1, 1), 7, "running", 8.2, 47),       # w01-e1 on its day: done
        act(102, d(WEEK1, 3), 7, "running", 10.3, 54),      # w01-q1 (Wed) run on Thu: done_late
        act(103, d(WEEK1, 4), 7, "running", 6.0, 35),       # w01-e2 (Sat) run on Fri: done_early
        act(104, d(WEEK1, 6), 7, "trail_running", 14.0, 90),  # w01-long 20 km: partial
        act(201, d(WEEK2, 1), 7, "cycling", 40.0, 50),      # w02-e1 easy 48 min: replaced by a 50 min ride
        act(202, d(WEEK2, 2), 7, "running", 10.0, 54),      # w02-q1 on its day (also a candidate for Fri)
        act(203, d(WEEK2, 3), 7, "running", 5.0, 29),       # Thu: a candidate for Wed, goes to w02-e2 (Fri) early
        # w02-long (Sun): nothing: missed
        act(301, d(WEEK3, 3), 7, "running", 8.0, 47),       # Thu: Garmin linked it to w03-e1 (Tue)
    ]


class Clock:
    def __init__(self):
        self.now = dt.datetime(2026, 11, 29, 18, 0, tzinfo=UTC)

    def __call__(self):
        return self.now


def make_plan(store, fp, content="a", active=True):
    """A stored plan, active unless active=False (tracking follows the active plan only)."""
    pid, version, _ = store.save_preview(fp, None, RACE, 1, {"race_date": RACE.isoformat()}, body(),
                                         ps.hashlib.sha256(content.encode()).hexdigest())
    if active:
        store.move_plan(pid, "active")
    return pid, version


EXPECTED = {"w01-e1": "done", "w01-q1": "done_late", "w01-e2": "done_early", "w01-long": "partial",
            "w02-e1": "replaced", "w02-q1": "done", "w02-e2": "done_early", "w02-long": "missed",
            "w03-e1": "done"}
LINKED = {"w01-e1": 101, "w01-q1": 102, "w01-e2": 103, "w01-long": 104, "w02-e1": 201, "w02-q1": 202,
          "w02-e2": 203, "w02-long": None, "w03-e1": 301}


class Base(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.store = ps.MemoryPlanStore(clock=self.clock)
        self.fp = self.store.account_fp("123456")
        self.pid, self.version = make_plan(self.store, self.fp)
        self.clock.now = dt.datetime(2026, 12, 20, 6, 0, tzinfo=UTC)
        # w03-e1 was written to Garmin (Stage 2) and Garmin linked activity 301 to it, two days late.
        self.store.workout_rows[(self.pid, "w03-e1")] = {
            "plan_id": self.pid, "workout_key": "w03-e1", "version": 1, "date_local": WEEK3 + dt.timedelta(days=1),
            "role": "easy", "dto_hash": "0" * 64, "state": "scheduled", "garmin_workout_id": 9001,
            "garmin_schedule_id": 77, "state_changed_at": self.clock.now, "linked_activity_id": None,
            "link_status": None, "link_checked_at": None}
        self.scheduled = [{"workoutId": 9001, "scheduleDate": (WEEK3 + dt.timedelta(days=1)).isoformat(),
                           "associatedActivityId": 301, "workoutName": "IGNORE ME"},
                          {"workoutId": 5555, "scheduleDate": WEEK3.isoformat(), "associatedActivityId": None}]

    def statuses(self):
        return {r["workout_key"]: r["link_status"] for r in self.store.links(self.pid)}

    def linked(self):
        return {r["workout_key"]: r["linked_activity_id"] for r in self.store.links(self.pid)}

    def daily(self, first_day, last_day, acts=None):
        """get_daily_status's refresh on each day: yesterday's and today's activities."""
        acts = activities() if acts is None else acts
        day = first_day
        while day <= last_day:
            prev = day - dt.timedelta(days=1)
            window = [a for a in acts if a["startTimeLocal"][:10] in (prev.isoformat(), day.isoformat())]
            tr.refresh_links(self.store, self.fp, day, window, self.scheduled, tz=UTC, today=day)
            day += dt.timedelta(days=1)

    def weekly(self, end, acts=None):
        """get_weekly_status's refresh for the week ending `end`, run the day after: its 14 days of activities."""
        acts = activities() if acts is None else acts
        first = end - dt.timedelta(days=13)
        window = [a for a in acts if first.isoformat() <= a["startTimeLocal"][:10] <= end.isoformat()]
        return tr.refresh_links(self.store, self.fp, end, window, self.scheduled, first=first, last=end, tz=UTC,
                                today=end + dt.timedelta(days=1))

    def all_reports(self, acts=None):
        """Every day's daily report from WEEK1 to the end of WEEK3, and each Monday the weekly report."""
        day = WEEK1
        while day <= WEEK3 + dt.timedelta(days=7):
            if day.weekday() == 0 and day > WEEK1:
                self.weekly(day - dt.timedelta(days=1), acts)
            if day <= WEEK3 + dt.timedelta(days=6):
                self.daily(day, day, acts)
            day += dt.timedelta(days=1)


class RealWeek(Base):
    def test_a_whole_range_at_once(self):
        tr.refresh_links(self.store, self.fp, WEEK3 + dt.timedelta(days=6), activities(), self.scheduled,
                         first=WEEK1 - dt.timedelta(days=1), last=WEEK3 + dt.timedelta(days=6), tz=UTC,
                         today=WEEK3 + dt.timedelta(days=7))
        self.assertEqual(self.statuses(), EXPECTED)
        self.assertEqual(self.linked(), LINKED)

    def test_day_by_day_as_the_reports_do_it(self):
        self.all_reports()
        self.assertEqual(self.statuses(), EXPECTED)
        self.assertEqual(self.linked(), LINKED)

    def test_the_daily_report_alone_never_settles_missed_or_replaced(self):
        self.daily(WEEK1, WEEK3 + dt.timedelta(days=6))
        settled = {k: v for k, v in EXPECTED.items() if v not in ("missed", "replaced")}
        self.assertEqual(self.statuses(), settled)

    def test_local_rows_and_garmin_rows(self):
        self.all_reports()
        rows = {r["workout_key"]: r for r in self.store.links(self.pid)}
        self.assertEqual(rows["w01-e1"]["state"], ps.LOCAL_STATE)
        self.assertIsNone(rows["w01-e1"]["garmin_workout_id"])
        self.assertEqual(rows["w03-e1"]["state"], "scheduled")       # a Garmin row keeps its state and date
        self.assertEqual(rows["w03-e1"]["garmin_schedule_id"], 77)
        self.assertNotIn("w03-b", rows)                              # a B race is not tracked
        self.assertEqual(self.store.live_workouts(), 1)              # local rows are not on the calendar

    def test_week_counts(self):
        self.all_reports()
        self.assertEqual(tr.week_counts(self.store, self.fp, WEEK1),
                         {"planned": 4, "done": 3, "partial": 1, "replaced": 0, "missed": 0, "open": 0, "moved": 2})
        self.assertEqual(tr.week_counts(self.store, self.fp, WEEK2),
                         {"planned": 4, "done": 2, "partial": 0, "replaced": 1, "missed": 1, "open": 0, "moved": 1})
        self.assertIsNone(tr.week_counts(self.store, self.fp, dt.date(2027, 6, 7)))

    def test_open_sessions_before_their_late_day(self):
        self.daily(WEEK2, WEEK2 + dt.timedelta(days=1))   # Tuesday: the ride, but Wednesday may still bring a run
        self.assertNotIn("w02-e1", self.statuses())
        counts = tr.week_counts(self.store, self.fp, WEEK2)
        self.assertEqual(counts["open"], 4)
        self.daily(WEEK2 + dt.timedelta(days=2), WEEK2 + dt.timedelta(days=3))
        self.assertNotIn("w02-e1", self.statuses())   # two days of data never cover Monday..Wednesday
        self.weekly(WEEK2 + dt.timedelta(days=3))       # Thursday's weekly report: 14 days, Wednesday is over
        self.assertEqual(self.statuses()["w02-e1"], "replaced")

    def test_a_short_ride_does_not_replace(self):
        acts = [a for a in activities() if a["activityId"] != 201] + [act(299, WEEK2 + dt.timedelta(days=1), 7,
                                                                         "cycling", 20.0, 30)]
        self.daily(WEEK2, WEEK2 + dt.timedelta(days=3), acts)
        self.weekly(WEEK2 + dt.timedelta(days=3), acts)
        self.assertEqual(self.statuses()["w02-e1"], "missed")

    def test_a_later_run_upgrades_missed_and_a_shorter_window_never_undoes_done(self):
        self.daily(WEEK2, WEEK3)
        self.weekly(WEEK3)
        self.assertEqual(self.statuses()["w02-long"], "missed")
        late = [act(250, WEEK2 + dt.timedelta(days=7), 9, "running", 18.0, 110)]   # Monday: a day late
        tr.refresh_links(self.store, self.fp, WEEK3, late, [], first=WEEK2 + dt.timedelta(days=5), last=WEEK3, tz=UTC)
        self.assertEqual(self.statuses()["w02-long"], "done_late")
        tr.refresh_links(self.store, self.fp, WEEK3 + dt.timedelta(days=1), [], [], tz=UTC)
        self.assertEqual(self.statuses()["w02-long"], "done_late")

    def test_an_activity_before_the_plan_never_counts(self):
        self.store.plans[self.pid]["activated_at"] = dt.datetime(2026, 12, 1, 12, 0, tzinfo=UTC)
        self.daily(WEEK1, WEEK1 + dt.timedelta(days=2))
        self.weekly(WEEK1 + dt.timedelta(days=2))
        self.assertEqual(self.statuses().get("w01-e1"), "missed")    # its run was at 07:00, before the plan

    def test_activation_wins_over_creation(self):
        row = self.store.plans[self.pid]
        row.update(created_at=dt.datetime(2026, 11, 1, 8, 0, tzinfo=UTC), activated_at=dt.datetime(2026, 12, 3, 8, 0, tzinfo=UTC))
        self.daily(WEEK1, WEEK1 + dt.timedelta(days=4))
        self.weekly(WEEK1 + dt.timedelta(days=4))
        self.assertEqual(self.statuses()["w01-q1"], "missed")
        self.assertEqual(self.statuses()["w01-e2"], "done_early")



class ReviewCases(unittest.TestCase):
    """The Stage 3 review's cases, each on its own small plan (UTC, so local time = UTC)."""

    MON = dt.date(2026, 11, 23)

    def setUp(self):
        self.clock = Clock()
        self.clock.now = dt.datetime(2026, 11, 23, 8, 0, tzinfo=UTC)   # the plan, activated: Monday 08:00
        self.store = ps.MemoryPlanStore(clock=self.clock)
        self.fp = self.store.account_fp("123456")

    def plan(self, sessions, active=True):
        body = {"weeks": [{"week": 1, "start": self.MON.isoformat(),
                           "end": (self.MON + dt.timedelta(days=6)).isoformat(), "sessions": sessions}],
                "content_hash": "r"}
        self.pid, _, _ = self.store.save_preview(self.fp, None, RACE, 1, {}, body, "b" * 64)
        if active:
            self.store.move_plan(self.pid, "active")

    def day(self, n):
        return self.MON + dt.timedelta(days=n)

    def daily(self, n, acts):
        day = self.day(n)
        window = [a for a in acts if a["startTimeLocal"][:10] in ((day - dt.timedelta(days=1)).isoformat(),
                                                                     day.isoformat())]
        return tr.refresh_links(self.store, self.fp, day, window, [], tz=UTC, today=day)

    def statuses(self):
        return {r["workout_key"]: (r["link_status"], r["linked_activity_id"]) for r in self.store.links(self.pid)}

    def test_a_draft_is_not_tracked_until_it_is_activated(self):
        self.plan([session("w01-e1", self.day(0), "easy", 8.0, 45),
                   session("w01-e2", self.day(1), "easy", 6.0, 36)], active=False)
        acts = [act(5, self.day(0), 10, "running", 8.0, 45)]               # Monday 10:00, after the draft
        self.assertIsNone(self.daily(0, acts))                             # a draft: no plan state, no links
        self.assertIsNone(tr.today_plan(self.store, self.fp, self.day(0)))
        self.assertIsNone(tr.week_counts(self.store, self.fp, self.MON))
        self.assertEqual(self.statuses(), {})
        self.clock.now = dt.datetime(2026, 11, 24, 7, 0, tzinfo=UTC)       # activated: Tuesday 07:00
        self.store.move_plan(self.pid, "active")
        acts.append(act(6, self.day(1), 8, "running", 6.0, 36))            # Tuesday 08:00, after the activation
        self.daily(1, acts)
        # The run from before the activation counts for nothing; the one after it does.
        self.assertEqual(self.statuses(), {"w01-e2": ("done", 6)})
        self.assertEqual(tr.today_plan(self.store, self.fp, self.day(1))["link_status"], "done")

    def test_a_run_the_day_before_is_not_missed_by_a_later_daily(self):
        self.plan([session("w01-e1", self.day(2), "easy", 8.0, 45)])      # Wednesday
        acts = [act(7, self.day(1), 18, "running", 8.0, 45)]               # Tuesday evening
        self.daily(3, acts)                                                # the first daily report: Thursday
        self.assertEqual(self.statuses(), {})
        first, last = self.day(3) - dt.timedelta(days=13), self.day(3)
        tr.refresh_links(self.store, self.fp, last, acts, [], first=first, last=last, tz=UTC, today=self.day(4))
        self.assertEqual(self.statuses(), {"w01-e1": ("done_early", 7)})

    def test_missed_waits_until_the_late_day_is_over(self):
        self.plan([session("w01-e1", self.day(2), "easy", 8.0, 45)])
        first = self.day(0) - dt.timedelta(days=13)
        tr.refresh_links(self.store, self.fp, self.day(3), [], [], first=first, last=self.day(3), tz=UTC,
                         today=self.day(3))
        self.assertEqual(self.statuses(), {})                               # Thursday is not over yet
        tr.refresh_links(self.store, self.fp, self.day(3), [], [], first=first, last=self.day(3), tz=UTC,
                         today=self.day(4))
        self.assertEqual(self.statuses(), {"w01-e1": ("missed", None)})

    def test_a_warm_up_jog_does_not_make_the_long_run_partial(self):
        self.plan([session("w01-long", self.day(6), "long", 16.0, 90)])
        acts = [act(1, self.day(6), 7, "running", 1.5, 10), act(2, self.day(6), 8, "running", 16.0, 90)]
        self.daily(6, acts)
        self.assertEqual(self.statuses(), {"w01-long": ("done", 2)})

    def test_two_sessions_one_run_goes_where_it_fits(self):
        self.plan([session("w01-e1", self.day(1), "easy", 5.0, 30),
                   session("w01-q1", self.day(1), "threshold", 10.0, 55)])
        self.daily(1, [act(3, self.day(1), 7, "running", 10.0, 55)])
        self.assertEqual(self.statuses(), {"w01-q1": ("done", 3)})        # the quality session first
        self.setUp()
        self.plan([session("w01-e1", self.day(1), "easy", 5.0, 30),
                   session("w01-q1", self.day(1), "threshold", 10.0, 55)])
        self.daily(1, [act(4, self.day(1), 7, "running", 5.0, 30)])
        self.assertEqual(self.statuses(), {"w01-e1": ("done", 4)})        # too short for it: the easy one

    def test_a_retired_row_is_not_ours(self):
        self.plan([session("w01-e1", self.day(1), "easy", 5.0, 30)])
        for key, wid, state in (("w01-e1", 11, "scheduled"), ("w01-e1~abcdef01", 10, "deleted")):
            self.store.put_workout(self.pid, {"workout_key": key, "version": 1, "date_local": self.day(1),
                                              "role": "easy", "dto_hash": "d" * 64, "state": state,
                                              "garmin_workout_id": wid})
        self.assertEqual(tr.ours_by_garmin_id(self.store, self.fp, [10, 11]),
                         {11: {"workout_key": "w01-e1", "role": "easy"}})

    def test_one_read_of_the_plan_per_report(self):
        self.plan([session("w01-e1", self.day(0), "easy", 8.0, 45)])
        calls = []
        store = self.store

        class Counting:
            def __getattr__(self, name):
                attr = getattr(store, name)
                if callable(attr):
                    def wrapped(*args, **kw):
                        calls.append(name)
                        return attr(*args, **kw)
                    return wrapped
                return attr

        counting = Counting()
        state = tr.refresh_links(counting, self.fp, self.day(0), [act(5, self.day(0), 10, "running", 8.0, 45)],
                                 [], tz=UTC, today=self.day(0))
        today = tr.today_plan(counting, self.fp, self.day(0), state=state)
        ours = tr.ours_by_garmin_id(counting, self.fp, [1], state=state)
        counts = tr.week_counts(counting, self.fp, self.MON, state=state)
        self.assertEqual(calls, ["tracked_plan", "links", "save_links", "feedback"])
        self.assertEqual((today["link_status"], today["feedback_due"], ours, counts["done"]), ("done", True, {}, 1))
        self.assertIsNone(tr.today_plan(counting, self.fp, self.day(0), state=None))   # None: no plan
        self.assertEqual(calls[4:], [])


class Readers(Base):
    def test_today_plan(self):
        self.daily(WEEK1, WEEK1 + dt.timedelta(days=1))
        tue = tr.today_plan(self.store, self.fp, WEEK1 + dt.timedelta(days=1))
        self.assertEqual(tue, {"workout_key": "w01-e1", "date": "2026-12-01", "role": "easy", "sport": "run",
                               "planned_km": 8.0, "planned_min": 48, "link_status": "done", "feedback_due": True,
                               "feedback_for": "w01-e1"})
        self.store.save_feedback(self.pid, "w01-e1", 3, 0, "none", False)
        wed = tr.today_plan(self.store, self.fp, WEEK1 + dt.timedelta(days=2))
        self.assertEqual((wed["workout_key"], wed["link_status"], wed["feedback_due"]), ("w01-q1", None, False))
        rest = tr.today_plan(self.store, self.fp, WEEK1)
        self.assertEqual((rest["role"], rest["workout_key"], rest["sport"]), ("rest", None, None))
        self.assertIsNone(tr.today_plan(self.store, self.fp, dt.date(2026, 11, 1)))
        self.assertIsNone(tr.today_plan(self.store, self.store.account_fp("999"), WEEK1))
        self.assertIsNone(tr.today_plan(None, self.fp, WEEK1))

    def test_ours_by_garmin_id(self):
        self.assertEqual(tr.ours_by_garmin_id(self.store, self.fp, [9001, 5555, "x", True]),
                         {9001: {"workout_key": "w03-e1", "role": "easy"}})
        self.assertEqual(tr.ours_by_garmin_id(self.store, self.fp, []), {})
        self.assertEqual(tr.ours_by_garmin_id(self.store, self.store.account_fp("999"), [9001]), {})

    def test_stored_links_for_plan_get(self):
        self.daily(WEEK1, WEEK1 + dt.timedelta(days=1))
        self.store.save_feedback(self.pid, "w01-e1", 4, 2, "calf", False)
        view = tr.stored_links(self.store, self.pid, UTC)
        self.assertEqual(view["links"], [{"key": "w01-e1", "date": "2026-12-01", "status": "done",
                                          "as_of": "2026-12-20T06:00"}])
        self.assertEqual(view["feedback"], [{"key": "w01-e1", "rpe": 4, "pain": 2, "pain_location": "calf",
                                             "sharp": False}])
        pid2, _ = make_plan(self.store, self.fp, "b", active=False)
        self.assertEqual(tr.stored_links(self.store, pid2), {})

    def test_failures_never_raise_and_log_only_the_type(self):
        class Broken:
            def tracked_plan(self, fp):
                raise ps.StoreError("plans: database error")

            def account_fp(self, pid):
                raise ps.StoreError("plans: database error")

        with self.assertLogs("garmin_ext.plans", "WARNING") as logs:
            self.assertIsNone(tr.refresh_links(Broken(), self.fp, WEEK1, activities(), None))
            self.assertIsNone(tr.today_plan(Broken(), self.fp, WEEK1))
            self.assertEqual(tr.ours_by_garmin_id(Broken(), self.fp, [1]), {})
            self.assertIsNone(tr.week_counts(Broken(), self.fp, WEEK1))
            self.assertIsNone(tr.account_fp(Broken(), "123456"))
        self.assertTrue(all(line.endswith("(StoreError)") for line in logs.output), logs.output)
        self.assertFalse(any("123456" in line or "IGNORE" in line for line in logs.output))
        tr.refresh_links(None, self.fp, WEEK1, [], [])
        tr.refresh_links(self.store, None, WEEK1, [], [])
        self.assertIsNone(tr.account_fp(self.store, None))
        self.assertEqual(tr.account_fp(self.store, 123456), self.fp)

    def test_configured_store(self):
        self.addCleanup(tr.configure, None)
        self.assertIsNone(tr.configured_store())
        tr.configure(lambda: self.store)
        self.assertIs(tr.configured_store(), self.store)

        def broken():
            raise ps.StoreError("plans: cannot connect to the database")
        tr.configure(broken)
        with self.assertLogs("garmin_ext.plans", "WARNING"):
            self.assertIsNone(tr.configured_store())
        tr.configure(None)
        self.assertIsNone(tr.configured_store())

    def test_parsers_skip_junk(self):
        self.assertEqual(tr.plan_sessions(None), [])
        self.assertEqual(tr.plan_sessions({"weeks": [{"sessions": [{"key": "bad", "date": "2026-12-01",
                                                                     "role": "easy"},
                                                                    {"key": "w01-x", "date": "nope"},
                                                                    {"key": "w01-y", "date": "2026-12-01",
                                                                     "role": 5}, "junk"]}, "junk"]}), [])
        acts = tr.normalize_activities([{"activityId": "12"}, {"activityId": True}, "junk",
                                        act("77", WEEK1, 6, "running", 5, 30)], UTC)
        self.assertEqual([a["id"] for a in acts], [77])
        self.assertEqual(tr.garmin_links([{"workoutId": "5", "associatedActivityId": "6"}, {"workoutId": 1}, 3]),
                         {5: 6})


def run(coro):
    return asyncio.run(coro)


class Feedback(unittest.TestCase):
    def setUp(self):
        self.store = ps.MemoryPlanStore()
        self.fp = self.store.account_fp("123456")
        self.store.meta[ps.META_ACCOUNT] = self.fp   # the account the history index recorded
        self.pid, _ = make_plan(self.store, self.fp)
        self.today = WEEK1 + dt.timedelta(days=1)
        self.svc = pt.PlanService(lambda: None, lambda: self.store, lambda: self.today)

    def log(self, **kw):
        args = {"workout_key": "w01-e1", "rpe": 4, "pain": 0, "pain_location": "none", "sharp": False, **kw}
        return self.svc.feedback(**args)

    def test_a_draft_is_refused(self):
        # Feedback goes to the active plan only: a draft, as the default or by its plan_id, gets a fixed text
        # and nothing is stored.
        self.store.move_plan(self.pid, "cancelled")
        draft, _ = make_plan(self.store, self.fp, "draft", active=False)
        self.assertEqual(self.log(), pt.TEXT_DRAFT)
        self.assertEqual(self.log(plan_id=draft), pt.TEXT_DRAFT)
        self.assertEqual(self.store.feedback(draft), [])
        # An active plan wins over a newer draft as the default.
        self.store.move_plan(draft, "active")
        newer, _ = make_plan(self.store, self.fp, "newer", active=False)
        self.assertEqual(json.loads(self.log())["plan_id"], draft)
        self.assertEqual(self.log(plan_id=newer), pt.TEXT_DRAFT)

    def test_stored_then_replaced(self):
        out = json.loads(self.log())
        self.assertEqual({k: out[k] for k in ("stored", "updated", "workout_key", "date", "role", "rpe", "pain",
                                              "pain_location", "sharp", "progression_hold")},
                         {"stored": True, "updated": False, "workout_key": "w01-e1", "date": "2026-12-01",
                          "role": "easy", "rpe": 4, "pain": 0, "pain_location": "none", "sharp": False,
                          "progression_hold": False})
        out = json.loads(self.log(rpe=6, pain=2, pain_location="Calf"))
        self.assertTrue(out["updated"])
        self.assertEqual(self.store.feedback(self.pid)[0]["pain_location"], "calf")
        self.assertEqual(len(self.store.feedback(self.pid)), 1)

    def test_progression_hold(self):
        self.assertTrue(json.loads(self.log(pain=4, pain_location="knee"))["progression_hold"])
        out = json.loads(self.log(pain=1, pain_location="shin", sharp=True))
        self.assertTrue(out["progression_hold"])
        self.assertIn("physiotherapist", out["note"])
        self.assertFalse(json.loads(self.log(pain=3, pain_location="hip"))["progression_hold"])

    def test_bad_input_has_fixed_texts(self):
        cases = [dict(workout_key="../x"), dict(rpe=0), dict(rpe=11), dict(rpe=True), dict(rpe="5"),
                 dict(pain=-1), dict(pain=11), dict(pain_location="left knee, it hurts"),
                 dict(pain=0, pain_location="knee"), dict(pain=0, sharp=True), dict(pain=3, pain_location="none"),
                 dict(sharp="yes"), dict(workout_key="w09-zz"), dict(workout_key="w02-long"),
                 dict(plan_id="nothex")]
        for kw in cases:
            with self.subTest(kw=kw):
                text = self.log(**kw)
                self.assertTrue(text.startswith("Error: Invalid input: "), text)
                self.assertNotIn("it hurts", text)
        self.assertEqual(self.store.feedback(self.pid), [])

    def test_tomorrow_is_allowed(self):
        self.assertTrue(json.loads(self.log(workout_key="w01-q1"))["stored"])

    def test_no_plan_and_database_texts(self):
        fresh = ps.MemoryPlanStore()
        empty = pt.PlanService(lambda: None, lambda: fresh, lambda: self.today)
        self.assertEqual(empty.feedback("w01-e1", 3, 0, "none", False), pt.TEXT_NO_ACCOUNT)
        fresh.meta[ps.META_ACCOUNT] = fresh.account_fp("123456")
        self.assertEqual(empty.feedback("w01-e1", 3, 0, "none", False), pt.TEXT_NO_PLAN)
        # Another account's plan is never touched, not even by its plan_id.
        other = self.store.account_fp("999")
        foreign, _ = make_plan(self.store, other, "foreign")
        self.assertEqual(self.log(plan_id=foreign), pt.TEXT_NOT_FOUND)
        self.store.meta[ps.META_MISMATCH] = "yes"
        self.assertEqual(self.log(), pt.TEXT_NO_ACCOUNT)
        del self.store.meta[ps.META_MISMATCH]
        self.assertEqual(self.log(plan_id="a" * 32), pt.TEXT_NOT_FOUND)
        self.store.set_status(self.pid, "cancelled")
        self.assertEqual(self.log(plan_id=self.pid), pt.TEXT_NOT_FOUND)

        class Down:
            def ready(self):
                return False

            def history_account(self):
                raise ps.StoreError("plans: database error")

        down = pt.PlanService(lambda: None, lambda: Down(), lambda: self.today)
        with self.assertLogs("garmin_ext.plans", "WARNING") as logs:
            self.assertEqual(down.feedback("w01-e1", 3, 0, "none", False), pt.TEXT_STARTING)
        self.assertEqual(logs.output, ["WARNING:garmin_ext.plans:plans: log_feedback failed (StoreError)"])

    def test_plan_get_shows_links_and_feedback(self):
        self.log(pain=2, pain_location="calf")
        tr.refresh_links(self.store, self.fp, self.today, activities()[:1], [], tz=UTC)
        got = json.loads(self.svc.get(""))
        self.assertEqual(got["tracking"]["links"][0]["status"], "done")
        self.assertEqual(got["tracking"]["feedback"][0]["pain_location"], "calf")
        self.assertIn("as of", got["note"])

    def test_registration_with_and_without_the_switch(self):
        added = {}

        def adder(fn, name=None, description=None, **kwargs):
            added[name] = (fn, description)
            return fn

        class Cfg:
            plan_tracking = True
        self.addCleanup(tr.configure, None)
        pt.register(adder, Cfg(), lambda: None, store=self.store, today=lambda: self.today)
        self.assertEqual(sorted(added), sorted(pt.TOOL_NAMES + pt.TRACKING_TOOL_NAMES + pt.REVIEW_TOOL_NAMES))
        self.assertIs(tr.configured_store(), self.store)
        text = added["log_feedback"][1]
        self.assertTrue(text.isascii() and len(text) < 2048)
        self.assertIn("never Garmin", text)
        out = json.loads(run(added["log_feedback"][0](workout_key="w01-e1", rpe=5)))
        self.assertTrue(out["stored"])
        added.clear()
        pt.register(adder, None, lambda: None, store=self.store, today=lambda: self.today)
        self.assertEqual(sorted(added), sorted(pt.TOOL_NAMES))
        self.assertIsNone(tr.configured_store())


class ArgumentChecks(unittest.TestCase):
    def test_log_feedback_row(self):
        ok = {"workout_key": "w03-long", "rpe": 5, "pain": 0, "pain_location": "none", "sharp": False,
              "plan_id": ""}
        self.assertIsNone(garmin_paths.refused_argument("log_feedback", ok))
        for name, bad in (("workout_key", "x/y"), ("rpe", 11), ("rpe", True), ("pain", "3"),
                          ("pain_location", "everywhere"), ("sharp", 1), ("plan_id", "zz")):
            with self.subTest(name=name):
                self.assertEqual(garmin_paths.refused_argument("log_feedback", {**ok, name: bad})[0], name)

    def test_pain_locations_agree(self):
        self.assertEqual(garmin_paths.PAIN_LOCATIONS - {""}, set(ps.PAIN_LOCATIONS))


if __name__ == "__main__":
    unittest.main()
