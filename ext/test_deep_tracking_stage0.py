"""Deep tests of Stage 0's fixes: the weekly plan completion over due workouts (weekly rules v3), yard pools in
the daily status, and dating an activity from its GMT start. Standard library only.

docs/weekly-status.md: completed and completion_pct count only due workouts, a workout after today is never
matched (a Garmin link counts in done_early), the share never passes 100%; status_common.step_distance_m
reads a distance step in meters in every pool, a yard pool too (spike S0.3); status_common.activity_times reads
startTimeLocal, else startTimeGMT in the zone, else (the daily's one-day query) the queried day.
"""

from __future__ import annotations

import asyncio
import copy
import datetime as dt
import unittest
from unittest import mock
from zoneinfo import ZoneInfo

import mcp2_compat

mcp2_compat.register_finder()

import daily_status as ds  # noqa: E402
import status_common as sc  # noqa: E402
import test_weekly_status as tws  # noqa: E402
import weekly_status as ws  # noqa: E402

TZ = ZoneInfo("Asia/Jerusalem")
MON, SUN = dt.date(2026, 11, 30), dt.date(2026, 12, 6)


def day(n):
    return MON + dt.timedelta(days=n)


def item(when, sport="running", link=None):
    return {"scheduleDate": when.isoformat(), "workoutType": sport, "associatedActivityId": link, "isRestDay": False}


def act(aid, when, hour=7, type_key="running"):
    return {"activityId": aid, "activityType": {"typeKey": type_key}, "startTimeLocal": f"{when} {hour:02d}:00:00",
            "duration": 3000.0, "distance": 9000.0}


def plan(items, acts, today, start=MON, end=SUN):
    schedule = ws.parse_schedule({"data": {"workoutScheduleSummariesScalar": items}}, start, end)
    return ws.plan_section(schedule, None if acts is None else ws.parse_activities(acts, TZ), start, end, today)


class DueCompletion(unittest.TestCase):
    def test_due_and_upcoming(self):
        items = [item(day(0)), item(day(1)), item(day(3), link=901), item(day(4))]
        out = plan(items, [act(1, day(0)), act(2, day(3)), act(3, day(4))], today=day(2))
        # Thursday and Friday are ahead: never matched (their activities are after today anyway); Thursday's
        # Garmin link counts in done_early.
        self.assertEqual(out, {"scheduled": 4, "due": 2, "completed": 1, "on_planned_day": 1, "moved": 0,
                               "upcoming": 2, "done_early": 1, "linked": 1, "completion_pct": 50})

    def test_today_is_due(self):
        out = plan([item(day(2))], [act(1, day(2))], today=day(2))
        self.assertEqual((out["due"], out["completed"], out["upcoming"], out["completion_pct"]), (1, 1, 0, 100))

    def test_an_activity_today_for_tomorrows_workout_is_not_counted(self):
        out = plan([item(day(3))], [act(1, day(2))], today=day(2))
        self.assertEqual((out["due"], out["completed"], out["upcoming"], out["done_early"], out["completion_pct"]),
                         (0, 0, 1, 0, None))

    def test_never_above_100_percent(self):
        items = [item(day(0)), item(day(1)), item(day(4), link=901), item(day(5), link=902)]
        out = plan(items, [act(1, day(0)), act(2, day(1))], today=day(2))
        self.assertEqual((out["completed"], out["due"], out["done_early"], out["completion_pct"]), (2, 2, 2, 100))
        for today in (day(n) for n in range(-1, 9)):
            with self.subTest(today=today):
                got = plan(items, [act(1, day(0)), act(2, day(1)), act(3, day(4)), act(4, day(5))], today)
                if got["completion_pct"] is not None:
                    self.assertLessEqual(got["completion_pct"], 100)
                    self.assertLessEqual(got["completed"], got["due"])

    def test_nothing_due_yet(self):
        out = plan([item(day(3)), item(day(4), link=7)], [], today=day(0))
        self.assertEqual((out["due"], out["completed"], out["completion_pct"], out["done_early"]), (0, 0, None, 1))

    def test_a_week_in_the_past_is_all_due(self):
        out = plan([item(day(0)), item(day(6))], [act(1, day(0))], today=day(30))
        self.assertEqual((out["due"], out["completed"], out["upcoming"]), (2, 1, 0))

    def test_without_activities_only_links_count(self):
        self.assertEqual(plan([item(day(0), link=1), item(day(1), link=2), item(day(4))], None, today=day(2))
                         ["completed"], 2)
        self.assertIsNone(plan([item(day(0), link=1), item(day(1))], None, today=day(2))["completed"])

    def test_one_day_early_two_days_late(self):
        for shift, done in ((-2, 0), (-1, 1), (0, 1), (1, 1), (2, 1), (3, 0)):
            with self.subTest(shift=shift):
                out = plan([item(day(3))], [act(1, day(3 + shift))], today=day(6))
                self.assertEqual(out["completed"], done)

    def test_the_week_before_never_counts(self):
        out = plan([item(day(0))], [act(1, day(-1), hour=23)], today=day(6))
        self.assertEqual(out["completed"], 0)

    def test_low_completion_needs_three_due(self):
        items = [item(day(0)), item(day(1)), item(day(4)), item(day(5)), item(day(6))]
        two_due = plan(items, [], today=day(1))
        self.assertEqual((two_due["due"], two_due["completion_pct"]), (2, 0))
        self.assertNotIn("low_completion", tws.flag_ids(tws.neutral(plan=two_due)))
        three_due = plan(items, [], today=day(4))
        self.assertEqual(three_due["due"], 3)
        self.assertIn("low_completion", tws.flag_ids(tws.neutral(plan=three_due)))

    def test_weekly_report_mid_week(self):
        fx = copy.deepcopy(tws.FX)
        fx["schedule"] = {"data": {"workoutScheduleSummariesScalar": [item(day(0)), item(day(1)), item(day(2)),
                                                                       item(day(4), link=901)]}}
        fx["activities"] = [act(1, day(0)), act(2, day(2)), act(3, day(4))]
        with mock.patch.object(ws, "_load_fit_helpers", tws.fit_helpers()):
            out = asyncio.run(ws.build_weekly_status(tws.FakeClient(fx), SUN.isoformat(), tz="Asia/Jerusalem",
                                                     now=dt.datetime(2026, 12, 2, 20, 0, tzinfo=TZ),
                                                     text_format="markdown"))
        self.assertEqual(out["plan"], {"scheduled": 4, "due": 3, "completed": 2, "on_planned_day": 2, "moved": 0,
                                       "upcoming": 1, "done_early": 1, "linked": 1, "completion_pct": 67})
        self.assertIn("Plan: 2 of 3 done (67%), 1 still ahead (1 already done)", out["text"])

    def test_same_day_matches_come_first(self):
        # Wed, Thu, Sat planned; runs Thu, Fri, Sun. Thu's run on Thu, Wed's on Fri (2 days late), Sat's on Sun
        # completes all three with one on its day, not every match a day late (Wed->Thu, Thu->Fri, Sat->Sun).
        items = [item(day(2)), item(day(3)), item(day(5))]
        out = plan(items, [act(1, day(3)), act(2, day(4)), act(3, day(6))], today=day(6))
        self.assertEqual(out["completed"], 3)
        self.assertEqual((out["on_planned_day"], out["moved"]), (1, 2))


