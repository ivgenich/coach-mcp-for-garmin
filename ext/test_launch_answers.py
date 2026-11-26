"""Tests for launch.py: the description notes and the answer additions for upstream's tools (tool_answers.py,
re-exported by launch.py).

    cd ext && python -m unittest test_launch_answers

Shared helpers: test_launch_support.py.
"""

from __future__ import annotations

import asyncio
import json
import unittest
from unittest import mock

import mcp2_compat  # noqa: E402
import server_instructions  # noqa: E402
import tool_errors  # noqa: E402

from test_launch_support import (answers, call_tools, ERAS, FakeServer, HAVE_UPSTREAM, load_launch, quiet, RATE_TEXT,
    Stubs)


class DescriptionNoteAtRegistration(unittest.TestCase):
    """add_tool() ends the description of each tool in TOOL_NOTES with its note
    and of each tool in FREE_TEXT_TOOLS with the data note (after the tool's
    own), and passes every other description through untouched."""

    def test_only_free_text_tools_get_the_note(self):
        class Recorder(FakeServer):
            def add_tool(self, fn, name=None, **kwargs):
                self.added.append((name or fn.__name__, kwargs.get("description")))

        def get_activities():
            """List activities.

            Args:
                limit: How many.
            """

        def get_stats():
            """Daily stats."""

        launch = load_launch(self, Stubs().modules)
        server = launch.read_only_server_class(Recorder, {"get_activities", "get_stats", "get_courses",
                                                          "get_vo2max_trend"})()
        note = "\n\n" + server_instructions.FREE_TEXT_NOTE
        server.tool()(get_activities)  # the docstring, as the SDK would use it
        server.tool()(get_stats)  # not a free-text tool
        server.tool(name="get_courses", description="Saved courses.  \n")(get_stats)  # an explicit description
        server.tool(name="get_vo2max_trend")(get_stats)  # a tool note only
        # The docstring loses its indentation, so the noted descriptions stay under 2,048 characters.
        self.assertEqual(server.added, [("get_activities", "List activities.\n\nArgs:\n    limit: How many.\n\n"
                                         + launch.TOOL_NOTES["get_activities"] + note),
                                        ("get_stats", None),
                                        ("get_courses", "Saved courses." + note),
                                        ("get_vo2max_trend", "Daily stats.\n\n" + launch.TOOL_NOTES["get_vo2max_trend"])])
        self.assertIn("band for acute load", launch.TOOL_NOTES["get_training_status"])
        self.assertIn("band for acute load", launch.TOOL_NOTES["get_training_load_trend"])
        for name, text in launch.TOOL_NOTES.items():
            with self.subTest(tool=name):
                text.encode("ascii")
                self.assertLessEqual(len(text), 300)


def upstream_training_answer(tool, client, **arguments):
    """The text upstream's training tool `tool` answers with `client` as its Garmin client: the exact shape
    the additions see (the function is called directly, no server)."""
    from garmin_mcp import training

    tools = {}

    class App:
        def tool(self, *args, **kwargs):
            def register(fn):
                tools[fn.__name__] = fn
                return fn
            return register

    saved = training.garmin_client
    training.garmin_client = client
    try:
        training.register_tools(App())
        return asyncio.run(tools[tool](**arguments))
    finally:
        training.garmin_client = saved


# Made-up fitnessstats rows in the units Garmin answers them (centimeters, milliseconds, calories x 4.19).
PROGRESS_ROWS = {
    "distance": {"running": {"count": 5, "sum": 3104354.0, "avg": 620870.8, "min": 402200.0, "max": 1003954.0},
                 "cycling": {"count": 1, "sum": 2210000.0, "avg": 2210000.0, "min": 2210000.0, "max": 2210000.0}},
    "duration": {"running": {"count": 5, "sum": 10748244.0, "avg": 2149648.8, "min": 1400000.0, "max": 3900244.0}},
    "elevationGain": {"running": {"count": 5, "sum": 129300.0, "avg": 25860.0, "min": 0.0, "max": 51300.0}},
    "calories": {"running": {"count": 5, "sum": 2104.0113 * 4.19, "avg": 420.80226 * 4.19,
                             "min": 300.4 * 4.19, "max": 700.6 * 4.19}},
}
LOAD_DTO = {"dailyTrainingLoadAcute": 612.0, "dailyTrainingLoadChronic": 455.0,
            "dailyAcuteChronicWorkloadRatio": 1.3, "acwrStatus": "OPTIMAL", "acwrPercent": 62,
            "minTrainingLoadChronic": 364.0, "maxTrainingLoadChronic": 682.5}


