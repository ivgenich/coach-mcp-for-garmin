"""Tests for daily_status. Standard library only:

    python3 -m unittest discover -s ext -p "test_daily_status*.py" -v

Fixtures in fixtures/daily_status/ are synthetic payloads in Garmin's raw
shapes. Tests that need upstream garmin_mcp (workout curation, selfcheck)
are skipped when it is not installed.
"""

from __future__ import annotations

import asyncio
import copy
import datetime as dt
import importlib.util
import json
import os
import re
import threading
import time
import unittest
from unittest import mock
from zoneinfo import ZoneInfo

import mcp2_compat  # noqa: E402

# On mcp 2.x, serve garmin_mcp's `from mcp.server.fastmcp import FastMCP` from
# the adapter, as the image's .pth file does; a no-op on mcp 1.x.
mcp2_compat.register_finder()

import daily_status as ds

HAVE_UPSTREAM = importlib.util.find_spec("garmin_mcp") is not None
NEEDS_UPSTREAM = "needs upstream garmin_mcp (workout curation)"

# Tests set their own zone: REPORT_TZ is required and has no default.
TZ_NAME = "Europe/Athens"
TZ = ZoneInfo(TZ_NAME)
DAY = "2026-03-18"            # a Wednesday; Athens is on UTC+2 that week
PREV, PREV2, NEXT = "2026-03-17", "2026-03-16", "2026-03-19"
NOW = dt.datetime(2026, 3, 18, 6, 30, tzinfo=TZ)
FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "daily_status")

# Synthetic workout ids the fake calendar points at (fixture files carry no ids).
EASY_ID, THRESHOLD_ID, SWIM_ID = 7770001, 7770002, 7770003
RANGE_RUN_ID, RANGE_RIDE_ID = 7770004, 7770005
# Synthetic athlete thresholds (the lactate_threshold and cycling_ftp fixtures).
ATHLETE = {"lt_speed_mps": 3.7, "lt_hr": 172, "ftp_w": 240}
EMPTY_SUMMARY = ("Garmin Connect request failed: No data received from server. Garmin Connect may be "
                 "unreachable; check your network connection or try again later.")


class GarminConnectConnectionError(Exception):
    """Same name as garminconnect's; upstream's proxy re-raises it with a hint appended."""


def fixture(name):
    with open(os.path.join(FIXTURES, name + ".json"), encoding="utf-8") as fh:
        return json.load(fh)


def run(coro):
    return asyncio.run(coro)


# --- rule-level helpers -------------------------------------------------------

def recovery(**kw):
    base = {
        "sleep_hours": 7.5, "sleep_hours_prev": 7.5, "hrv": 60, "hrv_7d": 60,
        "hrv_range": {"low_upper": 44, "balanced_low": 50, "balanced_upper": 70},
        "rhr": 48, "rhr_7d": 48, "readiness": 70, "recovery_hours": 2.0,
    }
    base.update(kw)
    return base


def load(**kw):
    base = {"acwr": 1.0, "acwr_status": "OPTIMAL"}
    base.update(kw)
    return base


EASY = {"class": "easy"}
HARD = {"class": "hard_or_long"}
DONE_EASY = {"class": "easy", "completed": True}
DONE_HARD = {"class": "hard_or_long", "completed": True}
OK_TREND = [(60, 50), (60, 50), (60, 50)]


def rules(rec=None, ld=None, today=(), trend=OK_TREND):
    today = None if today is None else list(today)
    flags, missing = ds.evaluate_rules(rec or recovery(), ld or load(), today, trend)
    return [f["id"] for f in flags], missing, flags


def flag(flag_id):
    return {"id": flag_id, "level": ds.FLAG_LEVELS[flag_id], "label_en": ds.flag_label(flag_id)}


# --- workout helpers (upstream's curated shape) -------------------------------

def step(kind, end="time", value=600.0, target=None, zone=None, low=None, high=None,
         sec=None, sec_zone=None, sec_low=None, sec_high=None, unit=None):
    s = {"type": kind, "end_condition": end}
    if value is not None:
        s["end_condition_value"] = value
    for prefix, t, z, lo, hi in (("", target, zone, low, high), ("secondary_", sec, sec_zone, sec_low, sec_high)):
        if t:
            s[f"{prefix}target_type"] = t
            if lo is not None:
                s[f"{prefix}target_value_low"] = lo
            if hi is not None:
                s[f"{prefix}target_value_high"] = hi
            if z is not None:
                s[f"{prefix}target_zone"] = z
    if unit:
        s["target_value_unit"] = unit
    return s


def repeat(count, *steps):
    return {"type": "repeat", "end_condition": "iterations", "end_condition_value": float(count),
            "repeat_count": count, "steps": list(steps), "step_count": len(steps)}


def curated(*steps, sport="running", secs=None, meters=None):
    w = {"name": "w", "sport": sport, "segments": [{"order": 1, "sport": sport, "steps": list(steps)}]}
    if secs:
        w["estimated_duration_seconds"] = secs
    if meters:
        w["estimated_distance_meters"] = meters
    return w


def entry(name="Workout", sport="running", secs=None, meters=None, date=DAY, **kw):
    e = {"scheduleDate": date, "workoutName": name, "workoutType": sport,
         "estimatedDurationInSecs": secs, "estimatedDistanceInMeters": meters, "associatedActivityId": None}
    e.update(kw)
    return e


def classify(steps=(), name="Workout", sport="running", secs=None, meters=None, details=True, athlete=None):
    cur = curated(*steps, sport=sport) if details else None
    return ds.classify_workout(entry(name, sport, secs, meters), cur, athlete=athlete)


# --- fake Garmin client -------------------------------------------------------

def schedule_with_ids():
    data = fixture("schedule")
    items = data["data"]["workoutScheduleSummariesScalar"]
    for item, workout_id in zip(items, (EASY_ID, THRESHOLD_ID, SWIM_ID)):
        item["workoutId"] = workout_id
    return data


def default_data():
    return {
        f"stats:{DAY}": fixture("stats"),
        f"sleep:{DAY}": fixture("sleep_night"),
        f"sleep:{PREV}": fixture("sleep_prev_night"),
        f"readiness:{DAY}": fixture("readiness"),
        f"hrv:{DAY}": fixture("hrv_d0"),
        f"hrv:{PREV}": fixture("hrv_d1"),
        f"hrv:{PREV2}": fixture("hrv_d2"),
        f"training_status:{DAY}": fixture("training_status"),
        "schedule": schedule_with_ids(),
        f"activities:{PREV}": fixture("activities_prev"),
        f"workout:{EASY_ID}": fixture("workout_easy_run"),
        f"workout:{THRESHOLD_ID}": fixture("workout_threshold_run"),
        f"workout:{SWIM_ID}": fixture("workout_technique_swim"),
        f"workout:{RANGE_RUN_ID}": fixture("workout_pace_range_run"),
        f"workout:{RANGE_RIDE_ID}": fixture("workout_power_range_ride"),
        "lactate_threshold": fixture("lactate_threshold"),
        "cycling_ftp": fixture("cycling_ftp"),
    }


def range_schedule():
    """Today a single-step pace-range run, tomorrow a ride with power ranges in a repeat."""
    return {"data": {"workoutScheduleSummariesScalar": [
        entry("Aerobic pace run", secs=3000, meters=9000.0, workoutId=RANGE_RUN_ID),
        entry("Threshold 4x8", "cycling", secs=4080, date=NEXT, workoutId=RANGE_RIDE_ID),
    ]}}


def with_activity_ids(activities, first=8880001):
    for i, activity in enumerate(activities):
        activity["activityId"] = first + i
    return activities


class FakeClient:
    """Synchronous stand-in for upstream's Garmin proxy, serving fixtures."""

    garmin_connect_activities = "/activitylist-service/activities/search/activities"

    def __init__(self, data=None, fail=None, slow=None):
        self.data = default_data() if data is None else data
        self.fail = fail or {}
        self.slow = slow or {}
        self.calls = []
        self.queries = []
        self._lock = threading.Lock()

    def _serve(self, key, empty):
        with self._lock:
            self.calls.append(key)
        if key in self.slow:
            time.sleep(self.slow[key])
        if key in self.fail:
            raise self.fail[key]
        return copy.deepcopy(self.data.get(key, empty))

    def get_stats(self, d):
        return self._serve(f"stats:{d}", {})

    def get_sleep_data(self, d):
        return self._serve(f"sleep:{d}", {})

    def get_training_readiness(self, d):
        return self._serve(f"readiness:{d}", [])

    def get_hrv_data(self, d):
        return self._serve(f"hrv:{d}", {})

    def get_training_status(self, d):
        return self._serve(f"training_status:{d}", {})

    def query_garmin_graphql(self, query):
        self.queries.append(query)
        return self._serve("schedule", {"data": {"workoutScheduleSummariesScalar": []}})

    def get_workout_by_id(self, workout_id):
        return self._serve(f"workout:{workout_id}", {})

    def get_lactate_threshold(self, *, latest=True, start_date=None, end_date=None, aggregation="daily"):
        assert latest is True and start_date is None
        return self._serve("lactate_threshold", {})

    def get_cycling_ftp(self):
        return self._serve("cycling_ftp", {})

    def connectapi(self, path, params=None):
        if path == self.garmin_connect_activities:
            assert params["startDate"] == params["endDate"], params
            return self._serve(f"activities:{params['startDate']}", [])
        prefix = "workout-service/fbt-adaptive/"
        if path.startswith(prefix):
            return self._serve(f"workout:{path[len(prefix):]}", {})
        raise AssertionError(f"unexpected path {path}")


def build(client=None, **kw):
    kw.setdefault("tz", TZ_NAME)
    kw.setdefault("now", NOW)
    return run(ds.build_daily_status(client or FakeClient(), kw.pop("date", ""), **kw))


# --- rules --------------------------------------------------------------------

