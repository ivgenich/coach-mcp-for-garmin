"""Offline tests of eval-run tracing (evals/tracing.py): off by default, Langfuse config and wire format,
trial / chat / tool spans with tokens and cost, server spans nested in process, no text exported.

    python -B -m unittest discover -s evals/tests -t . -p 'test_tracing.py'

The SDK tests need opentelemetry-sdk and the OTLP/HTTP exporter (config/requirements-telemetry.txt) and skip
without them; the nesting test also needs the real in-process server (upstream garmin_mcp on mcp 2.x).
"""

from __future__ import annotations

import base64
import contextlib
import importlib.util
import io
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import evals.__main__ as cli
from evals import runner, tracing
from evals.tests import test_runner as tr_stubs

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HAVE_SDK = (importlib.util.find_spec("opentelemetry") is not None
            and importlib.util.find_spec("opentelemetry.sdk") is not None
            and importlib.util.find_spec("opentelemetry.exporter.otlp.proto.http") is not None)
HAVE_SERVER = HAVE_SDK and importlib.util.find_spec("garmin_mcp") is not None
SDK_REASON = "needs opentelemetry-sdk and the OTLP/HTTP exporter"
MODEL = tr_stubs.MODEL
PROMPT_CANARY = "canary prompt text about HRV 4217"
ANSWER_CANARY = "canary answer: train as planned 1977-07-07"
SK = "sk-lf-canary-secret-0123456789"
PK = "pk-lf-canary-public-0123456789"


def telemetry():
    return tracing._telemetry()


def reset():
    tracing.shutdown()
    tracing._ON = False
    tracing.set_run(None)
    if HAVE_SDK:
        telemetry()._reset_for_tests()


class Off(unittest.TestCase):
    def tearDown(self):
        reset()

    def test_nothing_set_means_off_and_no_import(self):
        code = ("import sys; sys.path.insert(0, %r); from evals import tracing, runner; "
                "print(tracing.wanted(), tracing.setup(), "
                "sorted(m for m in sys.modules if m.startswith('opentelemetry')))" % ROOT)
        env = {k: v for k, v in os.environ.items() if not k.startswith(("LANGFUSE_", "OTEL_", "TRACING"))}
        out = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, env=env, check=True,
                             cwd=ROOT)
        self.assertEqual(out.stdout.strip(), "False False []")

    def test_hooks_are_noops_and_the_transcript_is_unchanged(self):
        self.assertFalse(tracing.enabled())
        case = tr_stubs.Case(prompt=PROMPT_CANARY)
        script = (tr_stubs.tool_use(("get_daily_status", {})), tr_stubs.answer(ANSWER_CANARY))
        plain = runner.run_trial(tr_stubs.FakeClient(*script), case, "instr", 1, MODEL, harness=tr_stubs.stub_harness())
        wrapped = runner.run_trial(tracing.wrap_client(tr_stubs.FakeClient(*script)), case, "instr", 1, MODEL,
                                   harness=tr_stubs.stub_harness())
        for k in ("started", "duration_s"):
            plain.pop(k)
            wrapped.pop(k)
        self.assertEqual(plain, wrapped)
        self.assertIsNone(tracing.begin_trial("T01", "instr", 1, MODEL))
        with tracing.tool_call("get_daily_status") as h:
            h.error()

    def test_problems(self):
        self.assertEqual(tracing.problems({}), [])
        self.assertTrue(tracing.problems({"LANGFUSE_PUBLIC_KEY": PK}))
        self.assertTrue(tracing.problems({"LANGFUSE_SECRET_KEY": SK}))
        self.assertTrue(tracing.problems({"LANGFUSE_PUBLIC_KEY": PK, "LANGFUSE_SECRET_KEY": SK,
                                          "LANGFUSE_BASE_URL": "http://langfuse.example"}))
        self.assertEqual(tracing.problems({"LANGFUSE_PUBLIC_KEY": PK, "LANGFUSE_SECRET_KEY": SK,
                                           "LANGFUSE_HOST": "https://us.cloud.langfuse.com"}), [])
        self.assertEqual(len(tracing.problems({"TRACING": "on"})), 2)
        for p in tracing.problems({"LANGFUSE_PUBLIC_KEY": PK, "LANGFUSE_BASE_URL": "https://u:" + SK + "@x"}):
            self.assertNotIn(SK, p)
            self.assertNotIn(PK, p)

    def test_langfuse_target(self):
        url, headers, secrets = tracing.langfuse_target({"LANGFUSE_PUBLIC_KEY": PK, "LANGFUSE_SECRET_KEY": SK})
        self.assertEqual(url, "https://cloud.langfuse.com/api/public/otel/v1/traces")
        self.assertEqual(headers["x-langfuse-ingestion-version"], "4")
        self.assertEqual(base64.b64decode(headers["Authorization"].split(" ", 1)[1]).decode(), f"{PK}:{SK}")
        self.assertIn(SK, secrets)

    def test_cli_refuses_a_half_set_langfuse(self):
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": tr_stubs.KEY, "LANGFUSE_PUBLIC_KEY": PK}), \
                contextlib.redirect_stdout(out):
            os.environ.pop("LANGFUSE_SECRET_KEY", None)
            code = cli.main(["run", "--runs-dir", tmp, "--run-id", "r1", "--model", MODEL],
                            client_factory=lambda: tr_stubs.FakeClient(tr_stubs.answer("x")),
                            harness=tr_stubs.stub_harness())
        self.assertEqual(code, 2)
        self.assertIn("LANGFUSE_SECRET_KEY", out.getvalue())
        self.assertNotIn(PK, out.getvalue())


