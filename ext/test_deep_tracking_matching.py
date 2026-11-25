"""Deep tests of plan tracking's matching and settling (plan_tracking on MemoryPlanStore). Standard library only.

Every case runs the real refresh_links on a small made-up plan (no personal data): which run completes which
session, partial and replaced shares at their bounds, activity shapes (GMT only, no distance, manual, other
types), Garmin's own link, when a session is settled, what a later refresh may and may not change, and the
dates around time zones, DST weekends and the year boundary.

A test that exposes a defect against docs/training-plan.md ("Tracking") is kept as an expected failure with
a `# BUG:` line.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import unittest
from zoneinfo import ZoneInfo

import plan_store as ps
import plan_tracking as tr
import status_common as sc

UTC = dt.timezone.utc
RACE = dt.date(2027, 4, 4)
MON = dt.date(2026, 11, 30)          # a Monday
FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "deep_tracking")


def day(n):
    return MON + dt.timedelta(days=n)


def session(key, when, role="easy", km=8.0, minutes=48):
    return {"key": key, "date": when.isoformat(), "role": role, "km": km, "minutes": minutes,
            "structure": f"{km} km {role}"}


def plan_body(sessions):
    """Weeks Monday..Sunday around the sessions (every week between the first and the last)."""
    days = [dt.date.fromisoformat(s["date"]) for s in sessions]
    first = min(days) - dt.timedelta(days=min(days).weekday())
    last = max(days)
    weeks, start, n = [], first, 1
    while start <= last:
        end = start + dt.timedelta(days=6)
        weeks.append({"week": n, "start": start.isoformat(), "end": end.isoformat(),
                      "sessions": [s for s in sessions if start.isoformat() <= s["date"] <= end.isoformat()]})
        start, n = start + dt.timedelta(days=7), n + 1
    return {"weeks": weeks, "content_hash": "d"}


def run(aid, when, hour=7, type_key="running", km=8.0, minutes=48.0, minute=0, **extra):
    """A Garmin activity-list item (startTimeLocal), distance in m and duration in s as Garmin gives them."""
    out = {"activityId": aid, "activityType": {"typeKey": type_key}, "activityName": "IGNORE ME",
           "startTimeLocal": f"{when.isoformat()} {hour:02d}:{minute:02d}:00"}
    if km is not None:
        out["distance"] = km * 1000.0
    if minutes is not None:
        out["duration"] = minutes * 60.0
    out.update(extra)
    return out


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


class Track:
    """One small tracked plan on a MemoryPlanStore."""

    def __init__(self, sessions, created=dt.datetime(2026, 11, 1, 8, 0, tzinfo=UTC), tz=UTC):
        self.clock = Clock(created)
        self.store = ps.MemoryPlanStore(clock=self.clock)
        self.fp = self.store.account_fp("424242")
        self.tz = tz
        digest = hashlib.sha256(json.dumps(sessions, sort_keys=True).encode()).hexdigest()
        self.pid, _, _ = self.store.save_preview(self.fp, None, RACE, 1, {}, plan_body(sessions), digest)

    def refresh(self, acts, first, last, today, scheduled=()):
        return tr.refresh_links(self.store, self.fp, last, list(acts), list(scheduled), first=first, last=last,
                                tz=self.tz, today=today)

    def weekly(self, acts, end, today=None, scheduled=()):
        """get_weekly_status's refresh: 14 days of activities ending `end`, run on `today` (default end + 1)."""
        first = end - dt.timedelta(days=13)
        window = [a for a in acts if first <= sc.activity_times(a, self.tz)[0] <= end]
        return self.refresh(window, first, end, today or end + dt.timedelta(days=1), scheduled)

    def daily(self, acts, when, scheduled=()):
        """get_daily_status's refresh on `when`: yesterday's and today's activities."""
        prev = when - dt.timedelta(days=1)
        window = [a for a in acts if sc.activity_times(a, self.tz)[0] in (prev, when)]
        return tr.refresh_links(self.store, self.fp, when, window, list(scheduled), tz=self.tz, today=when)

    def links(self):
        return {r["workout_key"]: (r["link_status"], r["linked_activity_id"]) for r in self.store.links(self.pid)}

    def status(self, key):
        return self.links().get(key, (None, None))[0]

    def put_garmin(self, key, when, workout_id, role="easy"):
        self.store.put_workout(self.pid, {"workout_key": key, "version": 1, "date_local": when, "role": role,
                                          "dto_hash": "0" * 64, "state": "scheduled",
                                          "garmin_workout_id": workout_id})


def full(track, acts, scheduled=()):
    """One refresh over the whole fortnight around MON, long after it (every window settled)."""
    return track.refresh(acts, day(-7), day(13), day(20), scheduled)


