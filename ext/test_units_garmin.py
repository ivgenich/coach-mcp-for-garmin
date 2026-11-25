"""Unit assumptions pinned to recorded-shape fixtures:

    python3 -m unittest discover -s ext -p "test_units_*.py" -v

Garmin answers the same quantity in different units on different endpoints:
fitnessstats (get_progress_summary_between_dates) sums durations in
milliseconds and distance / elevation in centimeters, while the activity list
gives seconds and meters. The fixtures in fixtures/units/ are synthetic,
in Garmin's shapes; each carries a "_note" naming the fields and units it
pins. A test here fails when a conversion's factor changes, so a unit change
is a deliberate edit of the fixture and its note.
"""

import datetime as dt
import importlib.util
import json
import os
import unittest
from unittest import mock
from zoneinfo import ZoneInfo

import mcp2_compat  # noqa: E402

# On mcp 2.x, serve garmin_mcp's `from mcp.server.fastmcp import FastMCP` from
# the adapter, as the image's .pth file does; a no-op on mcp 1.x.
mcp2_compat.register_finder()

import daily_status as ds
import launch
import status_common as sc
import weekly_status as ws

HAVE_UPSTREAM = importlib.util.find_spec("garmin_mcp") is not None
NEEDS_UPSTREAM = "needs upstream garmin_mcp (its progress-summary tool)"
UNITS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "units")
TZ = ZoneInfo("Europe/Athens")
WEEK_START, WEEK_END = dt.date(2026, 11, 16), dt.date(2026, 11, 22)


def recorded(name):
    """(the fixture's answer, its note)."""
    with open(os.path.join(UNITS, name + ".json"), encoding="utf-8") as fh:
        data = json.load(fh)
    return data["answer"], data["_note"]


def curated_progress(raw, request):
    """Upstream's get_progress_summary_between_dates curation for a non-calorie metric (sum/avg/min/max as
    Garmin sends them): the shape add_to_answer() receives."""
    metric = request["metric"]
    by_type = {}
    for sport, stats in (raw[0].get("stats") or {}).items():
        row = stats.get(metric)
        if row and row.get("count", 0) > 0:
            by_type[sport] = {k: row.get(k) for k in ("count", "sum", "avg", "min", "max")}
    return {"metric": metric, "start_date": request["start_date"], "end_date": request["end_date"],
            "stats_by_activity_type": by_type, "count_of_activities": sum(v["count"] for v in by_type.values())}


def with_additions(name):
    raw, _ = recorded(name)
    with open(os.path.join(UNITS, name + ".json"), encoding="utf-8") as fh:
        request = json.load(fh)["request"]
    text = json.dumps(curated_progress(raw, request), indent=2)
    return json.loads(launch.add_to_answer("get_progress_summary_between_dates", text))


