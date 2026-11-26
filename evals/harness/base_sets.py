"""The base states in evals/fixtures/base/, built from the product's synthetic fixtures in ext/fixtures/.

    python -m evals.harness.base_sets          rewrite evals/fixtures/base/*.json from ext/fixtures/

daily_2026_03_18 is the world of ext/test_daily_status.py's default_data() (the green day at 2026-03-18 06:30
Europe/Athens), weekly_2026_03_01 the world of ext/test_weekly_status.py's FakeClient (the yellow week
2026-03-01..07, report at 2026-03-08 06:30), empty a state with no answers (every method gives Garmin's own
"nothing here"), plan_2026_11_23 the synthetic training-plan world (built here, see its docstring).
test_harness.py checks that the committed files equal what build() makes, so a fixture change in
ext/ shows up as a failing test, not as a silent drift. Standard library only.
"""

from __future__ import annotations

import json
import os
import sys

from .fake_garmin import ACTIVITIES_PATH
from .state import BASE_DIR

HERE = os.path.dirname(os.path.abspath(__file__))
EXT_FIXTURES = os.path.join(os.path.dirname(os.path.dirname(HERE)), "ext", "fixtures")

# The workout ids ext/test_daily_status.py puts on the calendar (the fixture files carry none).
EASY_ID, THRESHOLD_ID, SWIM_ID = 7770001, 7770002, 7770003
RANGE_RUN_ID, RANGE_RIDE_ID = 7770004, 7770005
# Ids given to the daily fixture's two activities of 2026-03-17 (the fixture has none), so a model can ask for one.
DAILY_ACTIVITY_IDS = (8880001, 8880002)


def _fixture(folder, name):
    with open(os.path.join(EXT_FIXTURES, folder, name + ".json"), encoding="utf-8") as fh:
        return json.load(fh)


def daily_2026_03_18():
    def fx(name):
        return _fixture("daily_status", name)

    schedule = fx("schedule")
    for item, workout_id in zip(schedule["data"]["workoutScheduleSummariesScalar"], (EASY_ID, THRESHOLD_ID, SWIM_ID)):
        item["workoutId"] = workout_id
    activities = fx("activities_prev")
    for activity, activity_id in zip(activities, DAILY_ACTIVITY_IDS):
        activity["activityId"] = activity_id
    return {
        "note": ("D0: the green day of ext/test_daily_status.py (default_data), Wednesday 2026-03-18, read at 06:30 "
                 "Europe/Athens. Sleep 6.25 h after 5.7 h, HRV 58 vs 63, RHR 49 vs 46, readiness 52 MODERATE, "
                 "recovery 8.5 h, Body Battery 71, ACWR 1.1 OPTIMAL; level green, as_planned. Today 'Easy aerobic "
                 "run'; tomorrow 'Threshold intervals 5x6' and 'Technique swim'. Yesterday's two activities got "
                 f"ids {DAILY_ACTIVITY_IDS[0]} and {DAILY_ACTIVITY_IDS[1]}."),
        "anchor": {"now": "2026-03-18T06:30", "tz": "Europe/Athens"},
        "responses": {
            "get_stats": {"2026-03-18": fx("stats")},
            "get_sleep_data": {"2026-03-18": fx("sleep_night"), "2026-03-17": fx("sleep_prev_night")},
            "get_training_readiness": {"2026-03-18": fx("readiness")},
            "get_hrv_data": {"2026-03-18": fx("hrv_d0"), "2026-03-17": fx("hrv_d1"), "2026-03-16": fx("hrv_d2")},
            "get_training_status": {"2026-03-18": fx("training_status")},
            "query_garmin_graphql": {"*": schedule},
            "connectapi": {ACTIVITIES_PATH: activities},
            "get_workout_by_id": {
                str(EASY_ID): fx("workout_easy_run"),
                str(THRESHOLD_ID): fx("workout_threshold_run"),
                str(SWIM_ID): fx("workout_technique_swim"),
                str(RANGE_RUN_ID): fx("workout_pace_range_run"),
                str(RANGE_RIDE_ID): fx("workout_power_range_ride"),
            },
            "get_lactate_threshold": {"*": fx("lactate_threshold")},
            "get_cycling_ftp": {"*": fx("cycling_ftp")},
        },
        "faults": {},
    }