# --- which run completes which session --------------------------------------------------------------------------

class DayWindow(unittest.TestCase):
    def test_same_day_early_late_and_two_days_off(self):
        cases = [(0, ("done", 1)), (-1, ("done_early", 1)), (1, ("done_late", 1)),
                 (-2, ("missed", None)), (2, ("missed", None))]
        for shift, expected in cases:
            with self.subTest(shift=shift):
                t = Track([session("w01-e1", day(3))])
                full(t, [run(1, day(3 + shift))])
                self.assertEqual(t.links(), {"w01-e1": expected})

    def test_a_run_two_days_off_still_counts_for_nothing_else(self):
        t = Track([session("w01-e1", day(3))])
        full(t, [run(1, day(5)), run(2, day(1))])
        self.assertEqual(t.links(), {"w01-e1": ("missed", None)})

    def test_same_day_beats_a_moved_session(self):
        # Tue and Wed sessions, one run on Wed: the Wed session takes it, Tue is missed.
        t = Track([session("w01-e1", day(1)), session("w01-e2", day(2))])
        full(t, [run(1, day(2))])
        self.assertEqual(t.links(), {"w01-e1": ("missed", None), "w01-e2": ("done", 1)})

    def test_most_sessions_completed(self):
        # status_common's example: planned Tue + Wed, runs Mon + Tue -> Mon->Tue and Tue->Wed.
        t = Track([session("w01-e1", day(1)), session("w01-e2", day(2))])
        full(t, [run(1, day(0)), run(2, day(1))])
        self.assertEqual(t.links(), {"w01-e1": ("done_early", 1), "w01-e2": ("done_early", 2)})

    def test_a_moved_session_never_takes_the_run_of_a_session_done_on_its_day(self):
        t = Track([session("w01-a", day(2), km=10.0, minutes=None), session("w01-b", day(3), km=10.0, minutes=None),
                   session("w01-c", day(3), km=5.0, minutes=None), session("w01-d", day(4), km=5.0, minutes=None)])
        full(t, [run(1, day(3), km=5.0, minutes=None), run(2, day(4), km=10.0, minutes=None),
                 run(3, day(5), km=10.0, minutes=None)])
        # The 5 km run on Thursday is w01-c's own (a full run on its day); today it goes to Wednesday's w01-a
        # (partial_late) and w01-c is missed.
        self.assertEqual(t.links()["w01-c"], ("done", 1))

    def test_an_activity_completes_one_session_only(self):
        t = Track([session("w01-e1", day(1)), session("w01-e2", day(1)), session("w01-e3", day(2))])
        full(t, [run(1, day(1))])
        linked = [v[1] for v in t.links().values() if v[1] is not None]
        self.assertEqual(linked, [1])

    def test_an_activity_listed_twice_completes_one_session_only(self):
        t = Track([session("w01-e1", day(1)), session("w01-e2", day(2))])
        full(t, [run(5, day(1)), run(5, day(1))])
        linked = [v[1] for v in t.links().values() if v[1] is not None]
        self.assertEqual(linked, [5])


class Shares(unittest.TestCase):
    def check(self, planned_km, planned_min, km, minutes, expected):
        t = Track([session("w01-e1", day(2), km=planned_km, minutes=planned_min)])
        full(t, [run(1, day(2), km=km, minutes=minutes)])
        self.assertEqual(t.status("w01-e1"), expected)

    def test_distance_at_exactly_80_percent_is_done(self):
        self.check(10.0, None, 8.0, None, "done")      # 8000 m of 10 km: exactly 80%
        self.check(10.0, None, 7.999, None, "partial")
        self.check(20.0, None, 16.0, None, "done")

    def test_duration_at_exactly_80_percent_is_done(self):
        self.check(None, 50, None, 40.0, "done")       # 2400 s of 50 min: exactly 80%
        self.check(None, 50, None, 39.99, "partial")
        self.check(None, 90, None, 72.0, "done")

    def test_either_share_short_is_partial(self):
        self.check(10.0, 60, 10.0, 40.0, "partial")    # the distance, but two thirds of the time
        self.check(10.0, 60, 7.0, 60.0, "partial")     # the time, but 70% of the distance
        self.check(10.0, 60, 12.0, 70.0, "done")       # more than planned is done

    def test_partial_keeps_the_early_and_late_labels(self):
        for shift, expected in ((-1, "partial_early"), (1, "partial_late")):
            with self.subTest(shift=shift):
                t = Track([session("w01-l", day(6), role="long", km=20.0, minutes=120)])
                full(t, [run(1, day(6 + shift), km=12.0, minutes=75.0)])
                self.assertEqual(t.status("w01-l"), expected)

    def test_distance_at_exactly_80_percent_of_6_km_is_done(self):
        self.check(6.0, None, 4.8, None, "done")

    def test_duration_at_exactly_80_percent_of_48_min_is_done(self):
        self.check(None, 48, None, 38.4, "done")

    def test_planned_values_missing_or_zero_are_not_checked(self):
        self.check(None, None, 1.0, 5.0, "done")
        self.check(0.0, 0, 1.0, 5.0, "done")