class ProgressSummaryUnits(unittest.TestCase):
    """launch.py _PROGRESS_UNITS: fitnessstats durations are ms (-> *_s = /1000), elevation and distance cm
    (-> *_m = /100)."""

    def test_notes_name_the_units_they_pin(self):
        for name, words in (("progress_summary_movingDuration", ("movingDuration", "milliseconds")),
                            ("progress_summary_duration", ("duration", "milliseconds")),
                            ("progress_summary_elevationGain", ("elevationGain", "centimeters")),
                            ("progress_summary_distance", ("distance", "centimeters"))):
            note = recorded(name)[1]
            for word in words:
                self.assertIn(word, note, name)

    def test_moving_duration_is_milliseconds(self):
        out = with_additions("progress_summary_movingDuration")
        self.assertEqual(out["unit"], "ms")
        run = out["stats_by_activity_type"]["running"]
        self.assertEqual((run["sum_s"], run["avg_s"], run["min_s"], run["max_s"]),
                         (17470.4, 2911.7, 1712.4, 5268.9))
        self.assertEqual(run["sum"], 17470400.0)  # Garmin's raw value stays as it was
        self.assertEqual(out["stats_by_activity_type"]["cycling"]["max_s"], 10620.3)
        # 4:51:10 of running in three weeks, 6 runs between 28 min and 1:28 h: plausible only as ms.
        self.assertEqual(dt.timedelta(seconds=round(run["sum_s"])), dt.timedelta(hours=4, minutes=51, seconds=10))
        self.assertTrue(20 * 60 <= run["min_s"] <= run["max_s"] <= 3 * 3600)

    def test_moving_duration_matches_the_activity_list(self):
        # The longest run's movingDuration: 5268.9 s in the activity list, 5268900 ms in fitnessstats.
        acts, _ = recorded("activity_list")
        trail = next(a for a in acts if a["activityType"]["typeKey"] == "trail_running")
        out = with_additions("progress_summary_movingDuration")
        self.assertEqual(out["stats_by_activity_type"]["running"]["max_s"], trail["movingDuration"])

    def test_duration_is_milliseconds(self):
        out = with_additions("progress_summary_duration")
        self.assertEqual(out["unit"], "ms")
        run = out["stats_by_activity_type"]["running"]
        self.assertEqual((run["sum_s"], run["max_s"]), (17889.5, 5420.0))
        acts, _ = recorded("activity_list")
        self.assertIn(run["max_s"], [a["duration"] for a in acts])  # the trail run's duration, in seconds

    def test_elevation_gain_is_centimeters(self):
        out = with_additions("progress_summary_elevationGain")
        self.assertEqual(out["unit"], "cm")
        ride = out["stats_by_activity_type"]["cycling"]
        self.assertEqual((ride["sum_m"], ride["avg_m"], ride["min_m"], ride["max_m"]), (1008.8, 336.3, 81.5, 742.3))
        run = out["stats_by_activity_type"]["running"]
        self.assertEqual((run["sum_m"], run["max_m"]), (769.0, 455.0))
        # The activity list gives the same climbs in meters.
        acts, _ = recorded("activity_list")
        self.assertIn(run["max_m"], [a["elevationGain"] for a in acts])
        self.assertAlmostEqual(ride["max_m"], next(a["elevationGain"] for a in acts
                                                   if a["activityType"]["typeKey"] == "road_biking"), delta=0.5)

    def test_distance_is_centimeters(self):
        out = with_additions("progress_summary_distance")
        self.assertEqual(out["unit"], "cm")
        run = out["stats_by_activity_type"]["running"]
        self.assertEqual((run["sum_m"], run["max_m"], run["min_m"]), (49320.9, 13010.0, 5012.3))
        acts, _ = recorded("activity_list")
        self.assertIn(run["max_m"], [a["distance"] for a in acts])

    @unittest.skipUnless(HAVE_UPSTREAM, NEEDS_UPSTREAM)
    def test_upstream_passes_garmin_values_through(self):
        """Upstream's own tool, fed the fixture, answers what curated_progress() builds: the additions see
        Garmin's raw ms / cm."""
        import asyncio

        from garmin_mcp import training

        tools = {}

        class App:
            def tool(self, *args, **kwargs):
                def register(fn):
                    tools[fn.__name__] = fn
                    return fn
                return register

        for name in ("progress_summary_movingDuration", "progress_summary_elevationGain"):
            raw, _ = recorded(name)
            with open(os.path.join(UNITS, name + ".json"), encoding="utf-8") as fh:
                request = json.load(fh)["request"]
            client = mock.Mock()
            client.get_progress_summary_between_dates.return_value = raw
            with mock.patch.object(training, "garmin_client", client):
                training.register_tools(App())
                text = asyncio.run(tools["get_progress_summary_between_dates"](**request))
            with self.subTest(name=name):
                self.assertEqual(json.loads(text), curated_progress(raw, request))


