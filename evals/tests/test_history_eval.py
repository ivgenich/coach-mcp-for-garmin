"""Offline tests of the history retrieval set (evals/history): metrics, labels, the synthetic athlete and the
CI gate run without the model; and the live history cases' harness side (evals/cases/history.toml,
evals/harness/history.py): the fixtures served by the real in-process server, what each case's tools answer,
and the answer shapes (dates and the numbers behind every match). No network, no API key, no model. The gate
and the server runs need the history packages (config/requirements-history.txt) and skip without them."""

import contextlib
import importlib.util
import io
import json
import types
import unittest

from evals.history import athlete as A
from evals.history import metrics as M
from evals.history import run as R

HAVE_LC = all(importlib.util.find_spec(m) is not None
              for m in ("langchain_core", "langchain_classic", "langsmith", "numpy"))
HAVE_MCP = all(importlib.util.find_spec(m) is not None for m in ("mcp", "garmin_mcp"))
NOW, TZ = "2026-11-23T09:00", "Europe/London"
HISTORY_TOOLS = {"find_similar_weeks", "search_history"}


class Metrics(unittest.TestCase):
    def test_recall_mrr_ndcg(self):
        rel = {"a": 2, "b": 1}
        self.assertEqual(M.recall_at(["a", "x", "b"], rel, 5), 1.0)
        self.assertEqual(M.recall_at(["x", "a"], rel, 1), 0.0)
        self.assertEqual(M.reciprocal_rank(["x", "b", "a"], rel), 0.5)
        self.assertEqual(M.reciprocal_rank(["x"], rel), 0.0)
        self.assertAlmostEqual(M.ndcg_at(["a", "b"], rel, 5), 1.0)
        self.assertLess(M.ndcg_at(["b", "a"], rel, 5), 1.0)
        self.assertEqual(M.recall_at(["a"], {}, 5), 0.0)


class Athlete(unittest.TestCase):
    def test_deterministic_and_episodes_fire_the_rules(self):
        a, b = A.build(), A.build()
        wa = a.store.weeks(a.account_fp, a.week0, a.today)
        self.assertEqual(wa, [dict(w, updated_at=x["updated_at"]) for w, x in
                              zip(b.store.weeks(b.account_fp, b.week0, b.today), wa)])
        self.assertEqual(len(wa), A.WEEKS)
        by = {w["start"].isoformat(): w["numbers"] for w in wa}
        for name in ("hrv_drop_1", "hrv_drop_2", "hrv_drop_3"):
            self.assertIn("hrv_down", by[a.episodes[name][0]]["flags"])
        for name in ("sleep_debt_1", "sleep_debt_2"):
            self.assertIn("short_nights", by[a.episodes[name][0]]["flags"])
        for name in ("load_spike_1", "load_spike_2"):
            self.assertIn("load_high", by[a.episodes[name][0]]["flags"])
        self.assertEqual(by[a.week_start(70)]["mode"], "key")
        self.assertIsNone(by[a.week_start(70)]["acwr"], "key mode masks load")

    def test_labels_resolve(self):
        a = A.build()
        queries = R.load_labels()
        self.assertGreaterEqual(len(queries), 30)
        for q in queries:
            self.assertTrue(R.relevant_of(a, q), q["id"])
            if q["tool"] == "similar":
                self.assertTrue(R.reference_day(a, q["reference"]))