class Replaced(unittest.TestCase):
    def check(self, others, minutes=50, shift=0, expected="replaced"):
        t = Track([session("w01-e1", day(2), minutes=minutes)])
        full(t, [run(10 + i, day(2 + shift), type_key=k, km=None, minutes=m) for i, (k, m) in enumerate(others)])
        self.assertEqual(t.status("w01-e1"), expected)
        return t

    def test_bike_or_swim_at_80_percent_of_the_time(self):
        self.check([("cycling", 40.0)])                         # 2400 s of 50 min: exactly 80%
        self.check([("lap_swimming", 45.0)])
        self.check([("cycling", 39.9)], expected="missed")
        self.check([("virtual_ride", 120.0)])

    def test_other_sports_add_up_and_the_longest_is_linked(self):
        t = self.check([("cycling", 25.0), ("lap_swimming", 20.0)])
        self.assertEqual(t.links()["w01-e1"], ("replaced", 10))
        self.check([("cycling", 20.0), ("lap_swimming", 19.0)], expected="missed")

    def test_only_on_the_session_day(self):
        self.check([("cycling", 90.0)], shift=-1, expected="missed")
        self.check([("cycling", 90.0)], shift=1, expected="missed")

    def test_a_session_without_planned_time_is_missed(self):
        self.check([("cycling", 90.0)], minutes=None, expected="missed")

    def test_a_ride_with_no_duration_counts_nothing(self):
        t = Track([session("w01-e1", day(2), minutes=50)])
        full(t, [run(10, day(2), type_key="cycling", km=30.0, minutes=None)])
        self.assertEqual(t.status("w01-e1"), "missed")

    def test_a_ride_replaces_one_session_only(self):
        t = Track([session("w01-e1", day(2), minutes=50), session("w01-e2", day(2), minutes=40)])
        full(t, [run(10, day(2), type_key="cycling", km=None, minutes=60.0)])
        self.assertEqual(sorted(v for v in t.links().values()), [("missed", None), ("replaced", 10)])

    def test_other_sports_at_exactly_80_percent_of_48_min(self):
        self.check([("cycling", 38.4)], minutes=48)

    def test_a_ride_before_the_plan_does_not_replace(self):
        t = Track([session("w01-e1", day(2), minutes=50)], created=dt.datetime(2026, 12, 2, 9, 0, tzinfo=UTC))
        full(t, [run(10, day(2), hour=7, type_key="cycling", km=None, minutes=90.0)])
        self.assertEqual(t.status("w01-e1"), "missed")


class SameDay(unittest.TestCase):
    def test_warm_up_jog_and_main_run(self):
        t = Track([session("w01-q1", day(2), role="threshold", km=12.0, minutes=60)])
        full(t, [run(1, day(2), hour=6, km=2.0, minutes=12.0), run(2, day(2), hour=7, km=12.0, minutes=60.0)])
        self.assertEqual(t.links(), {"w01-q1": ("done", 2)})

    def test_main_run_then_cool_down(self):
        t = Track([session("w01-q1", day(2), role="threshold", km=12.0, minutes=60)])
        full(t, [run(1, day(2), hour=7, km=12.0, minutes=60.0), run(2, day(2), hour=8, km=2.0, minutes=12.0)])
        self.assertEqual(t.links(), {"w01-q1": ("done", 1)})

    def test_double_day_two_runs(self):
        t = Track([session("w01-am", day(2), km=6.0, minutes=36), session("w01-pm", day(2), km=10.0, minutes=60)])
        full(t, [run(1, day(2), hour=6, km=6.0, minutes=36.0), run(2, day(2), hour=18, km=10.0, minutes=60.0)])
        self.assertEqual(t.links(), {"w01-am": ("done", 1), "w01-pm": ("done", 2)})

    def test_one_run_long_and_easy(self):
        both = [session("w01-e1", day(6), km=6.0, minutes=36), session("w01-l", day(6), role="long", km=20.0,
                                                                       minutes=120)]
        t = Track(both)
        full(t, [run(1, day(6), km=20.0, minutes=118.0)])
        self.assertEqual(t.links(), {"w01-e1": ("missed", None), "w01-l": ("done", 1)})
        t = Track(both)
        full(t, [run(1, day(6), km=6.0, minutes=36.0)])                  # too short for the long run
        self.assertEqual(t.links(), {"w01-e1": ("done", 1), "w01-l": ("missed", None)})
        t = Track(both)
        full(t, [run(1, day(6), km=3.0, minutes=18.0)])                  # too short for either: the long one
        self.assertEqual(t.links(), {"w01-e1": ("missed", None), "w01-l": ("partial", 1)})

    def test_one_run_quality_and_easy(self):
        for role in ("threshold", "intervals", "mp", "race"):
            with self.subTest(role=role):
                both = [session("w01-e1", day(2), km=12.0, minutes=72),
                        session("w01-q1", day(2), role=role, km=10.0, minutes=55)]
                t = Track(both)
                full(t, [run(1, day(2), km=10.0, minutes=55.0)])
                self.assertEqual(t.links()["w01-q1"], ("done", 1))     # quality first, though the easy is longer

    def test_the_hand_over_keeps_the_day_gap(self):
        both = [session("w01-e1", day(6), km=6.0, minutes=36), session("w01-l", day(6), role="long", km=20.0,
                                                                       minutes=120)]
        t = Track(both)
        full(t, [run(1, day(7), km=6.0, minutes=36.0)])
        self.assertEqual(t.links(), {"w01-e1": ("done_late", 1), "w01-l": ("missed", None)})


