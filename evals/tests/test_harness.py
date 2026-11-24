"""Offline tests of evals/harness: the real server over the fake Garmin, the states, the faults, the clock.

No network and no API key: the server runs in process with the fake client; nothing here calls a model. The
server tests need upstream garmin-mcp on mcp 2.x (the CI full venv, ~/.cache/coach-mcp-ci/venv-evals); without
it they are skipped and the state tests still run.
"""

from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest

from evals.harness import FAULT_GROUPS, FIXED_REF, StateError, clock_line, load_state, start_server
from evals.harness import base_sets, fake_garmin, state as state_mod
from evals.harness.server import EXT, ext_modules

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DAILY_NOW, WEEKLY_NOW, TZ = "2026-03-18T06:30", "2026-03-08T06:30", "Europe/Athens"


def _have_upstream():
    if importlib.util.find_spec("garmin_mcp") is None or importlib.util.find_spec("mcp") is None:
        return False
    try:
        return ext_modules().mcp2_compat._mcp_major() == "2"
    except Exception:  # noqa: BLE001 - any import problem means: no server tests here
        return False


HAVE_UPSTREAM = _have_upstream()
NEEDS_UPSTREAM = "needs upstream garmin_mcp on mcp 2.x (the CI full venv)"


def tool_names(path):
    names = []
    with open(os.path.join(REPO, path), encoding="utf-8") as fh:
        for line in fh:
            name = "".join(line.split("#", 1)[0].split())
            if name:
                names.append(name)
    return names