def swim(steps, unit, meters=None):
    cur = {"name": "w", "sport": "swimming", "pool_length": 25.0, "pool_length_unit": unit,
           "segments": [{"order": 1, "sport": "swimming", "steps": steps}]}
    entry = {"scheduleDate": "2026-12-01", "workoutName": "Swim", "workoutType": "swimming",
             "estimatedDistanceInMeters": meters}
    return ds.classify_workout(entry, cur)


def interval(value, end="distance", zone=4):
    return {"type": "interval", "end_condition": end, "end_condition_value": value,
            "target_type": "heart.rate.zone", "target_zone": zone}


def repeat(count, *steps):
    return {"type": "repeat", "end_condition": "iterations", "end_condition_value": float(count),
            "repeat_count": count, "steps": list(steps), "step_count": len(steps)}


class YardPools(unittest.TestCase):
    """Spike S0.3: Garmin stores a distance step's endConditionValue in meters in a yard pool too."""

    def test_hard_work_bound_in_a_25_yd_pool(self):
        nine = swim([repeat(9, interval(100.0))], "yard")             # 900 m: under 1000 m
        self.assertFalse(nine["hard"])
        self.assertIn("900 m", nine["reason"])
        for unit in ("yard", "meter"):                                # 1000 m in either pool
            with self.subTest(unit=unit):
                self.assertTrue(swim([repeat(10, interval(100.0))], unit)["hard"])

    def test_unit_spellings(self):
        # Every pool unit spelling, a yard pool's included, leaves the meters as they are.
        for unit in ("yard", "Yard", " YARDS ", "yd", "yds", {"unitKey": "yard"}, {"unitKey": "YD", "factor": 91.44},
                     "meter", None, {"unitKey": "meter"}):
            with self.subTest(unit=unit):
                self.assertEqual(swim([interval(100.0)] * 7, unit)["km"], 0.7)
        self.assertEqual(sc.step_distance_m({"end_condition": "distance", "end_condition_value": 100.0}), 100.0)

    def test_mixed_distance_and_time_steps(self):
        steps = [{"type": "warmup", "end_condition": "distance", "end_condition_value": 200.0},
                 repeat(4, interval(100.0), {"type": "rest", "end_condition": "time", "end_condition_value": 20.0}),
                 {"type": "cooldown", "end_condition": "distance", "end_condition_value": 100.0}]
        out = swim(steps, "yard")
        self.assertEqual(out["km"], 0.7)            # 700 m; the timed rests add no distance
        self.assertIsNone(out["minutes"])           # not every step ends on time
        self.assertFalse(out["hard"])               # 400 m at zone 4
        self.assertEqual(swim(steps, "meter")["km"], 0.7)

    def test_the_estimate_wins_over_the_steps(self):
        self.assertEqual(swim([repeat(10, interval(100.0))], "yard", meters=1200.0)["km"], 1.2)

    def test_a_run_ignores_the_pool_unit(self):
        cur = {"name": "w", "sport": "running", "pool_length_unit": "yard",
               "segments": [{"order": 1, "sport": "running", "steps": [interval(1000.0), interval(1000.0)]}]}
        entry = {"scheduleDate": "2026-12-01", "workoutName": "Run", "workoutType": "running"}
        self.assertEqual(ds.classify_workout(entry, cur)["km"], 2.0)