class Shapes(unittest.TestCase):
    def test_a_run_over_midnight_counts_on_its_start_day(self):
        t = Track([session("w01-e1", day(2))])
        full(t, [run(1, day(1), hour=23, minute=40, minutes=50.0)])
        self.assertEqual(t.links(), {"w01-e1": ("done_early", 1)})

    def test_gmt_only_activity_is_read_in_the_zone(self):
        tz = ZoneInfo("Asia/Jerusalem")
        t = Track([session("w01-e1", day(2))], tz=tz)
        act = run(1, day(2))
        del act["startTimeLocal"]
        act["startTimeGMT"] = f"{day(1).isoformat()} 22:30:00"           # 00:30 the next day in Jerusalem
        full(t, [act])
        self.assertEqual(t.links(), {"w01-e1": ("done", 1)})
        t = Track([session("w01-e1", day(2))], tz=UTC)
        full(t, [act])
        self.assertEqual(t.links(), {"w01-e1": ("done_early", 1)})

    def test_start_time_local_wins_over_gmt(self):
        t = Track([session("w01-e1", day(2))], tz=ZoneInfo("Asia/Jerusalem"))
        act = run(1, day(1), hour=23)
        act["startTimeGMT"] = f"{day(1).isoformat()} 22:30:00"
        full(t, [act])
        self.assertEqual(t.links(), {"w01-e1": ("done_early", 1)})

    def test_activity_without_any_time_is_ignored(self):
        t = Track([session("w01-e1", day(2))])
        act = run(1, day(2))
        del act["startTimeLocal"]
        full(t, [act])
        self.assertEqual(t.links(), {"w01-e1": ("missed", None)})

    def test_date_only_start_counts_on_that_day(self):
        t = Track([session("w01-e1", day(2))])
        full(t, [run(1, day(2), startTimeLocal=day(2).isoformat())])
        self.assertEqual(t.links(), {"w01-e1": ("done", 1)})

    def test_missing_distance_or_duration(self):
        cases = [(None, 48.0, "done"), (None, 30.0, "partial"), (8.0, None, "done"), (5.0, None, "partial"),
                 (None, None, "done"), (0.0, 0.0, "done")]
        for km, minutes, expected in cases:
            with self.subTest(km=km, minutes=minutes):
                t = Track([session("w01-e1", day(2), km=8.0, minutes=48)])
                full(t, [run(1, day(2), km=km, minutes=minutes)])
                self.assertEqual(t.status("w01-e1"), expected)

    def test_moving_duration_when_duration_is_missing(self):
        t = Track([session("w01-e1", day(2), km=None, minutes=48)])
        full(t, [run(1, day(2), km=None, minutes=None, movingDuration=30 * 60.0)])
        self.assertEqual(t.status("w01-e1"), "partial")

    def test_manual_activity(self):
        t = Track([session("w01-e1", day(2))])
        full(t, [run(1, day(2), manualActivity=True, startTimeGMT=None)])
        self.assertEqual(t.links(), {"w01-e1": ("done", 1)})

    def test_every_run_type(self):
        for type_key in ("running", "treadmill_running", "trail_running", "virtual_run", "indoor_running",
                         "track_running", "street_running", "ultra_run", "obstacle_run"):
            with self.subTest(type_key=type_key):
                t = Track([session("w01-e1", day(2))])
                full(t, [run(1, day(2), type_key=type_key)])
                self.assertEqual(t.links(), {"w01-e1": ("done", 1)})

    def test_other_types_never_complete_a_run(self):
        for type_key in ("walking", "hiking", "strength_training", "cycling", "lap_swimming", "yoga",
                         "motorcycling", "", None):
            with self.subTest(type_key=type_key):
                t = Track([session("w01-e1", day(2), minutes=None)])
                act = run(1, day(2), type_key=type_key)
                if type_key is None:
                    del act["activityType"]
                full(t, [act])
                self.assertEqual(t.links(), {"w01-e1": ("missed", None)})

    def test_bad_ids_are_skipped(self):
        t = Track([session("w01-e1", day(2))])
        acts = [run(aid, day(2)) for aid in (None, True, 0, -5, "abc", "1" * 19, 2 ** 63)]
        full(t, acts)
        self.assertEqual(t.links(), {"w01-e1": ("missed", None)})
        full(t, [run("77", day(2))])                                       # digits as text are an id
        self.assertEqual(t.links(), {"w01-e1": ("done", 77)})


