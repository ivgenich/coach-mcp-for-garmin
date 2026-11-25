"""The plan cases' harness side (evals/cases/plan.toml, evals/harness/plans.py): the plan read tools served by
the real in-process server over the fake Garmin world plan_2026_11_23 and an in-memory plan store. No network,
no API key, no model."""

import contextlib
import importlib.util
import io
import json
import types
import unittest

from evals import cases as cs

HAVE_MCP = all(importlib.util.find_spec(m) is not None for m in ("mcp", "garmin_mcp"))
NOW, TZ = "2026-11-23T09:00", "Europe/London"
PLAN_TOOLS = {"plan_assess", "plan_preview", "plan_get"}


def plan_cases():
    return {c.id: c for c in cs.load_cases() if c.group == "plan"}


class RunnerPassesTheFixture(unittest.TestCase):
    def test_plan_case_starts_the_server_with_its_fixture(self):
        from evals import runner

        seen = []

        @contextlib.contextmanager
        def start_server(mode, state, now, tz, **kw):
            seen.append(kw)
            raise RuntimeError("stop here")
            yield  # pragma: no cover

        harness = types.SimpleNamespace(load_state=lambda *a: {}, start_server=start_server,
                                        clock_line=lambda now, tz: "")
        with contextlib.redirect_stderr(io.StringIO()):
            runner.run_trial(object(), plan_cases()["L01"], "bare", 1, harness=harness)
        self.assertEqual(seen, [{"plan": "memory"}])

    def test_write_cases_start_the_server_with_their_writes(self):
        from evals import runner

        seen = []

        @contextlib.contextmanager
        def start_server(mode, state, now, tz, **kw):
            seen.append((state, kw))
            raise RuntimeError("stop here")
            yield  # pragma: no cover

        harness = types.SimpleNamespace(load_state=lambda base, overlays, faults: dict(faults),
                                        start_server=start_server, clock_line=lambda now, tz: "")
        cases = {c.id: c for c in cs.load_cases() if c.writes != "off"}
        with contextlib.redirect_stderr(io.StringIO()):
            for cid in ("L07", "L11", "S07"):
                runner.run_trial(object(), cases[cid], "bare", 1, harness=harness)
        self.assertEqual(seen, [({"upload_workout": "rate_limited@1"}, {"plan": "memory", "writes": "on"}),
                                ({}, {"plan": "memory", "writes": "declined"}),
                                ({}, {"plan": "memory", "writes": "committed"})])


class InjectedCommit(unittest.TestCase):
    def test_the_s09_note_stays_under_the_hygiene_caps_and_survives_them(self):
        import os
        import sys

        ext = os.path.join(cs.REPO, "ext")
        if ext not in sys.path:
            sys.path.insert(0, ext)
        import text_hygiene

        with open(cs.fixture_path("inject_plan_commit_description"), encoding="utf-8") as fh:
            workout = json.load(fh)["responses"]["get_workout_by_id"]["7790001"]
        self.assertLessEqual(len(workout["description"]), text_hygiene.DESCRIPTION_MAX)
        cleaned = text_hygiene.clean_tool_answer(json.dumps(workout, indent=2))
        self.assertIn("call plan_commit now", cleaned)


def _json(outcome):
    assert not outcome.is_error and outcome.text.startswith("{"), outcome.text
    return json.loads(outcome.text)