@unittest.skipUnless(HAVE_SDK, SDK_REASON)
class Spans(unittest.TestCase):
    def setUp(self):
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

        reset()
        self.mem = InMemorySpanExporter()
        self.assertTrue(tracing.setup({}, exporters=[(self.mem, None)]))
        tracing.set_run("gh-1-1")

    def tearDown(self):
        reset()

    def spans(self):
        telemetry()._S.tracer_provider.force_flush(5000)
        return list(self.mem.get_finished_spans())

    def test_trial_chat_and_tool_spans(self):
        client = tracing.wrap_client(tr_stubs.FakeClient(tr_stubs.tool_use(("get_daily_status", {"x": PROMPT_CANARY})),
                                                         tr_stubs.answer(ANSWER_CANARY)))
        tr = runner.run_trial(client, tr_stubs.Case(prompt=PROMPT_CANARY), "instr", 1, MODEL,
                              harness=tr_stubs.stub_harness())
        self.assertEqual(tr["stop_reason"], "end_turn")
        spans = self.spans()
        names = sorted(s.name for s in spans)
        self.assertEqual(names, ["chat " + MODEL, "chat " + MODEL, "eval.trial", "execute_tool get_daily_status"])
        root = next(s for s in spans if s.name == "eval.trial")
        self.assertIsNone(root.parent)
        for s in spans:
            if s is not root:
                self.assertEqual(s.parent.span_id, root.context.span_id, s.name)
            self.assertEqual(s.attributes.get("langfuse.session.id"), "gh-1-1", s.name)
            for v in s.attributes.values():
                self.assertNotIn("canary", str(v))
        a = root.attributes
        self.assertEqual((a["coach.eval.case_id"], a["coach.eval.variant"], a["coach.eval.trial"]), ("T01", "instr", 1))
        self.assertEqual(a["langfuse.trace.name"], "eval T01 instr")
        self.assertEqual(tuple(a["langfuse.trace.tags"]), ("instr", MODEL))
        self.assertEqual(a["coach.eval.stop_reason"], "end_turn")
        self.assertAlmostEqual(a["coach.eval.cost_usd"], tr["cost_usd"], places=6)
        chats = [s for s in spans if s.name.startswith("chat")]
        c = chats[0].attributes
        self.assertEqual(c["langfuse.observation.type"], "generation")
        self.assertEqual((c["gen_ai.usage.input_tokens"], c["gen_ai.usage.output_tokens"]), (100, 50))
        self.assertEqual(c["langfuse.observation.usage_details"],
                         '{"cache_creation_input_tokens":0,"cache_read_input_tokens":0,"input":100,"output":50}')
        total = sum(s.attributes["gen_ai.usage.cost"] for s in chats)
        self.assertAlmostEqual(total, tr["cost_usd"], places=6)
        tool = next(s for s in spans if s.name.startswith("execute_tool"))
        self.assertEqual(tool.attributes["langfuse.observation.type"], "tool")
        self.assertEqual(tool.attributes["gen_ai.tool.name"], "get_daily_status")

    def test_tool_error_marks_the_span(self):
        client = tracing.wrap_client(tr_stubs.FakeClient(tr_stubs.tool_use(("get_stats", {})), tr_stubs.answer("x")))
        runner.run_trial(client, tr_stubs.Case(), "bare", 1, MODEL, harness=tr_stubs.stub_harness(errors={"get_stats"}))
        from opentelemetry.trace import StatusCode

        tool = next(s for s in self.spans() if s.name.startswith("execute_tool"))
        self.assertEqual(tool.status.status_code, StatusCode.ERROR)

    def test_unknown_model_price_still_exports_tokens(self):
        client = tracing.wrap_client(tr_stubs.FakeClient(tr_stubs.answer("x", model="claude-unpriced-9")))
        client.messages.create(model="claude-unpriced-9", messages=[])
        chat = next(s for s in self.spans() if s.name.startswith("chat"))
        self.assertEqual(chat.attributes["gen_ai.usage.input_tokens"], 100)
        self.assertNotIn("gen_ai.usage.cost", chat.attributes)