def overlay_file(test, content):
    """A temporary overlay file (absolute path), removed when `test` ends."""
    folder = tempfile.TemporaryDirectory()
    test.addCleanup(folder.cleanup)
    path = os.path.join(folder.name, "overlay.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(content, fh)
    return path


# --- states (standard library only) -------------------------------------------------------------------------

class States(unittest.TestCase):
    def test_base_files_equal_what_ext_fixtures_make(self):
        for name, content in base_sets.build().items():
            with self.subTest(name=name):
                with open(os.path.join(state_mod.BASE_DIR, name + ".json"), encoding="utf-8") as fh:
                    self.assertEqual(fh.read(), base_sets.dumps(content),
                                     "run: python -m evals.harness.base_sets")

    def test_base_files_load(self):
        for name in base_sets.BUILDERS:
            with self.subTest(name=name):
                loaded = load_state(name)
                self.assertEqual(loaded["faults"], {})
                self.assertEqual(loaded["files"], [f"fixtures/base/{name}.json"])
        self.assertEqual(load_state("empty")["responses"], {})

    def test_overlays_merge_deeply_and_faults_come_last(self):
        first = overlay_file(self, {"responses": {"get_stats": {"2026-03-18": {"restingHeartRate": 53}}},
                                    "faults": {"get_hrv_data": "no_data"}, "note": "first"})
        second = overlay_file(self, {"responses": {"get_stats": {"2026-03-18": {"minHeartRate": 40}},
                                                   "get_cycling_ftp": {"*": {"$replace": {"x": 1}}}},
                                     "faults": {"get_hrv_data": "internal"}})
        merged = load_state("daily_2026_03_18", [first, second], {"get_daily_status": "login"})
        stats = merged["responses"]["get_stats"]["2026-03-18"]
        base = load_state("daily_2026_03_18")["responses"]["get_stats"]["2026-03-18"]
        self.assertEqual((stats["restingHeartRate"], stats["minHeartRate"]), (53, 40))
        self.assertEqual(stats["totalSteps"], base["totalSteps"])  # untouched keys stay
        self.assertEqual(merged["responses"]["get_cycling_ftp"]["*"], {"x": 1})
        self.assertEqual(merged["faults"], {"get_hrv_data": "internal", "get_daily_status": "login"})
        self.assertIn("first", merged["notes"])

    def test_lists_replace(self):
        self.assertEqual(state_mod.deep_merge({"a": [1, 2], "b": {"c": 1}}, {"a": [3], "b": {"d": 2}}),
                         {"a": [3], "b": {"c": 1, "d": 2}})

    def test_bad_states_are_refused(self):
        with self.assertRaisesRegex(StateError, "no_such_method"):
            load_state("empty", [overlay_file(self, {"responses": {"no_such_method": {"*": 1}}})])
        with self.assertRaisesRegex(StateError, "is no group"):
            load_state("empty", [], {"get_stats": "explode"})
        with self.assertRaisesRegex(StateError, "no nope.json"):
            load_state("nope")
        with self.assertRaisesRegex(StateError, "unknown top-level"):
            load_state("empty", [overlay_file(self, {"respones": {}})])
        with self.assertRaisesRegex(StateError, r"\$fault"):
            load_state("empty", [overlay_file(self, {"responses": {"get_stats": {"*": {"$fault": "oops"}}}})])

    def test_overlay_names_are_looked_up(self):
        folder = os.path.join(state_mod.FIXTURES, "overlays")
        names = sorted(f[:-5] for f in os.listdir(folder) if f.endswith(".json")) if os.path.isdir(folder) else []
        for name in names[:3]:  # B3's files: a bare name resolves
            with self.subTest(name=name):
                self.assertIn(f"fixtures/overlays/{name}.json", load_state("empty", [name])["files"])

    def test_activity_list_filters_sorts_and_pages(self):
        acts = [{"activityId": i, "startTimeLocal": f"2026-03-0{i} 07:00:00",
                 "activityType": {"typeKey": key}} for i, key in ((1, "road_biking"), (2, "running"),
                                                                   (3, "treadmill_running"))]
        world = fake_garmin.World({"responses": {"connectapi": {fake_garmin.ACTIVITIES_PATH: acts}}})
        self.assertEqual([a["activityId"] for a in world.activities("connectapi")], [3, 2, 1])
        self.assertEqual([a["activityId"] for a in world.activities("connectapi", "2026-03-02", "2026-03-03")],
                         [3, 2])
        self.assertEqual([a["activityId"] for a in world.activities("connectapi", activity_type="running")], [3, 2])
        self.assertEqual([a["activityId"] for a in world.activities("get_activities", start=1, limit=1)], [2])

    def test_clock_line(self):
        self.assertEqual(clock_line(DAILY_NOW, TZ),
                         "Current date and time: Wednesday, 18 March 2026, 06:30 (Europe/Athens, UTC+02:00).")
        self.assertEqual(clock_line("2026-03-08T06:30", "UTC"),
                         "Current date and time: Sunday, 8 March 2026, 06:30 (UTC, UTC+00:00).")


# --- the server ---------------------------------------------------------------------------------------------

@unittest.skipUnless(HAVE_UPSTREAM, NEEDS_UPSTREAM)
class ToolsAndInstructions(unittest.TestCase):
    def test_full_and_key_mode_list_the_real_tools(self):
        ext = ext_modules()
        for mode, path, instructions in (
                ("full", "config/enabled-tools.txt", ext.server_instructions.INSTRUCTIONS),
                ("key", "config/enabled-tools-key.txt", ext.server_instructions.INSTRUCTIONS_KEY)):
            with self.subTest(mode=mode), start_server(mode, load_state("empty"), DAILY_NOW, TZ) as srv:
                names = [tool["name"] for tool in srv.tools]
                self.assertEqual(len(names), 31 if mode == "full" else 13)
                self.assertEqual(sorted(names), sorted(tool_names(path)))
                if mode == "key":
                    self.assertEqual(set(names), set(ext.key_mode.KEY_MODE_TOOLS))
                self.assertEqual(srv.instructions, instructions)
                self.assertTrue(srv.instructions.strip())
                for tool in srv.tools:
                    self.assertEqual(set(tool), {"name", "description", "input_schema"})
                    self.assertTrue(tool["description"], tool["name"])
                    self.assertEqual(tool["input_schema"].get("type"), "object", tool["name"])
                    if tool["name"] in ext.server_instructions.FREE_TEXT_TOOLS:
                        self.assertTrue(tool["description"].endswith(ext.server_instructions.FREE_TEXT_NOTE))
                    json.dumps(tool)  # plain JSON, ready for the Anthropic API
                self.assertEqual(srv.calls, [])

    def test_same_list_every_start_and_upstream_restored(self):
        import garmin_mcp

        before = garmin_mcp.FastMCP
        with start_server("full", None, DAILY_NOW, TZ) as srv:
            first = srv.tools
            with self.assertRaisesRegex(RuntimeError, "already running"):
                with start_server("full", None, DAILY_NOW, TZ):
                    pass
        self.assertIs(garmin_mcp.FastMCP, before)
        with start_server("full", None, DAILY_NOW, TZ) as srv:
            self.assertEqual(srv.tools, first)

    def test_bad_mode(self):
        with self.assertRaises(ValueError):
            with start_server("both", None, DAILY_NOW, TZ):
                pass


@unittest.skipUnless(HAVE_UPSTREAM, NEEDS_UPSTREAM)
class StatusTools(unittest.TestCase):
    def test_daily_green_train_as_planned(self):
        with start_server("full", load_state("daily_2026_03_18"), DAILY_NOW, TZ) as srv:
            out = srv.call("get_daily_status", {})
            calls = srv.calls
            self.assertEqual(srv.missing, [])
        self.assertFalse(out.is_error)
        status = json.loads(out.text)
        self.assertEqual((status["date"], status["generated_at"]), ("2026-03-18", "2026-03-18T06:30:00+02:00"))
        self.assertEqual((status["level"], status["recommendation"], status["recommendation_en"]),
                         ("green", "as_planned", "Train as planned."))
        self.assertEqual((status["flags"], status["missing"]), ([], []))
        rec = status["recovery"]
        self.assertEqual((rec["sleep_hours"], rec["sleep_hhmm"], rec["sleep_hours_prev"]), (6.25, "6:15", 5.7))
        self.assertEqual((rec["hrv"], rec["hrv_7d"], rec["rhr"], rec["rhr_7d"]), (58, 63, 49, 46))
        self.assertEqual((rec["readiness"], rec["recovery_hours"], rec["body_battery"]), (52, 8.5, 71))
        self.assertEqual((status["load"]["acwr"], status["load"]["acwr_status"]), (1.1, "OPTIMAL"))
        self.assertEqual([w["name"] for w in status["today"]], ["Easy aerobic run"])
        self.assertEqual([w["name"] for w in status["tomorrow"]], ["Threshold intervals 5x6", "Technique swim"])
        self.assertIn({"tool": "get_daily_status", "method": "get_stats", "args": ["2026-03-18"], "kwargs": {}},
                      calls)
        self.assertTrue(all(call["tool"] == "get_daily_status" for call in calls))

    def test_weekly_yellow_short_nights_gear(self):
        with start_server("full", load_state("weekly_2026_03_01"), WEEKLY_NOW, TZ) as srv:
            out = srv.call("get_weekly_status", {})
            self.assertEqual(srv.missing, [])
        status = json.loads(out.text)
        self.assertEqual((status["start"], status["end"]), ("2026-03-01", "2026-03-07"))
        self.assertEqual((status["level"], [f["id"] for f in status["flags"]]),
                         ("yellow", ["short_nights", "gear_near_limit"]))
        self.assertEqual(status["missing"], [])
        self.assertEqual(status["sleep"]["nights_under_6h"], 3)
        self.assertEqual(status["hrv"]["avg_28d"], 74)
        self.assertEqual((status["plan"]["completed"], status["plan"]["scheduled"]), (7, 8))
        # No FIT file in the fixtures: the decoupling comes from the activity-details chart.
        self.assertEqual(status["long_session"]["interpretation_code"], "moderate_drift")
        self.assertTrue(-8 <= status["long_session"]["decoupling_pct"] <= -5)

    def test_key_mode_daily_relays_the_key_limits(self):
        with start_server("key", load_state("daily_2026_03_18"), DAILY_NOW, TZ) as srv:
            status = json.loads(srv.call("get_daily_status", {}).text)
            methods = {call["method"] for call in srv.calls}
        self.assertIn("key_mode_note", json.dumps(status))
        self.assertEqual(status["recovery"]["sleep_hours"], 6.25)
        self.assertLessEqual(methods, set(ext_modules().key_mode.KeyModeGarmin._IMPLEMENTED))


@unittest.skipUnless(HAVE_UPSTREAM, NEEDS_UPSTREAM)
class Faults(unittest.TestCase):
    def expected(self, group, key_mode):
        tool_errors = ext_modules().tool_errors
        detail = tool_errors.GARMIN_400_DETAIL if group == "bad_input" else None
        return tool_errors.client_text(group, ref=FIXED_REF, detail=detail, key_mode=key_mode)

    def test_every_group_gives_its_fixed_text_in_both_modes(self):
        for mode in ("full", "key"):
            for group in FAULT_GROUPS:
                with self.subTest(mode=mode, group=group):
                    state = load_state("daily_2026_03_18", [], {"get_stats": group})
                    with start_server(mode, state, DAILY_NOW, TZ) as srv:
                        out = srv.call("get_stats", {"date": "2026-03-18"})
                    self.assertEqual(out.text, self.expected(group, mode == "key"))
                    self.assertEqual(out.is_error, group != "no_data")
        self.assertIn("(ref 0badc0de)", self.expected("internal", False))

    def test_a_tool_fault_fails_every_garmin_call_of_that_tool(self):
        state = load_state("daily_2026_03_18", [], {"get_daily_status": "rate_limited"})
        with start_server("full", state, DAILY_NOW, TZ) as srv:
            out = srv.call("get_daily_status", {})
            first = srv.calls
            other = srv.call("get_stats", {"date": "2026-03-18"})
        self.assertEqual((out.text, out.is_error), (self.expected("rate_limited", False), True))
        self.assertEqual([call["method"] for call in first], ["get_stats"])  # the systemic stop: one call
        self.assertFalse(other.is_error)  # other tools still answer

    def test_login_fault_on_the_status_tool_in_key_mode(self):
        state = load_state("daily_2026_03_18", [], {"get_daily_status": "login"})
        with start_server("key", state, DAILY_NOW, TZ) as srv:
            out = srv.call("get_daily_status", {})
        self.assertEqual(out.text, self.expected("login", True))
        self.assertIn("Personal API key", out.text)

    def test_one_answer_can_fail(self):
        path = overlay_file(self, {"responses": {"get_stats": {"2026-03-17": {"$fault": "internal"}}}})
        with start_server("full", load_state("daily_2026_03_18", [path]), DAILY_NOW, TZ) as srv:
            self.assertEqual(srv.call("get_stats", {"date": "2026-03-17"}).text, self.expected("internal", False))
            self.assertFalse(srv.call("get_stats", {"date": "2026-03-18"}).is_error)

    def test_empty_sleep_is_the_no_sleep_text(self):
        tool_errors = ext_modules().tool_errors
        for mode in ("full", "key"):
            with self.subTest(mode=mode), start_server(mode, load_state("empty"), DAILY_NOW, TZ) as srv:
                out = srv.call("get_sleep_summary", {"date": "2026-03-18"})
            self.assertEqual((out.text, out.is_error), (tool_errors.NO_SLEEP_TEXT, False))


@unittest.skipUnless(HAVE_UPSTREAM, NEEDS_UPSTREAM)
class ClockAndHygiene(unittest.TestCase):
    def test_the_clock_is_frozen(self):
        with start_server("full", load_state("weekly_2026_03_01"), WEEKLY_NOW, TZ) as srv:
            weekly = json.loads(srv.call("get_weekly_status", {}).text)
            bad = srv.call("get_daily_status", {"date": "2026-02-30"})
        self.assertEqual((weekly["end"], weekly["generated_at"]), ("2026-03-07", "2026-03-08T06:30:00+02:00"))
        self.assertTrue(bad.is_error)
        self.assertIn("for example 2026-03-08 (today)", bad.text)
        with start_server("full", None, "2026-03-18T23:59", "UTC") as srv:
            daily = json.loads(srv.call("get_daily_status", {}).text)
            self.assertEqual(srv.clock_line, clock_line("2026-03-18T23:59", "UTC"))
        self.assertEqual((daily["date"], daily["tz"]), ("2026-03-18", "UTC"))

    def test_text_hygiene_runs_on_garmin_text(self):
        name = "Easy\u200b run\u202e [click](https://evil.example)"
        acts = [{"activityId": 91000001, "activityName": name, "startTimeLocal": "2026-03-17 07:00:00",
                 "activityType": {"typeKey": "running"}, "duration": 1800.0, "distance": 5000.0}]
        path = overlay_file(self, {"responses": {"connectapi": {fake_garmin.ACTIVITIES_PATH: acts}}})
        for mode in ("full", "key"):
            with self.subTest(mode=mode), start_server(mode, load_state("empty", [path]), DAILY_NOW, TZ) as srv:
                out = srv.call("get_activities", {})
            self.assertFalse(out.is_error)
            self.assertIn("Easy run", out.text)
            for bad in ("\u200b", "\u202e", "evil.example"):
                self.assertNotIn(bad, out.text)
            self.assertIn("[link]", out.text)


@unittest.skipUnless(HAVE_UPSTREAM, NEEDS_UPSTREAM)
class EveryToolAnswers(unittest.TestCase):
    """Each tool, in each mode, on the empty state and the base states: an answer, never an internal error or a
    client method the fake lacks (full mode), so a missing fake method cannot pass for a model failure."""

    ARGS = {"date": "2026-03-18", "start_date": "2026-03-12", "end_date": "2026-03-18", "activity_id": 73000007,
            "workout_id": "7770002", "course_id": 78000002, "metric": "distance"}

    def test_every_tool(self):
        internal = ext_modules().tool_errors.GROUP_PHRASE["internal"]
        for mode in ("full", "key"):
            for base, now in (("empty", DAILY_NOW), ("daily_2026_03_18", DAILY_NOW), ("weekly_2026_03_01", WEEKLY_NOW)):
                with start_server(mode, load_state(base), now, TZ) as srv:
                    for tool in srv.tools:
                        args = {r: self.ARGS[r] for r in tool["input_schema"].get("required", [])}
                        with self.subTest(mode=mode, base=base, tool=tool["name"]):
                            out = srv.call(tool["name"], args)
                            self.assertNotIn(internal, out.text)
                    if mode == "full":
                        self.assertEqual(srv.missing, [], base)
                    else:  # key mode fails closed: only names KeyModeGarmin lacks are refused
                        implemented = set(ext_modules().key_mode.KeyModeGarmin._IMPLEMENTED)
                        self.assertFalse({m["method"] for m in srv.missing} & implemented)


class Layout(unittest.TestCase):
    def test_harness_imports_ext_from_this_repo(self):
        self.assertEqual(EXT, os.path.join(REPO, "ext"))


if __name__ == "__main__":
    unittest.main()