class Flags(unittest.TestCase):
    def test_baseline_has_no_flags_and_nothing_missing(self):
        ids, missing, _ = rules()
        self.assertEqual(ids, [])
        self.assertEqual(missing, [])

    def test_low_hrv_at_threshold(self):
        self.assertEqual(rules(recovery(hrv=50, hrv_7d=60))[0], ["low_hrv"])
        self.assertEqual(rules(recovery(hrv=51, hrv_7d=60))[0], [])

    def test_high_rhr_at_threshold(self):
        self.assertEqual(rules(recovery(rhr=53, rhr_7d=48))[0], ["high_rhr"])
        self.assertEqual(rules(recovery(rhr=52, rhr_7d=48))[0], [])

    def test_short_sleep_below_six_hours(self):
        self.assertEqual(rules(recovery(sleep_hours=5.99))[0], ["short_sleep"])
        self.assertEqual(rules(recovery(sleep_hours=6.0))[0], [])

    def test_sleep_debt_supersedes_short_sleep(self):
        ids, _, flags = rules(recovery(sleep_hours=5.99, sleep_hours_prev=5.99))
        self.assertEqual(ids, ["sleep_debt"])
        self.assertEqual(flags[0]["level"], "red")
        self.assertEqual(rules(recovery(sleep_hours=5.99, sleep_hours_prev=6.0))[0], ["short_sleep"])

    def test_short_sleep_still_fires_when_previous_night_missing(self):
        ids, missing, _ = rules(recovery(sleep_hours=4.5, sleep_hours_prev=None))
        self.assertEqual(ids, ["short_sleep"])
        self.assertEqual(missing, ["recovery.sleep_hours_prev"])

    def test_low_readiness_below_forty(self):
        self.assertEqual(rules(recovery(readiness=39))[0], ["low_readiness"])
        self.assertEqual(rules(recovery(readiness=40))[0], [])

    def test_load_spike_from_garmin_status(self):
        for status in ("HIGH", "VERY_HIGH"):
            ids, missing, flags = rules(ld=load(acwr_status=status, acwr=1.0))
            self.assertEqual(ids, ["load_spike"], status)
            self.assertIn("HIGH", flags[0]["label_en"])
            self.assertEqual(missing, [])
        # A status short of HIGH never makes a spike, whatever the ratio.
        self.assertEqual(rules(ld=load(acwr_status="OPTIMAL", acwr=2.0))[0], ["load_rising"])
        self.assertEqual(rules(ld=load(acwr_status="LOW", acwr=0.4))[0], [])

    def test_load_rising_fires_with_a_status_present(self):
        # Garmin always sends a status: the ratio alone decides load_rising.
        self.assertEqual(rules(ld=load(acwr_status="OPTIMAL", acwr=1.31))[0], ["load_rising"])
        self.assertEqual(rules(ld=load(acwr_status="OPTIMAL", acwr=1.3))[0], [])
        ids, missing, flags = rules(ld=load(acwr_status="OPTIMAL", acwr=None))
        self.assertEqual((ids, missing), ([], ["load.acwr"]))
        # A spike suppresses it and then needs no ratio.
        ids, _, flags = rules(ld=load(acwr_status="HIGH", acwr=1.4))
        self.assertEqual((ids, flags[0]["level"]), (["load_spike"], "red"))
        self.assertEqual(rules(ld=load(acwr_status="VERY_HIGH", acwr=None))[:2], (["load_spike"], []))
        self.assertEqual(rules(ld=load(acwr_status="OPTIMAL", acwr=1.4))[2][0]["level"], "yellow")

    def test_ratio_rules_only_when_status_missing(self):
        cases = [(1.3, []), (1.31, ["load_rising"]), (1.5, ["load_rising"]), (1.51, ["load_spike"])]
        for acwr, expected in cases:
            ids, missing, _ = rules(ld=load(acwr_status=None, acwr=acwr))
            self.assertEqual(ids, expected, acwr)
            self.assertEqual(missing, ["load.acwr_status"])

    def test_load_spike_suppresses_load_rising(self):
        ids, _, flags = rules(ld=load(acwr_status=None, acwr=1.8))
        self.assertEqual(ids, ["load_spike"])
        self.assertIn("1.5", flags[0]["label_en"])

    def test_load_rules_skipped_without_status_and_ratio(self):
        ids, missing, _ = rules(ld=load(acwr_status=None, acwr=None))
        self.assertEqual(ids, [])
        self.assertEqual(missing, ["load.acwr_status", "load.acwr"])

    def test_recovery_incomplete_only_on_hard_day(self):
        self.assertEqual(rules(recovery(recovery_hours=6.0), today=[HARD])[0], ["recovery_incomplete"])
        self.assertEqual(rules(recovery(recovery_hours=5.9), today=[HARD])[0], [])
        self.assertEqual(rules(recovery(recovery_hours=20.0), today=[EASY])[0], [])
        self.assertEqual(rules(recovery(recovery_hours=20.0), today=[EASY, HARD])[0], ["recovery_incomplete"])

    def test_completed_workouts_do_not_make_the_day_hard(self):
        self.assertEqual(rules(recovery(recovery_hours=20.0), today=[DONE_HARD])[0], [])
        self.assertEqual(rules(recovery(recovery_hours=20.0), today=[DONE_HARD, EASY])[0], [])
        self.assertEqual(rules(recovery(recovery_hours=20.0), today=[DONE_EASY, HARD])[0], ["recovery_incomplete"])

    def test_recovery_incomplete_skipped_without_input(self):
        ids, missing, _ = rules(recovery(recovery_hours=None), today=[HARD])
        self.assertEqual((ids, missing), ([], ["recovery.recovery_hours"]))
        ids, missing, _ = rules(recovery(recovery_hours=20.0), today=None)
        self.assertEqual((ids, missing), ([], ["today"]))

    def test_hrv_low_trend_needs_three_days_below(self):
        below = [(48, 50), (47, 50), (49, 50)]
        ids, _, flags = rules(recovery(hrv_7d=48), trend=below)
        self.assertEqual(ids, ["hrv_low_trend"])
        self.assertEqual(flags[0]["level"], "red")
        # Equal to balancedLow is not below it.
        self.assertEqual(rules(recovery(hrv_7d=48), trend=[(48, 50), (50, 50), (49, 50)])[0], [])
        # The report day's own value comes from the status fields.
        self.assertEqual(rules(recovery(hrv_7d=55), trend=below)[0], [])

    def test_hrv_low_trend_skipped_when_a_day_is_missing(self):
        ids, missing, _ = rules(recovery(hrv_7d=48), trend=[(48, 50), (47, 50), None])
        self.assertEqual((ids, missing), ([], ["hrv_trend.d-2"]))
        ids, missing, _ = rules(recovery(hrv_7d=48, hrv_range=None), trend=[None, (47, 50), (49, 50)])
        self.assertEqual((ids, missing), ([], ["recovery.hrv_range"]))
        rec = recovery(hrv_7d=48, hrv_range={"low_upper": None, "balanced_low": None, "balanced_upper": None})
        self.assertEqual(rules(rec, trend=[None, (47, 50), (49, 50)])[1], ["recovery.hrv_range.balanced_low"])

    def test_rule_with_missing_input_is_skipped_not_counted(self):
        ids, missing, _ = rules(recovery(rhr=None, hrv=None, readiness=None))
        self.assertEqual(ids, [])
        self.assertEqual(missing, ["recovery.hrv", "recovery.rhr", "recovery.readiness"])

    def test_labels_state_thresholds(self):
        expected = {
            "low_hrv": "10", "hrv_low_trend": "3", "high_rhr": "5", "short_sleep": "6", "sleep_debt": "6",
            "low_readiness": "40", "load_rising": "1.3", "recovery_incomplete": "6",
        }
        for flag_id, number in expected.items():
            self.assertIn(number, ds.flag_label(flag_id), flag_id)
        self.assertEqual(ds.flag_label("short_sleep"), "Short sleep: under 6 h")
        self.assertEqual(ds.flag_label("low_readiness"), "Low readiness: Training Readiness below 40")
        self.assertEqual(ds.flag_label("load_spike", by_status=True), "Load spike: ACWR status HIGH or VERY_HIGH")
        self.assertEqual(ds.flag_label("recovery_incomplete"),
                         "Recovery not finished: 6 h or more on a hard or long day")
        self.assertIn("1.5", ds.flag_label("load_spike"))


class LevelAndRecommendation(unittest.TestCase):
    def test_level_counting(self):
        rec = recovery()
        self.assertEqual(ds.level_of([], rec), "green")
        self.assertEqual(ds.level_of([flag("short_sleep")], rec), "yellow")
        self.assertEqual(ds.level_of([flag("short_sleep"), flag("low_hrv")], rec), "yellow")
        self.assertEqual(ds.level_of([flag("short_sleep"), flag("low_hrv"), flag("high_rhr")], rec), "red")
        self.assertEqual(ds.level_of([flag("sleep_debt")], rec), "red")

    def test_incomplete_only_without_sleep_and_readiness(self):
        empty = recovery(sleep_hours=None, readiness=None)
        self.assertEqual(ds.level_of([], empty), "incomplete")
        self.assertEqual(ds.level_of([flag("high_rhr")], empty), "incomplete")
        self.assertEqual(ds.level_of([flag("load_spike")], empty), "red")
        self.assertEqual(ds.level_of([], recovery(sleep_hours=None)), "green")
        self.assertEqual(ds.level_of([], recovery(readiness=None)), "green")

    def test_recommendation_table(self):
        y1 = [flag("short_sleep")]
        y2 = [flag("short_sleep"), flag("low_hrv")]
        y3 = [flag("short_sleep"), flag("low_hrv"), flag("high_rhr")]
        red = [flag("sleep_debt")]
        cases = [
            ("green", [], [HARD], "as_planned"),
            ("green", [], [EASY], "as_planned"),
            ("yellow", y1, [HARD], "as_planned_may_stop"),
            ("yellow", y1, [EASY], "as_planned"),
            ("yellow", y2, [EASY, HARD], "zone2_or_shorten"),
            ("yellow", y2, [EASY], "as_planned"),
            ("red", red, [HARD], "short_zone2_or_rest"),
            ("red", red, [EASY], "shorten"),
            ("red", y3, [HARD], "short_zone2_or_rest"),
            ("red", y3, [EASY, EASY], "shorten"),
            ("red", red, [], "no_planned_workout"),
            ("incomplete", [], [], "no_planned_workout"),
            ("incomplete", [], [HARD], "no_recovery_data"),
            # Calendar unreadable: the cautious hard-day column.
            ("yellow", y1, None, "as_planned_may_stop"),
            ("red", red, None, "short_zone2_or_rest"),
            # Everything planned is done, whatever the level.
            ("green", [], [DONE_EASY], "done_today"),
            ("red", red, [DONE_HARD], "done_today"),
            ("incomplete", [], [DONE_HARD, DONE_EASY], "done_today"),
            # Only the workouts still ahead decide the column.
            ("yellow", y1, [DONE_HARD, EASY], "as_planned"),
            ("yellow", y2, [DONE_EASY, HARD], "zone2_or_shorten"),
            ("red", red, [DONE_HARD, EASY], "shorten"),
            ("incomplete", [], [DONE_HARD, EASY], "no_recovery_data"),
        ]
        for level, flags, today, expected in cases:
            with self.subTest(level=level, flags=len(flags), today=today):
                self.assertEqual(ds.recommend(level, flags, today), expected)
                self.assertIn(expected, ds.RECOMMENDATION_EN)

    def test_english_wording(self):
        self.assertEqual(ds.RECOMMENDATION_EN["no_planned_workout"], "No workout planned today.")
        self.assertEqual(ds.recommendation_en("done_today", [DONE_EASY]), "Today's workout is already done.")
        self.assertEqual(ds.recommendation_en("done_today", [DONE_EASY, DONE_HARD]), "Today's workouts are already done.")
        self.assertEqual(ds.recommendation_en("as_planned", [EASY, HARD]), "Train as planned.")
        self.assertEqual(ds.recommendation_en("as_planned_may_stop", [EASY]),
                         "Train as planned; stop if it doesn't feel right.")
        self.assertEqual(ds.LEVEL_EN, {"green": "Green", "yellow": "Yellow", "red": "Red", "incomplete": "Incomplete data"})


# --- extraction ---------------------------------------------------------------

def snapshot(ts, minutes, score=52, phrase=None, date=DAY):
    return {"calendarDate": date, "timestampLocal": ts, "score": score, "level": "MODERATE",
            "recoveryTime": minutes, "recoveryTimeChangePhrase": phrase, "acuteLoad": 600}


def readiness(payload, now=NOW, is_today=True):
    return ds.extract_readiness(payload, DAY, zone=TZ, now=now, is_today=is_today)


