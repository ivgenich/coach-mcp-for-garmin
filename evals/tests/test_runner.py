"""Offline tests of the runner, the cost guard and the CLI: a scripted fake Anthropic client
and a stub harness; no network, no API key, no server.

    python -B -m unittest discover -s evals/tests -t . -p 'test_runner.py'
"""

from __future__ import annotations

import contextlib
import dataclasses
import io
import json
import os
import subprocess
import tempfile
import types
import unittest
from unittest import mock

import evals.__main__ as cli
from evals import cost, runner

MODEL = "claude-opus-5-5"
KEY = "sk-ant-test-SENTINEL-0123456789abcdef"
CONTRACT_KEYS = {"schema", "case_id", "variant", "model", "trial", "started", "duration_s", "stop_reason", "system",
                 "tools", "messages", "tool_calls", "final_text", "garmin_calls", "usage", "cost_usd", "error"}


# --- stubs ----------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class Expect:
    judge: tuple = ()


@dataclasses.dataclass(frozen=True)
class Case:
    id: str = "T01"
    group: str = "tool_choice"
    title: str = "How am I today"
    prompt: object = "How am I today?"
    mode: str = "full"
    now: str = "2026-03-18T06:30"
    tz: str = "Europe/Athens"
    variants: tuple = ("bare", "instr")
    garmin: dict = dataclasses.field(default_factory=lambda: {"base": "daily_2026_03_18", "overlays": [],
                                                              "faults": {}})
    smoke: bool = True
    trials: int = 3
    all_trials_must_pass: bool = False
    why: str = "test"
    expect: Expect = Expect()
    followups: tuple = ()
    extra_tools: tuple = ()
    report_url: object = None
    db_seed: tuple = ()
    max_turns: object = None


TOOLS = [
    {"name": "get_daily_status", "description": "Today's level.", "input_schema": {"type": "object"}},
    {"name": "get_stats", "description": "Stats.", "input_schema": {"type": "object"}},
]


class StubServer:
    def __init__(self, errors=(), raise_on=()):
        self.tools = [dict(t) for t in TOOLS]
        self.instructions = "SERVER INSTRUCTIONS"
        self.calls = []
        self._errors, self._raise = set(errors), set(raise_on)

    def call(self, name, args):
        if name in self._raise:
            raise RuntimeError("boom")
        self.calls.append((name, dict(args)))
        if name in self._errors:
            return types.SimpleNamespace(text=f"{name} failed: sign in again.", is_error=True)
        if name == "get_daily_status":
            return types.SimpleNamespace(text=json.dumps({
                "date": "2026-03-18", "level": "green", "generated_at": "2026-03-18T06:30:00+02:00",
                "sources": {"x": 1}, "thresholds": {}, "thresholds_athlete": {}, "args": args}), is_error=False)
        return types.SimpleNamespace(text=f'{{"tool": "{name}", "level": "green", "value": 58}}', is_error=False)


def stub_harness(**kw):
    started = []

    @contextlib.contextmanager
    def start_server(mode, state, now, tz):
        srv = StubServer(**kw)
        started.append({"mode": mode, "state": state, "now": now, "tz": tz, "srv": srv})
        yield srv

    return types.SimpleNamespace(load_state=lambda base, overlays, faults: {"base": base, "faults": faults},
                                 start_server=start_server, started=started,
                                 clock_line=lambda now, tz: f"CLOCK {now} {tz}")