class GmtDating(unittest.TestCase):
    LATE = {"activityId": 1, "activityType": {"typeKey": "running"}, "startTimeGMT": "2026-12-01 22:30:00",
            "duration": 1800.0, "distance": 5000.0}

    def test_each_reader(self):
        # 22:30 GMT is 00:30 on 2 Dec in Jerusalem.
        self.assertEqual(sc.activity_times(self.LATE, TZ), (day(2), dt.datetime(2026, 12, 2, 0, 30)))
        self.assertEqual(sc.activity_times(self.LATE, TZ, day(1)), (day(1), dt.datetime(2026, 12, 2, 0, 30)))
        self.assertEqual(ws.parse_activities([self.LATE], TZ)[0]["date"], day(2))
        self.assertEqual(ds.completion_activities([self.LATE], TZ, day(1))[0]["date"], "2026-12-01")
        self.assertEqual(ds.completion_activities([self.LATE], TZ)[0]["date"], "2026-12-02")

    def test_daily_marks_the_queried_days_workout_done(self):
        entry = {"scheduleDate": "2026-12-01", "workoutType": "running", "associatedActivityId": None}
        workouts = [{"sport": "run"}]
        ds.mark_completed(workouts, [entry], ds.completion_activities([self.LATE], TZ, day(1)))
        self.assertTrue(workouts[0]["completed"])

    def test_gmt_fractions_and_separators(self):
        for text in ("2026-12-01T22:30:00.0", "2026-12-01 22:30:00.123", "2026-12-01T22:30", "2026-12-01 22:30:00"):
            with self.subTest(text=text):
                got = sc.activity_times({"startTimeGMT": text}, TZ)
                self.assertEqual(got, (day(2), dt.datetime(2026, 12, 2, 0, 30)))

    def test_dst_and_year_edges(self):
        cases = [("Europe/London", "2026-03-29 00:59:00", dt.datetime(2026, 3, 29, 0, 59)),
                 ("Europe/London", "2026-03-29 01:00:00", dt.datetime(2026, 3, 29, 2, 0)),
                 ("Europe/London", "2026-10-25 00:59:00", dt.datetime(2026, 10, 25, 1, 59)),
                 ("Europe/London", "2026-10-25 01:00:00", dt.datetime(2026, 10, 25, 1, 0)),
                 ("Asia/Jerusalem", "2026-12-31 21:59:00", dt.datetime(2026, 12, 31, 23, 59)),
                 ("Asia/Jerusalem", "2026-12-31 22:00:00", dt.datetime(2027, 1, 1, 0, 0)),
                 ("Pacific/Kiritimati", "2026-12-31 09:59:00", dt.datetime(2026, 12, 31, 23, 59))]
        for zone, gmt, local in cases:
            with self.subTest(zone=zone, gmt=gmt):
                self.assertEqual(sc.activity_times({"startTimeGMT": gmt}, ZoneInfo(zone)), (local.date(), local))

    def test_junk_times(self):
        for value in (None, "", "2026-12-01", "01/12/2026 22:30", 1764628200000, "2026-02-30 10:00:00"):
            with self.subTest(value=value):
                self.assertEqual(sc.activity_times({"startTimeGMT": value}, TZ), (None, None))
                self.assertEqual(sc.activity_times({"startTimeGMT": value}, TZ, day(1)), (day(1), None))


if __name__ == "__main__":
    unittest.main()