class GarminLink(unittest.TestCase):
    def setUp(self):
        self.t = Track([session("w01-e1", day(1)), session("w01-e2", day(3))])
        self.t.put_garmin("w01-e1", day(1), 9001)

    def entry(self, activity_id):
        return [{"workoutId": 9001, "scheduleDate": day(1).isoformat(), "associatedActivityId": activity_id,
                 "workoutName": "IGNORE ME"}]

    def test_link_present_wins_whatever_the_dates(self):
        full(self.t, [run(1, day(1)), run(2, day(3))], self.entry(2))
        # Garmin linked Thursday's run to Tuesday's session: done, no late label; the Tuesday run then takes
        # nothing (Thursday's session lost its run to the link and Tuesday's run is two days off).
        self.assertEqual(self.t.links(), {"w01-e1": ("done", 2), "w01-e2": ("missed", None)})

    def test_link_absent_matching_does_the_work(self):
        full(self.t, [run(1, day(1)), run(2, day(3))], self.entry(None))
        self.assertEqual(self.t.links(), {"w01-e1": ("done", 1), "w01-e2": ("done", 2)})
        full(self.t, [run(1, day(1)), run(2, day(3))], [])
        self.assertEqual(self.t.links(), {"w01-e1": ("done", 1), "w01-e2": ("done", 2)})

    def test_link_to_an_activity_not_in_the_data(self):
        full(self.t, [run(2, day(3))], self.entry(555))
        self.assertEqual(self.t.links(), {"w01-e1": ("done", 555), "w01-e2": ("done", 2)})

    def test_link_to_a_short_run_is_partial_without_label(self):
        full(self.t, [run(1, day(2), km=3.0, minutes=20.0)], self.entry(1))
        self.assertEqual(self.t.links()["w01-e1"], ("partial", 1))

    def test_a_link_of_another_workout_is_not_ours(self):
        other = [{"workoutId": 5555, "scheduleDate": day(1).isoformat(), "associatedActivityId": 1}]
        full(self.t, [run(1, day(1))], other)
        # 5555 is not the plan's workout: the run still matches by date.
        self.assertEqual(self.t.links()["w01-e1"], ("done", 1))

    def test_junk_calendar_entries(self):
        junk = ["x", None, {"workoutId": "abc", "associatedActivityId": 1}, {"workoutId": 9001},
                {"workoutId": True, "associatedActivityId": 2}]
        full(self.t, [run(1, day(1))], junk)
        self.assertEqual(self.t.links()["w01-e1"], ("done", 1))


# --- when a session is settled, what a later refresh may change -------------------------------------------------