class ActivityListUnits(unittest.TestCase):
    """The activity list's duration is seconds and distance meters: daily_status _minutes/_km and weekly
    parse_activities / volume_section divide by 60 / 3600 and 1000, never 1000 / 100 first."""

    def test_note(self):
        note = recorded("activity_list")[1]
        for word in ("duration", "seconds", "distance", "meters"):
            self.assertIn(word, note)

    def test_weekly_reads_seconds_and_meters(self):
        acts, _ = recorded("activity_list")
        parsed = {a["type"]: a for a in ws.parse_activities(acts, TZ)}
        self.assertEqual((parsed["road_biking"]["seconds"], parsed["road_biking"]["meters"]), (10980.0, 88340.0))
        self.assertEqual((parsed["trail_running"]["seconds"], parsed["trail_running"]["meters"]), (5420.0, 13010.0))
        vol = ws.volume_section(list(parsed.values()), WEEK_START, WEEK_END)
        self.assertEqual((vol["bike"]["km"], vol["bike"]["hours"], vol["bike"]["hhmm"]), (88.3, 3.05, "3:03"))
        self.assertEqual((vol["run"]["sessions"], vol["run"]["km"], vol["run"]["hours"]), (2, 23.1, 2.42))
        self.assertEqual(vol["total_hours"], 5.47)

    def test_daily_reads_seconds_and_meters(self):
        acts, _ = recorded("activity_list")
        sessions = {s["type"]: s for s in ds.extract_sessions(acts, start=True)}
        self.assertEqual((sessions["road_biking"]["minutes"], sessions["road_biking"]["km"]), (183, 88.3))
        self.assertEqual((sessions["running"]["minutes"], sessions["running"]["km"]), (55, 10.1))
        self.assertEqual((sessions["trail_running"]["minutes"], sessions["trail_running"]["km"]), (90, 13.0))


class PoolUnits(unittest.TestCase):
    """A pool workout's distance steps in meters (bug #2). Spike S0.3: in a yard pool too the step's
    endConditionValue is meters; the steps add up to estimatedDistanceInMeters."""

    def raw_steps(self):
        workout, _ = recorded("pool_workout_yards")

        def walk(steps, times):
            for step in steps:
                if step.get("workoutSteps"):
                    yield from walk(step["workoutSteps"], times * int(step["numberOfIterations"]))
                else:
                    yield step, times

        return workout, list(walk(workout["workoutSegments"][0]["workoutSteps"], 1))

    def test_note(self):
        note = recorded("pool_workout_yards")[1]
        for word in ("endConditionValue", "yard", "estimatedDistanceInMeters", "S0.3", "meters"):
            self.assertIn(word, note)

    def test_yard_pool_steps_add_up_to_the_estimate_in_meters(self):
        workout, steps = self.raw_steps()
        self.assertEqual(workout["poolLengthUnit"]["unitKey"], "yard")
        meters = sum((sc.step_distance_m(step) or 0.0) * times for step, times in steps)
        self.assertEqual(meters, workout["estimatedDistanceInMeters"])
        self.assertEqual(meters, 1100.0)

    def test_step_distance_m(self):
        raw = {"endCondition": {"conditionTypeKey": "distance"}, "endConditionValue": 100.0}
        curated = {"end_condition": "distance", "end_condition_value": 100.0}
        for step in (raw, curated):
            self.assertEqual(sc.step_distance_m(step), 100.0)
        self.assertIsNone(sc.step_distance_m({"end_condition": "time", "end_condition_value": 600.0}))
        self.assertIsNone(sc.step_distance_m({"endCondition": {"conditionTypeKey": "lap.button"}}))
        self.assertIsNone(sc.step_distance_m({"end_condition": "distance"}))
        self.assertIsNone(sc.step_distance_m({"end_condition": "distance", "end_condition_value": 0}))
        self.assertIsNone(sc.step_distance_m({"end_condition": "distance", "end_condition_value": "x"}))
        self.assertIsNone(sc.step_distance_m({"endCondition": "distance", "endConditionValue": 5.0}))

    def test_daily_classify_reads_the_curated_pool_unit(self):
        # Upstream's curated workout keeps pool_length_unit as text; with no estimate the steps give the km.
        steps = [{"type": "interval", "end_condition": "distance", "end_condition_value": 100.0}] * 20
        cur = {"name": "w", "sport": "swimming", "pool_length": 25.0, "pool_length_unit": "yard",
               "segments": [{"order": 1, "sport": "swimming", "steps": steps}]}
        entry = {"scheduleDate": "2026-11-18", "workoutName": "Swim", "workoutType": "swimming"}
        self.assertEqual(ds.classify_workout(entry, cur)["km"], 2.0)  # 20 x 100 m, in the yard pool too