class TrainingClient:
    """The garminconnect calls upstream's progress and training tools make, in the shapes Garmin answers."""

    def get_progress_summary_between_dates(self, start, end, metric):
        stats = {sport: {metric: row} for sport, row in PROGRESS_ROWS[metric].items()}
        return [{"date": end, "countOfActivities": 9, "stats": stats}]

    def get_devices(self):
        return [{"deviceId": 3400000001, "primaryDevice": True, "displayName": "Sample Watch"}]

    def get_training_status(self, date):
        return {"mostRecentTrainingStatus": {"latestTrainingStatusData": {"3400000001": {
                    "deviceId": 3400000001, "calendarDate": date, "trainingStatus": 4, "sport": "RUNNING",
                    "trainingStatusFeedbackPhrase": "PRODUCTIVE_1", "fitnessTrend": 2,
                    "primaryTrainingDevice": True, "acuteTrainingLoadDTO": dict(LOAD_DTO)}}},
                "mostRecentVO2Max": {"generic": {"vo2MaxValue": 53.0, "vo2MaxPreciseValue": 53.4}}}


@unittest.skipUnless(HAVE_UPSTREAM, "needs mcp 2.x and upstream garmin_mcp")
class AnswerAdditions(unittest.TestCase):
    """add_to_answer() over upstream's own answers: fields added, none removed or renamed, the dump style kept,
    and anything unexpected left as it was."""

    def setUp(self):
        self.launch = load_launch(self, Stubs().modules)

    def progress(self, metric):
        return upstream_training_answer("get_progress_summary_between_dates", TrainingClient(),
                                        start_date="2026-11-01", end_date="2026-11-23", metric=metric)

    def assert_only_added(self, before, after):
        if isinstance(before, dict):
            for key, value in before.items():
                self.assertIn(key, after)
                self.assertTrue(isinstance(value, (dict, list)) or after[key] == value, key)
                self.assert_only_added(value, after[key])
        elif isinstance(before, list):
            self.assertEqual(len(before), len(after))
            for old, new in zip(before, after):
                self.assert_only_added(old, new)

    def test_progress_summary_gets_its_unit_and_converted_copies(self):
        add = self.launch.add_to_answer
        expected = {
            "distance": ("cm", {"sum_m": 31043.5, "avg_m": 6208.7, "min_m": 4022.0, "max_m": 10039.5}),
            "duration": ("ms", {"sum_s": 10748.2, "avg_s": 2149.6, "min_s": 1400.0, "max_s": 3900.2}),
            "elevationGain": ("cm", {"sum_m": 1293.0, "avg_m": 258.6, "min_m": 0.0, "max_m": 513.0}),
            "calories": ("kcal", {"sum_rounded": 2104, "avg_rounded": 421, "min_rounded": 300, "max_rounded": 701}),
        }
        for metric, (unit, copies) in expected.items():
            with self.subTest(metric=metric):
                raw = self.progress(metric)
                text = add("get_progress_summary_between_dates", raw)
                before, after = json.loads(raw), json.loads(text)
                self.assert_only_added(before, after)
                self.assertEqual(after["unit"], unit)
                self.assertEqual(list(after)[:2], ["metric", "unit"])
                running = after["stats_by_activity_type"]["running"]
                self.assertEqual({k: running[k] for k in copies}, copies)
                self.assertEqual(list(running)[-4:], list(copies))
                self.assertEqual(text, json.dumps(after, indent=2))  # upstream's indent=2, ensure_ascii
        cycling = json.loads(add("get_progress_summary_between_dates", self.progress("distance")))
        self.assertEqual(cycling["stats_by_activity_type"]["cycling"]["sum_m"], 22100.0)

    def test_training_status_and_load_trend_get_the_acute_band(self):
        add = self.launch.add_to_answer
        raw = upstream_training_answer("get_training_status", TrainingClient(), date="2026-11-23")
        after = json.loads(add("get_training_status", raw))
        self.assert_only_added(json.loads(raw), after)
        self.assertEqual((after["optimal_acute_load_min"], after["optimal_acute_load_max"]), (364.0, 682.5))
        keys = list(after)
        at = keys.index("optimal_chronic_load_max")
        self.assertEqual(keys[at + 1:at + 3], ["optimal_acute_load_min", "optimal_acute_load_max"])

        raw = upstream_training_answer("get_training_load_trend", TrainingClient(),
                                       start_date="2026-11-21", end_date="2026-11-23")
        text = add("get_training_load_trend", raw)
        after = json.loads(text)
        self.assert_only_added(json.loads(raw), after)
        self.assertEqual(len(after["trend"]), 3)
        for entry in after["trend"]:
            self.assertEqual((entry["optimal_acute_load_min"], entry["optimal_acute_load_max"]), (364.0, 682.5))
        self.assertEqual(text, json.dumps(after, indent=2))

    def test_present_fields_are_never_doubled_or_overwritten(self):
        add = self.launch.add_to_answer
        fixed = json.dumps({"metric": "distance", "unit": "m", "stats_by_activity_type": {
            "running": {"count": 1, "sum": 31043.5, "avg": 31043.5, "min": 31043.5, "max": 31043.5}}}, indent=2)
        self.assertIs(add("get_progress_summary_between_dates", fixed), fixed)  # an upstream fix: left alone
        status = json.dumps({"optimal_chronic_load_min": 364.0, "optimal_chronic_load_max": 682.5,
                             "optimal_acute_load_min": 1.0, "optimal_acute_load_max": 2.0})
        self.assertIs(add("get_training_status", status), status)
        partly = json.dumps({"optimal_chronic_load_min": 364.0, "optimal_chronic_load_max": 682.5,
                             "optimal_acute_load_min": 1.0})
        self.assertEqual(json.loads(add("get_training_status", partly))["optimal_acute_load_min"], 1.0)
        self.assertEqual(json.loads(add("get_training_status", partly))["optimal_acute_load_max"], 682.5)
        compact = json.dumps({"optimal_chronic_load_min": 3.0}, separators=(",", ":"))
        self.assertEqual(add("get_training_status", compact), '{"optimal_chronic_load_min":3.0,'
                                                              '"optimal_acute_load_min":3.0}')

    def test_unexpected_shapes_and_other_tools_come_back_unchanged(self):
        add = self.launch.add_to_answer
        cases = [("get_progress_summary_between_dates", text) for text in (
            "No progress summary found for distance between 2026-11-01 and 2026-11-23.",
            "[1, 2]", "{not json", json.dumps({"metric": "steps", "stats_by_activity_type": {}}),
            json.dumps({"metric": "distance", "stats_by_activity_type": []}),
            json.dumps({"metric": "distance", "stats_by_activity_type": {"running": 5}}),
            json.dumps({"metric": "distance", "stats_by_activity_type": {"running": {"sum": "x"}}}))]
        cases += [("get_training_status", json.dumps({"acute_load": 612.0})),
                  ("get_training_load_trend", json.dumps({"trend": "none"})),
                  ("get_training_load_trend", json.dumps({"trend": [{"atl": 1.0}, 7]})),
                  ("get_activity", json.dumps({"optimal_chronic_load_min": 3.0})),
                  ("get_training_status", None)]
        for name, text in cases:
            with self.subTest(text=text):
                self.assertIs(add(name, text), text)
        with mock.patch.dict(self.launch.ANSWER_ADDITIONS, {"get_activity": mock.Mock(side_effect=RuntimeError)}):
            self.assertEqual(add("get_activity", "{}"), "{}")

    def test_through_the_override_text_and_structured_result_agree(self):
        FastMCP = mcp2_compat.install().FastMCP
        names = ("get_progress_summary_between_dates", "get_training_status")
        server = self.launch.read_only_server_class(FastMCP, set(names))("test")
        server.tool(name=names[0], structured_output=True)(answers)
        server.tool(name=names[1], structured_output=False)(answers)
        quiet(self, "mcp", "garmin_ext")
        raw = self.progress("distance")
        status = upstream_training_answer("get_training_status", TrainingClient(), date="2026-11-23")
        for mode in ERAS:
            progress, training, failed = call_tools(server, mode, [(names[0], {"text": raw}),
                                                                   (names[1], {"text": status}),
                                                                   (names[1], {"text": RATE_TEXT})])
            with self.subTest(mode=mode):
                (text,) = [block.text for block in progress.content]
                self.assertEqual(text, self.launch.add_to_answer(names[0], raw))
                self.assertNotEqual(text, raw)
                self.assertEqual(progress.structured_content, {"result": text})
                (text,) = [block.text for block in training.content]
                self.assertIn('"optimal_acute_load_max": 682.5', text)
                self.assertIsNone(training.structured_content)
                self.assertEqual([block.text for block in failed.content],
                                 [tool_errors.client_text(tool_errors.RATE_LIMITED)])


if __name__ == "__main__":
    unittest.main()