@unittest.skipUnless(HAVE_LC, "needs the history packages (config/requirements-history.txt)")
class Gate(unittest.TestCase):
    def test_gate_without_model_passes(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = R.main(["--embeddings", "none", "--gate"])
        self.assertEqual(rc, 0, out.getvalue())
        self.assertIn("gate PASS", out.getvalue())

    def test_gate_fails_under_floor(self):
        res = [{"config": "numbers", "all": {"recall@5": 0.1, "mrr": 0.1}}]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(R.gate(res), 1)
            self.assertEqual(R.gate([]), 1)


def history_cases():
    from evals import cases as cs

    return {c.id: c for c in cs.load_cases() if c.group == "history"}


class RunnerPassesTheFixture(unittest.TestCase):
    def test_history_case_starts_the_server_with_its_fixture(self):
        from evals import runner

        seen = []

        @contextlib.contextmanager
        def start_server(mode, state, now, tz, **kw):
            seen.append(kw)
            raise RuntimeError("stop here")
            yield  # pragma: no cover

        harness = types.SimpleNamespace(load_state=lambda *a: {}, start_server=start_server,
                                        clock_line=lambda now, tz: "")
        cases = history_cases()
        for cid, want in (("H01", {"history": "athlete"}), ("H03", {"history": "partial"}), ("H05", {})):
            seen.clear()
            with contextlib.redirect_stderr(io.StringIO()):
                runner.run_trial(object(), cases[cid], "bare", 1, harness=harness)
            self.assertEqual(seen, [want], cid)


@unittest.skipUnless(HAVE_MCP and not HAVE_LC, "runs only where mcp is installed but the history packages are not")
class WithoutThePackages(unittest.TestCase):
    def test_history_case_fails_with_a_clear_error(self):
        from evals.harness import history as hh
        from evals.harness import start_server

        with self.assertRaises(hh.HistoryPackagesMissing) as ctx:
            with start_server("full", None, NOW, TZ, history="athlete"):
                pass
        self.assertIn("config/requirements-history.txt", str(ctx.exception))
        with start_server("full", None, NOW, TZ) as srv:  # the server still starts for the other cases
            self.assertFalse(HISTORY_TOOLS & {t["name"] for t in srv.tools})


def _call(fixture, mode, calls):
    from evals.harness import start_server

    with start_server(mode, None, NOW, TZ, history=fixture) as srv:
        names = {t["name"] for t in srv.tools}
        return names, [srv.call(tool, args) for tool, args in calls]


def _json(outcome):
    assert not outcome.is_error and outcome.text.startswith("{"), outcome.text
    return json.loads(outcome.text)


@unittest.skipUnless(HAVE_MCP and HAVE_LC, "needs mcp and the history packages (config/requirements-history.txt)")
class LiveCaseFixtures(unittest.TestCase):
    """Each live case's fixture through the real server, no model: the tool answers the case relies on."""

    def test_tools_listed_only_with_history_on(self):
        from evals.harness import start_server

        for fixture, mode, want in (("athlete", "full", True), ("athlete", "key", True), ("off", "full", False),
                                    ("mismatch", "key", True)):
            with self.subTest(fixture=fixture, mode=mode), start_server(mode, None, NOW, TZ, history=fixture) as srv:
                tools = {t["name"]: t for t in srv.tools}
                self.assertEqual(HISTORY_TOOLS <= set(tools), want)
                if want:
                    self.assertTrue(tools["search_history"]["description"])
        with self.assertRaises(ValueError):
            with start_server("full", None, NOW, TZ, history="on"):
                pass

    def test_each_case_gets_the_answer_it_is_graded_on(self):
        cases = history_cases()
        _, (h01,) = _call(cases["H01"].history, "full", [("find_similar_weeks", {})])
        out = _json(h01)
        self.assertEqual(out["reference"]["start"], "2026-11-16")
        self.assertTrue(out["results"] and all(r["followed_by"] for r in out["results"][:3]))
        _, (h02,) = _call(cases["H02"].history, "full", [("search_history", {"flags": "short_nights",
                                                                              "order": "newest"})])
        self.assertEqual(_json(h02)["results"][0]["start"], "2026-06-15")
        _, (h03,) = _call(cases["H03"].history, "full", [("find_similar_weeks", {})])
        index = _json(h03)["index"]
        self.assertEqual((index["complete"], index["weeks"]), (False, 20))
        _, (h04,) = _call(cases["H04"].history, cases["H04"].mode, [("search_history", {"flags": "load_high"})])
        self.assertTrue(h04.is_error)
        self.assertIn("Personal API key", h04.text)
        names, _ = _call(cases["H05"].history, "full", [])
        self.assertFalse(HISTORY_TOOLS & names)
        _, (h06,) = _call(cases["H06"].history, "full", [("find_similar_weeks", {})])
        self.assertTrue(h06.text.startswith("No data:") and "at least 8 indexed weeks" in h06.text, h06.text)
        self.assertIn("2 so far", h06.text)
        _, (h07,) = _call(cases["H07"].history, "full", [("search_history", {"flags": "bike_heavy",
                                                                              "order": "newest"})])
        self.assertEqual(_json(h07)["results"][0]["start"], "2026-05-18")
        _, h08 = _call(cases["H08"].history, "full", [("find_similar_weeks", {}), ("search_history",
                                                                                      {"query": "bike"})])
        for outcome in h08:
            self.assertIn("different Garmin account", outcome.text)

    def test_every_result_shows_dates_and_the_numbers_it_matched_on(self):
        calls = [("find_similar_weeks", {}), ("find_similar_weeks", {"focus": "recovery"}),
                 ("find_similar_weeks", {"focus": "training"}),
                 ("search_history", {"flags": "short_nights"}), ("search_history", {"query": "bike heavy week"}),
                 ("search_history", {"query": "poor sleep", "order": "newest"}),
                 ("search_history", {"kind": "day", "flags": "short_sleep"}),
                 ("search_history", {"kind": "day", "query": "high resting heart rate"})]
        for mode in ("full", "key"):
            _, outcomes = _call("athlete", mode, calls)
            for (tool, args), outcome in zip(calls, outcomes):
                with self.subTest(mode=mode, tool=tool, **args):
                    out = _json(outcome)
                    ref = out.get("reference", {}).get("numbers", {})
                    for r in out["results"]:
                        self.assertRegex(r["start"], r"^\d{4}-\d{2}-\d{2}$")
                        self.assertRegex(r["end"], r"^\d{4}-\d{2}-\d{2}$")
                        self.assertTrue(r["numbers"])
                        if tool == "find_similar_weeks":
                            self.assertTrue(r["why"])
                            for reason in r["why"]:  # "<number> <result value> vs <reference value>"
                                key, mine, _vs, theirs = reason.split(" ")
                                self.assertEqual(float(mine), float(r["numbers"][key]), reason)
                                self.assertEqual(float(theirs), float(ref[key]), reason)
                        elif r["flags"] or r["tags"]:
                            self.assertTrue(r["why"])
                            self.assertTrue(any("(" in w for w in r["why"]), r["why"])


if __name__ == "__main__":
    unittest.main()