class RecoveryHours(unittest.TestCase):
    def test_reached_zero_is_zero(self):
        out = readiness([snapshot("2026-03-18T05:58:41.0", 900, phrase="REACHED_ZERO")])
        self.assertEqual(out["recovery_hours"], 0.0)

    def test_reached_zero_after_exercise_is_the_new_recovery(self):
        # Wake-up row with a minute left; the workout then resets the clock.
        # Garmin marks the post-exercise row REACHED_ZERO (the old clock ran
        # out) while its recoveryTime is the new clock: 690 min = 11.5 h.
        wake = dict(snapshot("2026-03-18T05:00:00.0", 1, score=60, phrase="NO_CHANGE_SLEEP"),
                    inputContext=ds.WAKEUP_CONTEXT)
        post = dict(snapshot("2026-03-18T06:50:00.0", 690, score=48, phrase="REACHED_ZERO"),
                    inputContext=ds.POST_EXERCISE_CONTEXT)
        now = dt.datetime(2026, 3, 18, 7, 30, tzinfo=TZ)        # 40 min after the workout row
        today = readiness([wake, post], now=now)
        self.assertEqual((today["readiness"], today["recovery_hours"]), (60, 10.8))
        self.assertEqual(readiness([wake, post], is_today=False)["recovery_hours"], 11.5)
        # REACHED_ZERO on any other row still reads as 0 (upstream's meaning).
        wake_zero = dict(snapshot("2026-03-18T05:00:00.0", 690, phrase="REACHED_ZERO"), inputContext=ds.WAKEUP_CONTEXT)
        self.assertEqual(readiness([wake_zero], is_today=False)["recovery_hours"], 0.0)

    def test_elapsed_time_subtracted_for_today(self):
        # Snapshot 05:58:41 local, 9 h left; three hours later 6 h are left.
        # Reading the naive timestamp as UTC would be two hours off.
        now = dt.datetime(2026, 3, 18, 8, 58, 41, tzinfo=TZ)
        self.assertEqual(readiness([snapshot("2026-03-18T05:58:41.0", 540)], now=now)["recovery_hours"], 6.0)

    def test_naive_now_is_read_in_the_report_zone(self):
        out = run(ds.build_daily_status(FakeClient(), DAY, tz=TZ_NAME, now=dt.datetime(2026, 3, 18, 8, 58, 41)))
        self.assertEqual(out["recovery"]["recovery_hours"], 6.0)

    def test_no_subtraction_for_another_day_and_floor_zero(self):
        snap = [snapshot("2026-03-18T05:58:41.0", 540)]
        self.assertEqual(readiness(snap, is_today=False)["recovery_hours"], 9.0)
        late = dt.datetime(2026, 3, 18, 20, 0, tzinfo=TZ)
        self.assertEqual(readiness(snap, now=late)["recovery_hours"], 0.0)

    def test_newest_snapshot_wins(self):
        snaps = [snapshot("2026-03-18T05:58:41.0", 540, score=52), snapshot("2026-03-18T10:20:00.0", 900, score=44)]
        now = dt.datetime(2026, 3, 18, 10, 20, tzinfo=TZ)
        out = readiness(snaps, now=now)
        self.assertEqual((out["readiness"], out["recovery_hours"]), (44, 15.0))

    def test_elapsed_time_from_the_gmt_timestamp(self):
        # A watch on another clock (here nine hours ahead of GMT): timestampLocal read in the
        # report's zone would be seven hours off; the GMT timestamp is not.
        snap = snapshot("2026-03-18T12:58:41.0", 540)
        snap["timestamp"] = "2026-03-18T03:58:41.0"             # 05:58:41 in the report's zone
        now = dt.datetime(2026, 3, 18, 8, 58, 41, tzinfo=TZ)
        self.assertEqual(readiness([snap], now=now)["recovery_hours"], 6.0)
        self.assertEqual(ds.snapshot_time(snap, TZ), dt.datetime(2026, 3, 18, 3, 58, 41, tzinfo=dt.timezone.utc))
        # Epoch milliseconds work too; without a GMT value the local one is read in the zone.
        snap["timestamp"] = 1773806321000
        self.assertEqual(readiness([snap], now=now)["recovery_hours"], 6.0)
        del snap["timestamp"]
        self.assertEqual(ds.snapshot_time(snap, TZ), dt.datetime(2026, 3, 18, 12, 58, 41, tzinfo=TZ))
        self.assertIsNone(ds.snapshot_time({"timestamp": "x", "timestampLocal": None}, TZ))
        # Newest first goes by GMT as well.
        early = dict(snapshot("2026-03-18T23:00:00.0", 600, score=61), timestamp="2026-03-18T02:00:00.0")
        late = dict(snapshot("2026-03-18T06:00:00.0", 300, score=47), timestamp="2026-03-18T04:00:00.0")
        self.assertEqual(readiness([early, late], is_today=False)["readiness"], 47)

    def test_rows_of_the_primary_training_device_win(self):
        watch = dict(snapshot("2026-03-18T05:58:41.0", 540, score=52), deviceId=990001)
        other = dict(snapshot("2026-03-18T09:30:00.0", 120, score=77), deviceId=990002)

        def pick(rows, device_id):
            return ds.extract_readiness(rows, DAY, zone=TZ, now=NOW, is_today=False, device_id=device_id)["readiness"]

        self.assertEqual(pick([watch, other], "990001"), 52)     # not the newest row, but the primary's
        self.assertEqual(pick([watch, other], "990002"), 77)
        self.assertEqual(pick([watch, other], None), 77)         # primary unknown: newest row
        self.assertEqual(pick([watch, other], "990003"), 77)     # primary has no rows that day
        plain = [snapshot("2026-03-18T05:58:41.0", 540, score=52), snapshot("2026-03-18T09:30:00.0", 120, score=77)]
        self.assertEqual(pick(plain, "990001"), 77)              # rows carry no device id
        records = {"990002": {"acuteTrainingLoadDTO": {}},
                   "990001": {"primaryTrainingDevice": True, "deviceId": 990001, "acuteTrainingLoadDTO": {}}}
        payload = {"mostRecentTrainingStatus": {"latestTrainingStatusData": records}}
        self.assertEqual(ds.primary_device_id(payload), "990001")
        del records["990001"]["deviceId"]
        self.assertEqual(ds.primary_device_id(payload), "990001")    # the map key is the device id
        del records["990001"]["primaryTrainingDevice"]
        self.assertIsNone(ds.primary_device_id(payload))             # no device marked primary
        self.assertIsNone(ds.primary_device_id("x"))

    def test_snapshots_of_other_days_or_without_score_ignored(self):
        self.assertEqual(readiness([snapshot("2026-03-17T06:00:00.0", 540, date=PREV)])["readiness"], None)
        self.assertEqual(readiness([snapshot("2026-03-18T06:00:00.0", 540, score=None)])["readiness"], None)
        self.assertEqual(readiness({})["recovery_hours"], None)

    def test_fixture_values(self):
        out = readiness(fixture("readiness"), is_today=False)
        self.assertEqual(out, {"readiness": 52, "readiness_level": "MODERATE", "recovery_hours": 9.0, "acute_load": 610})

    def test_fixture_is_a_wake_up_row(self):
        self.assertEqual(fixture("readiness")[0]["inputContext"], ds.WAKEUP_CONTEXT)

    @unittest.skipUnless(HAVE_UPSTREAM, "needs upstream garmin_mcp")
    def test_fallback_matches_upstream(self):
        from garmin_mcp.health_wellness import _hours_from_recovery_minutes as upstream
        for args in ((90, None), (0, None), (-5, None), (None, None), (True, None), (899, "REACHED_ZERO"), (61, "X")):
            self.assertEqual(ds._hours_from_minutes_fallback(*args), upstream(*args), args)


def row(ts, context, minutes=300, score=52, sleep_score=74, phrase=None):
    """A readiness row with Garmin's inputContext; sleep_score=None = not scored yet."""
    out = dict(snapshot(ts, minutes, score=score, phrase=phrase), inputContext=context)
    if sleep_score is not None:
        out["sleepScore"] = sleep_score
    return out


PRE_SLEEP = row("2026-03-18T00:13:00.0", ds.REALTIME_CONTEXT, minutes=0, score=41, sleep_score=None)
WAKE = row("2026-03-18T06:10:00.0", ds.WAKEUP_CONTEXT, minutes=240, score=63)


class MorningReadiness(unittest.TestCase):
    """readiness = the morning picture; the recovery clock = the newest row."""

    def test_pre_sleep_row_alone_is_no_readiness_yet(self):
        out = readiness([PRE_SLEEP], is_today=False)
        self.assertEqual((out["readiness"], out["readiness_level"]), (None, None))
        self.assertEqual(out["recovery_hours"], 0.0)        # the clock still comes from the newest row
        self.assertIsNone(ds.morning_snapshot([PRE_SLEEP]))

    def test_wake_up_row_gives_the_score(self):
        out = readiness([PRE_SLEEP, WAKE], is_today=False)
        self.assertEqual((out["readiness"], out["readiness_level"], out["recovery_hours"]), (63, "MODERATE", 4.0))

    def test_later_post_exercise_row_keeps_the_morning_score(self):
        post = row("2026-03-18T09:40:00.0", ds.POST_EXERCISE_CONTEXT, minutes=900, score=38)
        post["acuteLoad"] = 720
        now = dt.datetime(2026, 3, 18, 10, 40, tzinfo=TZ)
        out = readiness([post, WAKE, PRE_SLEEP], now=now)
        self.assertEqual((out["readiness"], out["readiness_level"]), (63, "MODERATE"))       # wake-up row
        self.assertEqual((out["recovery_hours"], out["acute_load"]), (14.0, 720))            # newest row

    def test_post_exercise_rows_only_use_the_newest(self):
        early = row("2026-03-18T07:00:00.0", ds.POST_EXERCISE_CONTEXT, minutes=600, score=55)
        late = row("2026-03-18T11:00:00.0", ds.POST_EXERCISE_CONTEXT, minutes=480, score=47)
        out = readiness([early, late, PRE_SLEEP], is_today=False)
        self.assertEqual((out["readiness"], out["recovery_hours"]), (47, 8.0))

    def test_scored_realtime_row_counts_before_the_wake_up_row(self):
        scored = row("2026-03-18T05:20:00.0", ds.REALTIME_CONTEXT, score=57)
        self.assertEqual(readiness([PRE_SLEEP, scored], is_today=False)["readiness"], 57)
        # Once the wake-up row exists, a later realtime row does not replace it.
        evening = row("2026-03-18T21:00:00.0", ds.REALTIME_CONTEXT, score=35)
        self.assertEqual(readiness([evening, WAKE, scored], is_today=False)["readiness"], 63)

    def test_full_status_before_sync_is_incomplete(self):
        data = default_data()
        data[f"sleep:{DAY}"] = fixture("sleep_shell")
        data[f"readiness:{DAY}"] = [PRE_SLEEP]
        out = build(FakeClient(data))
        self.assertIsNone(out["recovery"]["readiness"])
        self.assertIn("recovery.readiness", out["missing"])
        self.assertEqual(out["level"], "incomplete")
        data[f"readiness:{DAY}"] = [PRE_SLEEP, WAKE]
        self.assertEqual(build(FakeClient(data))["recovery"]["readiness"], 63)


class Sleep(unittest.TestCase):
    def test_fixture_night(self):
        out = ds.extract_sleep(fixture("sleep_night"), DAY, TZ)
        self.assertEqual(out["sleep_hours"], 6.25)
        self.assertEqual(out["sleep_hhmm"], "6:15")
        self.assertEqual((out["sleep_score"], out["rem_min"], out["deep_min"], out["awake_min"]), (74, 80, 70, 26))
        self.assertEqual(out["sleep_end_local"], "05:52")
        self.assertEqual((out["spo2_avg"], out["spo2_min"], out["resp_sleep_avg"]), (95, 88, 13.6))

    def test_end_time_from_gmt_when_local_missing(self):
        data = fixture("sleep_night")
        del data["dailySleepDTO"]["sleepEndTimestampLocal"]
        self.assertEqual(ds.extract_sleep(data, DAY, TZ)["sleep_end_local"], "05:52")

    def test_shell_and_other_night_are_not_this_night(self):
        self.assertIsNone(ds.night(fixture("sleep_shell"), DAY))
        self.assertIsNone(ds.night(fixture("sleep_prev_night"), DAY))
        self.assertTrue(all(v is None for v in ds.extract_sleep(fixture("sleep_shell"), DAY, TZ).values()))

    def test_one_digit_fraction_timestamp(self):
        self.assertEqual(ds._parse_local_ts("2026-03-18T05:58:41.0"), dt.datetime(2026, 3, 18, 5, 58, 41))
        self.assertIsNone(ds._parse_local_ts("not a time"))


