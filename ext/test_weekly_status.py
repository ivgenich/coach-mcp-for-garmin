"""Tests for weekly_status. Standard library only:

    python3 -m unittest discover -s ext -p "test_weekly_status*.py" -v

The fixtures in fixtures/weekly_status/ are synthetic payloads in Garmin's raw
shapes (week 2026-03-01 .. 2026-03-07). Tests that need upstream garmin_mcp
(selfcheck, the real FIT helpers, upstream's device selection) are skipped
when it is not installed.
"""

from __future__ import annotations

import asyncio
import copy
import datetime as dt
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

import weekly_status as ws

try:
    import garmin_mcp  # noqa: F401
    HAVE_UPSTREAM = True
except ImportError:
    HAVE_UPSTREAM = False

# Tests set their own zone: REPORT_TZ is required and has no default.
TZ_NAME = "Europe/Athens"
TZ = ZoneInfo(TZ_NAME)
FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "weekly_status")
START, END = dt.date(2026, 3, 1), dt.date(2026, 3, 7)
NOW = dt.datetime(2026, 3, 8, 6, 30, tzinfo=TZ)
FIXTURE_NAMES = ("device_last_used", "devices", "hrv", "sleep", "activities", "training_status",
                 "schedule", "weigh_ins", "gear", "gear_stats", "activity_details")
IDENTIFIER_KEYS = {
    "uuid", "userProfilePk", "userProfilePK", "userProfileNumber", "profileId", "userId", "ownerId",
    "deviceId", "userDeviceId", "activityId", "workoutId", "scheduledWorkoutId", "gearPk", "samplePk",
    "displayName", "fullName", "email", "serialNumber",
}


def load(name):
    with open(os.path.join(FIXTURES, name + ".json"), encoding="utf-8") as fh:
        return json.load(fh)


FX = {name: load(name) for name in FIXTURE_NAMES}


PLANTED_ID_KEYS = {
    "userProfileNumber", "userProfilePk", "userProfilePK", "userId", "ownerId", "userDeviceId", "deviceId",
    "activityId", "associatedActivityId", "workoutId", "scheduledWorkoutId", "uuid", "gearPk", "samplePk",
}


def planted_ids():
    """Every identifier value planted in the fixtures (none may reach a result)."""
    found = set()

    def walk(obj):
        if isinstance(obj, dict):
            for key, value in obj.items():
                if key in PLANTED_ID_KEYS and isinstance(value, (int, str)) and not isinstance(value, bool):
                    found.add(str(value))
                walk(value)
        elif isinstance(obj, list):
            for value in obj:
                walk(value)

    walk(FX)
    return found


class GarminConnectConnectionError(Exception):
    """Stands in for garminconnect's class of the same name (raised for empty days and 404s too)."""


UPSTREAM_NO_DATA = ("Garmin Connect request failed: No data received from server. Garmin Connect may be "
                    "unreachable; check your network connection or try again later.")


class FakeClient:
    """Stands in for upstream's Garmin proxy, answering from the fixtures."""

    garmin_connect_activities = "/activitylist-service/activities/search/activities"

    def __init__(self, fx=None, fail=None, delay=None):
        self.fx = copy.deepcopy(fx or FX)
        self.fail = dict(fail or {})    # method name -> exception to raise
        self.delay = dict(delay or {})  # method name -> seconds to stall
        self.calls = []
        self._lock = threading.Lock()

    def _enter(self, name, *args, **kwargs):
        with self._lock:
            self.calls.append((name, args, kwargs))
        if name in self.delay:
            time.sleep(self.delay[name])
        if name in self.fail:
            raise self.fail[name]

    def names(self):
        return [c[0] for c in self.calls]

    def get_device_last_used(self):
        self._enter("get_device_last_used")
        return copy.deepcopy(self.fx["device_last_used"])

    def get_devices(self):
        self._enter("get_devices")
        return copy.deepcopy(self.fx["devices"])

    def get_hrv_data(self, day):
        self._enter("get_hrv_data", day)
        return copy.deepcopy(self.fx["hrv"].get(day)) or {}

    def get_sleep_data(self, day):
        self._enter("get_sleep_data", day)
        # Before a sync Garmin answers with an empty shell.
        return copy.deepcopy(self.fx["sleep"].get(day)) or {"dailySleepDTO": {"calendarDate": None}}

    def connectapi(self, path, **kwargs):
        self._enter("connectapi", path, **kwargs)
        if path != self.garmin_connect_activities:
            raise ValueError("unexpected path")
        return copy.deepcopy(self.fx["activities"])

    def get_training_status(self, day):
        self._enter("get_training_status", day)
        return copy.deepcopy(self.fx["training_status"].get(day)) or {}

    def query_garmin_graphql(self, query):
        self._enter("query_garmin_graphql", query)
        return copy.deepcopy(self.fx["schedule"])

    def get_weigh_ins(self, start, end):
        self._enter("get_weigh_ins", start, end)
        return copy.deepcopy(self.fx["weigh_ins"])

    def get_gear(self, profile_number):
        self._enter("get_gear", profile_number)
        return copy.deepcopy(self.fx["gear"])

    def get_gear_stats(self, uuid):
        self._enter("get_gear_stats", uuid)
        return copy.deepcopy(self.fx["gear_stats"].get(uuid)) or {}

    def download_activity(self, activity_id, dl_fmt=None):
        self._enter("download_activity", activity_id, dl_fmt=dl_fmt)
        return b"FIT-BYTES"

    def get_activity_details(self, activity_id, maxchart=2000, maxpoly=4000):
        self._enter("get_activity_details", activity_id, maxchart=maxchart, maxpoly=maxpoly)
        return copy.deepcopy(self.fx["activity_details"])


def fit_helpers(pct=-6.1, error=None):
    """A _load_fit_helpers stand-in whose _parse_fit returns upstream's shape."""

    def parse_fit(raw, include_records):
        if error is not None:
            raise error
        assert raw == b"FIT-BYTES" and include_records is False
        session = {"sport": "cycling"}
        if pct is not None:
            session["hr_drift"] = {"hr_drift_pct": pct, "first_half_power_hr_ratio": 1.52,
                                   "second_half_power_hr_ratio": 1.43, "interpretation": "moderate_drift"}
        return {"session": session, "laps": []}

    return lambda: ("ORIGINAL", parse_fit, True)