@unittest.skipUnless(HAVE_SDK, SDK_REASON)
class LangfuseWire(unittest.TestCase):
    """The real OTLP/HTTP exporter to a local stand-in for Langfuse: path, auth headers, and no text."""

    def setUp(self):
        sys.path.insert(0, os.path.join(ROOT, "ext"))
        import test_telemetry

        reset()
        self.rx = test_telemetry._Capture()

    def tearDown(self):
        reset()
        self.rx.close()

    def test_bytes_and_headers(self):
        env = {"LANGFUSE_PUBLIC_KEY": PK, "LANGFUSE_SECRET_KEY": SK, "LANGFUSE_BASE_URL": self.rx.url}
        self.assertTrue(tracing.setup(env))
        self.assertNotIn("LANGFUSE_SECRET_KEY", env)
        tracing.set_run("r-wire")
        client = tracing.wrap_client(tr_stubs.FakeClient(tr_stubs.tool_use(("get_daily_status", {"q": PROMPT_CANARY})),
                                                         tr_stubs.answer(ANSWER_CANARY)))
        runner.run_trial(client, tr_stubs.Case(prompt=PROMPT_CANARY), "coach", 1, MODEL,
                         harness=tr_stubs.stub_harness(), coach_prompt="coach prompt canary text")
        tracing.shutdown()
        self.assertTrue(self.rx.requests)
        raw = self.rx.body()
        for text in (PROMPT_CANARY, ANSWER_CANARY, "coach prompt canary", "SERVER INSTRUCTIONS", SK, PK, "level"):
            self.assertNotIn(text.encode(), raw, text)
        self.assertIn(b"eval.trial", raw)
        self.assertIn(b"execute_tool get_daily_status", raw)
        for path, headers, _ in self.rx.requests:
            self.assertEqual(path, "/api/public/otel/v1/traces")
            h = {k.lower(): v for k, v in headers.items()}
            self.assertEqual(h["x-langfuse-ingestion-version"], "4")
            self.assertEqual(base64.b64decode(h["authorization"].split(" ", 1)[1]).decode(), f"{PK}:{SK}")


    def test_langfuse_gets_only_its_own_headers_when_grafana_headers_are_set(self):
        import test_telemetry

        grafana = test_telemetry._Capture()
        self.addCleanup(grafana.close)
        grafana_env = {"TRACING": "on", "OTEL_EXPORTER_OTLP_ENDPOINT": grafana.url,
                       "OTEL_EXPORTER_OTLP_HEADERS": "Authorization=Basic%20grafanafake1,x-scope-orgid=grafana-org",
                       "OTEL_EXPORTER_OTLP_TRACES_HEADERS": "x-grafana-extra=tracesonly1"}
        env = {"LANGFUSE_PUBLIC_KEY": PK, "LANGFUSE_SECRET_KEY": SK, "LANGFUSE_BASE_URL": self.rx.url,
               **grafana_env}
        # The OTLP exporter reads os.environ itself, so the Grafana headers are set there too.
        with mock.patch.dict(os.environ, grafana_env):
            self.assertTrue(tracing.setup(env))
            for name in ("OTEL_EXPORTER_OTLP_HEADERS", "OTEL_EXPORTER_OTLP_TRACES_HEADERS"):
                self.assertNotIn(name, os.environ)
                self.assertNotIn(name, env)
        tracing.set_run("r-headers")
        client = tracing.wrap_client(tr_stubs.FakeClient(tr_stubs.answer(ANSWER_CANARY)))
        runner.run_trial(client, tr_stubs.Case(prompt=PROMPT_CANARY), "coach", 1, MODEL,
                         harness=tr_stubs.stub_harness(), coach_prompt="coach prompt canary text")
        tracing.shutdown()
        self.assertTrue(self.rx.requests)
        for _path, headers, _ in self.rx.requests:
            h = {k.lower(): v for k, v in headers.items()}
            self.assertNotIn("x-scope-orgid", h)
            self.assertNotIn("x-grafana-extra", h)
            self.assertEqual(base64.b64decode(h["authorization"].split(" ", 1)[1]).decode(), f"{PK}:{SK}")
        self.assertTrue(grafana.requests)
        for _path, headers, _ in grafana.requests:
            h = {k.lower(): v for k, v in headers.items()}
            self.assertEqual(h["authorization"], "Basic grafanafake1")
            self.assertEqual(h["x-scope-orgid"], "grafana-org")
            self.assertNotIn("x-grafana-extra", h)