class OtherExtraction(unittest.TestCase):
    def test_sessions_none_empty_and_sorted(self):
        self.assertIsNone(ds.extract_sessions(None))
        self.assertEqual(ds.extract_sessions({}), [])
        sessions = ds.extract_sessions(fixture("activities_prev"))
        self.assertEqual([s["type"] for s in sessions], ["strength_training", "virtual_ride"])
        ride = sessions[1]
        self.assertEqual(
            (ride["sport"], ride["minutes"], ride["km"], ride["avg_hr"], ride["max_hr"], ride["load"],
             ride["aerobic_te"], ride["anaerobic_te"]),
            ("bike", 67, 33.6, 128, 151, 92, 3.0, 0.7),
        )

    def test_athlete_thresholds_from_payloads(self):
        athlete = ds.athlete_thresholds(fixture("lactate_threshold"), fixture("cycling_ftp"))
        self.assertAlmostEqual(athlete["lt_speed_mps"], 3.7)    # raw 0.37 cm/ms
        self.assertEqual((athlete["lt_hr"], athlete["ftp_w"]), (172, 240))
        self.assertEqual(ds.athlete_out(athlete), {"lt_pace_min_km": "4:30", "lt_hr": 172, "ftp_w": 240})
        self.assertEqual(ds.athlete_thresholds(None, [fixture("cycling_ftp")])["ftp_w"], 240)
        self.assertEqual(ds.athlete_thresholds("x", 7), {"lt_speed_mps": None, "lt_hr": None, "ftp_w": None})
        self.assertEqual(ds.athlete_out(None), {"lt_pace_min_km": None, "lt_hr": None, "ftp_w": None})
        # A value already in m/s (x10 = 37 m/s) means the unit changed: unknown, not misjudged.
        changed = {"speed_and_heart_rate": {"speed": 3.7, "heartRate": 172}}
        self.assertIsNone(ds.athlete_thresholds(changed, None)["lt_speed_mps"])
        self.assertEqual(ds.pace_min_km(4.0), "4:10")
        self.assertIsNone(ds.pace_min_km(0))

    @unittest.skipUnless(HAVE_UPSTREAM, "needs upstream garmin_mcp")
    def test_lt_speed_fallback_matches_upstream(self):
        from garmin_mcp.training import _lactate_threshold_speed_mps as upstream
        for raw in (0.37, 0.4, 0.29):
            self.assertAlmostEqual(ds._lt_speed_fallback(raw), upstream(raw))
        for raw in (None, 0):
            self.assertEqual(ds._lt_speed_fallback(raw), upstream(raw))

    def test_completion_matching(self):
        today_runs = fixture("activities_today")                       # one run, no ids
        run = ds.classify_workout(entry("Run"), None)
        ride = ds.classify_workout(entry("Ride", "cycling"), None)
        cases = [
            # (workouts as (entry, classified), activities, expected)
            ([(entry("Run"), run)], today_runs, [True]),
            ([(entry("Ride", "cycling"), ride)], today_runs, [False]),               # other sport
            ([(entry("Run"), run), (entry("Run 2"), dict(run))], today_runs, [True, False]),  # one each
            ([(entry("Run", associatedActivityId=5), run)], [], [True]),              # linked by Garmin
            ([(entry("Run"), run)], [], [False]),
        ]
        for pairs, payload, expected in cases:
            with self.subTest(expected=expected):
                workouts = [copy.deepcopy(w) for _, w in pairs]
                ds.mark_completed(workouts, [e for e, _ in pairs], ds.completion_activities(copy.deepcopy(payload)))
                self.assertEqual([w["completed"] for w in workouts], expected)
        # A linked activity is not used again for another workout.
        linked = [entry("Run", associatedActivityId=5), entry("Run 2")]
        workouts = [dict(run), dict(run)]
        ds.mark_completed(workouts, linked, [{"date": DAY, "sport": "run", "id": 5}])
        self.assertEqual([w["completed"] for w in workouts], [True, False])
        # Activities without ids still pair one to one; another date does not count.
        two = fixture("activities_today") * 2
        acts = ds.completion_activities(two)
        self.assertEqual([a["id"] for a in acts], ["#0", "#1"])
        workouts = [dict(run), dict(run)]
        ds.mark_completed(workouts, [entry("Run"), entry("Run 2")], acts)
        self.assertEqual([w["completed"] for w in workouts], [True, True])
        workouts = [dict(run)]
        ds.mark_completed(workouts, [entry("Run", date=NEXT)], acts)
        self.assertEqual(workouts[0]["completed"], False)
        self.assertEqual(ds.completion_activities({}), [])

    def test_other_sports_complete_by_their_own_type(self):
        def done(workout_type, activity_type):
            planned_entry = entry("Session", workout_type)
            workouts = [ds.classify_workout(planned_entry, None)]
            acts = ds.completion_activities([{"startTimeLocal": f"{DAY} 07:00:00", "activityId": 8880009,
                                              "activityType": {"typeKey": activity_type}}])
            ds.mark_completed(workouts, [planned_entry], acts)
            return workouts[0]["completed"]

        self.assertFalse(done("strength_training", "walking"))      # both "other", different things
        self.assertTrue(done("strength_training", "strength_training"))
        self.assertTrue(done("Strength_Training", "strength_training"))
        self.assertFalse(done("yoga", "pilates"))
        # run / bike / swim still pair by sport group.
        self.assertTrue(done("running", "treadmill_running"))
        self.assertTrue(done("cycling", "virtual_ride"))
        self.assertTrue(done("swimming", "open_water_swimming"))
        self.assertFalse(done("running", "walking"))
        self.assertEqual(ds.completion_sport(None), "other")
        self.assertEqual(ds.completion_sport("lap_swimming"), "swim")

    def test_training_device_choice(self):
        latest = {"1": {"acuteTrainingLoadDTO": {"acwrStatus": "LOW"}},
                  "2": {"primaryTrainingDevice": True, "acuteTrainingLoadDTO": {"acwrStatus": "HIGH"}}}
        self.assertEqual(ds.pick_training_device(latest)["acuteTrainingLoadDTO"]["acwrStatus"], "HIGH")
        del latest["2"]["primaryTrainingDevice"]
        self.assertEqual(ds.pick_training_device(latest)["acuteTrainingLoadDTO"]["acwrStatus"], "LOW")
        self.assertEqual(ds.pick_training_device(None), {})

    def test_load_fixture(self):
        self.assertEqual(ds.extract_load(fixture("training_status")),
                         {"acute": 640, "chronic": 582, "acwr": 1.1, "acwr_status": "OPTIMAL", "acwr_percent": 44})

    def test_load_acwr_from_loads_not_garmins_cut_ratio(self):
        latest = {"1": {"primaryTrainingDevice": True, "acuteTrainingLoadDTO": {
            "dailyTrainingLoadAcute": 1390, "dailyTrainingLoadChronic": 1000,
            "dailyAcuteChronicWorkloadRatio": 1.3, "acwrStatus": "OPTIMAL"}}}
        load = ds.extract_load({"mostRecentTrainingStatus": {"latestTrainingStatusData": latest}})
        self.assertEqual(load["acwr"], 1.39)
        ids = [f["id"] for f in ds.evaluate_rules({}, load, [])[0]]
        self.assertIn("load_rising", ids)

    def test_schedule_entries(self):
        self.assertIsNone(ds.schedule_entries({"data": None, "errors": [{"message": "x"}]}))
        self.assertEqual(ds.schedule_entries({"data": {"workoutScheduleSummariesScalar": None}}), [])
        rest = {"data": {"workoutScheduleSummariesScalar": [entry(isRestDay=True), entry("Run")]}}
        self.assertEqual([e["workoutName"] for e in ds.schedule_entries(rest)], ["Run"])

    def test_key_paths_mask_device_ids(self):
        raw = {"latestTrainingStatusData": {"3141592653": {"acwr": 1}}, "list": [{"x": 1}]}
        paths = ds.safe_key_paths(raw)
        self.assertIn("latestTrainingStatusData.<id>", paths)
        self.assertNotIn("3141592653", " ".join(paths))
        self.assertIn("list[].x", paths)


# --- workout classes ----------------------------------------------------------