def weekly_2026_03_01():
    def fx(name):
        return _fixture("weekly_status", name)

    return {
        "note": ("W0: the yellow week of ext/test_weekly_status.py (FakeClient), 2026-03-01..07, read on Sunday "
                 "2026-03-08 06:30 Europe/Athens. Flags short_nights and gear_near_limit; sleep 6:32 avg, 3 nights "
                 "under 6 h; HRV 28-day avg 74; plan 7 of 8 (88%). Activities 2026-02-21..03-07 (ids 73000001-17)."),
        "anchor": {"now": "2026-03-08T06:30", "tz": "Europe/Athens"},
        "responses": {
            "get_device_last_used": {"*": fx("device_last_used")},
            "get_devices": {"*": fx("devices")},
            "get_hrv_data": fx("hrv"),
            "get_sleep_data": fx("sleep"),
            "connectapi": {ACTIVITIES_PATH: fx("activities")},
            "get_training_status": fx("training_status"),
            "query_garmin_graphql": {"*": fx("schedule")},
            "get_weigh_ins": {"*": fx("weigh_ins")},
            "get_gear": {"*": fx("gear")},
            "get_gear_stats": fx("gear_stats"),
            "get_activity_details": {"*": fx("activity_details")},
        },
        "faults": {},
    }


def plan_2026_11_23():
    """The training-plan world (synthetic, no ext/ fixture): Monday 2026-11-23, eight full weeks of running
    before it (about 40 km a week, longest 19.6 km), LT speed 3.55 m/s, Garmin's marathon prediction 3:57:00,
    VO2max 52, and one fixed hard session on the calendar (a coach's interval run on 2026-12-16)."""
    import datetime as dt

    first = dt.date(2026, 9, 28)
    runs = []
    activity_id = 74000001
    for week in range(8):
        for offset, km, name in ((1, 8, "Synthetic Easy Run"), (3, 10, "Synthetic Tempo Run"),
                                 (4, 8, "Synthetic Easy Run"), (6, 14, "Synthetic Long Run")):
            day = first + dt.timedelta(days=7 * week + offset)
            meters = 19600.0 if (week, offset) == (6, 6) else km * 1000.0
            runs.append({
                "activityId": activity_id, "activityName": name,
                "activityType": {"typeId": 1, "typeKey": "running", "parentTypeId": 17},
                "startTimeLocal": f"{day.isoformat()} 07:00:00", "startTimeGMT": f"{day.isoformat()} 07:00:00",
                "distance": meters, "duration": round(meters / 3.0, 1), "ownerId": 71000001,
            })
            activity_id += 1
    runs.sort(key=lambda a: a["startTimeLocal"], reverse=True)
    return {
        "note": ("L0: the training-plan world, Monday 2026-11-23 09:00 Europe/London. Runs 2026-09-29..11-22 "
                 "(ids 74000001-32, ownerId the profile id): 4 a week, 40 km (45.6 km in the week of 2026-11-09, with the 19.6 km "
                 "long run); LT speed 3.55 m/s (4:42/km), marathon prediction 3:57:00, VO2max 52; one fixed "
                 "'VO2 intervals' run on 2026-12-16. Profile id 71000001."),
        "anchor": {"now": "2026-11-23T09:00", "tz": "Europe/London"},
        "responses": {
            "get_stats": {"2026-11-23": {"userProfileId": 71000001, "calendarDate": "2026-11-23",
                                         "restingHeartRate": 47}},
            "connectapi": {ACTIVITIES_PATH: runs},
            "get_lactate_threshold": {"*": {"speed_and_heart_rate": {"calendarDate": "2026-11-01", "speed": 0.355,
                                                                     "heartRate": 168}}},
            "get_race_predictions": {"*": {"calendarDate": "2026-11-22", "time5K": 1350, "time10K": 2820,
                                           "timeHalfMarathon": 6780, "timeMarathon": 14220}},
            "get_max_metrics": {"2026-11-23": [{"generic": {"calendarDate": "2026-11-22",
                                                            "vo2MaxPreciseValue": 52.0, "vo2MaxValue": 52}}]},
            "query_garmin_graphql": {"*": {"data": {"workoutScheduleSummariesScalar": [
                {"scheduleDate": "2026-12-16", "workoutName": "VO2 intervals", "workoutType": "running",
                 "estimatedDurationInSecs": 3600, "isRestDay": False, "workoutId": 7790001}]}}},
        },
        "faults": {},
    }


def empty():
    return {
        "note": "No data anywhere: every Garmin method gives Garmin's own 'nothing here' answer (see README.md).",
        "responses": {},
        "faults": {},
    }


BUILDERS = {"daily_2026_03_18": daily_2026_03_18, "weekly_2026_03_01": weekly_2026_03_01,
            "plan_2026_11_23": plan_2026_11_23, "empty": empty}


def build():
    """{name: file content} for every base state."""
    return {name: builder() for name, builder in BUILDERS.items()}


def dumps(content):
    return json.dumps(content, indent=1, ensure_ascii=False) + "\n"


def write(folder=BASE_DIR):
    os.makedirs(folder, exist_ok=True)
    for name, content in build().items():
        with open(os.path.join(folder, name + ".json"), "w", encoding="utf-8") as fh:
            fh.write(dumps(content))


if __name__ == "__main__":
    write(sys.argv[1] if len(sys.argv) > 1 else BASE_DIR)