@unittest.skipUnless(HAVE_SERVER, "needs opentelemetry-sdk and upstream garmin_mcp on mcp 2.x")
class ServerSpansNest(unittest.TestCase):
    """The real in-process server: its SDK span (tools/call) is a child of the runner's execute_tool span."""

    def setUp(self):
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

        reset()
        self.mem = InMemorySpanExporter()
        tracing.setup({}, exporters=[(self.mem, None)])

    def tearDown(self):
        reset()

    def test_nesting(self):
        from evals.cases import load_cases

        case = next(c for c in load_cases(os.path.join(ROOT, "evals", "cases")) if c.id == "T01")
        client = tracing.wrap_client(tr_stubs.FakeClient(
            tr_stubs.tool_use(("get_daily_status", {}), model=MODEL), tr_stubs.answer("Green.", model=MODEL)))
        tr = runner.run_trial(client, case, "bare", 1, MODEL)
        self.assertIsNone(tr["error"], tr["error"])
        telemetry()._S.tracer_provider.force_flush(5000)
        spans = list(self.mem.get_finished_spans())
        tool = next(s for s in spans if s.name == "execute_tool get_daily_status")
        server = next(s for s in spans if s.name == "tools/call get_daily_status")
        self.assertEqual(server.context.trace_id, tool.context.trace_id)
        chain, cur = [], server
        by_id = {s.context.span_id: s for s in spans}
        while cur.parent is not None and cur.parent.span_id in by_id:
            cur = by_id[cur.parent.span_id]
            chain.append(cur.name)
        self.assertIn("execute_tool get_daily_status", chain)
        self.assertEqual(chain[-1], "eval.trial")


if __name__ == "__main__":
    unittest.main()