class WorkoutClass(unittest.TestCase):
    def test_primary_zone_4_is_hard(self):
        w = classify([step("warmup"), step("interval", target="heart.rate.zone", zone=4)], secs=3000)
        self.assertEqual((w["class"], w["hard"], w["long"], w["structured"]), ("hard_or_long", True, False, True))
        self.assertIn("HR zone 4", w["reason"])
        self.assertEqual(classify([step("interval", target="heart.rate.zone", zone=3)], secs=3000)["class"], "easy")

    def test_secondary_zone_4_is_hard(self):
        w = classify([step("interval", target="power.zone", zone=2, sec="heart.rate.zone", sec_zone=4)],
                     sport="cycling", secs=3600)
        self.assertTrue(w["hard"])
        self.assertIn("secondary HR zone 4", w["reason"])

    def test_power_and_pace_zones_count(self):
        self.assertTrue(classify([step("interval", target="power.zone", zone=5)], sport="cycling", secs=3600)["hard"])
        self.assertTrue(classify([step("other", target="pace.zone", zone=4)], secs=1800)["hard"])

    def test_recovery_and_rest_steps_never_count(self):
        steps = [step("recovery", target="heart.rate.zone", zone=5), step("rest", target="pace.zone", zone=4),
                 repeat(4, step("recovery", target="pace.zone", low=3.6, high=3.8))]
        self.assertFalse(classify(steps, secs=1800, athlete=ATHLETE)["hard"])
        self.assertFalse(classify(steps, secs=1800)["hard"])

    def test_pace_range_against_threshold_speed(self):
        # Threshold 3.7 m/s: hard from a midpoint of 0.90 x 3.7 = 3.33 m/s.
        self.assertEqual(ds.THRESHOLDS["hard_pace_of_lt_speed"], 0.90)
        at = classify([step("interval", target="pace.zone", low=3.26, high=3.40)], secs=1800, athlete=ATHLETE)
        self.assertEqual((at["class"], at["hard"]), ("hard_or_long", True))
        self.assertIn("interval step pace range at 90% of threshold speed", at["reason"])
        below = classify([step("interval", target="speed.zone", low=3.2, high=3.4)], secs=1800, athlete=ATHLETE)
        self.assertFalse(below["hard"])
        self.assertIn("highest pace range at 89% of threshold speed", below["reason"])
        # The TrainingPeaks shape: one easy interval step with a pace range.
        easy = classify([step("interval", target="pace.zone", low=2.9, high=3.1)], secs=3000, athlete=ATHLETE)
        self.assertEqual(easy["class"], "easy")
        self.assertIn("81% of threshold speed", easy["reason"])

    def test_power_range_against_ftp(self):
        # FTP 240 W: hard from a midpoint of 0.91 x 240 = 218.4 W (rides).
        at = classify([step("interval", target="power.between", low=210.0, high=226.8)], sport="cycling",
                      secs=3600, athlete=ATHLETE)
        self.assertTrue(at["hard"])
        self.assertIn("power range at 91% of FTP", at["reason"])
        below = classify([step("interval", target="power.between", low=200, high=230)], sport="cycling",
                         secs=3600, athlete=ATHLETE)
        self.assertFalse(below["hard"])
        # A range in % FTP is compared as it is, without fetching FTP.
        pct = [step("other", target="power.zone", low=90, high=95, unit="percent")]
        self.assertTrue(classify(pct, sport="cycling", secs=3600)["hard"])
        self.assertFalse(classify([step("other", target="power.zone", low=85, high=95, unit="percent")],
                                  sport="cycling", secs=3600)["hard"])
        # Running power in watts: cycling FTP does not apply, the repeat rule decides.
        run_power = step("interval", target="power.zone", low=300, high=340)
        self.assertFalse(classify([run_power], secs=1800, athlete=ATHLETE)["hard"])
        repeated = classify([repeat(3, run_power)], secs=1800, athlete=ATHLETE)
        self.assertTrue(repeated["hard"])
        self.assertIn("inside a repeat (no threshold)", repeated["reason"])

    def test_hr_range_against_lthr(self):
        # LTHR 172: hard from a midpoint of 0.95 x 172 = 163.4 bpm.
        at = classify([step("interval", target="heart.rate.zone", low=160, high=166.8)], secs=1800, athlete=ATHLETE)
        self.assertTrue(at["hard"])
        self.assertIn("HR range at 95% of threshold HR", at["reason"])
        self.assertFalse(classify([step("interval", target="heart.rate.zone", low=150, high=160)], secs=1800,
                                  athlete=ATHLETE)["hard"])
        secondary = classify([step("interval", target="pace.zone", zone=2, sec="heart.rate.zone", sec_low=165,
                                   sec_high=175)], sport="cycling", secs=1800, athlete=ATHLETE)
        self.assertIn("secondary HR range", secondary["reason"])

    def test_without_thresholds_only_repeated_intervals_count(self):
        pace = dict(target="pace.zone", low=3.6, high=3.8)
        self.assertFalse(classify([step("interval", **pace)], secs=1800)["hard"])
        self.assertFalse(classify([repeat(1, step("interval", **pace))], secs=1800)["hard"])
        w = classify([step("warmup"), repeat(2, step("interval", **pace), step("recovery"))], secs=1800)
        self.assertTrue(w["hard"])
        self.assertIn("interval step pace range inside a repeat (no threshold)", w["reason"])
        # Known threshold but not this one (only FTP known): same fallback.
        self.assertTrue(classify([repeat(2, step("interval", **pace))], secs=1800, athlete={"ftp_w": 240})["hard"])

    def test_swim_pace_always_uses_the_repeat_rule(self):
        swim = dict(target="pace.zone", low=1.2, high=1.4)
        self.assertFalse(classify([step("interval", "distance", 400.0, **swim)], sport="lap_swimming",
                                  secs=1800, athlete=ATHLETE)["hard"])
        w = classify([repeat(8, step("interval", "distance", 200.0, **swim))], sport="lap_swimming", secs=1800,
                     athlete=ATHLETE)
        self.assertTrue(w["hard"])
        self.assertIn("(swim pace)", w["reason"])
        self.assertIn("hard work 1600 m", w["reason"])

    def test_more_target_types_carry_ranges_and_zones(self):
        # TrainingPeaks rides arrive as power.3s with watt ranges (FTP 240 W).
        for key in ("power.3s", "power.10s", "power.30s", "power.lap", "power.between", "power.zone"):
            with self.subTest(key=key):
                hard = classify([step("interval", target=key, low=225, high=245)], sport="cycling", secs=3600,
                                athlete=ATHLETE)
                self.assertTrue(hard["hard"])
                self.assertIn("power range at 98% of FTP", hard["reason"])
                self.assertFalse(classify([step("interval", target=key, low=150, high=170)], sport="cycling",
                                          secs=3600, athlete=ATHLETE)["hard"])
                self.assertEqual(ds.thresholds_needed([("bike", curated(step("interval", target=key, low=1, high=2)))]),
                                 ["ftp_w"])
                self.assertTrue(classify([step("interval", target=key, zone=4)], sport="cycling", secs=3600)["hard"])
        lap_hr = classify([step("interval", target="heart.rate.lap", low=165, high=175)], secs=1800, athlete=ATHLETE)
        self.assertIn("HR range at 99% of threshold HR", lap_hr["reason"])
        lap_speed = classify([step("interval", target="speed.lap", low=3.5, high=3.7)], secs=1800, athlete=ATHLETE)
        self.assertIn("pace range at 97% of threshold speed", lap_speed["reason"])
        # Cadence says nothing about intensity.
        self.assertFalse(classify([repeat(4, step("interval", target="cadence.zone", low=170, high=180))],
                                  secs=1800, athlete=ATHLETE)["hard"])

    def test_strides_do_not_make_a_run_hard(self):
        fast = dict(target="pace.zone", low=4.4, high=4.8)             # well above threshold speed
        self.assertEqual((ds.THRESHOLDS["hard_min_work_s"], ds.THRESHOLDS["hard_min_work_m"]), (300, 1000))
        strides = classify([step("interval", value=1800.0, target="pace.zone", low=2.9, high=3.1),
                            repeat(6, step("interval", value=20.0, **fast), step("rest", value=60.0))],
                           secs=2400, athlete=ATHLETE)
        self.assertEqual((strides["class"], strides["hard"]), ("easy", False))
        self.assertIn("but only 120 s of hard work (under 300 s and 1000 m)", strides["reason"])
        # Without a threshold the repeat rule qualifies the same strides: still too little work.
        self.assertFalse(classify([repeat(6, step("interval", value=20.0, **fast))], secs=2400)["hard"])
        cases = [
            ([repeat(5, step("interval", value=60.0, **fast))], True, "hard work 300 s"),
            ([repeat(4, step("interval", value=74.0, **fast))], False, "only 296 s"),
            ([repeat(10, step("interval", "distance", 100.0, **fast))], True, "hard work 1000 m"),
            ([repeat(4, step("interval", "distance", 200.0, **fast))], False, "only 800 m"),
            # Seconds and metres are counted apart: neither total reaches its minimum.
            ([step("interval", value=200.0, **fast), step("interval", "distance", 800.0, **fast)], False,
             "only 200 s + 800 m"),
            ([step("interval", value=200.0, **fast), step("interval", "distance", 1000.0, **fast)], True,
             "hard work 200 s + 1000 m"),
            # A lap-button step has no length: it counts as enough.
            ([step("interval", "lap.button", None, **fast)], True, "hard work an open-ended step"),
            ([step("interval", value=30.0, target="heart.rate.zone", zone=5)], False, "only 30 s"),
            ([step("interval", value=300.0, target="heart.rate.zone", zone=5)], True, "HR zone 5; hard work 300 s"),
        ]
        for steps, expected, words in cases:
            with self.subTest(words=words):
                w = classify(steps, secs=2400, athlete=ATHLETE)
                self.assertEqual(w["hard"], expected)
                self.assertIn(words, w["reason"])

    def test_duration_from_steps_only_when_every_step_is_timed(self):
        # A swim of distance repeats with timed rests: the rests are not the duration.
        swim_steps = [step("warmup", "distance", 300.0),
                      repeat(8, step("interval", "distance", 50.0), step("rest", value=20.0)),
                      step("cooldown", "distance", 200.0)]
        swim = classify(swim_steps, sport="lap_swimming", meters=3600.0)
        self.assertEqual((swim["minutes"], swim["km"], swim["long"], swim["class"]), (None, 3.6, True, "hard_or_long"))
        self.assertIn("3.6 km", swim["reason"])
        short = classify(swim_steps, sport="lap_swimming", meters=2000.0)
        self.assertEqual((short["minutes"], short["long"]), (None, False))
        # A lap-button step has no length either.
        self.assertIsNone(classify([step("interval", value=3000.0), step("cooldown", "lap.button", None)])["minutes"])
        self.assertEqual(classify([step("warmup", value=600.0), step("interval", value=1200.0)])["minutes"], 30)
        # Garmin's own estimate still wins when it is there.
        self.assertEqual(classify(swim_steps, sport="lap_swimming", secs=2400, meters=2000.0)["minutes"], 40)
        # The text leaves the unknown duration out.
        text = ds._workout_txt(dict(short, name="Swim"), 6)
        self.assertEqual(text, "Swim (swim, 2 km, easy)")

    def test_thresholds_needed(self):
        def needed(sport, *steps):
            return ds.thresholds_needed([(sport, curated(*steps))])
        self.assertEqual(needed("run", step("interval", target="pace.zone", low=3, high=3.2)), ["lt_speed_mps"])
        self.assertEqual(needed("bike", step("interval", target="heart.rate.zone", low=140, high=150)), ["lt_hr"])
        self.assertEqual(needed("bike", step("interval", target="power.between", low=200, high=220)), ["ftp_w"])
        for sport, s in (("bike", step("interval", target="power.zone", low=90, high=95, unit="percent")),
                         ("swim", step("interval", target="pace.zone", low=1.2, high=1.4)),
                         ("run", step("interval", target="power.zone", low=300, high=340)),
                         ("run", step("interval", target="heart.rate.zone", zone=4)),
                         ("run", step("warmup", target="pace.zone", low=2.5, high=2.7))):
            self.assertEqual(needed(sport, s), [], (sport, s))
        both = ds.thresholds_needed([
            ("bike", curated(step("interval", target="power.between", low=200, high=220))),
            ("run", curated(step("interval", target="heart.rate.zone", low=140, high=150),
                            step("interval", target="pace.zone", low=3, high=3.2))),
            ("run", None),
        ])
        self.assertEqual(both, ["lt_speed_mps", "lt_hr", "ftp_w"])

    def test_explicit_range_on_other_steps_is_not_hard(self):
        steps = [step("warmup", target="pace.zone", low=2.4, high=2.6),
                 step("recovery", target="pace.zone", low=2.2, high=2.4),
                 step("interval", target="heart.rate.zone", low=120, high=140)]
        self.assertFalse(classify(steps, secs=2400)["hard"])

    def test_warmup_and_cooldown_zone_5_ignored(self):
        steps = [step("warmup", target="heart.rate.zone", zone=5), step("interval", target="heart.rate.zone", zone=2),
                 step("cooldown", target="pace.zone", zone=5)]
        self.assertEqual(classify(steps, secs=2400)["class"], "easy")

    def test_long_by_minutes_per_sport(self):
        for sport, key, minutes in (("running", "run", 75), ("cycling", "bike", 150), ("lap_swimming", "swim", 75),
                                    ("strength_training", "other", 90)):
            with self.subTest(sport=sport):
                at = classify([step("interval")], sport=sport, secs=minutes * 60)
                self.assertEqual((at["sport"], at["long"], at["class"]), (key, True, "hard_or_long"))
                self.assertFalse(classify([step("interval")], sport=sport, secs=(minutes - 1) * 60)["long"])

    def test_long_by_km_per_sport(self):
        for sport, km in (("running", 15), ("road_biking", 70), ("open_water_swimming", 3.5)):
            with self.subTest(sport=sport):
                self.assertTrue(classify([step("interval")], sport=sport, secs=600, meters=km * 1000)["long"])
                self.assertFalse(classify([step("interval")], sport=sport, secs=600, meters=km * 1000 - 100)["long"])
        # No distance threshold for other sports.
        self.assertFalse(classify([step("interval")], sport="hiking", secs=600, meters=100000)["long"])

    def test_estimate_from_steps_multiplies_repeats(self):
        steps = [step("warmup", value=600.0),
                 repeat(10, step("interval", value=240.0), step("recovery", value=120.0)),
                 step("cooldown", value=600.0)]
        w = classify(steps)
        self.assertEqual(w["minutes"], 80)
        self.assertTrue(w["long"])
        nested = classify([repeat(2, repeat(3, step("interval", value=300.0)))])
        self.assertEqual(nested["minutes"], 30)
        dist = classify([repeat(4, step("interval", "distance", 4000.0))])
        self.assertEqual((dist["km"], dist["long"]), (16.0, True))

    def test_schedule_estimate_wins(self):
        w = ds.classify_workout(entry(secs=1200, meters=3000.0),
                                curated(step("interval", value=9000.0), secs=7000, meters=20000.0))
        self.assertEqual((w["minutes"], w["km"], w["long"]), (20, 3.0, False))

    def test_unstructured_uses_name_keywords(self):
        hard_names = ["Tempo run", "VO2max 5x3", "Race day", "Brick bike + run", "Hard hills", "threshold 3x10",
                      "Intervals 6x400"]
        for name in hard_names:
            with self.subTest(name=name):
                w = classify(name=name, secs=1800)
                self.assertEqual((w["structured"], w["hard"]), (False, True))
        # Keywords are English only: a name in another language is not hard by name.
        for name in ["Easy spin", "Traceability", RTL_RUN + " 6x1000"]:
            with self.subTest(name=name):
                self.assertFalse(classify(name=name, secs=1800)["hard"])
        self.assertTrue(classify(name="Long ride", sport="cycling", secs=4 * 3600)["long"])

    def test_without_details_structured_is_null(self):
        w = classify(name="Intervals 6x1000", secs=3600, details=False)
        self.assertIsNone(w["structured"])
        self.assertTrue(w["hard"])
        self.assertTrue(w["reason"].startswith("workout details unavailable"))

    def test_completed_and_key_order(self):
        w = ds.classify_workout(entry(secs=1800, associatedActivityId=5), curated(step("interval")))
        self.assertTrue(w["completed"])
        self.assertEqual(list(w), ["name", "sport", "km", "minutes", "class", "hard", "long", "structured",
                                   "reason", "completed"])


# --- probe --------------------------------------------------------------------

class Probe(unittest.TestCase):
    def probe(self, sleep=None, ready=None):
        data = {f"sleep:{DAY}": sleep if sleep is not None else fixture("sleep_night"),
                f"readiness:{DAY}": ready if ready is not None else fixture("readiness")}
        client = FakeClient(data)
        return build(client, probe=True), client

    def test_synced_night(self):
        out, client = self.probe()
        self.assertEqual(set(out), {"kind", "date", "generated_at", "tz", "sleep_synced", "sleep_end_local",
                                    "readiness_ready", "sources"})
        self.assertEqual((out["kind"], out["sleep_synced"], out["sleep_end_local"], out["readiness_ready"]),
                         ("daily_probe", True, "05:52", True))
        self.assertEqual(sorted(client.calls), [f"readiness:{DAY}", f"sleep:{DAY}"])

    def test_empty_shell_is_not_synced(self):
        out, _ = self.probe(sleep=fixture("sleep_shell"))
        self.assertEqual((out["sleep_synced"], out["sleep_end_local"]), (False, None))

    def test_other_calendar_date_is_not_synced(self):
        out, _ = self.probe(sleep=fixture("sleep_prev_night"))
        self.assertFalse(out["sleep_synced"])

    def test_readiness_without_score_is_not_ready(self):
        snap = fixture("readiness")
        snap[0]["score"] = None
        self.assertFalse(self.probe(ready=snap)[0]["readiness_ready"])
        self.assertFalse(self.probe(ready=[])[0]["readiness_ready"])

    def test_pre_sleep_row_alone_is_not_ready(self):
        self.assertFalse(self.probe(ready=[PRE_SLEEP])[0]["readiness_ready"])
        self.assertTrue(self.probe(ready=[PRE_SLEEP, WAKE])[0]["readiness_ready"])
        post = row("2026-03-18T07:00:00.0", ds.POST_EXERCISE_CONTEXT, score=55)
        self.assertTrue(self.probe(ready=[PRE_SLEEP, post])[0]["readiness_ready"])

    def test_probe_text(self):
        out = build(FakeClient({f"sleep:{DAY}": fixture("sleep_shell")}), probe=True, text_format="markdown")
        self.assertIn("Sleep: not synced from the watch yet", out["text"])
        synced, _ = self.probe()
        self.assertIn("05:52", ds.render_text(synced, "markdown"))


# --- end to end ---------------------------------------------------------------