def read_jsonl(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def usage(i=100, o=50, r=0, w=0):
    return {"input_tokens": i, "output_tokens": o, "cache_read_input_tokens": r, "cache_creation_input_tokens": w}


def tool_use(*calls, model=MODEL):
    content = [{"type": "thinking", "thinking": "", "signature": "sig-1"}]
    content += [{"type": "tool_use", "id": f"tu_{n}", "name": n, "input": a} for n, a in calls]
    return {"model": model, "stop_reason": "tool_use", "content": content, "usage": usage()}


def answer(text, model=MODEL, stop="end_turn"):
    return {"model": model, "stop_reason": stop, "content": [{"type": "text", "text": text}], "usage": usage()}


class FakeClient:
    """Scripted messages.create: returns the scripted responses in order (the last one repeats)."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []
        self.messages = self

    def create(self, **kwargs):
        self.requests.append(json.loads(json.dumps(kwargs)))
        i = min(len(self.requests) - 1, len(self.responses) - 1)
        return json.loads(json.dumps(self.responses[i]))


# --- runner ---------------------------------------------------------------------------

class ToolLoop(unittest.TestCase):
    def run_one(self, client, case=None, variant="instr", harness=None, **kw):
        case = case or Case()
        return runner.run_trial(client, case, variant, 1, MODEL, harness=harness or stub_harness(), **kw)

    def test_parallel_tools_one_user_message_then_answer(self):
        client = FakeClient(tool_use(("get_daily_status", {"date": ""}), ("get_stats", {"date": "2026-03-18"})),
                            answer("Green: train as planned. HRV 58."))
        tr = self.run_one(client)
        self.assertEqual(tr["stop_reason"], "end_turn")
        self.assertIsNone(tr["error"])
        self.assertEqual(tr["final_text"], "Green: train as planned. HRV 58.")
        self.assertEqual([c["name"] for c in tr["tool_calls"]], ["get_daily_status", "get_stats"])
        self.assertEqual([c["index"] for c in tr["tool_calls"]], [0, 1])
        self.assertEqual(tr["tool_calls"][1]["input"], {"date": "2026-03-18"})
        roles = [m["role"] for m in tr["messages"]]
        self.assertEqual(roles, ["user", "assistant", "user", "assistant"])
        results = tr["messages"][2]["content"]
        self.assertEqual([r["tool_use_id"] for r in results], ["tu_get_daily_status", "tu_get_stats"])
        self.assertTrue(all("is_error" not in r for r in results))
        # the thinking block goes back unchanged on the next request
        self.assertEqual(client.requests[1]["messages"][1]["content"][0],
                         {"type": "thinking", "thinking": "", "signature": "sig-1"})
        self.assertEqual(tr["garmin_calls"], [{"method": "get_daily_status", "args": {"date": ""}},
                                              {"method": "get_stats", "args": {"date": "2026-03-18"}}])
        self.assertEqual(tr["usage"], usage(200, 100))
        self.assertAlmostEqual(tr["cost_usd"], 2 * (100 * 4 + 50 * 20) / 1e6)
        self.assertEqual(tr["turns"], 2)

    def test_tool_error_goes_back_with_is_error(self):
        client = FakeClient(tool_use(("get_daily_status", {}), ("get_stats", {})), answer("Please sign in."))
        tr = self.run_one(client, harness=stub_harness(errors=["get_daily_status"]))
        results = tr["messages"][2]["content"]
        self.assertIs(results[0]["is_error"], True)
        self.assertEqual(results[0]["content"], "get_daily_status failed: sign in again.")
        self.assertNotIn("is_error", results[1])
        self.assertEqual([c["is_error"] for c in tr["tool_calls"]], [True, False])
        self.assertEqual(client.requests[1]["messages"][2]["content"][0]["is_error"], True)

    def test_max_turns_stop(self):
        client = FakeClient(tool_use(("get_stats", {})))
        tr = self.run_one(client)
        self.assertEqual(len(client.requests), runner.MAX_TURNS)
        self.assertEqual(tr["stop_reason"], "max_turns")
        self.assertEqual(tr["turns"], 8)
        self.assertEqual(tr["final_text"], "")

    def test_an_empty_turn_ends_the_trial_and_is_never_sent_back(self):
        # The API rejects an empty assistant message in the middle (review V6).
        empty = {"model": MODEL, "stop_reason": "end_turn", "content": [], "usage": usage()}
        client = FakeClient(tool_use(("get_stats", {})), empty, answer("never asked"))
        tr = self.run_one(client, dataclasses.replace(Case(), followups=("Try again please",)))
        self.assertEqual(len(client.requests), 2)
        self.assertEqual((tr["final_text"], tr["answers"]), ("", [""]))
        self.assertTrue(all(m["content"] for m in tr["messages"]))

    def test_refusal_and_max_tokens_stop_the_trial(self):
        for stop in ("refusal", "max_tokens"):
            with self.subTest(stop=stop):
                tr = self.run_one(FakeClient(answer("partial", stop=stop)), case=Case(prompt=["a", "b"]))
                self.assertEqual(tr["stop_reason"], stop)
                self.assertEqual(tr["turns"], 1)

    def test_two_user_turns(self):
        client = FakeClient(answer("Rate limited; wait 15 minutes."), answer("Still waiting."))
        tr = self.run_one(client, case=Case(prompt=["How am I today?", "Try again please"]))
        self.assertEqual([m["role"] for m in tr["messages"]], ["user", "assistant", "user", "assistant"])
        self.assertEqual(tr["messages"][2]["content"], "Try again please")
        self.assertEqual(tr["answers"], ["Rate limited; wait 15 minutes.", "Still waiting."])
        self.assertEqual(tr["final_text"], "Still waiting.")

    def test_served_model_must_be_requested_model(self):
        tr = self.run_one(FakeClient(answer("hi", model="claude-opus-4-8")))
        self.assertIn("served model", tr["error"])

    def test_harness_exception_is_a_harness_error(self):
        tr = self.run_one(FakeClient(tool_use(("get_stats", {}))), harness=stub_harness(raise_on=["get_stats"]))
        self.assertEqual(tr["stop_reason"], "harness_error")
        self.assertIn("boom", tr["error"])

    def test_api_error_is_recorded_not_raised(self):
        class Broken:
            messages = types.SimpleNamespace(create=mock.Mock(side_effect=ConnectionError("down")))

        tr = self.run_one(Broken())
        self.assertEqual(tr["stop_reason"], "error")
        self.assertIn("down", tr["error"])

    def test_request_shape(self):
        client = FakeClient(answer("ok"))
        h = stub_harness()
        self.run_one(client, harness=h, effort="high")
        req = client.requests[0]
        self.assertEqual(req["model"], MODEL)
        self.assertEqual(req["thinking"], {"type": "adaptive"})
        self.assertEqual(req["output_config"], {"effort": "high"})
        self.assertEqual(req["cache_control"], {"type": "ephemeral"})
        self.assertEqual([t["name"] for t in req["tools"]], ["get_daily_status", "get_stats"])
        self.assertEqual(req["tools"][0], TOOLS[0])  # verbatim
        self.assertEqual(req["tools"][-1]["cache_control"], {"type": "ephemeral"})
        self.assertEqual(req["system"][0]["cache_control"], {"type": "ephemeral"})
        for banned in ("fallbacks", "temperature", "top_p", "top_k", "tool_choice", "betas"):
            self.assertNotIn(banned, req)
        self.assertEqual(h.started[0]["mode"], "full")
        self.assertEqual(h.started[0]["now"], "2026-03-18T06:30")
        self.assertEqual(h.started[0]["state"]["base"], "daily_2026_03_18")

    def test_transcript_schema(self):
        tr = self.run_one(FakeClient(tool_use(("get_stats", {})), answer("ok")))
        self.assertLessEqual(CONTRACT_KEYS, set(tr))
        self.assertEqual(tr["schema"], 1)
        self.assertEqual((tr["case_id"], tr["variant"], tr["model"], tr["trial"]), ("T01", "instr", MODEL, 1))
        self.assertEqual(tr["tools"], ["get_daily_status", "get_stats"])
        # The optional tool_docs: each description and schema as the model saw them (grading.numbers).
        self.assertEqual(tr["tool_docs"], ['Today\'s level.\n{"type": "object"}', 'Stats.\n{"type": "object"}'])
        self.assertRegex(tr["started"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertIsInstance(tr["duration_s"], float)
        self.assertEqual(set(tr["usage"]), set(cost.USAGE_KEYS))
        self.assertEqual(set(tr["tool_calls"][0]), {"index", "name", "input", "text", "is_error"})
        json.dumps(tr)  # plain JSON data


URL = "https://claude.ai/artifact/eval-report"


class ClientToolFakes(unittest.TestCase):
    def test_artifact_data_is_deferred_until_tool_search(self):
        ct = runner.ClientTools(("ArtifactData", "ToolSearch"), URL)
        self.assertEqual([t["name"] for t in ct.definitions()], ["ToolSearch"])
        text, err = ct.call("ArtifactData", {"action": "get", "url": URL, "collection": "days", "doc_id": "x"})
        self.assertTrue(err)
        text, err = ct.call("ToolSearch", {"query": "select:ArtifactData"})
        self.assertFalse(err)
        self.assertIn('"name": "ArtifactData"', text)
        self.assertEqual([t["name"] for t in ct.definitions()], ["ArtifactData", "ToolSearch"])

    def test_versions_pins_url_and_log(self):
        ct = runner.ClientTools(("ArtifactData",), URL, [{"collection": "feel", "doc_id": "d", "data": {"v": 1}}])
        call = lambda **a: ct.call("ArtifactData", {"url": URL, **a})  # noqa: E731
        self.assertTrue(call(action="get", collection="days", doc_id="2026-03-18")[1])  # not found
        text, err = call(action="set", collection="days", doc_id="2026-03-18", data={"level": "green"})
        self.assertFalse(err)
        self.assertEqual(json.loads(text)["version"], 1)
        self.assertTrue(call(action="set", collection="days", doc_id="2026-03-18", data={})[1])  # no pin
        self.assertTrue(call(action="set", collection="days", doc_id="2026-03-18", data={}, if_version=7)[1])
        self.assertFalse(call(action="update", collection="days", doc_id="2026-03-18", data={"a": 1},
                              if_version=1)[1])
        got = json.loads(call(action="get", collection="days", doc_id="2026-03-18")[0])
        self.assertEqual((got["version"], got["data"]), (2, {"level": "green", "a": 1}))
        self.assertTrue(ct.call("ArtifactData", {"action": "get", "url": "https://claude.ai/artifact/other",
                                                 "collection": "days", "doc_id": "2026-03-18"})[1])
        self.assertFalse(call(action="delete", collection="feel", doc_id="d", if_version=1)[1])
        state = ct.state()
        self.assertIn("feel/d", state["seeded"])
        self.assertNotIn("feel/d", state["docs"])
        self.assertEqual([o["action"] for o in state["ops"]],
                         ["get", "set", "set", "set", "update", "get", "get", "delete"])
        self.assertEqual([o["ok"] for o in state["ops"]], [False, True, False, False, True, True, False, True])
        with self.assertRaises(ValueError):
            runner.ClientTools(("Artifact",))

    def test_recipe_trial_with_seed_and_db_in_transcript(self):
        case = Case(id="R02", extra_tools=("ArtifactData", "ToolSearch"), report_url=URL,
                    db_seed=({"collection": "days", "doc_id": "2026-03-18", "status_of": ["daily_incomplete"]},))
        client = FakeClient(
            tool_use(("ToolSearch", {"query": "select:ArtifactData"})),
            tool_use(("ArtifactData", {"action": "get", "url": URL, "collection": "days", "doc_id": "2026-03-18"})),
            answer("Updated; level green."))
        h = stub_harness()
        tr = runner.run_trial(client, case, "bare", 1, MODEL, harness=h)
        self.assertIsNone(tr["error"], tr["error"])
        self.assertEqual(len(h.started), 2)  # the seed's own server, then the trial's
        self.assertEqual(h.started[0]["state"]["base"], "daily_2026_03_18")
        seeded = tr["db"]["seeded"]["days/2026-03-18"]["data"]
        self.assertNotIn("sources", seeded)
        self.assertEqual(seeded["stored_at"], "2026-03-18T06:30:00+02:00")
        self.assertEqual(seeded["args"], {"text_format": "markdown"})
        self.assertEqual(tr["tools"], ["get_daily_status", "get_stats", "ToolSearch"])
        self.assertEqual([t["name"] for t in client.requests[0]["tools"]], tr["tools"])
        self.assertIn("ArtifactData", [t["name"] for t in client.requests[1]["tools"]])
        self.assertEqual([c["name"] for c in tr["tool_calls"]], ["ToolSearch", "ArtifactData"])
        self.assertEqual(tr["garmin_calls"], [])  # client tools are not Garmin calls

    def test_followups_are_more_turns(self):
        client = FakeClient(answer("one"), answer("two"))
        tr = runner.run_trial(client, Case(followups=("Try again please",)), "bare", 1, MODEL,
                              harness=stub_harness())
        self.assertEqual(tr["answers"], ["one", "two"])


class SdkObjects(unittest.TestCase):
    def test_sdk_message_objects(self):
        try:
            from anthropic.types import Message
        except ImportError:
            self.skipTest("needs anthropic")
        raw = [tool_use(("get_stats", {})), answer("ok")]
        objs = [Message.model_validate({"id": f"msg_{i}", "type": "message", "role": "assistant",
                                        "stop_sequence": None, **r}) for i, r in enumerate(raw)]
        client = FakeClient()
        client.create = lambda **kw: (client.requests.append(kw), objs[len(client.requests) - 1])[1]
        tr = runner.run_trial(client, Case(), "bare", 1, MODEL, harness=stub_harness())
        self.assertEqual((tr["stop_reason"], tr["final_text"]), ("end_turn", "ok"))
        self.assertEqual(tr["messages"][1]["content"][0]["signature"], "sig-1")
        json.dumps(tr)


class Script(unittest.TestCase):
    def test_evals_sh_refuses_live_run_without_key(self):
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        p = subprocess.run(["bash", os.path.join(runner.ROOT, "scripts", "evals.sh"), "run", "--smoke"],
                           env=env, capture_output=True, text=True, check=False)
        self.assertEqual(p.returncode, 2)
        self.assertIn("ANTHROPIC_API_KEY", p.stderr)
        p = subprocess.run(["bash", os.path.join(runner.ROOT, "scripts", "evals.sh"), "grade", KEY],
                           env=env, capture_output=True, text=True, check=False)
        self.assertEqual(p.returncode, 2)
        self.assertNotIn(KEY, p.stdout + p.stderr)


class SystemPrompt(unittest.TestCase):
    def test_clock_line_is_the_harness_one(self):
        tr = runner.run_trial(FakeClient(answer("ok")), Case(), "bare", 1, MODEL, harness=stub_harness())
        self.assertEqual(tr["system"], "CLOCK 2026-03-18T06:30 Europe/Athens")
        try:
            from evals.harness import clock_line
        except ImportError:
            self.skipTest("needs evals.harness (mcp and upstream)")
        self.assertEqual(clock_line("2026-03-18T06:30", "Europe/Athens"),
                         "Current date and time: Wednesday, 18 March 2026, 06:30 (Europe/Athens, UTC+02:00).")

    def test_variants(self):
        clock = "Current date and time: Wednesday, 18 March 2026, 06:30 (UTC, UTC)."
        bare = runner.build_system("bare", clock, "INSTR", "COACH")
        instr = runner.build_system("instr", clock, "INSTR", "COACH")
        coach = runner.build_system("coach", clock, "INSTR", "COACH")
        for s in (bare, instr, coach):
            self.assertTrue(s.startswith(clock))
        self.assertEqual(bare, clock)
        self.assertIn("INSTR", instr)
        self.assertNotIn("COACH", instr)
        self.assertTrue(coach.endswith("INSTR\n\nCOACH"))
        with self.assertRaises(ValueError):
            runner.build_system("web", clock, "", "")

    def test_coach_prompt_is_the_recipe(self):
        self.assertIn("Read-only", runner.read_coach_prompt())


# --- cost -----------------------------------------------------------------------------

class Cost(unittest.TestCase):
    def test_usage_cost(self):
        u = usage(1_000_000, 1_000_000, 1_000_000, 1_000_000)
        self.assertAlmostEqual(cost.usage_cost("claude-opus-5-5", u), 4 + 20 + 0.20 + 5)
        self.assertAlmostEqual(cost.usage_cost("claude-sonnet-5-5", u), 2 + 10 + 0.10 + 2.5)
        self.assertEqual(cost.usage_cost(MODEL, None), 0)

    def test_unknown_model(self):
        with self.assertRaises(cost.UnknownModel):
            cost.usage_cost("claude-unknown", usage())

    def test_budget_stops_before_crossing(self):
        b = cost.Budget(max_usd=0.0015)
        client = cost.MeteredClient(FakeClient(tool_use(("get_stats", {}))), b)
        req = {"model": MODEL, "messages": [], "max_tokens": 10}  # worst case: about $0.0002 on Opus 5.5
        client.messages.create(**req)  # 0 + 0.0002 <= cap; the call costs 0.0014
        self.assertAlmostEqual(b.spent, 0.0014)
        with self.assertRaises(cost.BudgetExceeded):
            client.messages.create(**req)  # 0.0014 + 0.0002 > cap
        self.assertEqual(b.calls, 1)

    def test_projection_is_the_worst_case_of_the_call(self):
        # Every output token the call may use counts, however cheap earlier calls were (review V4).
        b = cost.Budget(max_usd=1.0)
        b.costliest = 0.03
        req = {"model": MODEL, "messages": [], "max_tokens": 16000}
        self.assertGreaterEqual(b.estimate(req), 16000 * 20.00 / 1e6)
        b.spent = 0.96
        with self.assertRaises(cost.BudgetExceeded):
            b.check(req)

    def test_first_call_estimate(self):
        b = cost.Budget(max_usd=0.01)
        with self.assertRaises(cost.BudgetExceeded):  # 4,000 output tokens alone are $0.08 on Opus 5.5
            b.check({"model": MODEL, "messages": []})
        cost.Budget(max_usd=None).check({"model": MODEL})

    def test_budget_ends_trial(self):
        b = cost.Budget(max_usd=0.0015)  # the first call (0.0014) fits, a second would not
        client = cost.MeteredClient(FakeClient(tool_use(("get_stats", {}))), b)
        with mock.patch.object(cost.Budget, "estimate", lambda self, request: 0.0002):
            tr = runner.run_trial(client, Case(), "bare", 1, MODEL, harness=stub_harness())
        self.assertEqual(tr["stop_reason"], "budget")
        self.assertIn("--max-usd", tr["error"])
        self.assertEqual(tr["turns"], 1)
        self.assertLessEqual(b.spent, 0.0015)


# --- CLI ------------------------------------------------------------------------------

@dataclasses.dataclass
class StubCheck:
    name: str
    passed: bool
    detail: str = ""
    kind: str = "code"


class StubGrade:
    def __init__(self, checks, passed):
        self.checks, self.passed = checks, passed

    def to_dict(self):
        return {"checks": [c if isinstance(c, dict) else dataclasses.asdict(c) for c in self.checks],
                "passed": self.passed}


class StubGrading:
    Check = StubCheck

    def __init__(self):
        self.summarized, self.judged = [], []

    def grade(self, case, transcript, judge_checks=None):
        ok = "train as planned" in transcript["final_text"].lower()
        return StubGrade([StubCheck("mentions", ok)] + list(judge_checks or []), ok)

    def judge(self, case, transcript, client, model):
        self.judged.append(model)
        return [StubCheck("no_redecide", True, "ok", "judge")]

    def grade_run(self, run_dir, cases, client=None, judge_model=None, write=True):
        rows = []
        tdir = os.path.join(run_dir, "transcripts")
        for name in sorted(os.listdir(tdir)):
            with open(os.path.join(tdir, name), encoding="utf-8") as f:
                tr = json.load(f)
            case = cases[tr["case_id"]]
            judged = self.judge(case, tr, client, judge_model) if client is not None and case.expect.judge else []
            row = self.grade(case, tr, judged).to_dict()
            row.update({"case_id": tr["case_id"], "variant": tr["variant"], "trial": tr["trial"], "transcript": name,
                        "judge_model": judge_model if judged else None,
                        "judge_usage": {"input_tokens": 9} if judged else None})
            rows.append(row)
        if not write:
            return rows
        with open(os.path.join(run_dir, "grades.jsonl"), "w", encoding="utf-8") as f:
            f.writelines(json.dumps(r) + "\n" for r in rows)
        return rows

    def summarize(self, run_dir):
        self.summarized.append(run_dir)
        with open(os.path.join(run_dir, "summary.json"), "w") as f:
            f.write("{}")

    def compare(self, a, b):
        return f"compared {os.path.basename(a)} {os.path.basename(b)}"


class Cli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.runs = os.path.join(self.tmp.name, "runs")
        cases = [Case(), Case(id="F04", smoke=False, variants=("instr",), expect=Expect(judge=("no_redecide",)))]
        patcher = mock.patch.object(cli, "load_cases", lambda path=None: cases)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.grading = StubGrading()
        p2 = mock.patch.object(cli, "_grading", lambda: self.grading)
        p2.start()
        self.addCleanup(p2.stop)

    def cli(self, argv, env=None, client=None, harness=None):
        out = io.StringIO()
        env = {} if env is None else env
        with mock.patch.dict(os.environ, env, clear=False), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(out):
            if KEY not in env.values():
                os.environ.pop("ANTHROPIC_API_KEY", None)
            code = cli.main(argv, client_factory=(lambda: client) if client else None,
                            harness=harness or stub_harness())
        return code, out.getvalue()

    def run_argv(self, *extra):
        return ["run", "--runs-dir", self.runs, "--run-id", "r1", "--model", MODEL, *extra]

    def test_refuses_live_run_without_key(self):
        client = FakeClient(answer("x"))
        code, out = self.cli(self.run_argv("--smoke"), client=client)
        self.assertEqual(code, 2)
        self.assertIn("ANTHROPIC_API_KEY", out)
        self.assertEqual(client.requests, [])
        self.assertFalse(os.path.exists(self.runs))

    def test_refuses_a_cap_that_is_not_a_finite_number(self):
        # NaN would turn the cap off (review V5).
        for value in ("nan", "inf", "-1", "0"):
            with self.subTest(value=value):
                client = FakeClient(answer("x"))
                code, out = self.cli(self.run_argv("--max-usd", value), env={"ANTHROPIC_API_KEY": KEY}, client=client)
                self.assertEqual(code, 2, out)
                self.assertEqual(client.requests, [])

    def test_a_run_whose_trials_errored_exits_1(self):
        # A bad key or a rejected request must not look like a measured run (review V2).
        class Down:
            def __init__(self):
                self.messages = self

            def create(self, **kwargs):
                raise RuntimeError("401 invalid x-api-key")
        code, out = self.cli(self.run_argv("--smoke"), env={"ANTHROPIC_API_KEY": KEY}, client=Down())
        self.assertEqual(code, 1, out)
        self.assertIn("ended in an error", out)

    def test_a_case_may_raise_its_turn_cap(self):
        # The morning-report recipe needs more model calls than the default cap (review V1).
        seen = []
        real = runner.run_trial

        def spy(*a, **kw):
            seen.append(kw.get("max_turns"))
            return real(*a, **kw)
        cases = [dataclasses.replace(Case(), max_turns=16)]
        with mock.patch.object(cli, "load_cases", lambda path=None: cases), \
                mock.patch.object(runner, "run_trial", spy):
            code, out = self.cli(self.run_argv("--variants", "bare"), env={"ANTHROPIC_API_KEY": KEY},
                                 client=FakeClient(answer("Train as planned.")))
        self.assertEqual((code, seen), (0, [16]), out)

    def test_refuses_judge_equal_to_model_and_unknown_models(self):
        env = {"ANTHROPIC_API_KEY": KEY}
        code, out = self.cli(self.run_argv("--judge-model", MODEL), env=env)
        self.assertEqual(code, 2)
        self.assertIn("judge model", out)
        code, out = self.cli(self.run_argv("--model", "claude-nope"), env=env)
        self.assertEqual(code, 2)
        self.assertIn("no price", out)
        code, out = self.cli(self.run_argv("--cases", "ZZ9"), env=env)
        self.assertEqual(code, 2)

    def test_judge_swaps_when_haiku_is_under_test(self):
        client = FakeClient(answer("Train as planned.", model="claude-haiku-5-5"))
        code, _ = self.cli(self.run_argv("--model", "claude-haiku-5-5", "--cases", "F04", "--trials", "1"),
                           env={"ANTHROPIC_API_KEY": KEY}, client=client)
        self.assertEqual(code, 0)
        self.assertEqual(self.grading.judged, ["claude-sonnet-5-5"])

    def test_run_writes_the_run_dir_and_never_the_key(self):
        client = FakeClient(tool_use(("get_daily_status", {})), answer("Green. Train as planned."))
        code, out = self.cli(self.run_argv("--trials", "2"), env={"ANTHROPIC_API_KEY": KEY}, client=client)
        self.assertEqual(code, 0, out)
        run_dir = os.path.join(self.runs, "r1")
        names = sorted(os.listdir(os.path.join(run_dir, "transcripts")))
        self.assertEqual(names, ["F04__instr__t1.json", "F04__instr__t2.json", "T01__bare__t1.json",
                                 "T01__bare__t2.json", "T01__instr__t1.json", "T01__instr__t2.json"])
        with open(os.path.join(run_dir, "config.json")) as f:
            config = json.load(f)
        self.assertTrue(config["complete"])
        self.assertEqual(config["judge_model"], "claude-haiku-5-5")
        rows = read_jsonl(os.path.join(run_dir, "grades.jsonl"))
        self.assertEqual(len(rows), 6)
        f04 = [r for r in rows if r["case_id"] == "F04"]
        self.assertEqual([c["kind"] for c in f04[0]["checks"]], ["code", "judge"])
        self.assertEqual(self.grading.summarized, [run_dir])
        # the key is nowhere: not in a file, not in the output
        self.assertNotIn(KEY, out)
        for dirpath, _, files in os.walk(run_dir):
            for name in files:
                with open(os.path.join(dirpath, name), encoding="utf-8") as f:
                    self.assertNotIn("SENTINEL", f.read(), name)

    def test_key_echoed_by_a_tool_is_redacted(self):
        harness = stub_harness()
        client = FakeClient(answer(f"your key is {KEY}"))
        self.cli(self.run_argv("--smoke", "--variants", "bare"), env={"ANTHROPIC_API_KEY": KEY},
                 client=client, harness=harness)
        with open(os.path.join(self.runs, "r1", "transcripts", "T01__bare__t1.json")) as f:
            text = f.read()
        self.assertNotIn(KEY, text)
        self.assertIn("[redacted]", text)

    def test_budget_stop_exit_3(self):
        costly = dict(tool_use(("get_stats", {})), usage=usage(100, 2000))  # $0.0404 a call
        client = FakeClient(costly)
        with mock.patch.object(cost.Budget, "estimate", lambda self, request: 0.03):
            code, out = self.cli(self.run_argv("--max-usd", "0.09", "--trials", "1"), env={"ANTHROPIC_API_KEY": KEY},
                                 client=client)
        self.assertEqual(code, 3, out)
        with open(os.path.join(self.runs, "r1", "config.json")) as f:
            config = json.load(f)
        self.assertFalse(config["complete"])
        self.assertLessEqual(config["spent_usd"], 0.09)
        self.assertEqual(len(client.requests), 2)
        self.assertIn("incomplete", out)

    def test_grade_offline_carries_judge_checks_over(self):
        client = FakeClient(answer("Train as planned."))
        self.cli(self.run_argv("--cases", "F04", "--trials", "1"), env={"ANTHROPIC_API_KEY": KEY}, client=client)
        run_dir = os.path.join(self.runs, "r1")
        out_dir = os.path.join(self.tmp.name, "regraded")
        code, out = self.cli(["grade", run_dir, "--out", out_dir])
        self.assertEqual(code, 0, out)
        rows = read_jsonl(os.path.join(out_dir, "grades.jsonl"))
        self.assertEqual([c["kind"] for c in rows[0]["checks"]], ["code", "judge"])
        self.assertEqual(rows[0]["checks"][1]["name"], "no_redecide")
        self.assertEqual(rows[0]["judge_usage"], {"input_tokens": 9})
        self.assertEqual(rows[0]["judge_model"], "claude-haiku-5-5")
        self.assertEqual(self.grading.judged, ["claude-haiku-5-5"])  # the live run only: no judge call offline
        self.assertIn("1 passed; judge checks carried over for 1", out)
        code, out = self.cli(["compare", run_dir, out_dir])
        self.assertEqual((code, out.strip()), (0, "compared r1 regraded"))

    def test_list(self):
        code, out = self.cli(["list"])
        self.assertEqual(code, 0)
        self.assertIn("T01", out)
        self.assertIn("smoke", out)


if __name__ == "__main__":
    unittest.main()