class TimeUnits(unittest.TestCase):
    """Garmin's activity times (bug #8): startTimeLocal is the watch's wall clock, startTimeGMT is UTC; both
    are text without a zone, with or without a fraction, "T" or a space."""

    def test_parse_garmin_time(self):
        expect = dt.datetime(2026, 11, 22, 8, 0, 5)
        for text in ("2026-11-22 08:00:05", "2026-11-22T08:00:05", "2026-11-22T08:00:05.0",
                     "2026-11-22 08:00:05.123456", " 2026-11-22 08:00:05"):
            self.assertEqual(sc.parse_garmin_time(text), expect, text)
        self.assertEqual(sc.parse_garmin_time("2026-11-22 08:00"), dt.datetime(2026, 11, 22, 8, 0))
        for bad in ("2026-11-22", "2026-13-01 08:00:00", "2026-11-22 25:00:00", "yesterday", "", None, 1763798400000):
            self.assertIsNone(sc.parse_garmin_time(bad), bad)

    def test_gmt_fallback_matches_the_local_time(self):
        # The fixture's activities carry both; without startTimeLocal, startTimeGMT read in the zone gives the
        # same local time (Athens is UTC+2 in November).
        acts, _ = recorded("activity_list")
        for act in acts:
            local = sc.activity_times(act, TZ)
            gmt_only = {k: v for k, v in act.items() if k != "startTimeLocal"}
            self.assertEqual(sc.activity_times(gmt_only, TZ), local, act["activityId"])
            self.assertEqual(local[1], sc.parse_garmin_time(act["startTimeLocal"]))

    def test_activity_times_edges(self):
        utc = dt.timezone.utc
        late_gmt = {"startTimeGMT": "2026-11-22 23:30:00"}
        self.assertEqual(sc.activity_times(late_gmt, TZ), (dt.date(2026, 11, 23), dt.datetime(2026, 11, 23, 1, 30)))
        # A one-day query: the list's day wins over the GMT time read in the zone.
        self.assertEqual(sc.activity_times(late_gmt, TZ, dt.date(2026, 11, 22)),
                         (dt.date(2026, 11, 22), dt.datetime(2026, 11, 23, 1, 30)))
        # startTimeLocal wins, even a date alone; the queried day is only the GMT fallback.
        self.assertEqual(sc.activity_times({"startTimeLocal": "2026-11-21 23:50:00", **late_gmt}, utc,
                                           dt.date(2026, 11, 22)),
                         (dt.date(2026, 11, 21), dt.datetime(2026, 11, 21, 23, 50)))
        self.assertEqual(sc.activity_times({"startTimeLocal": "2026-11-21"}, utc), (dt.date(2026, 11, 21), None))
        self.assertEqual(sc.activity_times({}, utc), (None, None))
        self.assertEqual(sc.activity_times({"startTimeGMT": "bad"}, utc, dt.date(2026, 11, 22)),
                         (dt.date(2026, 11, 22), None))
        self.assertEqual(sc.activity_times({"startTimeLocal": "bad", "startTimeGMT": "bad"}, utc), (None, None))

    def test_weekly_and_daily_read_the_same_day(self):
        # A GMT-only activity late in the evening GMT: the weekly report (no queried day) reads it in the zone,
        # the daily one (asked for that local day) keeps it on that day.
        act = {"activityId": 1, "activityType": {"typeKey": "running"}, "startTimeGMT": "2026-11-18 21:00:00",
               "duration": 1800.0, "distance": 5000.0}
        parsed = ws.parse_activities([act], TZ)
        self.assertEqual((parsed[0]["date"], parsed[0]["start_local"]), (dt.date(2026, 11, 18), "2026-11-18 23:00:00"))
        self.assertEqual(ds.completion_activities([act], TZ, dt.date(2026, 11, 18))[0]["date"], "2026-11-18")