class EndToEnd(unittest.TestCase):
    def test_full_report_from_fixtures(self):
        out = build()
        self.assertEqual(
            list(out),
            ["kind", "rules_version", "date", "weekday_en", "generated_at", "tz", "level", "level_en",
             "recommendation", "recommendation_en", "recovery", "load", "today", "tomorrow", "flags", "missing",
             "thresholds", "thresholds_athlete", "sources"],
        )
        self.assertEqual((out["kind"], out["rules_version"], out["date"], out["weekday_en"]),
                         ("daily_status", 1, DAY, "Wednesday"))
        self.assertEqual((out["generated_at"], out["tz"]), ("2026-03-18T06:30:00+02:00", TZ_NAME))
        rec = out["recovery"]
        self.assertEqual(
            list(rec),
            ["sleep_hours", "sleep_hhmm", "sleep_hours_prev", "sleep_score", "rem_min", "deep_min", "awake_min",
             "sleep_end_local", "hrv", "hrv_7d", "hrv_delta", "hrv_status", "hrv_range", "rhr", "rhr_7d",
             "rhr_delta", "readiness", "readiness_level", "recovery_hours", "body_battery", "stress_avg",
             "spo2_avg", "spo2_min", "resp_sleep_avg"],
        )
        self.assertEqual((rec["sleep_hours"], rec["sleep_hhmm"], rec["sleep_hours_prev"]), (6.25, "6:15", 5.7))
        self.assertEqual((rec["hrv"], rec["hrv_7d"], rec["hrv_delta"], rec["hrv_status"]), (58, 63, -5, "BALANCED"))
        self.assertEqual(rec["hrv_range"], {"low_upper": 49, "balanced_low": 55, "balanced_upper": 72})
        self.assertEqual((rec["rhr"], rec["rhr_7d"], rec["rhr_delta"]), (49, 46, 3))
        # 9.0 h at 05:58:41, report at 06:30.
        self.assertEqual((rec["readiness"], rec["readiness_level"], rec["recovery_hours"]), (52, "MODERATE", 8.5))
        self.assertEqual((rec["body_battery"], rec["stress_avg"]), (71, 24))
        ld = out["load"]
        self.assertEqual(list(ld), ["acute", "chronic", "acwr", "acwr_status", "acwr_percent",
                                    "readiness_acute_load", "yesterday"])
        self.assertEqual((ld["acwr"], ld["acwr_status"], ld["readiness_acute_load"]), (1.1, "OPTIMAL", 610))
        self.assertEqual([s["sport"] for s in ld["yesterday"]], ["other", "bike"])
        self.assertEqual((out["level"], out["level_en"], out["flags"]), ("green", "Green", []))
        self.assertEqual(out["recommendation"], "as_planned")
        self.assertEqual(out["thresholds"], ds.THRESHOLDS)
        self.assertEqual([w["name"] for w in out["today"]], ["Easy aerobic run"])
        self.assertEqual([w["name"] for w in out["tomorrow"]], ["Threshold intervals 5x6", "Technique swim"])
        self.assertTrue(all(src["ok"] for src in out["sources"].values()))
        self.assertIn("activities_today", out["sources"])
        # No range target in these workouts: no threshold fetched.
        self.assertEqual(out["thresholds_athlete"], {"lt_pace_min_km": None, "lt_hr": None, "ftp_w": None})
        self.assertFalse({"lactate_threshold", "cycling_ftp"} & set(out["sources"]))
        self.assertEqual([w["completed"] for w in out["today"]], [False])

    def test_schedule_query_covers_today_and_tomorrow(self):
        client = FakeClient()
        build(client)
        self.assertEqual(len(client.queries), 1)
        self.assertIn(f'startDate:"{DAY}", endDate:"{NEXT}"', client.queries[0]["query"])

    @unittest.skipUnless(HAVE_UPSTREAM, NEEDS_UPSTREAM)
    def test_workouts_classified_from_curated_details(self):
        client = FakeClient()
        out = build(client)
        today, tomorrow = out["today"], out["tomorrow"]
        self.assertEqual(
            today,
            [{"name": "Easy aerobic run", "sport": "run", "km": 6.8, "minutes": 48, "class": "easy", "hard": False,
              "long": False, "structured": True, "reason": today[0]["reason"], "completed": False}],
        )
        self.assertEqual((tomorrow[0]["class"], tomorrow[0]["hard"], tomorrow[0]["long"]), ("hard_or_long", True, True))
        self.assertIn("HR zone 4", tomorrow[0]["reason"])
        self.assertIn("hard work 1800 s", tomorrow[0]["reason"])            # 5 x 6 min
        self.assertEqual((tomorrow[1]["sport"], tomorrow[1]["class"], tomorrow[1]["structured"]), ("swim", "easy", True))
        # The swim has no duration estimate and its timed rests are not its duration.
        self.assertEqual((tomorrow[1]["minutes"], tomorrow[1]["km"]), (None, 2.0))
        self.assertEqual(out["missing"], [])
        self.assertEqual(sorted(k for k in out["sources"] if k.startswith("workout_")),
                         ["workout_today_1", "workout_tomorrow_1", "workout_tomorrow_2"])
        self.assertEqual(len(client.calls), 14)
        self.assertNotIn("lactate_threshold", client.calls)
        self.assertNotIn("cycling_ftp", client.calls)

    @unittest.skipUnless(HAVE_UPSTREAM, NEEDS_UPSTREAM)
    def test_thresholds_fetched_only_for_range_targets(self):
        data = default_data()
        data["schedule"] = range_schedule()
        client = FakeClient(data)
        out = build(client, include_sources=True)
        # Fetched after the workout steps were read.
        self.assertGreater(client.calls.index("lactate_threshold"), client.calls.index(f"workout:{RANGE_RUN_ID}"))
        self.assertIn("cycling_ftp", client.calls)
        self.assertEqual(out["thresholds_athlete"], {"lt_pace_min_km": "4:30", "lt_hr": 172, "ftp_w": 240})
        run_, ride = out["today"][0], out["tomorrow"][0]
        self.assertEqual((run_["class"], run_["structured"]), ("easy", True))
        self.assertIn("pace range at 81% of threshold speed", run_["reason"])
        self.assertEqual((ride["class"], ride["hard"]), ("hard_or_long", True))
        self.assertIn("interval step power range at 98% of FTP", ride["reason"])
        self.assertTrue(out["sources"]["lactate_threshold"]["ok"] and out["sources"]["cycling_ftp"]["ok"])
        self.assertFalse([p for p in out["missing"] if p.startswith("thresholds_athlete")])
        # Debug keeps the thresholds but not the body weight.
        slim = out["debug"]["lactate_threshold"]["slim"]
        self.assertEqual(slim["speed_and_heart_rate"]["heartRate"], 172)
        self.assertNotIn("weight", json.dumps(slim))
        self.assertEqual(out["debug"]["cycling_ftp"]["slim"]["functionalThresholdPower"], 240)

    @unittest.skipUnless(HAVE_UPSTREAM, NEEDS_UPSTREAM)
    def test_threshold_failures_fall_back_and_are_missing(self):
        data = default_data()
        data["schedule"] = range_schedule()
        client = FakeClient(data, fail={"lactate_threshold": GarminConnectConnectionError("Garmin Connect request "
                                        "failed: API client error (404)"), "cycling_ftp": RuntimeError("x")})
        out = build(client)
        run_, ride = out["today"][0], out["tomorrow"][0]
        self.assertFalse(run_["hard"])                       # one step, not inside a repeat
        self.assertTrue(ride["hard"])                        # power ranges inside a 4x repeat
        self.assertIn("inside a repeat (no threshold)", ride["reason"])
        self.assertEqual(out["thresholds_athlete"], {"lt_pace_min_km": None, "lt_hr": None, "ftp_w": None})
        self.assertIn("thresholds_athlete.lt_pace_min_km", out["missing"])
        self.assertIn("thresholds_athlete.ftp_w", out["missing"])
        self.assertNotIn("thresholds_athlete.lt_hr", out["missing"])      # no HR range planned
        self.assertFalse(out["sources"]["lactate_threshold"]["ok"])

    @unittest.skipUnless(HAVE_UPSTREAM, NEEDS_UPSTREAM)
    def test_threshold_fetch_within_report_deadline(self):
        data = default_data()
        data["schedule"] = range_schedule()
        client = FakeClient(data, slow={"lactate_threshold": 1.0})
        started = time.monotonic()
        with mock.patch.object(ds, "DEADLINE_S", 0.4):
            out = build(client)
        self.assertLess(time.monotonic() - started, 5)
        self.assertFalse(out["sources"]["lactate_threshold"]["ok"])
        self.assertIn("thresholds_athlete.lt_pace_min_km", out["missing"])
        self.assertEqual(out["today"][0]["class"], "easy")

    def test_hebrew_name_with_digits_keeps_its_order(self):
        # Without isolates, a Hebrew name followed by "5x1000" inside "Today: ..."
        # shows the number on the wrong side.
        name = RTL_RUN + " 5x1000"
        data = default_data()
        data["schedule"] = {"data": {"workoutScheduleSummariesScalar": [entry(name, secs=3600)]}}
        out = build(FakeClient(data), text_format="markdown")
        self.assertIn("\u2068" + name + "\u2069 (", out["text"])
        self.assertEqual(out["today"][0]["name"], name)  # the JSON keeps the bare name

    def test_output_is_english_except_workout_names(self):
        # Workout names may be typed in Hebrew (or any script): they pass through as data,
        # every other string the tool produces is English, and no key is *_he.
        data = default_data()
        data["schedule"] = {"data": {"workoutScheduleSummariesScalar": [
            entry(RTL_RUN + " hills", secs=3600), entry(RTL_SWIM, "lap_swimming", secs=2400, date=NEXT)]}}
        activities = fixture("activities_prev")
        activities[0]["activityName"] = RTL_RIDE
        data[f"activities:{PREV}"] = activities
        for fmt in ds.TEXT_FORMATS:
            with self.subTest(fmt=fmt):
                out = build(FakeClient(data), text_format=fmt, include_sources=True)
                names = [w["name"] for w in out["today"] + out["tomorrow"] + out["load"]["yesterday"] if w.get("name")]
                self.assertEqual(sum(1 for n in names if HEBREW.search(n)), 3)
                # The Hebrew name sits inside invisible direction isolates in the English line.
                self.assertIn("\u2068" + RTL_RUN + " hills\u2069", out["text"])
                found = []

                def walk(node, path):
                    if isinstance(node, dict):
                        for key, value in node.items():
                            self.assertFalse(str(key).endswith("_he"), path + [key])
                            walk(value, path + [key])
                    elif isinstance(node, list):
                        for i, value in enumerate(node):
                            walk(value, path + [i])
                    elif isinstance(node, str):
                        rest = node
                        for name in names:
                            rest = rest.replace(name, "")
                        # Any Hebrew word left over must come from a workout name.
                        words = re.findall("[\\u0590-\\u05FF]+", rest)
                        if any(not any(w in n for n in names) for w in words):
                            found.append((path, node))

                walk(out, [])
                self.assertEqual(found, [])
                for key in ("weekday_en", "level_en", "recommendation_en"):
                    self.assertIsInstance(out[key], str)

    def test_workout_done_today(self):
        data = default_data()
        data[f"activities:{DAY}"] = with_activity_ids(fixture("activities_today"))
        out = build(FakeClient(data), text_format="markdown")
        self.assertEqual([w["completed"] for w in out["today"]], [True])
        self.assertEqual([w["completed"] for w in out["tomorrow"]], [False, False])
        self.assertEqual((out["recommendation"], out["recommendation_en"]), ("done_today", "Today's workout is already done."))
        self.assertIn(", done)", out["text"])
        self.assertNotIn("8880001", json.dumps(out))

    def test_all_of_todays_workouts_done_plural(self):
        data = default_data()
        data["schedule"] = {"data": {"workoutScheduleSummariesScalar": [
            entry("Run", secs=1800), entry("Ride", "cycling", secs=3600)]}}
        ride = fixture("activities_prev")[0]                       # a virtual ride
        ride["startTimeLocal"] = f"{DAY} 05:50:00"
        data[f"activities:{DAY}"] = with_activity_ids(fixture("activities_today") + [ride])
        out = build(FakeClient(data))
        self.assertEqual((out["recommendation"], out["recommendation_en"]),
                         ("done_today", "Today's workouts are already done."))
        # Only the run done: the ride still decides the day.
        data[f"activities:{DAY}"] = with_activity_ids(fixture("activities_today"))
        out = build(FakeClient(data))
        self.assertEqual([w["completed"] for w in out["today"]], [True, False])
        self.assertNotEqual(out["recommendation"], "done_today")

    def test_hard_workout_done_clears_recovery_incomplete(self):
        data = default_data()
        # Recovery 8.5 h left; a tempo run (hard by name) planned today.
        data["schedule"] = {"data": {"workoutScheduleSummariesScalar": [entry("Tempo run", secs=2400)]}}
        before = build(FakeClient(copy.deepcopy(data)))
        self.assertEqual([f["id"] for f in before["flags"]], ["recovery_incomplete"])
        self.assertEqual(before["recommendation"], "as_planned_may_stop")
        data[f"activities:{DAY}"] = with_activity_ids(fixture("activities_today"))
        after = build(FakeClient(data))
        self.assertEqual((after["flags"], after["level"], after["recommendation"]), ([], "green", "done_today"))

    def test_readiness_from_the_primary_training_device(self):
        data = default_data()
        latest = data[f"training_status:{DAY}"]["mostRecentTrainingStatus"]["latestTrainingStatusData"]
        latest["990001"] = latest.pop("device-a")               # marked primaryTrainingDevice in the fixture
        row = data[f"readiness:{DAY}"][0]
        row["deviceId"] = 990001
        newer = dict(row, deviceId=990002, score=81, level="HIGH", timestamp="2026-03-18T04:20:00.0",
                     timestampLocal="2026-03-18T06:20:00.0")
        data[f"readiness:{DAY}"] = [newer, row]
        out = build(FakeClient(data), include_sources=True, text_format="markdown")
        self.assertEqual((out["recovery"]["readiness"], out["recovery"]["readiness_level"]), (52, "MODERATE"))
        self.assertNotIn("99000", json.dumps(out))

    def test_empty_daily_summary_is_not_systemic(self):
        # Upstream's proxy words an empty day like a network failure; the rest must still load.
        client = FakeClient(fail={f"stats:{DAY}": GarminConnectConnectionError(EMPTY_SUMMARY)})
        out = build(client)
        self.assertGreater(len(client.calls), 1)
        self.assertFalse(out["sources"]["stats"]["ok"])
        self.assertIn("No data received", out["sources"]["stats"]["error"])
        self.assertTrue(all(src["ok"] for name, src in out["sources"].items() if name != "stats"))
        self.assertEqual((out["recovery"]["sleep_hours"], out["recovery"]["readiness"]), (6.25, 52))
        self.assertIsNone(out["recovery"]["rhr"])
        self.assertIn("recovery.rhr", out["missing"])
        self.assertNotEqual(out["level"], "incomplete")
        self.assertEqual([w["name"] for w in out["today"]], ["Easy aerobic run"])

    @unittest.skipIf(HAVE_UPSTREAM, "covers the plain-python path")
    def test_without_upstream_workouts_are_unstructured_guesses(self):
        out = build()
        self.assertEqual(out["missing"], ["today[0].structured", "tomorrow[0].structured", "tomorrow[1].structured"])
        self.assertTrue(out["tomorrow"][0]["long"])

    def test_failing_source_is_missing_not_fatal(self):
        out = build(FakeClient(fail={f"stats:{DAY}": ValueError("boom")}))
        rec = out["recovery"]
        self.assertEqual((rec["rhr"], rec["rhr_7d"], rec["rhr_delta"], rec["body_battery"]), (None, None, None, None))
        self.assertIn("recovery.rhr", out["missing"])
        self.assertIn("recovery.rhr_7d", out["missing"])
        self.assertFalse(out["sources"]["stats"]["ok"])
        self.assertIn("boom", out["sources"]["stats"]["error"])
        # Not a systemic error: the rest still ran.
        self.assertTrue(out["sources"]["sleep"]["ok"])
        self.assertEqual(rec["sleep_hours"], 6.25)

    def test_failing_hrv_keeps_overnight_value_from_sleep(self):
        out = build(FakeClient(fail={f"hrv:{DAY}": RuntimeError("no hrv")}))
        rec = out["recovery"]
        self.assertEqual((rec["hrv"], rec["hrv_7d"], rec["hrv_range"]), (58, None, None))
        self.assertIn("recovery.hrv_7d", out["missing"])
        self.assertIn("recovery.hrv_range", out["missing"])
        self.assertFalse(out["sources"]["hrv"]["ok"])

    def test_systemic_first_failure_skips_the_rest(self):
        client = FakeClient(fail={f"stats:{DAY}": RuntimeError("Garmin rate limit hit: 429. Wait a few minutes")})
        out = build(client)
        self.assertEqual(client.calls, [f"stats:{DAY}"])
        self.assertEqual((out["level"], out["recommendation"]), ("incomplete", "no_recovery_data"))
        self.assertIsNone(out["today"])
        self.assertTrue(all(not src["ok"] for src in out["sources"].values()))
        self.assertTrue(out["sources"]["sleep"]["error"].startswith("skipped"))

    def test_slow_source_times_out(self):
        client = FakeClient(slow={f"training_status:{DAY}": 1.0})
        with mock.patch.object(ds, "CALL_TIMEOUT_S", 0.3):
            out = build(client)
        self.assertFalse(out["sources"]["training_status"]["ok"])
        self.assertIn("timeout", out["sources"]["training_status"]["error"])
        self.assertIsNone(out["load"]["acwr"])
        self.assertIn("load.acwr", out["missing"])
        self.assertTrue(out["sources"]["sleep"]["ok"])

    def test_slow_source_cut_by_report_deadline(self):
        client = FakeClient(slow={f"training_status:{DAY}": 1.0})
        started = time.monotonic()
        with mock.patch.object(ds, "DEADLINE_S", 0.4):
            out = build(client)
        self.assertFalse(out["sources"]["training_status"]["ok"])
        self.assertIn("timeout", out["sources"]["training_status"]["error"])
        self.assertIsNone(out["load"]["acwr_status"])
        self.assertTrue(out["sources"]["stats"]["ok"])
        self.assertLess(time.monotonic() - started, 5)

    @unittest.skipUnless(HAVE_UPSTREAM, NEEDS_UPSTREAM)
    def test_deadline_spent_skips_workout_details(self):
        client = FakeClient(slow={f"hrv:{PREV2}": 0.8})
        with mock.patch.object(ds, "DEADLINE_S", 0.5):
            out = build(client)
        self.assertFalse(out["sources"]["hrv_d2"]["ok"])
        detail = out["sources"]["workout_today_1"]
        self.assertFalse(detail["ok"])
        self.assertIn("deadline", detail["error"])
        self.assertIsNone(out["today"][0]["structured"])
        self.assertTrue(out["today"][0]["reason"].startswith("workout details unavailable"))
        self.assertIn("today[0].structured", out["missing"])
        self.assertIn("hrv_trend.d-2", out["missing"])

    def test_graphql_error_means_unknown_calendar(self):
        data = default_data()
        data["schedule"] = {"data": None, "errors": [{"message": "boom"}]}
        out = build(FakeClient(data))
        self.assertEqual((out["today"], out["tomorrow"]), (None, None))
        self.assertFalse(out["sources"]["schedule"]["ok"])
        self.assertIn("today", out["missing"])
        self.assertIn("tomorrow", out["missing"])

    def test_rest_day_is_no_planned_workout(self):
        data = default_data()
        data["schedule"] = {"data": {"workoutScheduleSummariesScalar": [entry("Rest", isRestDay=True)]}}
        out = build(FakeClient(data))
        self.assertEqual((out["today"], out["recommendation"]), ([], "no_planned_workout"))

    @unittest.skipUnless(HAVE_UPSTREAM, NEEDS_UPSTREAM)
    def test_coach_workout_by_uuid(self):
        data = default_data()
        data["schedule"] = {"data": {"workoutScheduleSummariesScalar": [
            entry("Coach tempo", secs=2400, workoutUuid="coach-uuid-1")]}}
        data["workout:coach-uuid-1"] = fixture("workout_threshold_run")
        client = FakeClient(data)
        out = build(client)
        self.assertIn("workout:coach-uuid-1", client.calls)
        self.assertEqual((out["today"][0]["hard"], out["today"][0]["structured"]), (True, True))

    def test_flags_level_and_recommendation_end_to_end(self):
        data = default_data()
        data[f"sleep:{DAY}"]["dailySleepDTO"]["sleepTimeSeconds"] = 18000      # 5:00
        data[f"readiness:{DAY}"][0]["score"] = 35
        data[f"stats:{DAY}"]["restingHeartRate"] = 53                           # 7-day 46
        out = build(FakeClient(data))
        self.assertEqual([f["id"] for f in out["flags"]], ["high_rhr", "sleep_debt", "low_readiness"])
        self.assertEqual((out["level"], out["recommendation"]), ("red", "shorten"))

    def test_odd_payload_shapes_never_raise(self):
        odd_values = ["text", 7, [], [1, "x"], {"dailySleepDTO": []}, {"hrvSummary": "x"},
                      {"mostRecentTrainingStatus": {"latestTrainingStatusData": [1]}}, {"data": "x"}]
        keys = [k for k in default_data() if not k.startswith("workout:")]
        for odd in odd_values:
            with self.subTest(odd=odd):
                data = default_data()
                for key in keys:
                    data[key] = copy.deepcopy(odd)
                out = build(FakeClient(data), include_sources=True, text_format="markdown")
                self.assertEqual(out["recovery"]["sleep_hours"], None)
                self.assertIn("recovery.sleep_hours", out["missing"])
                self.assertIn("text", out)
        weird = curated(step("interval", target={"workoutTargetTypeKey": "pace.zone"}, zone=5), "not a step")
        weird["segments"].append("not a segment")
        self.assertEqual(ds.classify_workout(entry(secs=600), weird)["class"], "easy")

    def test_bad_arguments_raise_value_error(self):
        for kw in ({"date": "2026-02-30"}, {"date": "tomorrow"}, {"text_format": "html"},
                   {"text_format": "telegram"}, {"tz": "Mars/Base"}):
            with self.subTest(**kw), self.assertRaises(ValueError):
                build(**kw)