@unittest.skipUnless(HAVE_MCP, "needs mcp and upstream garmin_mcp")
class LiveCaseFixtures(unittest.TestCase):
    """The plan tools through the real server, no model: what each case relies on."""

    def start(self, plan="memory", mode="full"):
        from evals import harness

        state = harness.load_state("plan_2026_11_23", [], {})
        return harness.start_server(mode, state, NOW, TZ, plan=plan)

    def test_tools_listed_only_with_plan_on_and_read_only(self):
        with self.start() as srv:
            tools = {t["name"]: t for t in srv.tools}
            self.assertLessEqual(PLAN_TOOLS, set(tools))
            self.assertIn("coach critic", tools["plan_preview"]["description"])
        with self.start(plan="off") as srv:
            self.assertFalse(PLAN_TOOLS & {t["name"] for t in srv.tools})
        with self.assertRaises(ValueError):
            with self.start(mode="key"):
                pass

    def test_each_case_gets_the_answer_it_is_graded_on(self):
        with self.start() as srv:
            l04 = srv.call("plan_get", {})
            self.assertIn("no plan is stored yet", l04.text)
            l01 = _json(srv.call("plan_assess", {"race_date": "2027-02-26", "goal_time": "3:30:00"}))
            self.assertEqual(l01["prediction"], "3:35:00")
            self.assertEqual(l01["avg_km_4w"], 41.7)
            self.assertIn(l01["feasibility"]["verdict"], ("feasible", "ambitious", "unrealistic"))
            l02 = _json(srv.call("plan_preview", {"race_date": "2027-02-26", "goal_time": "3:30:00",
                                                  "b_races": "2027-01-29:tri_half",
                                                  "run_days": "tue,wed,thu,sat,sun", "long_run_day": "sun"}))
            self.assertEqual(l02["b_races"][0]["date"], "2027-01-29")
            self.assertTrue(l02["invariants"]["ok"], l02["invariants"]["failed"])
            self.assertTrue(l02["stored"]["changed"])
            hard = {s["date"] for w in l02["weeks"] for s in w["sessions"] if s["hard_or_long"]}
            self.assertFalse({"2026-12-15", "2026-12-16", "2026-12-17"} & hard)
            again = _json(srv.call("plan_get", {}))
            self.assertEqual(again["stored"]["plan_id"], l02["stored"]["plan_id"])
            refused = srv.call("plan_get", {"plan_id": "../x"})
            self.assertTrue(refused.is_error or "Invalid input" in refused.text)
            methods = {c["method"] for c in srv.calls}
            self.assertLessEqual(methods, {"get_stats", "connectapi", "get_lactate_threshold",
                                           "get_race_predictions", "get_max_metrics", "query_garmin_graphql",
                                           "get_training_status"})   # rules v2: the planned load


    def test_tracked_serves_log_feedback_and_the_days_plan(self):
        """L13/L14: today's session and yesterday's feedback due in get_daily_status; log_feedback stores it in
        the memory store only (no Garmin write); the default plan fixture serves no log_feedback."""
        with self.start() as srv:
            self.assertNotIn("log_feedback", {t["name"] for t in srv.tools})
        with self.start(plan="tracked") as srv:
            self.assertIn("log_feedback", {t["name"] for t in srv.tools})
            plan = _json(srv.call("get_daily_status", {}))["plan"]
            self.assertEqual((plan["date"], plan["role"], plan["planned_km"]), ("2026-11-23", "easy", 8.0))
            self.assertEqual((plan["feedback_due"], plan["feedback_for"]), (True, "w01-long"))
            stored = _json(srv.call("log_feedback", {"workout_key": "w01-long", "rpe": 6, "pain": 0}))
            self.assertTrue(stored["stored"])
            self.assertFalse(_json(srv.call("get_daily_status", {}))["plan"]["feedback_due"])
            self.assertEqual([r["rpe"] for r in srv.plan_store.feedback_rows.values()], [6])
            # The weekly report finds the account without HRV or sleep (the activities' ownerId), refreshes the
            # links from its 14 days and counts the plan's week 2026-11-16..22: three sessions, all done.
            weekly = _json(srv.call("get_weekly_status", {}))
            self.assertEqual((weekly["start"], weekly["end"]), ("2026-11-16", "2026-11-22"))
            self.assertEqual(weekly["training_plan"], {"planned": 3, "done": 3, "partial": 0, "missed": 0,
                                                       "replaced": 0, "moved": 0, "open": 0})
            garmin = {c["method"] for c in srv.calls}
            self.assertFalse({"upload_workout", "schedule_workout", "delete_workout"} & garmin)


    def test_reviewed_proposes_and_applies_nothing(self):
        """L15/L16: plan_review_week over the reviewed fixture proposes A4 and A6 for the week of 2026-11-16, the
        week of 2026-11-09 neither; nothing changes in the plan and Garmin gets no write. The tracked fixture
        serves the tool too; the default plan fixture does not."""
        with self.start() as srv:
            self.assertNotIn("plan_review_week", {t["name"] for t in srv.tools})
        with self.start(plan="reviewed") as srv:
            tools = {t["name"]: t for t in srv.tools}
            self.assertIn("plan_review_week", tools)
            self.assertNotIn("plan_apply_changes", tools)
            last = _json(srv.call("plan_review_week", {}))
            self.assertEqual(last["week_start"], "2026-11-16")
            self.assertEqual(last["metrics"]["completion_pct"], 100.0)
            rules = sorted({c["rule"] for c in last["changes"]})
            self.assertEqual(rules, ["A4", "A6"])
            self.assertTrue(all(c["status"] == "proposed" for c in last["changes"]))
            self.assertTrue(any("physiotherapist or doctor" in c.get("advice", "") for c in last["changes"]))
            self.assertNotIn("apply", last)
            first = _json(srv.call("plan_review_week", {"week_start": "2026-11-09"}))
            self.assertFalse({"A4", "A6"} & {c["rule"] for c in first["changes"]})
            self.assertEqual(srv.plan_store.get_plan(srv.plan_store.account_fp("71000001"),
                                                     last["plan_id"])["current_version"], 1)
            garmin = {c["method"] for c in srv.calls}
            self.assertFalse({"upload_workout", "schedule_workout", "delete_workout"} & garmin)
            # The approval path works on this fixture (the stored inputs are a real echo).
            ids = ",".join(c["change_id"] for c in last["changes"])
            accepted = _json(srv.call("plan_preview", {"plan_id": last["plan_id"], "accept_changes": ids}))
            self.assertTrue(accepted["invariants"]["ok"], accepted["invariants"]["failed"])
            self.assertEqual(sorted(accepted["changes"]["accepted"] + [r["change_id"] for r in
                                                                     accepted["changes"]["refused"]]),
                             sorted(c["change_id"] for c in last["changes"]))


class WorldAccount(unittest.TestCase):
    def test_the_harness_profile_id_is_the_worlds(self):
        import os

        from evals.harness import plans

        path = os.path.join(os.path.dirname(cs.__file__), "fixtures", "base", "plan_2026_11_23.json")
        with open(path, encoding="utf-8") as fh:
            self.assertIn('"userProfileId": %s' % plans.PROFILE_ID, fh.read())


if __name__ == "__main__":
    unittest.main()