class SteadinessUnits(unittest.TestCase):
    """weekly_status steadiness inputs: the FIT session's total_ascent_m (m), total_timer_time_s (s),
    avg/normalized power (W), and the details chart's directElevation samples (m)."""

    def setUp(self):
        ws._drift_cache.clear()
        self.addCleanup(ws._drift_cache.clear)

    def fit_drift(self, act):
        session, _ = recorded("fit_session_ride")

        def parse_fit(raw, include_records):
            return {"session": dict(session), "laps": []}

        client = mock.Mock()
        client.download_activity.return_value = b"FIT-BYTES"
        with mock.patch.object(ws, "_load_fit_helpers", return_value=("ORIGINAL", parse_fit, True)):
            return ws._fit_drift(client, 91000003, act)

    def test_note(self):
        note = recorded("fit_session_ride")[1]
        for word in ("total_ascent_m", "meters", "total_timer_time_s", "seconds", "watts"):
            self.assertIn(word, note)
        note = recorded("activity_details_trail_run")[1]
        for word in ("directElevation", "meters"):
            self.assertIn(word, note)

    def test_fit_session_variability_and_climbing(self):
        out = self.fit_drift({"sport": "bike", "type": "road_biking", "seconds": 10980.0})
        # VI = 199 / 182 W; climbing = 742 m over 10980 s of timer time (not the 11400 s elapsed).
        self.assertEqual((out["variability_index"], out["ascent_m_per_h"]), (1.093, 243.0))
        self.assertLessEqual(out["variability_index"], ws.STEADY_MAX_VI)
        self.assertLess(out["ascent_m_per_h"], ws.STEADY_MAX_ASCENT_M_PER_H["bike"])
        self.assertIs(out["steady"], True)
        self.assertEqual(out["pct"], -4.2)

    def test_fit_session_steadiness_matches_the_activity_list(self):
        # The same ride in the activity list: elevationGain 742 m, duration 10980 s, avgPower / normPower W.
        acts, _ = recorded("activity_list")
        ride = next(a for a in acts if a["activityType"]["typeKey"] == "road_biking")
        session, _ = recorded("fit_session_ride")
        self.assertEqual((session["total_ascent_m"], session["total_timer_time_s"]),
                         (ride["elevationGain"], ride["duration"]))
        self.assertEqual((session["avg_power_w"], session["normalized_power_w"]), (ride["avgPower"], ride["normPower"]))

    def test_details_elevation_is_meters(self):
        details, _ = recorded("activity_details_trail_run")
        index = {d["key"]: d["metricsIndex"] for d in details["metricDescriptors"]}
        rows = [r["metrics"] for r in details["activityDetailMetrics"]]
        # 120.0 -> 335.3 m (334.5 + 0.8 jitter), down, then 150.0 -> 299.0 m: 215.3 + 149.0 m climbed.
        climbed = ws._details_ascent(rows, index["directElevation"])
        self.assertAlmostEqual(climbed, 215.3 + 149.0, places=6)
        act = {"sport": "run", "type": "trail_running", "seconds": 5420.0}
        drift = ws.decoupling_from_details(details, act)
        self.assertEqual((drift["basis"], drift["samples"]), ("speed", 120))
        self.assertIs(drift["steady"], False)  # ~241 m/h, far above the 100 m/h a run allows
        self.assertGreater(climbed / (act["seconds"] / 3600), ws.STEADY_MAX_ASCENT_M_PER_H["run"])

    def test_details_steady_on_flat_ground(self):
        details, _ = recorded("activity_details_trail_run")
        flat = json.loads(json.dumps(details))
        for n, row in enumerate(flat["activityDetailMetrics"]):
            row["metrics"][3] = 40.0 + (1.5 if n % 4 == 0 else 0.0)  # noise only
        drift = ws.decoupling_from_details(flat, {"sport": "run", "type": "running", "seconds": 5420.0})
        self.assertIs(drift["steady"], True)


if __name__ == "__main__":
    unittest.main()