# --- text ---------------------------------------------------------------------

def planned(name, sport="running", secs=1800, meters=None, hard_zone=None, date=DAY):
    steps = [step("interval", target="heart.rate.zone", zone=hard_zone or 2)]
    return ds.classify_workout(entry(name, sport, secs, meters, date=date), curated(*steps, sport=sport))


def status(today=None, tomorrow=None, **payload_changes):
    payloads = {
        "stats": fixture("stats"), "sleep": fixture("sleep_night"), "sleep_prev": fixture("sleep_prev_night"),
        "readiness": fixture("readiness"), "hrv": fixture("hrv_d0"), "hrv_d1": fixture("hrv_d1"),
        "hrv_d2": fixture("hrv_d2"), "training_status": fixture("training_status"),
        "activities_prev": fixture("activities_prev"),
    }
    payloads.update(payload_changes)
    return ds.daily_status_from(
        payloads, day=dt.date(2026, 3, 18), zone=TZ, now=NOW,
        today=[planned("Easy aerobic run")] if today is None else today,
        tomorrow=[] if tomorrow is None else tomorrow,
    )


HEBREW = re.compile("[\u0590-\u05FF]")
# Hebrew workout names for the right-to-left tests, written as escapes so the
# file holds no Hebrew letters: "run", "swim", "ride".
RTL_RUN = "\u05E8\u05D9\u05E6\u05D4"
RTL_SWIM = "\u05E9\u05D7\u05D9\u05D9\u05D4"
RTL_RIDE = "\u05E8\u05DB\u05D9\u05D1\u05D4"
DIRECTION_MARKS = ("\u200e", "\u200f")