def build(client=None, end_date="2026-03-07", fit=None, **kw):
    with mock.patch.object(ws, "_load_fit_helpers", fit or fit_helpers()):
        return asyncio.run(ws.build_weekly_status(client or FakeClient(), end_date, tz=TZ_NAME,
                                                  now=NOW, **kw))


def neutral(**sections):
    """A status whose sections trigger no flag; override one section per test."""
    status = {
        "hrv": {"avg_7d": 70, "avg_28d": 70, "balanced_low": 60},
        "sleep": {"nights": 7, "nights_under_6h": 0},
        "volume": {"total_hours": 8.0, "prev_total_hours": 8.0},
        "load": {"acwr_status": "OPTIMAL"},
        "plan": {"scheduled": 5, "completed": 5, "completion_pct": 100},
        "gear": [],
    }
    status.update(sections)
    return status


def flag_ids(status):
    return [f["id"] for f in ws.evaluate_flags(status)]


def activity(start_local, type_key="running", seconds=3600.0, meters=10000.0, aid=1, name="x"):
    return {"activityId": aid, "activityName": name, "activityType": {"typeKey": type_key},
            "startTimeLocal": start_local, "duration": seconds, "distance": meters}


def keys_of(obj):
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield key
            yield from keys_of(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from keys_of(value)


# --- rules --------------------------------------------------------------------

class Flags(unittest.TestCase):
    def test_neutral_status_has_no_flags(self):
        self.assertEqual(flag_ids(neutral()), [])

    def test_hrv_down_by_28_day_trend(self):
        for avg_7d, fires in ((65, True), (64, True), (66, False)):
            ids = flag_ids(neutral(hrv={"avg_7d": avg_7d, "avg_28d": 70, "balanced_low": 50}))
            self.assertEqual("hrv_down" in ids, fires, avg_7d)

    def test_hrv_down_below_balanced_low(self):
        self.assertIn("hrv_down", flag_ids(neutral(hrv={"avg_7d": 65, "avg_28d": None, "balanced_low": 66})))
        self.assertIn("hrv_down", flag_ids(neutral(hrv={"avg_7d": 60, "avg_28d": 61, "balanced_low": 66})))
        self.assertNotIn("hrv_down", flag_ids(neutral(hrv={"avg_7d": 66, "avg_28d": None, "balanced_low": 66})))

    def test_short_nights(self):
        for short, fires in ((3, True), (4, True), (2, False), (None, False)):
            ids = flag_ids(neutral(sleep={"nights": 7, "nights_under_6h": short}))
            self.assertEqual("short_nights" in ids, fires, short)

    def test_volume_jump(self):
        cases = (
            (13.0, 10.0, True),   # exactly 1.3x
            (13.5, 10.0, True),
            (12.99, 10.0, False),
            (3.9, 3.0, True),     # 1.3 x 3.0 is 3.9000000000000004 in floats
            (10.0, 2.99, False),  # previous week under 3 h
            (None, 10.0, False),
        )
        for total, prev, fires in cases:
            ids = flag_ids(neutral(volume={"total_hours": total, "prev_total_hours": prev}))
            self.assertEqual("volume_jump" in ids, fires, (total, prev))

    def test_load_high_is_red(self):
        for status, fires in (("HIGH", True), ("VERY_HIGH", True), ("OPTIMAL", False), (None, False)):
            flags = ws.evaluate_flags(neutral(load={"acwr_status": status}))
            self.assertEqual([f["level"] for f in flags if f["id"] == "load_high"], ["red"] if fires else [])

    def test_low_completion(self):
        cases = ((3, 69, True), (5, 0, True), (3, 70, False), (2, 0, False), (0, None, False), (None, None, False))
        for scheduled, pct, fires in cases:
            ids = flag_ids(neutral(plan={"scheduled": scheduled, "completion_pct": pct}))
            self.assertEqual("low_completion" in ids, fires, (scheduled, pct))

    def test_gear_near_limit_once_for_several_items(self):
        worn = [{"name": "a", "pct_of_max": 90}, {"name": "b", "pct_of_max": 120}]
        self.assertEqual(flag_ids(neutral(gear=worn)), ["gear_near_limit"])
        self.assertEqual(flag_ids(neutral(gear=[{"name": "c", "pct_of_max": 89}, {"name": "d", "pct_of_max": None}])), [])

    def test_missing_inputs_skip_every_rule(self):
        empty = {"hrv": {}, "sleep": {}, "volume": {}, "load": {}, "plan": {}, "gear": []}
        self.assertEqual(ws.evaluate_flags(empty), [])

    def test_labels_carry_thresholds(self):
        flags = ws.evaluate_flags(neutral(sleep={"nights": 7, "nights_under_6h": 3},
                                          volume={"total_hours": 20.0, "prev_total_hours": 10.0},
                                          plan={"scheduled": 5, "completed": 1, "completion_pct": 20}))
        labels = {f["id"]: f["label_en"] for f in flags}
        self.assertIn("under 6 h", labels["short_nights"])
        self.assertIn("30%", labels["volume_jump"])
        # Not done this week on any day, not "on the planned day".
        self.assertIn("70%", labels["low_completion"])
        self.assertIn("done this week (on any day)", labels["low_completion"])
        # Every label keeps the "Name: detail" shape (the page shows the name as a chip).
        for label in labels.values():
            self.assertRegex(label, r"^[A-Z][^:]+: \S")


class Level(unittest.TestCase):
    def level(self, levels, **missing):
        args = {"hrv_missing": False, "sleep_missing": False, "volume_missing": False, **missing}
        return ws.compute_level([{"level": lv} for lv in levels], **args)

    def test_counting(self):
        self.assertEqual(self.level([]), "green")
        self.assertEqual(self.level(["yellow"]), "yellow")
        self.assertEqual(self.level(["yellow", "yellow"]), "yellow")
        self.assertEqual(self.level(["yellow", "yellow", "yellow"]), "red")
        self.assertEqual(self.level(["red"]), "red")

    def test_incomplete_only_when_hrv_sleep_and_volume_all_missing(self):
        everything = {"hrv_missing": True, "sleep_missing": True, "volume_missing": True}
        self.assertEqual(self.level([], **everything), "incomplete")
        self.assertEqual(self.level(["yellow"], **everything), "incomplete")
        self.assertEqual(self.level(["red"], **everything), "red")
        self.assertEqual(self.level([], hrv_missing=True, sleep_missing=True), "green")


# --- sections -----------------------------------------------------------------

class Volume(unittest.TestCase):
    def test_split_at_local_midnight(self):
        acts = ws.parse_activities([
            activity("2026-02-21 23:59:59", aid=1),                    # before both weeks: ignored
            activity("2026-02-22 00:00:00", aid=2),                    # first second of the previous week
            activity("2026-02-28 23:59:59", aid=3),                    # last second of the previous week
            activity("2026-03-01 00:00:00", aid=4),                    # first second of this week
            activity("2026-03-07 23:59:59", "virtual_ride", aid=5),    # last second of this week
            activity("2026-03-08 00:00:00", aid=6),                    # after the week: ignored
        ], TZ)
        vol = ws.volume_section(acts, START, END)
        self.assertEqual((vol["run"]["sessions"], vol["run"]["prev_sessions"]), (1, 2))
        self.assertEqual((vol["bike"]["sessions"], vol["bike"]["prev_sessions"]), (1, 0))
        self.assertEqual((vol["total_hours"], vol["prev_total_hours"]), (2.0, 2.0))
        self.assertEqual(vol["change_pct"], 0)

    def test_gmt_start_used_when_local_is_absent(self):
        # 22:30 UTC on 28 Feb is 00:30 on 1 Mar in Athens (UTC+2): this week.
        act = {"activityId": 9, "activityType": {"typeKey": "running"}, "startTimeGMT": "2026-02-28 22:30:00",
               "duration": 1800.0, "distance": 5000.0}
        (parsed,) = ws.parse_activities([act], TZ)
        self.assertEqual(parsed["date"], START)

    def test_fixture_week(self):
        vol = ws.volume_section(ws.parse_activities(FX["activities"], TZ), START, END)
        self.assertEqual(vol["run"], {"sessions": 2, "km": 25.7, "hours": 2.25, "hhmm": "2:15",
                                      "prev_sessions": 3, "prev_km": 27.2, "prev_hours": 2.33, "prev_hhmm": "2:20"})
        self.assertEqual((vol["bike"]["sessions"], vol["bike"]["km"], vol["bike"]["hours"]), (3, 167.4, 5.58))
        self.assertEqual((vol["swim"]["km"], vol["swim"]["prev_km"]), (4.7, 4.9))
        self.assertEqual((vol["other"]["sessions"], vol["other"]["hours"]), (1, 0.5))
        self.assertEqual((vol["total_hours"], vol["prev_total_hours"], vol["change_pct"]), (9.92, 8.67, 14))

    def test_no_previous_week_has_no_change_pct(self):
        vol = ws.volume_section(ws.parse_activities([activity("2026-03-03 07:00:00")], TZ), START, END)
        self.assertIsNone(vol["change_pct"])
        self.assertEqual(vol["prev_total_hours"], 0.0)


def schedule_payload(*items):
    return {"data": {"workoutScheduleSummariesScalar": [
        {"scheduleDate": day, "workoutType": sport, "associatedActivityId": link, "isRestDay": False}
        for day, sport, link in items]}}


class Plan(unittest.TestCase):
    def plan(self, payload, activities):
        acts = None if activities is None else ws.parse_activities(activities, TZ)
        return ws.plan_section(ws.parse_schedule(payload, START, END), acts, START, END)

    def test_fixture_week(self):
        # Rest days and other weeks are out. 5 workouts are linked by Garmin; the
        # unlinked swim on 4 Mar has a swim that day; the easy run planned for
        # 2 Mar was run on 3 Mar (moved). The run of 5 Mar was not done: the
        # week's other run is linked, and the previous week's run 5 days before
        # it never counts.
        plan = self.plan(FX["schedule"], FX["activities"])
        self.assertEqual(plan, {"scheduled": 8, "completed": 7, "on_planned_day": 6, "linked": 5,
                                "completion_pct": 88})

    def test_moved_session_counts_anywhere_in_the_week(self):
        plan = self.plan(schedule_payload(("2026-03-01", "running", None)),
                         [activity("2026-03-07 06:00:00", "running", aid=1)])  # 6 days later
        self.assertEqual(plan, {"scheduled": 1, "completed": 1, "on_planned_day": 0, "linked": 0,
                                "completion_pct": 100})

    def test_previous_week_activity_does_not_count(self):
        plan = self.plan(schedule_payload(("2026-03-01", "running", None)),
                         [activity("2026-02-28 18:00:00", "running", aid=1)])  # the evening before
        self.assertEqual((plan["completed"], plan["on_planned_day"], plan["completion_pct"]), (0, 0, 0))

    def test_one_activity_never_completes_two_workouts(self):
        plan = self.plan(schedule_payload(
            ("2026-03-03", "running", None),   # the only run goes to the workout planned for its day
            ("2026-03-04", "running", None),
            ("2026-03-05", "running", None),   # nothing left
            ("2026-03-05", "swimming", None),  # the ride is another sport
        ), [activity("2026-03-04 06:00:00", "treadmill_running", aid=2),
            activity("2026-03-04 18:00:00", "virtual_ride", aid=3)])
        self.assertEqual(plan, {"scheduled": 4, "completed": 1, "on_planned_day": 1, "linked": 0,
                                "completion_pct": 25})
        self.assertIn("low_completion", flag_ids(neutral(plan=plan)))

    def test_other_sports_match_by_their_own_type(self):
        strength = schedule_payload(("2026-03-03", "strength_training", None))
        walk = self.plan(strength, [activity("2026-03-03 06:00:00", "walking", aid=1)])
        self.assertEqual(walk["completed"], 0)  # both are "other", but a walk is not strength work
        done = self.plan(strength, [activity("2026-03-03 06:00:00", "Strength_Training", aid=1)])
        self.assertEqual((done["completed"], done["on_planned_day"]), (1, 1))
        # run/bike/swim still match by group: a treadmill run completes a "running" workout.
        run = self.plan(schedule_payload(("2026-03-03", "running", None)),
                        [activity("2026-03-03 06:00:00", "treadmill_running", aid=1)])
        self.assertEqual(run["completed"], 1)

    def test_linked_activity_is_never_reused(self):
        plan = self.plan(schedule_payload(("2026-03-04", "running", 2), ("2026-03-05", "running", None)),
                         [activity("2026-03-04 06:00:00", "running", aid=2)])
        self.assertEqual(plan, {"scheduled": 2, "completed": 1, "on_planned_day": 1, "linked": 1,
                                "completion_pct": 50})

    def test_without_activities_only_full_links_count(self):
        plan = self.plan(schedule_payload(("2026-03-03", "running", 1), ("2026-03-04", "cycling", 2)), None)
        self.assertEqual((plan["completed"], plan["on_planned_day"], plan["completion_pct"]), (2, 2, 100))
        plan = self.plan(schedule_payload(("2026-03-03", "running", 1), ("2026-03-04", "cycling", None)), None)
        self.assertEqual((plan["completed"], plan["on_planned_day"], plan["linked"], plan["completion_pct"]),
                         (None, None, 1, None))
        self.assertNotIn("low_completion", flag_ids(neutral(plan=plan)))

    def test_nothing_scheduled_gives_null_pct_and_no_flag(self):
        plan = self.plan({"data": {"workoutScheduleSummariesScalar": []}}, [])
        self.assertEqual(plan, {"scheduled": 0, "completed": 0, "on_planned_day": 0, "linked": 0,
                                "completion_pct": None})
        self.assertNotIn("low_completion", flag_ids(neutral(plan=plan)))
        null_list = ws.parse_schedule({"data": {"workoutScheduleSummariesScalar": None}}, START, END)
        self.assertEqual(null_list, [])

    def test_graphql_error_is_missing_not_zero(self):
        self.assertIsNone(ws.parse_schedule({"errors": [{"message": "x"}], "data": None}, START, END))
        self.assertEqual(ws.plan_section(None, [], START, END)["scheduled"], None)


class OtherSections(unittest.TestCase):
    def test_hrv_from_four_weekly_averages(self):
        weeks = [ws.parse_hrv(FX["hrv"][d]) for d in ("2026-03-07", "2026-02-28", "2026-02-21", "2026-02-14")]
        hrv = ws.hrv_section(weeks)
        self.assertEqual((hrv["avg_7d"], hrv["avg_28d"], hrv["delta_ms"]), (70, 74, -4))
        self.assertEqual((hrv["balanced_low"], hrv["balanced_upper"], hrv["status_en"]), (66, 84, "balanced"))
        only_now = ws.hrv_section([weeks[0], ws.parse_hrv({}), ws.parse_hrv(None), ws.parse_hrv({})])
        self.assertEqual((only_now["avg_7d"], only_now["avg_28d"], only_now["delta_ms"]), (70, None, None))

    def test_sleep_counts_only_synced_nights(self):
        night = FX["sleep"]["2026-03-02"]
        shell = {"dailySleepDTO": {"calendarDate": "2026-03-03", "sleepTimeSeconds": None}}
        wrong_day = FX["sleep"]["2026-03-01"]
        nights = [ws.parse_sleep(night, dt.date(2026, 3, 2)), ws.parse_sleep(shell, dt.date(2026, 3, 3)),
                  ws.parse_sleep(wrong_day, dt.date(2026, 3, 4))]
        sleep = ws.sleep_section(nights)
        self.assertEqual((sleep["nights"], sleep["nights_under_6h"], sleep["avg_hhmm"]), (1, 1, "5:30"))
        self.assertEqual(ws.sleep_section([None] * 7)["nights_under_6h"], None)

    def test_weight_change_only_for_a_weigh_in_inside_the_week(self):
        weighs = ws.parse_weigh_ins(FX["weigh_ins"])
        body = ws.body_section(weighs, START, END)
        self.assertEqual((body["weight_kg"], body["weight_change_kg"]), (70.2, -0.4))
        self.assertEqual((body["weighed_on"], body["weighed_on_en"], body["compared_with"]),
                         ("2026-03-05", "5 Mar", "2026-02-24"))
        later = ws.body_section(weighs, dt.date(2026, 3, 8), dt.date(2026, 3, 14))
        self.assertEqual((later["weight_kg"], later["weight_change_kg"]), (70.2, None))
        self.assertIsNone(ws.body_section([], START, END)["weight_kg"])

    def test_gear_in_use_against_its_limit(self):
        items = ws.parse_gear(FX["gear"])
        self.assertEqual([g["name"] for g in items],
                         ["Synthetic Race Shoe", "Synthetic Trainer Shoe", "Synthetic Road Bike"])
        for item in items:
            item["stats"] = FX["gear_stats"].get(item["uuid"])
        gear, complete = ws.gear_section(items)
        self.assertTrue(complete)
        self.assertEqual(gear[0], {"name": "Synthetic Trainer Shoe", "type": "shoes", "km": 742, "max_km": 800,
                                   "pct_of_max": 93})
        self.assertEqual(gear[1]["pct_of_max"], 35)
        self.assertEqual((gear[2]["type"], gear[2]["max_km"], gear[2]["pct_of_max"]), ("bike", None, None))
        items[0]["stats"] = {}
        self.assertFalse(ws.gear_section(items)[1])

    def test_training_status_fallback_uses_the_marked_device(self):
        latest = FX["training_status"]["2026-03-07"]["mostRecentTrainingStatus"]["latestTrainingStatusData"]
        record, reporting_id, primary = ws._select_device_fallback(latest)
        self.assertEqual((record["acuteTrainingLoadDTO"]["dailyTrainingLoadAcute"], primary), (812, None))
        self.assertEqual(reporting_id, "72000001")
        ts = ws.parse_training_status(FX["training_status"]["2026-03-07"], FX["devices"])
        self.assertEqual((ts["acute"], ts["chronic"], ts["acwr"], ts["acwr_status"]), (812, 776, 1.05, "OPTIMAL"))
        self.assertEqual((ts["training_status"], ts["training_balance"]), ("PRODUCTIVE_2", "AEROBIC_LOW_SHORTAGE"))
        self.assertEqual((ts["vo2max_run"], ts["vo2max_bike"]), (56.3, 53.8))

    def test_english_code_maps(self):
        self.assertEqual(ws._label_map(ws.TRAINING_STATUS_EN, "RECOVERY_1"), "recovery")
        self.assertEqual(ws._label_map(ws.TRAINING_BALANCE_EN, "AEROBIC_LOW_SHORTAGE"), "low aerobic shortage")
        self.assertEqual(ws._label_map(ws.TRAINING_STATUS_EN, "SOMETHING_NEW_2"), "SOMETHING_NEW_2")
        self.assertIsNone(ws._label_map(ws.ACWR_STATUS_EN, None))


class LongSession(unittest.TestCase):
    def acts(self, *specs):
        return ws.parse_activities([activity(s, t, sec, aid=i) for i, (s, t, sec) in enumerate(specs, 1)], TZ)

    def test_longest_bike_or_run_of_an_hour(self):
        acts = self.acts(
            ("2026-03-02 06:00:00", "open_water_swimming", 9000.0),  # longest, but a swim
            ("2026-03-03 06:00:00", "running", 3540.0),              # 59 min
            ("2026-03-04 06:00:00", "virtual_ride", 3600.0),         # exactly 60 min
            ("2026-03-05 06:00:00", "treadmill_running", 5400.0),    # the longest bike/run
            ("2026-02-28 06:00:00", "road_biking", 12000.0),         # previous week
        )
        self.assertEqual(ws.pick_long_session(acts, START, END)["type"], "treadmill_running")
        self.assertEqual(ws.pick_long_session(acts[:3], START, END)["type"], "virtual_ride")

    def test_none_without_a_qualifying_session(self):
        acts = self.acts(("2026-03-03 06:00:00", "running", 3540.0), ("2026-03-04 06:00:00", "lap_swimming", 5400.0))
        self.assertIsNone(ws.pick_long_session(acts, START, END))
        self.assertIsNone(ws.pick_long_session(None, START, END))
        self.assertIsNone(ws.long_session_section(None, None))

    def test_interpretation_boundaries_and_sign(self):
        cases = ((-4.9, "stable"), (-4.96, "moderate_drift"), (-5.0, "moderate_drift"), (-10.0, "moderate_drift"),
                 (-10.1, "large_drift"), (4.9, "stable"), (5.0, "output_rose"), (0, "stable"))
        for pct, code in cases:
            self.assertEqual(ws.interpret_decoupling(pct)[0], code, pct)
        self.assertEqual(ws.interpret_decoupling(-6, "speed")[1], "Moderate drift: heart rate rose relative to speed")
        self.assertEqual(ws.interpret_decoupling(-12)[1], "Large drift: heart rate rose a lot relative to power")
        self.assertEqual(ws.interpret_decoupling(6, "speed")[1], "Speed rose relative to heart rate (not drift)")
        self.assertEqual(ws.interpret_decoupling(1)[1], "Stable: heart rate stayed in step with power")
        self.assertEqual(ws.interpret_decoupling(None), (None, None))

    def test_details_decoupling(self):
        drift = ws.decoupling_from_details(FX["activity_details"])
        self.assertEqual(drift["basis"], "power")
        self.assertTrue(-8 < drift["pct"] < -5, drift)
        no_power = copy.deepcopy(FX["activity_details"])
        no_power["metricDescriptors"] = [d for d in no_power["metricDescriptors"] if d["key"] != "directPower"]
        self.assertEqual(ws.decoupling_from_details(no_power)["basis"], "speed")
        short = copy.deepcopy(FX["activity_details"])
        short["activityDetailMetrics"] = short["activityDetailMetrics"][:40]
        self.assertIsNone(ws.decoupling_from_details(short))
        no_hr = copy.deepcopy(FX["activity_details"])
        no_hr["metricDescriptors"] = [d for d in no_hr["metricDescriptors"] if d["key"] != "directHeartRate"]
        self.assertIsNone(ws.decoupling_from_details(no_hr))
        self.assertIsNone(ws.decoupling_from_details(None))


# --- the whole report ----------------------------------------------------------

class EndToEnd(unittest.TestCase):
    def test_fixture_week(self):
        client = FakeClient()
        status = build(client)
        self.assertEqual((status["kind"], status["rules_version"], status["start"], status["end"]),
                         ("weekly_status", 1, "2026-03-01", "2026-03-07"))
        self.assertEqual((status["generated_at"], status["tz"]), ("2026-03-08T06:30:00+02:00", TZ_NAME))
        self.assertEqual([f["id"] for f in status["flags"]], ["short_nights", "gear_near_limit"])
        self.assertEqual((status["level"], status["level_en"]), ("yellow", "Yellow"))
        self.assertEqual(status["missing"], [])
        self.assertEqual(status["hrv"]["avg_28d"], 74)
        self.assertEqual(status["sleep"], {"avg_h": 6.53, "avg_hhmm": "6:32", "nights": 7, "nights_under_6h": 3,
                                           "avg_score": 72})
        self.assertEqual(status["load"]["chronic_7d_ago"], 731)
        self.assertEqual((status["load"]["chronic_change"], status["load"]["optimal_min"],
                          status["load"]["optimal_max"]), (45, 621, 1164))
        self.assertEqual(status["plan"], {"scheduled": 8, "completed": 7, "on_planned_day": 6, "linked": 5,
                                          "completion_pct": 88})
        long_session = status["long_session"]
        self.assertEqual((long_session["date"], long_session["date_en"], long_session["weekday_en"],
                          long_session["sport"]), ("2026-03-07", "7 Mar", "Saturday", "bike"))
        self.assertEqual((long_session["minutes"], long_session["hhmm"]), (185, "3:05"))
        self.assertEqual((long_session["decoupling_pct"], long_session["interpretation_code"],
                          long_session["source"]), (-6.1, "moderate_drift", "fit"))
        self.assertEqual(status["fitness"]["training_status_en"], "productive")
        self.assertEqual(status["load"]["acwr_status_en"], "optimal")
        self.assertEqual(status["body"]["weight_change_kg"], -0.4)
        self.assertEqual(status["gear"][0]["pct_of_max"], 93)
        self.assertTrue(all(meta["ok"] for meta in status["sources"].values()), status["sources"])
        self.assertNotIn("decoupling_details", status["sources"])
        self.assertNotIn("text", status)
        self.assertNotIn("debug", status)
        # The serial first call ran before anything else.
        self.assertEqual(client.names()[0], "get_device_last_used")
        self.assertEqual(client.names().count("get_sleep_data"), 7)
        self.assertEqual(client.names().count("get_hrv_data"), 4)
        (activities_call,) = [c for c in client.calls if c[0] == "connectapi"]
        self.assertEqual(activities_call[2]["params"]["startDate"], "2026-02-22")

    def test_default_end_date_is_yesterday(self):
        status = build(end_date="")
        self.assertEqual((status["start"], status["end"]), ("2026-03-01", "2026-03-07"))

    def test_malformed_end_date_raises(self):
        with self.assertRaises(ValueError):
            build(end_date="7/3/2026")
        for fmt in ("html", "telegram"):
            with self.subTest(fmt=fmt), self.assertRaises(ValueError):
                build(text_format=fmt)

    def test_failing_source_is_missing_not_fatal(self):
        client = FakeClient(fail={"get_training_status": RuntimeError("API Error 500 - boom")})
        status = build(client)
        self.assertIsNone(status["load"]["acute"])
        self.assertIsNone(status["fitness"]["vo2max_run"])
        for key in ("load.acute", "load.chronic_7d_ago", "fitness.training_status"):
            self.assertIn(key, status["missing"])
        self.assertFalse(status["sources"]["training_status@2026-03-07"]["ok"])
        self.assertIn("boom", status["sources"]["training_status@2026-03-07"]["error"])
        self.assertEqual(status["sleep"]["nights"], 7)
        self.assertEqual(status["level"], "yellow")

    def test_upstream_no_data_on_the_first_call_is_not_systemic(self):
        client = FakeClient(fail={"get_device_last_used": GarminConnectConnectionError(UPSTREAM_NO_DATA)})
        status = build(client)
        self.assertFalse(status["sources"]["device_last_used"]["ok"])
        self.assertEqual(client.names().count("get_sleep_data"), 7)
        self.assertEqual((status["hrv"]["avg_7d"], status["sleep"]["nights"]), (70, 7))
        self.assertEqual((status["volume"]["total_hours"], status["load"]["acute"]), (9.92, 812))
        self.assertEqual((status["plan"]["completed"], status["long_session"]["source"]), (7, "fit"))
        self.assertEqual(status["level"], "yellow")
        # Only the gear list needs that call's profile number.
        self.assertTrue(status["sources"]["gear"]["error"].startswith("skipped:"))
        self.assertIn("gear", status["missing"])

    def test_fit_failure_kind_decides_the_details_call(self):
        not_found = UPSTREAM_NO_DATA.replace("No data received from server", "API client error (404)")
        client = FakeClient(fail={"download_activity": GarminConnectConnectionError(not_found)})
        self.assertEqual(build(client)["long_session"]["source"], "details")
        client = FakeClient(fail={"download_activity": RuntimeError("Garmin rate limit hit: 429. Wait.")})
        status = build(client)
        self.assertNotIn("get_activity_details", client.names())
        self.assertTrue(status["sources"]["decoupling_details"]["error"].startswith("skipped:"))
        self.assertIn("long_session.decoupling_pct", status["missing"])

    def test_activity_list_failure_leaves_completion_missing(self):
        status = build(FakeClient(fail={"connectapi": RuntimeError("API Error 500 - boom")}))
        self.assertEqual(status["plan"], {"scheduled": 8, "completed": None, "on_planned_day": None, "linked": 5,
                                          "completion_pct": None})
        for key in ("volume", "plan.completed", "plan.on_planned_day", "plan.completion_pct", "long_session"):
            self.assertIn(key, status["missing"])
        self.assertNotIn("low_completion", [f["id"] for f in status["flags"]])

    def test_fit_failure_falls_back_to_details_without_gps(self):
        client = FakeClient()
        status = build(client, fit=fit_helpers(error=ValueError("bad FIT")))
        long_session = status["long_session"]
        self.assertEqual((long_session["source"], long_session["basis"]), ("details", "power"))
        self.assertTrue(-8 < long_session["decoupling_pct"] < -5)
        self.assertFalse(status["sources"]["decoupling_fit"]["ok"])
        (details_call,) = [c for c in client.calls if c[0] == "get_activity_details"]
        self.assertEqual(details_call[2]["maxpoly"], 0)

    def test_no_drift_anywhere_is_missing(self):
        client = FakeClient(fail={"get_activity_details": RuntimeError("API Error 404 - gone")})
        status = build(client, fit=fit_helpers(pct=None))
        self.assertIsNone(status["long_session"]["decoupling_pct"])
        self.assertIsNone(status["long_session"]["interpretation"])
        self.assertIn("long_session.decoupling_pct", status["missing"])

    def test_week_without_a_long_session(self):
        fx = copy.deepcopy(FX)
        fx["activities"] = [a for a in fx["activities"] if a["duration"] < 3600]
        client = FakeClient(fx=fx)
        status = build(client)
        self.assertIsNone(status["long_session"])
        self.assertIn("long_session", status["missing"])
        self.assertNotIn("download_activity", client.names())

    def test_systemic_first_failure_stops_every_other_call(self):
        client = FakeClient(fail={"get_device_last_used": RuntimeError("Garmin rate limit hit: 429. Wait.")})
        status = build(client)
        self.assertEqual(client.names(), ["get_device_last_used"])
        self.assertEqual(status["level"], "incomplete")
        self.assertEqual(status["level_en"], "Incomplete data")
        self.assertTrue(status["sources"]["gear"]["error"].startswith("skipped:"))
        for key in ("volume", "sleep", "plan", "gear", "body.weight_kg", "hrv.avg_7d"):
            self.assertIn(key, status["missing"])
        self.assertEqual(status["gear"], [])

    def test_deadline_bounds_the_report(self):
        client = FakeClient(delay={"get_weigh_ins": 1.5, "download_activity": 1.5})

        async def timed():
            started = time.monotonic()
            with mock.patch.object(ws, "_load_fit_helpers", fit_helpers()):
                status = await ws.build_weekly_status(client, "2026-03-07", tz=TZ_NAME, now=NOW)
            return status, time.monotonic() - started

        with mock.patch.object(ws, "DEADLINE_S", 1.0), mock.patch.object(ws, "CALL_TIMEOUT_S", 0.6):
            status, took = asyncio.run(timed())
        # About 1.0 s with the deadline; at least 2.1 s without it (0.6 s weigh-in timeout + 1.5 s FIT).
        self.assertLess(took, 1.8)
        self.assertIn("timeout", status["sources"]["weigh_ins"]["error"])
        self.assertIn("body.weight_kg", status["missing"])
        self.assertIn("long_session.decoupling_pct", status["missing"])
        self.assertTrue(status["sources"]["decoupling_details"]["error"].startswith("skipped:"))
        self.assertEqual(status["sleep"]["nights"], 7)

    def test_text_and_debug_on_request(self):
        status = build(include_sources=True, text_format="markdown")
        self.assertTrue(status["text"].startswith("**Weekly summary 1 Mar–7 Mar**"))
        self.assertIn("hrv@2026-03-07", status["debug"])
        self.assertIn("hrvSummary.weeklyAvg", status["debug"]["hrv@2026-03-07"]["keys"])
        self.assertEqual(status["debug"]["hrv@2026-03-07"]["slim"]["weekly_avg"], 70)
        self.assertIn("mostRecentTrainingStatus.latestTrainingStatusData.<id>",
                      status["debug"]["training_status@2026-03-07"]["keys"])

    def test_no_identifiers_leave_the_module(self):
        client = FakeClient(fail={"get_gear_stats": RuntimeError(
            "API Error 404 - gear 0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a01 of 71000001 not found")})
        status = build(client, include_sources=True, text_format="markdown")
        text = json.dumps(status, ensure_ascii=False)
        planted = planted_ids()
        self.assertGreaterEqual(len(planted), 30)  # the walk found the fixtures' ids
        for value in planted:
            self.assertNotIn(value, text)
        self.assertEqual(IDENTIFIER_KEYS & set(keys_of(status)), set())
        self.assertIn("<id>", status["sources"]["gear_stats_1"]["error"])


# --- text ---------------------------------------------------------------------

def worst_case_status():
    status = build(text_format="")
    # Special characters first: names are cut at 30 characters.
    name = "<Trainer> & *marks* and a very long synthetic gear name"
    status["gear"] = [{"name": f"{name} {i}", "type": "shoes", "km": 900, "max_km": 800, "pct_of_max": 113}
                      for i in range(5)]
    status["long_session"]["name"] = "A " * 40
    status["flags"] = ws.evaluate_flags(neutral(
        hrv={"avg_7d": 50, "avg_28d": 70}, sleep={"nights_under_6h": 6},
        volume={"total_hours": 20.0, "prev_total_hours": 5.0}, load={"acwr_status": "VERY_HIGH"},
        plan={"scheduled": 9, "completion_pct": 10}, gear=status["gear"]))
    status["level"] = ws.compute_level(status["flags"], hrv_missing=False, sleep_missing=False,
                                       volume_missing=False)
    status["level_en"] = ws.LEVEL_EN[status["level"]]
    return status


class Render(unittest.TestCase):
    def test_markdown_escaping(self):
        status = worst_case_status()
        status["long_session"]["name"] = "Ride <script> & co"
        text = ws.render_text(status, "markdown")
        self.assertIn("\\<Trainer\\> & \\*marks\\*", text)
        self.assertEqual(text.count("Gear: "), 4)  # three items and "2 more"
        self.assertNotIn("<script>", text)
        self.assertIn("**Flags:**", text)
        self.assertIn("Level: **Red**", text)

    def test_markdown(self):
        text = ws.render_text(worst_case_status(), "markdown")
        self.assertNotIn("<b>", text)
        self.assertTrue(text.startswith("**Weekly summary 1 Mar–7 Mar**"))
        self.assertIn("\n- HRV: ", text)
        self.assertIn("\\*marks\\*", text)

    def test_markdown_is_well_formed_commonmark(self):
        for status in (build(), worst_case_status()):
            lines = ws.render_text(status, "markdown").split("\n")
            self.assertTrue(lines[0] and lines[-1])
            for i, line in enumerate(lines):
                if not line:
                    # One blank line between blocks, never inside a group of bullets.
                    self.assertTrue(lines[i - 1] and lines[i + 1], i)
                    self.assertFalse(lines[i + 1].startswith("- "), lines[i + 1])
                    continue
                # Left-to-right English: no bidi marks anywhere.
                self.assertNotIn("\u200f", line)
                self.assertNotIn("\u200e", line)
                if i and not line.startswith("- "):
                    self.assertEqual(lines[i - 1], "", line)  # a non-bullet line starts a new block
            headings = [i for i, line in enumerate(lines) if line.startswith("**") and line.endswith("**")]
            self.assertEqual(len(headings), 6)  # the title and five sections
            for i in headings[1:]:
                self.assertTrue(lines[i + 1].startswith("- "), lines[i])  # its bullets follow directly
            self.assertTrue(lines[-1].startswith("**Flags:**"))
            self.assertEqual(lines[-2], "")

    def test_word_limits(self):
        full = build(text_format="markdown")
        self.assertLessEqual(ws._word_count(full["text"]), ws.MAX_WORDS)
        self.assertIn("(Synthetic Long Ride)", full["text"])  # the full form fits
        self.assertLessEqual(ws._word_count(ws.render_text(worst_case_status(), "markdown")), ws.MAX_WORDS)

    def test_fixture_text_content(self):
        text = build(text_format="markdown")["text"]
        for piece in ("7-day average 70", "average 6:32 over 7 nights", "167.4 km", "9:55 h", "+14%",
                      "Plan: 7 of 8 done (88%), 6 of them on the planned day",
                      "Optimal range for acute load: 621–1164",
                      "Long session: bike on Saturday 7 Mar · 3:05 h",
                      "HR drift -6.1% · Moderate drift", "70.2 kg (weighed 5 Mar, change -0.4 kg)",
                      "742 of 800 km (93%)", "Other: 0:30 · 1 session", "3 sessions", "ACWR 1.05 (optimal)",
                      "training status: productive", "load balance: low aerobic shortage"):
            self.assertIn(piece, text)
        self.assertNotIn("linked", text)
        self.assertNotIn("Synthetic Race Shoe", text)  # only gear at the limit is listed

    def test_plan_line_names_the_planned_day_only_when_sessions_moved(self):
        status = build()
        status["plan"]["on_planned_day"] = status["plan"]["completed"]
        text = ws.render_text(status, "markdown")
        self.assertIn("Plan: 7 of 8 done (88%)\n", text)
        self.assertNotIn("of them", text)

    def test_nulls_print_no_data(self):
        dead = FakeClient(fail={name: RuntimeError("API Error 500") for name in (
            "get_device_last_used", "get_devices", "get_hrv_data", "get_sleep_data", "connectapi",
            "get_training_status", "query_garmin_graphql", "get_weigh_ins")})
        status = build(dead, text_format="markdown")
        self.assertEqual(status["level"], "incomplete")
        self.assertIn("no data", status["text"])
        self.assertNotIn("None", status["text"])
        self.assertIsNone(status["long_session"])
        ws.render_text({}, "markdown")  # an empty dict renders too

    def test_output_is_english_only(self):
        """No Hebrew letters and no *_he key in the JSON or the text.

        Names are Garmin data and pass through as typed; the fixture's names
        are English, so every string here must be free of Hebrew."""
        hebrew = re.compile("[\u0590-\u05ff\ufb1d-\ufb4f]")
        for status in (build(include_sources=True, text_format="markdown"), worst_case_status()):
            status.setdefault("text", ws.render_text(status, "markdown"))
            self.assertIsNone(hebrew.search(json.dumps(status, ensure_ascii=False)))
            self.assertEqual([k for k in keys_of(status) if k.endswith("_he")], [])
        status = build()
        self.assertEqual(
            {k for k in keys_of(status) if k.endswith("_en")},
            {"level_en", "status_en", "acwr_status_en", "training_status_en", "training_balance_en",
             "weighed_on_en", "date_en", "weekday_en", "label_en"},
        )

    def test_a_hebrew_name_passes_through(self):
        status = build()
        status["long_session"]["name"] = "\u05e8\u05db\u05d9\u05d1\u05d4 \u05d0\u05e8\u05d5\u05db\u05d4"
        # Wrapped in invisible direction isolates, so its words keep their order in an English line.
        self.assertIn(f"(\u2068{status['long_session']['name']}\u2069)", ws.render_text(status, "markdown"))

    def test_unknown_format_rejected(self):
        for fmt in ("html", "telegram"):
            with self.subTest(fmt=fmt), self.assertRaises(ValueError):
                ws.render_text({}, fmt)
        self.assertEqual(ws.TEXT_FORMATS, ("", "markdown"))


# --- MCP tool -----------------------------------------------------------------

class Tool(unittest.TestCase):
    def setUp(self):
        self.tools = {}
        test = self

        class App:
            def tool(self):
                def deco(fn):
                    test.tools[fn.__name__] = fn
                    return fn
                return deco

        self.client = FakeClient()
        ws.register_tools(App(), lambda: self.client)
        # The tool reads REPORT_TZ (required, no default): set it explicitly.
        env = mock.patch.dict(os.environ, {"REPORT_TZ": TZ_NAME})
        env.start()
        self.addCleanup(env.stop)

    def call(self, **kw):
        with mock.patch.object(ws, "_load_fit_helpers", fit_helpers()):
            return asyncio.run(self.tools["get_weekly_status"](**kw))

    def test_unset_or_bad_report_tz_is_error_text(self):
        # No default zone: an unset REPORT_TZ is a clear error, not a crash or a guess.
        with mock.patch.dict(os.environ, {}, clear=True):
            out = self.call(end_date="2026-03-07")
        self.assertTrue(out.startswith("Error: REPORT_TZ is not set"), out)
        with mock.patch.dict(os.environ, {"REPORT_TZ": "Mars/Base"}):
            out = self.call(end_date="2026-03-07")
        self.assertTrue(out.startswith("Error: REPORT_TZ: unknown time zone 'Mars/Base'"), out)

    def test_returns_compact_json(self):
        out = self.call(end_date="2026-03-07", text_format="markdown")
        self.assertNotIn("\n", out.split('"text"')[0])
        status = json.loads(out)
        self.assertEqual((status["kind"], status["level"]), ("weekly_status", "yellow"))
        self.assertIn("text", status)

    def test_errors_only_for_bad_arguments(self):
        self.assertTrue(self.call(end_date="2026-02-30").startswith("Error: end_date must be YYYY-MM-DD"))
        for fmt in ("html", "telegram"):
            self.assertTrue(self.call(text_format=fmt).startswith("Error: text_format"), fmt)
        broken = FakeClient(fail={"get_hrv_data": RuntimeError("API Error 500")})
        self.client = broken
        self.assertFalse(self.call(end_date="2026-03-07").startswith("Error"))


# --- upstream -----------------------------------------------------------------

@unittest.skipUnless(HAVE_UPSTREAM, "garmin_mcp not installed")
class Upstream(unittest.TestCase):
    def test_selfcheck_passes(self):
        ws.selfcheck()

    def test_fit_helpers_are_upstreams(self):
        from garmin_mcp.activity_analysis import _parse_fit
        fmt, parse_fit, available = ws._load_fit_helpers()
        self.assertIs(parse_fit, _parse_fit)
        self.assertEqual(fmt.name, "ORIGINAL")
        self.assertTrue(available)

    def test_upstream_drift_sign_matches_the_interpretation(self):
        from garmin_mcp.activity_analysis import _compute_hr_drift
        t0 = dt.datetime(2026, 3, 7, 5, 30)
        records = [{"timestamp": str(t0 + dt.timedelta(seconds=s)), "power_w": 200,
                    "heart_rate_bpm": 130 if s < 2700 else 140} for s in range(5400)]
        pct = _compute_hr_drift(records)["hr_drift_pct"]
        self.assertLess(pct, 0)  # heart rate rose against steady power
        self.assertEqual(ws.interpret_decoupling(pct)[0], "moderate_drift")

    def test_device_selection_follows_upstream(self):
        ts = ws.parse_training_status(FX["training_status"]["2026-03-07"], FX["devices"])
        self.assertEqual((ts["acute"], ts["training_balance"], ts["other_device"]), (812, "AEROBIC_LOW_SHORTAGE", False))
        # The primary tracker is a third device absent from the map: the load
        # on record belongs to another device and is not reported as the athlete's.
        other = [{"deviceId": 72000009, "primaryDevice": True}]
        ts = ws.parse_training_status(FX["training_status"]["2026-03-07"], other)
        self.assertTrue(ts["other_device"])
        self.assertIsNone(ts["acute"])
        self.assertEqual(ts["training_status"], "PRODUCTIVE_2")


if __name__ == "__main__":
    unittest.main()