class Settling(unittest.TestCase):
    def test_a_cut_list_settles_only_the_days_it_holds_whole(self):
        # A full newest-first page whose oldest item is Thursday: Thursday may have lost activities, Friday on
        # is whole. Tuesday's session stays open, Saturday's run counts, the next Wednesday's is missed.
        t = Track([session("w01-e1", day(1)), session("w01-e2", day(5)), session("w02-e1", day(9))])
        tr.refresh_links(t.store, t.fp, day(13), [run(1, day(3)), run(2, day(5))], [], first=day(-7),
                         last=day(13), tz=t.tz, today=day(20), cut=True)
        self.assertEqual(t.links(), {"w01-e2": ("done", 2), "w02-e1": ("missed", None)})

    def test_a_failed_search_settles_nothing_and_keeps_the_stored_links(self):
        t = Track([session("w01-e1", day(1)), session("w01-e2", day(5))])
        t.refresh([run(1, day(1))], day(-7), day(3), today=day(3))
        tr.refresh_links(t.store, t.fp, day(13), None, [], first=day(-7), last=day(13), tz=t.tz, today=day(20))
        self.assertEqual(t.links(), {"w01-e1": ("done", 1)})

    def test_missed_only_once_the_late_day_is_over(self):
        t = Track([session("w01-e1", day(2))])
        t.refresh([], day(-11), day(3), today=day(3))                  # Thursday is not over
        self.assertEqual(t.links(), {})
        t.refresh([], day(-11), day(3), today=day(4))
        self.assertEqual(t.links(), {"w01-e1": ("missed", None)})

    def test_missed_needs_the_early_day_in_the_data(self):
        t = Track([session("w01-e1", day(2))])
        t.refresh([], day(2), day(5), today=day(6))                    # Tuesday (the early day) not covered
        self.assertEqual(t.links(), {})
        t.refresh([], day(1), day(5), today=day(6))
        self.assertEqual(t.links(), {"w01-e1": ("missed", None)})

    def test_missed_needs_the_late_day_in_the_data(self):
        t = Track([session("w01-e1", day(2))])
        t.refresh([], day(-5), day(2), today=day(9))
        self.assertEqual(t.links(), {})

    def test_replaced_waits_like_missed(self):
        t = Track([session("w01-e1", day(2), minutes=50)])
        ride = [run(10, day(2), type_key="cycling", km=None, minutes=60.0)]
        t.refresh(ride, day(-11), day(3), today=day(3))
        self.assertEqual(t.links(), {})
        t.refresh(ride, day(-11), day(3), today=day(4))
        self.assertEqual(t.links(), {"w01-e1": ("replaced", 10)})

    def test_daily_reports_alone_never_settle_a_week(self):
        sessions = [session("w01-e1", day(1), minutes=50), session("w01-q1", day(2), role="threshold"),
                    session("w01-e2", day(4)), session("w01-l", day(6), role="long", km=18.0, minutes=110)]
        acts = [run(10, day(1), type_key="cycling", km=None, minutes=70.0), run(11, day(5), km=8.0, minutes=48.0)]
        t = Track(sessions)
        for n in range(0, 14):
            t.daily(acts, day(n))
        self.assertEqual(t.links(), {"w01-e2": ("done_late", 11)})
        t.weekly(acts, day(13))
        self.assertEqual(t.links(), {"w01-e1": ("replaced", 10), "w01-q1": ("missed", None),
                                     "w01-e2": ("done_late", 11), "w01-l": ("missed", None)})

    def test_a_done_link_is_never_undone_by_fewer_days(self):
        t = Track([session("w01-e1", day(2))])
        acts = [run(1, day(1))]
        t.weekly(acts, day(6))
        self.assertEqual(t.links(), {"w01-e1": ("done_early", 1)})
        for first, last in ((day(2), day(3)), (day(3), day(3)), (day(2), day(8)), (day(-20), day(0))):
            t.refresh([], first, last, today=day(9))
            self.assertEqual(t.links(), {"w01-e1": ("done_early", 1)}, (first, last))
        t.daily([], day(3))
        self.assertEqual(t.links(), {"w01-e1": ("done_early", 1)})

    def test_partial_and_replaced_also_stay(self):
        t = Track([session("w01-e1", day(2), minutes=50), session("w01-l", day(6), role="long", km=20.0,
                                                                  minutes=120)])
        acts = [run(10, day(2), type_key="cycling", km=None, minutes=60.0), run(11, day(6), km=10.0, minutes=60.0)]
        t.weekly(acts, day(8))
        expected = {"w01-e1": ("replaced", 10), "w01-l": ("partial", 11)}
        self.assertEqual(t.links(), expected)
        t.refresh([], day(1), day(8), today=day(9))
        self.assertEqual(t.links(), expected)

    def test_activation_after_a_done_link_keeps_it(self):
        t = Track([session("w01-e1", day(1)), session("w01-e2", day(3))], created=dt.datetime(2026, 11, 29, 8, 0,
                                                                                         tzinfo=UTC))
        acts = [run(1, day(1), hour=7), run(2, day(3), hour=7)]
        t.daily(acts, day(1))
        self.assertEqual(t.links(), {"w01-e1": ("done", 1)})
        t.clock.now = dt.datetime(2026, 12, 2, 12, 0, tzinfo=UTC)          # plan_commit on Wednesday noon
        t.store.move_plan(t.pid, "active")
        t.weekly(acts, day(6))
        # Tuesday's run predates the activation but was already counted; Thursday's counts anew.
        self.assertEqual(t.links(), {"w01-e1": ("done", 1), "w01-e2": ("done", 2)})

    def test_activation_before_a_run_ignores_runs_before_it(self):
        t = Track([session("w01-e1", day(2)), session("w01-e2", day(3))], created=dt.datetime(2026, 11, 29, 8, 0,
                                                                                         tzinfo=UTC))
        t.clock.now = dt.datetime(2026, 12, 2, 12, 0, tzinfo=UTC)
        t.store.move_plan(t.pid, "active")
        t.weekly([run(1, day(2), hour=7), run(2, day(2), hour=13)], day(6))
        self.assertEqual(t.links(), {"w01-e1": ("done", 2), "w01-e2": ("missed", None)})

    def test_a_run_at_the_activation_minute_counts(self):
        t = Track([session("w01-e1", day(2))], created=dt.datetime(2026, 12, 2, 7, 0, tzinfo=UTC))
        t.weekly([run(1, day(2), hour=7)], day(6))
        self.assertEqual(t.links(), {"w01-e1": ("done", 1)})

    def test_missed_upgraded_by_a_late_run(self):
        t = Track([session("w01-l", day(6), role="long", km=18.0, minutes=110)])
        t.weekly([], day(7), today=day(8))
        self.assertEqual(t.links(), {"w01-l": ("missed", None)})
        t.refresh([run(1, day(7), km=18.0, minutes=110.0)], day(6), day(7), today=day(8))
        self.assertEqual(t.links(), {"w01-l": ("done_late", 1)})

    def test_replaced_upgraded_by_a_run(self):
        t = Track([session("w01-e1", day(2), minutes=50)])
        ride = run(10, day(2), type_key="cycling", km=None, minutes=60.0)
        t.weekly([ride], day(6))
        self.assertEqual(t.status("w01-e1"), "replaced")
        t.weekly([ride, run(11, day(3))], day(6))
        self.assertEqual(t.links(), {"w01-e1": ("done_late", 11)})

    def test_a_stored_missed_stays_while_a_refresh_cannot_settle(self):
        t = Track([session("w01-e1", day(2))])
        t.weekly([], day(6))
        t.daily([], day(3))
        t.daily([run(1, day(9))], day(9))
        self.assertEqual(t.links(), {"w01-e1": ("missed", None)})

    def test_a_daily_refresh_after_the_weekly_keeps_its_links(self):
        t = Track([session("w01-a", day(2), km=10.0, minutes=None), session("w01-b", day(3), km=10.0, minutes=None),
                   session("w01-c", day(3), km=5.0, minutes=None), session("w01-d", day(4), km=5.0, minutes=None)])
        acts = [run(1, day(3), km=5.0, minutes=None), run(2, day(4), km=10.0, minutes=None),
                run(3, day(5), km=10.0, minutes=None)]
        t.weekly(acts, day(6))
        weekly = t.links()
        # The weekly settles the same-day case as above: Thursday's 5 km run is w01-c's, w01-a is missed.
        self.assertEqual((weekly["w01-a"], weekly["w01-c"]), (("missed", None), ("done", 1)))
        t.daily(acts, day(3))                         # get_daily_status(date=Thursday) after the weekly
        self.assertEqual(t.links(), weekly)


