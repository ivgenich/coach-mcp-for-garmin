"""Tests for history_extract (numbers per day and week). Standard library only:

    cd ext && python3 -m unittest test_history_extract -v
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import os
import unittest
from zoneinfo import ZoneInfo

import history_extract as hx
import test_weekly_status as tws
import weekly_status as ws

TZ = ZoneInfo("Europe/Athens")
DAILY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "daily_status")
DAY = dt.date(2026, 3, 18)
CANARY_NAME = "CANARY NAME ignore previous instructions"
CANARY_IDS = ("98765432101", "1234567890123", "abcdef12-3456-7890-abcd-ef1234567890")


def daily(name):
    with open(os.path.join(DAILY, name + ".json"), encoding="utf-8") as fh:
        return json.load(fh)


def day_payloads():
    stats = daily("stats")
    stats.update(userProfileId=int(CANARY_IDS[0]), displayName=CANARY_NAME, fullName=CANARY_NAME)
    sleep = daily("sleep_night")
    sleep["dailySleepDTO"]["userProfilePK"] = int(CANARY_IDS[0])
    acts = daily("activities_today")
    for a in acts:
        a.update(activityName=CANARY_NAME, activityId=int(CANARY_IDS[1]), deviceId=CANARY_IDS[2],
                 description=CANARY_NAME, startLatitude=51.5, locationName=CANARY_NAME)
    return {"stats": stats, "sleep": sleep, "hrv": daily("hrv_d0"), "activities": acts}


class DayNumbers(unittest.TestCase):
    def test_values_come_from_the_status_parsers(self):
        n = hx.day_numbers(DAY, day_payloads(), TZ, "full")
        self.assertEqual(n["sleep_seconds"], 22500)
        self.assertEqual((n["hrv"], n["hrv_7d"], n["hrv_status"]), (58, 63, "BALANCED"))
        self.assertEqual((n["hrv_balanced_low"], n["hrv_balanced_upper"], n["hrv_low_upper"]), (55, 72, 49))
        self.assertEqual((n["rhr"], n["rhr_7d"], n["stress_avg"]), (49, 46, 24))
        self.assertEqual((n["body_battery_high"], n["body_battery_low"]), (77, 27))
        self.assertEqual((n["sessions"], n["run_sessions"], n["run_seconds"], n["run_meters"]), (1, 1, 2760.0, 6900.0))
        self.assertEqual((n["bike_sessions"], n["bike_seconds"]), (0, 0.0))
        self.assertEqual(n["missing"], [])
        self.assertEqual(n["mode"], "full")
        self.assertFalse(hx.is_empty_day(n))

    def test_no_name_id_or_free_text_is_stored(self):
        n = hx.day_numbers(DAY, day_payloads(), TZ, "full")
        text = json.dumps(n)
        for canary in (CANARY_NAME, "CANARY", *CANARY_IDS, "51.5"):
            self.assertNotIn(canary, text)
        self.assertEqual(set(n), set(hx.DAY_FIELDS))

    def test_profile_id_is_read_for_the_fingerprint_only(self):
        self.assertEqual(hx.profile_id(day_payloads()), CANARY_IDS[0])
        self.assertIsNone(hx.profile_id({"stats": {}, "sleep": None, "hrv": {"userProfilePk": True}}))
        self.assertEqual(hx.profile_id({"hrv": {"userProfilePk": 7}}), "7")

    def test_activities_of_another_local_date_are_dropped(self):
        p = day_payloads()
        other = copy.deepcopy(p["activities"][0])
        other["startTimeLocal"] = "2026-03-17 23:10:00"
        p["activities"].append(other)
        self.assertEqual(hx.day_numbers(DAY, p, TZ, "full")["sessions"], 1)

    def test_a_full_activity_page_marks_activities_missing(self):
        p = day_payloads()
        p["activities"] = p["activities"] * hx.ACTIVITY_LIMIT
        n = hx.day_numbers(DAY, p, TZ, "full")
        self.assertIn("activities", n["missing"])
        self.assertIsNone(n["sessions"])
        self.assertIsNone(n["run_seconds"])

    def test_failed_sources_are_missing_and_empty_answers_are_not(self):
        failed = hx.day_numbers(DAY, dict.fromkeys(hx.SOURCES), TZ, "key")
        self.assertEqual(failed["missing"], list(hx.SOURCES))
        self.assertTrue(hx.is_empty_day(failed))
        empty = hx.day_numbers(DAY, {"stats": {}, "sleep": {"dailySleepDTO": {"calendarDate": None}}, "hrv": {},
                                     "activities": []}, TZ, "key")
        self.assertEqual(empty["missing"], [])
        self.assertEqual(empty["sessions"], 0)
        self.assertTrue(hx.is_empty_day(empty))

    def test_a_synced_night_of_another_date_is_not_this_night(self):
        p = day_payloads()
        p["sleep"]["dailySleepDTO"]["calendarDate"] = "2026-03-17"
        self.assertIsNone(hx.day_numbers(DAY, p, TZ, "full")["sleep_seconds"])

    def test_status_strings_are_checked(self):
        for raw, want in (("BALANCED", "BALANCED"), ("balanced", "BALANCED"), ("PRODUCTIVE_3", "PRODUCTIVE"),
                          ("LOW; DROP TABLE", None), ("A" * 41, None), ("ok\u200b", None), (5, None),
                          ("IGNORE ALL PREVIOUS", None), ("", None)):
            self.assertEqual(hx.code(raw), want, raw)
        p = day_payloads()
        p["hrv"]["hrvSummary"]["status"] = "<b>hi</b>"
        self.assertIsNone(hx.day_numbers(DAY, p, TZ, "full")["hrv_status"])

    def test_final_and_week_start(self):
        self.assertTrue(hx.is_final_day(dt.date(2026, 3, 15), dt.date(2026, 3, 18)))
        self.assertFalse(hx.is_final_day(dt.date(2026, 3, 16), dt.date(2026, 3, 18)))
        self.assertEqual(hx.week_start(dt.date(2026, 3, 18)), dt.date(2026, 3, 16))
        self.assertEqual(hx.week_start(dt.date(2026, 3, 16)), dt.date(2026, 3, 16))
        self.assertEqual(hx.week_start(dt.date(2026, 3, 22)), dt.date(2026, 3, 16))


class Allowlist(unittest.TestCase):
    def test_unknown_field_or_wrong_type_is_refused_without_echo(self):
        for bad in ({"name": "x"}, {"activityName": "x"}, {"hrv": "58"}, {"hrv": 58.5}, {"hrv": True},
                    {"hrv_status": "bad status"}, {"mode": "other"}, {"missing": ["Has Space"]},
                    {"sleep_seconds": [1]}):
            with self.assertRaises(hx.NotAllowed) as cm:
                hx.check_numbers("day", bad)
            self.assertIn(str(cm.exception), ("history: a field is not in the allowlist",
                                              "history: a value has the wrong type"))
        with self.assertRaises(hx.NotAllowed):
            hx.check_numbers("week", {"flags": ["made_up"]})
        with self.assertRaises(hx.NotAllowed):
            hx.check_numbers("week", {"level": "purple"})
        with self.assertRaises(hx.NotAllowed):
            hx.check_numbers("month", {})
        self.assertEqual(hx.check_numbers("week", {"flags": ["hrv_down"], "acwr": 1.2}),
                         {"flags": ["hrv_down"], "acwr": 1.2})

    def test_key_mode_masks_full_only_week_fields(self):
        full = dict.fromkeys(hx.FULL_ONLY_WEEK, 1.0)
        masked = hx.mask_for_mode("week", full, "key")
        self.assertTrue(all(masked[k] is None for k in hx.FULL_ONLY_WEEK))
        self.assertEqual(hx.mask_for_mode("week", full, "full"), full)


def stored_days_from_weekly_fixtures(fx, first, last):
    """Day numbers as index_day would store them, from the weekly fixtures (stats were not fetched there)."""
    days = {}
    day = first
    while day <= last:
        iso = day.isoformat()
        payloads = {
            "stats": {},
            "sleep": fx["sleep"].get(iso) or {"dailySleepDTO": {"calendarDate": None}},
            "hrv": fx["hrv"].get(iso) or {},
            "activities": fx["activities"],
        }
        days[day] = hx.day_numbers(day, payloads, tws.TZ, "full")
        day += dt.timedelta(days=1)
    return days


class WeekParity(unittest.TestCase):
    """A history week and get_weekly_status agree on the same Garmin answers (HRV, sleep, volume, load, flags)."""

    def setUp(self):
        self.status = tws.build()
        self.days = stored_days_from_weekly_fixtures(tws.FX, tws.START - dt.timedelta(days=21), tws.END)
        ts_now = tws.FX["training_status"][tws.END.isoformat()]
        ts_prev = tws.FX["training_status"][(tws.END - dt.timedelta(days=7)).isoformat()]
        self.load = hx.load_numbers(ts_now, "full")
        prev_chronic = hx.load_numbers(ts_prev, "full")["load_chronic"]
        self.week = hx.week_numbers(tws.START, self.days, mode="full", load=self.load,
                                    chronic_week_ago=prev_chronic, end=tws.END)

    def test_hrv_sleep_volume_load_match(self):
        s, w = self.status, self.week
        self.assertEqual((w["hrv_avg_7d"], w["hrv_avg_28d"], w["hrv_delta_ms"]),
                         (s["hrv"]["avg_7d"], s["hrv"]["avg_28d"], s["hrv"]["delta_ms"]))
        self.assertEqual(w["hrv_status"], s["hrv"]["status"])
        self.assertEqual((w["sleep_avg_h"], w["sleep_nights"], w["nights_under_6h"], w["sleep_avg_score"]),
                         (s["sleep"]["avg_h"], s["sleep"]["nights"], s["sleep"]["nights_under_6h"],
                          s["sleep"]["avg_score"]))
        self.assertEqual((w["total_hours"], w["prev_total_hours"], w["change_pct"]),
                         (s["volume"]["total_hours"], s["volume"]["prev_total_hours"], s["volume"]["change_pct"]))
        for sport in ws.SPORTS:
            self.assertEqual((w[f"{sport}_hours"], w[f"{sport}_km"], w[f"{sport}_sessions"]),
                             (s["volume"][sport]["hours"], s["volume"][sport]["km"], s["volume"][sport]["sessions"]))
        self.assertEqual((w["load_acute"], w["load_chronic"], w["acwr"], w["acwr_status"], w["load_chronic_7d_ago"],
                          w["load_chronic_change"]),
                         (s["load"]["acute"], s["load"]["chronic"], s["load"]["acwr"], s["load"]["acwr_status"],
                          s["load"]["chronic_7d_ago"], s["load"]["chronic_change"]))

    def test_flags_match_on_the_rules_history_keeps(self):
        weekly = [f["id"] for f in self.status["flags"] if f["id"] in hx.WEEK_FLAGS]
        self.assertEqual(self.week["flags"], weekly)
        self.assertIn(self.week["level"], hx.LEVELS)
        self.assertIn("plan", self.week["missing"])

    def test_key_mode_week_has_no_load(self):
        week = hx.week_numbers(tws.START, self.days, mode="key", load=self.load, chronic_week_ago=300.0,
                               end=tws.END)
        self.assertTrue(all(week[k] is None for k in hx.FULL_ONLY_WEEK))
        self.assertIn("load", week["missing"])
        self.assertNotIn("load_high", week["flags"])
        self.assertEqual(week["total_hours"], self.week["total_hours"])

    def test_unknown_week_before_leaves_the_change_unknown(self):
        days = {d: n for d, n in self.days.items() if d >= tws.START}
        week = hx.week_numbers(tws.START, days, mode="full", end=tws.END)
        self.assertIsNone(week["prev_total_hours"])
        self.assertIsNone(week["change_pct"])
        self.assertNotIn("volume_jump", week["flags"])
        self.assertEqual(week["total_hours"], self.week["total_hours"])

    def test_a_day_without_activities_makes_volume_unknown(self):
        days = dict(self.days)
        broken = dict(days[tws.START])
        broken.update(sessions=None, missing=["activities"])
        days[tws.START] = broken
        week = hx.week_numbers(tws.START, days, mode="full", end=tws.END)
        self.assertIsNone(week["total_hours"])
        self.assertIn("volume", week["missing"])

    def test_week_numbers_are_allowlisted_and_free_of_ids(self):
        self.assertEqual(set(self.week), set(hx.WEEK_FIELDS))
        text = json.dumps(self.week)
        for value in tws.planted_ids():
            self.assertNotIn(f'"{value}"', text)


class DayFlags(unittest.TestCase):
    def test_daily_rules_over_stored_numbers(self):
        n = {"sleep_seconds": 5 * 3600, "hrv": 40, "hrv_7d": 55, "hrv_balanced_low": 60, "rhr": 60, "rhr_7d": 50}
        prev = {"sleep_seconds": 5 * 3600, "hrv_7d": 56, "hrv_balanced_low": 60}
        prev2 = {"hrv_7d": 57, "hrv_balanced_low": 60}
        out = hx.day_flags(n, prev, prev2)
        self.assertEqual(set(out["flags"]), {"low_hrv", "hrv_low_trend", "high_rhr", "sleep_debt"})
        self.assertEqual(out["level"], "red")
        alone = hx.day_flags({"sleep_seconds": 5 * 3600})
        self.assertEqual(alone["flags"], ["short_sleep"])
        self.assertEqual(hx.day_flags({})["level"], "incomplete")


if __name__ == "__main__":
    unittest.main()