class Text(unittest.TestCase):
    def test_layout(self):
        text = ds.render_text(status())
        lines = text.split("\n")
        self.assertEqual(lines[0], "**Daily status, Wed 18 Mar**")
        self.assertEqual(lines[2], "- Level: **Green**")
        self.assertIn("Sleep: 6:15 (score 74)", text)
        self.assertIn("HRV: 58 (7-day avg: 63)", text)
        self.assertIn("Resting HR: 49 (7-day avg: 46)", text)
        self.assertIn("Readiness: 52 (moderate), recovery: 8.5 h", text)
        self.assertIn("Load: ACWR 1.1 (optimal)", text)
        self.assertIn("Yesterday: strength 30 min", text)
        self.assertIn("bike 67 min 33.6 km, load 92", text)
        self.assertIn("Today: Easy aerobic run (run, 30 min, easy)", text)
        self.assertIn("Recommendation: **Train as planned.**", text)
        self.assertEqual(lines[-1], "- Flags: none")
        self.assertNotIn("Tomorrow", text)

    def test_markdown_layout(self):
        text = ds.render_text(status(), "markdown")
        lines = text.split("\n")
        self.assertEqual(lines[0], "**Daily status, Wed 18 Mar**")
        self.assertEqual(lines[1], "")
        self.assertTrue(all(line.startswith("- ") for line in lines[2:]))
        self.assertIn("- Level: **Green**", lines)
        self.assertIn("- HRV: 58 (7-day avg: 63)", lines)
        self.assertNotIn("<b>", text)

    def test_english_left_to_right_text(self):
        probe = {"kind": "daily_probe", "date": DAY, "sleep_synced": True, "sleep_end_local": "05:52",
                 "readiness_ready": True}
        for fmt in ds.TEXT_FORMATS:
            for st in (status(), probe):
                text = ds.render_text(st, fmt)
                self.assertIsNone(HEBREW.search(text), text)
                for mark in DIRECTION_MARKS:
                    self.assertNotIn(mark, text)
        self.assertEqual(ds.render_text(probe, "markdown"),
                         "**Wed 18 Mar**\n\n- Sleep: synced, ended at 05:52\n- Readiness: has a score")
        waiting = dict(probe, sleep_synced=False, readiness_ready=False)
        self.assertEqual(ds.render_text(waiting, "markdown"),
                         "**Wed 18 Mar**\n\n- Sleep: not synced from the watch yet\n- Readiness: no score yet")

    def test_markdown_escaping(self):
        st = status(today=[planned("Hills <x3> & *strides*", hard_zone=4)])
        md = ds.render_text(st, "markdown")
        self.assertIn(r"Hills \<x3\> & \*strides\*", md)
        self.assertNotIn("<b>", md)

    def test_nulls_print_no_value(self):
        st = status(stats={}, sleep={}, readiness=[], hrv={}, training_status={}, activities_prev=None)
        text = ds.render_text(st, "markdown")
        for expected in ("Sleep: no data", "HRV: no data", "Resting HR: no data",
                         "Readiness: no data, recovery: no data", "ACWR no data", "Yesterday: no data",
                         "Level: **Incomplete data**"):
            self.assertIn(expected, text)
        self.assertEqual(st["level"], "incomplete")
        self.assertIn("7-day avg: no data", ds.render_text(status(hrv={}), "markdown"))

    def test_done_workouts_are_marked(self):
        done = dict(planned("Easy aerobic run"), completed=True)
        st = status(today=[done])
        self.assertEqual(st["recommendation"], "done_today")
        text = ds.render_text(st, "markdown")
        self.assertIn("Today: Easy aerobic run (run, 30 min, easy, done)", text)
        self.assertIn("Recommendation: **Today's workout is already done.**", text)
        md = ds.render_text(status(today=[done, planned("Swim", "lap_swimming")]), "markdown")
        self.assertEqual(md.count(", done)"), 1)

    def test_tomorrow_line_only_for_hard_or_long(self):
        hard = [planned("Threshold", hard_zone=4, date=NEXT)]
        easy = [planned("Shakeout", date=NEXT)]
        self.assertIn("Tomorrow: Threshold (run, 30 min, hard)", ds.render_text(status(tomorrow=hard), "markdown"))
        self.assertIn("Tomorrow:", ds.render_text(status(tomorrow=easy + [planned("Long", secs=6000, date=NEXT)]),
                                                  "markdown"))
        for tomorrow in (easy, [], None):
            st = status(tomorrow=easy)
            st["tomorrow"] = tomorrow
            self.assertNotIn("Tomorrow", ds.render_text(st, "markdown"))

    def test_word_limit_with_everything_on(self):
        long_name = "Very long workout name with many words that keeps going on"
        st = status(
            today=[planned(long_name, hard_zone=4), planned(long_name, "cycling", 9000), planned(long_name)],
            tomorrow=[planned(long_name, hard_zone=5, date=NEXT), planned(long_name, "lap_swimming", 5000, date=NEXT),
                      planned(long_name, secs=7200, date=NEXT)],
        )
        st["load"]["yesterday"] = st["load"]["yesterday"] * 3
        st["flags"] = [flag(i) for i in ("low_hrv", "hrv_low_trend", "high_rhr", "sleep_debt", "low_readiness",
                                         "recovery_incomplete")]
        st["flags"].append({"id": "load_spike", "level": "red", "label_en": ds.flag_label("load_spike", by_status=True)})
        for fmt in ds.TEXT_FORMATS:
            with self.subTest(fmt=fmt):
                text = ds.render_text(st, fmt)
                self.assertLessEqual(ds.word_count(text), ds.MAX_WORDS)
                for fl in st["flags"]:
                    self.assertIn(ds._Fmt().esc(fl["label_en"]), text)
                self.assertIn("Recommendation", text)

    def test_normal_texts_are_short(self):
        for fmt in ds.TEXT_FORMATS:
            self.assertLessEqual(ds.word_count(ds.render_text(status(), fmt)), 90)

    def test_names_are_one_short_line(self):
        st = status(today=[planned("Run\nwith   breaks " + "x" * 80)])
        line = [ln for ln in ds.render_text(st, "markdown").split("\n") if "Today" in ln][0]
        self.assertIn("Run with breaks…", line)
        self.assertNotIn("x" * 41, line)
        self.assertEqual(ds._short_name("Easy aerobic run", 6), "Easy aerobic run")
        self.assertEqual(ds._short_name("a b c d e f g", 3), "a b c…")

    def test_renders_the_same_from_parsed_json(self):
        st = status(tomorrow=[planned("Threshold", hard_zone=4, date=NEXT)])
        parsed = json.loads(json.dumps(st, ensure_ascii=False))
        for fmt in ds.TEXT_FORMATS:
            self.assertEqual(ds.render_text(parsed, fmt), ds.render_text(st, fmt))

    def test_text_added_on_request(self):
        out = build(text_format="markdown")
        self.assertEqual(out["text"], ds.render_text({k: v for k, v in out.items() if k != "text"}, "markdown"))
        self.assertNotIn("text", build())

    def test_bad_format(self):
        for fmt in ("html", "telegram"):
            with self.subTest(fmt=fmt), self.assertRaises(ValueError):
                ds.render_text(status(), fmt)
        self.assertEqual(ds.TEXT_FORMATS, ("markdown",))


# --- identifiers --------------------------------------------------------------

FAKE_IDS = ("424242", "fixture-display-name", "Fixture Owner", "4242aaaa", "4242bbbb", "777000", "fixture@example")
FORBIDDEN_KEYS = {
    "displayname", "userprofilepk", "userprofileid", "profileid", "uuid", "workoutuuid", "email", "fullname",
    "deviceid", "serialnumber", "unitid", "ownerid", "ownerdisplayname", "ownerfullname", "activityid",
    "workoutid", "scheduledworkoutid", "associatedactivityid", "author", "userid",
}


def with_identifiers(data):
    """Plant fake identifiers wherever Garmin puts real ones."""
    person = {"userProfilePk": 424242001, "displayName": "fixture-display-name", "fullName": "Fixture Owner",
              "email": "fixture@example.com"}
    data[f"stats:{DAY}"].update(userProfileId=424242001, uuid="4242aaaa-0000-0000-0000-000000000001")
    data[f"sleep:{DAY}"]["dailySleepDTO"].update(id=424242005, userProfilePK=424242001)
    data[f"readiness:{DAY}"][0].update(userProfilePK=424242001, deviceId=424242002)
    data[f"hrv:{DAY}"]["userProfilePk"] = 424242001
    latest = data[f"training_status:{DAY}"]["mostRecentTrainingStatus"]["latestTrainingStatusData"]
    record = latest.pop("device-a")
    record.update(deviceId=424242002, userId=424242001)
    latest["424242002"] = record
    data[f"training_status:{DAY}"]["mostRecentTrainingStatus"]["recordedDevices"] = [
        {"deviceId": 424242002, "imageURL": "https://example.invalid/424242.png", "deviceName": "Fixture Watch"}]
    for item in data["schedule"]["data"]["workoutScheduleSummariesScalar"]:
        item.update(workoutUuid="4242bbbb-0000-0000-0000-000000000002", scheduledWorkoutId=424242006)
    data[f"activities:{DAY}"] = fixture("activities_today")
    for activity in data[f"activities:{PREV}"] + data[f"activities:{DAY}"]:
        activity.update(activityId=424242007, ownerId=424242001, ownerDisplayName="fixture-display-name",
                        ownerFullName="Fixture Owner", deviceId=424242002)
    for workout_id in (EASY_ID, THRESHOLD_ID, SWIM_ID, RANGE_RUN_ID, RANGE_RIDE_ID):
        data[f"workout:{workout_id}"].update(workoutId=workout_id, ownerId=424242001, author=dict(person),
                                             workoutUuid="4242bbbb-0000-0000-0000-000000000003")
    data["lactate_threshold"]["speed_and_heart_rate"].update(userProfilePK=424242001, sequence=424242008)
    data["lactate_threshold"]["power"].update(userProfilePK=424242001, weight=424242.0)
    data["cycling_ftp"].update(userProfilePK=424242001, deviceId=424242002)
    return data


def walk_keys(node, path=""):
    if isinstance(node, dict):
        for key, value in node.items():
            yield key, f"{path}.{key}"
            yield from walk_keys(value, f"{path}.{key}")
    elif isinstance(node, list):
        for i, value in enumerate(node):
            yield from walk_keys(value, f"{path}[{i}]")


class Identifiers(unittest.TestCase):
    def check(self, out):
        for key, path in walk_keys(out):
            self.assertNotIn(str(key).lower(), FORBIDDEN_KEYS, path)
        dumped = json.dumps(out, ensure_ascii=False)
        for value in FAKE_IDS:
            self.assertNotIn(value, dumped)

    def test_no_identifiers_in_full_output_with_debug(self):
        out = build(FakeClient(with_identifiers(default_data())), include_sources=True, text_format="markdown")
        self.assertIn("debug", out)
        self.assertIn("<id>", " ".join(out["debug"]["training_status"]["keys"]))
        self.check(out)

    @unittest.skipUnless(HAVE_UPSTREAM, NEEDS_UPSTREAM)
    def test_no_identifiers_with_thresholds_fetched(self):
        data = with_identifiers(default_data())
        data["schedule"] = range_schedule()
        out = build(FakeClient(data), include_sources=True, text_format="markdown")
        self.assertIn("lactate_threshold", out["debug"])
        self.assertIn("cycling_ftp", out["debug"])
        self.assertTrue(out["today"][0]["completed"])
        self.check(out)

    def test_no_identifiers_in_probe(self):
        out = build(FakeClient(with_identifiers(default_data())), probe=True, include_sources=True)
        self.check(out)

    def test_debug_is_key_paths_and_slim_values(self):
        out = build(include_sources=True)
        debug = out["debug"]
        self.assertEqual(debug["stats"]["slim"]["restingHeartRate"], 49)
        self.assertIn("dailySleepDTO.sleepTimeSeconds", debug["sleep"]["keys"])
        self.assertEqual(debug["training_status"]["slim"]["devices"], 1)
        self.assertNotIn("activityName", json.dumps(debug["activities_prev"]["slim"]))
        self.assertNotIn("workoutName", json.dumps(debug["schedule"]["slim"]))


# --- the registered tool ------------------------------------------------------

class Tool(unittest.TestCase):
    def setUp(self):
        self.tools = {}
        test = self

        class App:
            def tool(self, *args, **kwargs):
                def deco(fn):
                    test.tools[fn.__name__] = fn
                    return fn
                return deco

        self.client = FakeClient()
        self.app = App()
        self.assertIs(ds.register_tools(self.app, lambda: self.client), self.app)
        # The tool reads REPORT_TZ (required, no default): set it explicitly.
        env = mock.patch.dict(os.environ, {"REPORT_TZ": TZ_NAME})
        env.start()
        self.addCleanup(env.stop)

    def call(self, **kw):
        return run(self.tools["get_daily_status"](**kw))

    def test_unset_or_bad_report_tz_is_error_text(self):
        # No default zone: an unset REPORT_TZ is a clear error, not a crash or a guess.
        with mock.patch.dict(os.environ, {}, clear=True):
            out = self.call(date=DAY)
        self.assertTrue(out.startswith("Error: REPORT_TZ is not set"), out)
        with mock.patch.dict(os.environ, {"REPORT_TZ": "Mars/Base"}):
            out = self.call(date=DAY)
        self.assertTrue(out.startswith("Error: REPORT_TZ: unknown time zone 'Mars/Base'"), out)
        self.assertEqual(self.client.calls, [])

    def test_bad_date_and_format_are_error_text(self):
        for kw in ({"date": "2026-13-01"}, {"date": "yesterday"}, {"text_format": "html"},
                   {"text_format": "telegram"}):
            with self.subTest(**kw):
                self.assertTrue(self.call(**kw).startswith("Error: "))
        self.assertEqual(self.client.calls, [])

    def test_valid_call_returns_compact_json(self):
        out = self.call(date=DAY, text_format="markdown")
        self.assertNotIn("\n", out)
        res = json.loads(out)
        self.assertEqual((res["kind"], res["date"]), ("daily_status", DAY))
        self.assertTrue(res["text"].startswith("**Daily status, "), res["text"][:40])
        # Not "today" for the server clock, so no elapsed time is subtracted.
        self.assertEqual(res["recovery"]["recovery_hours"], 9.0)

    def test_probe_call(self):
        res = json.loads(self.call(date=DAY, probe=True))
        self.assertEqual((res["kind"], res["sleep_synced"]), ("daily_probe", True))

    def test_docstring_tells_the_model_not_to_recompute(self):
        doc = self.tools["get_daily_status"].__doc__
        for phrase in ("computed on the server", "as given", "null", "probe", "include_sources"):
            self.assertIn(phrase, doc)

    def test_unexpected_bug_is_error_text_without_urls(self):
        with mock.patch.object(ds, "build_daily_status", side_effect=KeyError("https://x.invalid/u/name")):
            out = self.call(date=DAY)
        self.assertTrue(out.startswith("Error: daily status failed"))
        self.assertNotIn("x.invalid", out)


@unittest.skipUnless(HAVE_UPSTREAM, "needs upstream garmin_mcp")
class SelfCheck(unittest.TestCase):
    def test_passes_with_pinned_upstream(self):
        ds.selfcheck()

    def test_detects_changed_curation(self):
        import garmin_mcp.workouts as workouts
        with mock.patch.object(workouts, "_curate_workout_details", lambda w: {"segments": []}):
            with self.assertRaises(RuntimeError):
                ds.selfcheck()

    def test_detects_changed_lt_speed_unit(self):
        import garmin_mcp.training as training
        with mock.patch.object(training, "_lactate_threshold_speed_mps", lambda v: v):
            with self.assertRaises(RuntimeError):
                ds.selfcheck()


if __name__ == "__main__":
    unittest.main()