# --- dates: time zones, DST weekends, the year boundary ----------------------------------------------------------

class Dates(unittest.TestCase):
    def test_activation_is_read_in_the_report_zone(self):
        tz = ZoneInfo("Asia/Jerusalem")
        created = dt.datetime(2026, 12, 2, 5, 30, tzinfo=UTC)               # 07:30 in Jerusalem
        for hour, expected in ((7, ("missed", None)), (8, ("done", 1))):
            with self.subTest(hour=hour):
                t = Track([session("w01-e1", day(2))], created=created, tz=tz)
                t.weekly([run(1, day(2), hour=hour)], day(6))
                self.assertEqual(t.links(), {"w01-e1": expected})

    def test_dst_weekends_gmt_only(self):
        cases = [
            # (zone, GMT start, session day, expected)
            ("Europe/London", "2026-10-24 23:30:00", dt.date(2026, 10, 25), "done"),      # 00:30 BST Sunday
            ("Europe/London", "2026-10-25 00:30:00", dt.date(2026, 10, 25), "done"),      # 01:30 BST
            ("Europe/London", "2026-10-25 01:30:00", dt.date(2026, 10, 25), "done"),      # 01:30 GMT again
            ("Europe/Berlin", "2026-03-28 23:30:00", dt.date(2026, 3, 29), "done"),       # 00:30 CET Sunday
            ("Europe/Berlin", "2026-03-28 22:30:00", dt.date(2026, 3, 29), "done_early"),  # 23:30 CET Saturday
            ("Asia/Jerusalem", "2026-03-26 22:30:00", dt.date(2026, 3, 27), "done"),      # 00:30 IST Friday
            ("Asia/Jerusalem", "2026-10-24 21:30:00", dt.date(2026, 10, 25), "done"),     # 00:30 IDT Sunday
            ("Asia/Jerusalem", "2026-10-24 20:30:00", dt.date(2026, 10, 25), "done_early"),
        ]
        for zone, gmt, when, expected in cases:
            with self.subTest(zone=zone, gmt=gmt):
                t = Track([session("w01-e1", when)], created=dt.datetime(2026, 1, 1, tzinfo=UTC), tz=ZoneInfo(zone))
                act = {"activityId": 1, "activityType": {"typeKey": "running"}, "startTimeGMT": gmt,
                       "distance": 8000.0, "duration": 2880.0}
                t.refresh([act], when - dt.timedelta(days=7), when + dt.timedelta(days=3),
                          today=when + dt.timedelta(days=4))
                self.assertEqual(t.status("w01-e1"), expected)

    def test_two_runs_in_the_repeated_hour(self):
        # London, 25 Oct 2026: 00:30 and 01:30 GMT both read 01:30 local; two runs, two sessions.
        tz = ZoneInfo("Europe/London")
        when = dt.date(2026, 10, 25)
        t = Track([session("w01-am", when, km=6.0, minutes=36), session("w01-pm", when, km=10.0, minutes=60)],
                  created=dt.datetime(2026, 1, 1, tzinfo=UTC), tz=tz)
        acts = [{"activityId": i, "activityType": {"typeKey": "running"}, "startTimeGMT": g, "distance": km * 1000,
                 "duration": m * 60.0} for i, g, km, m in ((1, "2026-10-25 00:30:00", 6.0, 36),
                                                           (2, "2026-10-25 01:30:00", 10.0, 60))]
        t.refresh(acts, when - dt.timedelta(days=7), when + dt.timedelta(days=3), today=when + dt.timedelta(days=4))
        self.assertEqual(t.links(), {"w01-am": ("done", 1), "w01-pm": ("done", 2)})

    def test_year_boundary(self):
        dec31, jan1 = dt.date(2026, 12, 31), dt.date(2027, 1, 1)
        t = Track([session("w53-e1", dec31), session("w53-e2", jan1 + dt.timedelta(days=1))],
                  created=dt.datetime(2026, 12, 1, tzinfo=UTC))
        t.weekly([run(1, jan1), run(2, jan1 + dt.timedelta(days=1))], dt.date(2027, 1, 3), today=dt.date(2027, 1, 4))
        self.assertEqual(t.links(), {"w53-e1": ("done_late", 1), "w53-e2": ("done", 2)})
        counts = tr.week_counts(t.store, t.fp, dt.date(2026, 12, 28))
        self.assertEqual((counts["planned"], counts["done"], counts["moved"], counts["open"]), (2, 2, 1, 0))
        t2 = Track([session("w01-e1", jan1)], created=dt.datetime(2026, 12, 1, tzinfo=UTC),
                   tz=ZoneInfo("Asia/Jerusalem"))
        act = {"activityId": 7, "activityType": {"typeKey": "running"}, "startTimeGMT": "2026-12-31 22:30:00",
               "distance": 8000.0, "duration": 2880.0}                     # 00:30 on 1 Jan in Jerusalem
        t2.weekly([act], dt.date(2027, 1, 3))
        self.assertEqual(t2.links(), {"w01-e1": ("done", 7)})

    def test_week_counts_from_any_weekday(self):
        t = Track([session("w01-e1", day(1)), session("w01-e2", day(3)), session("w01-l", day(6), role="long"),
                   session("w02-e1", day(8))])
        t.weekly([run(1, day(1)), run(2, day(4))], day(9), today=day(10))
        # A week Wednesday..Tuesday (the weekly report with end_date on a Tuesday).
        counts = tr.week_counts(t.store, t.fp, day(2))
        self.assertEqual(counts, {"planned": 3, "done": 1, "partial": 0, "replaced": 0, "missed": 2, "open": 0,
                                  "moved": 1})
        self.assertIsNone(tr.week_counts(t.store, t.fp, day(-14)))


class FixtureBlock(unittest.TestCase):
    """fixtures/deep_tracking/block.json: a made-up two-week block in Garmin's shapes with its expected links."""

    def setUp(self):
        with open(os.path.join(FIXTURES, "block.json"), encoding="utf-8") as fh:
            self.fx = json.load(fh)

    def test_weekly_then_daily_reports(self):
        sessions = [s for w in self.fx["plan"]["weeks"] for s in w["sessions"]]
        t = Track(sessions, created=dt.datetime.fromisoformat(self.fx["created_at"]), tz=ZoneInfo(self.fx["tz"]))
        acts = self.fx["activities"]
        start = dt.date.fromisoformat(self.fx["plan"]["weeks"][0]["start"])
        for n in range(14):
            t.daily(acts, start + dt.timedelta(days=n))
        t.weekly(acts, start + dt.timedelta(days=13), today=start + dt.timedelta(days=15))
        got = {k: list(v) for k, v in t.links().items()}
        self.assertEqual(got, self.fx["expected"])


if __name__ == "__main__":
    unittest.main()
