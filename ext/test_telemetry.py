"""Tests for telemetry: off by default, the allowlist, the export filter, the wire-bytes canary, a dead backend.

    cd ext && python -m unittest test_telemetry -v

The value checks, header handling, log collapse and the off path need only the standard library and run on
the stdlib leg. The SDK tests need opentelemetry-sdk and the OTLP/HTTP exporter
(config/requirements-telemetry.txt) and are skipped without them; the full leg installs them.
"""

from __future__ import annotations

import base64
import http.server
import importlib.util
import logging
import os
import socket
import statistics
import subprocess
import sys
import threading
import time
import types
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import telemetry as t
import tool_errors

HAVE_SDK = (importlib.util.find_spec("opentelemetry") is not None
            and importlib.util.find_spec("opentelemetry.sdk") is not None
            and importlib.util.find_spec("opentelemetry.exporter.otlp.proto.http") is not None)
HAVE_MCP = importlib.util.find_spec("mcp") is not None and importlib.util.find_spec("mcp.server.mcpserver") is not None
SDK_REASON = "needs opentelemetry-sdk and the OTLP/HTTP exporter"

# Things that must never leave the process. Each is unique enough that a hit is a real leak.
CANARIES = {
    "display": "canary.display.name",
    "email": "canary.owner@example.com",
    "bearer": "Bearer cnryBEARERtoken0123456789",
    # Built from parts, so the secret scan (gitleaks generic-api-key) does not read the fake values as keys.
    "garmin_key": "CNRY" + "GARMIN" + "KEY" + "0123456789abcdef",
    "owner_secret": "cnry-owner-" + "secret-" + "0123456789",
    "hrv": "HRV 4217",
    "date": "1977-07-07",
    "note": "canary planted note text",
    "otlp_token": "glc_cnryOTLPtoken0123456789",
    "request_id": "canary-request-id-with spaces",
    "resource": "canaryresourcevalue",
    "baggage": "canarybaggagevalue",
    "tool": "get_canary_tool_name",
}


def cfg(**kw):
    base = {"tracing": False, "history": False, "role": "app", "otlp_endpoint": None}
    base.update(kw)
    return types.SimpleNamespace(**base)


class Strict:
    def __enter__(self):
        self.saved = t.STRICT
        t.STRICT = True

    def __exit__(self, *exc):
        t.STRICT = self.saved


# --- standard library only ------------------------------------------------------------------------------

class OffPath(unittest.TestCase):
    def test_off_imports_no_opentelemetry_and_starts_no_thread(self):
        code = ("import sys, threading, types; sys.path.insert(0, %r); import telemetry as t; "
                "n = threading.active_count(); "
                "t.setup(types.SimpleNamespace(tracing=False, otlp_endpoint='https://x.example', role='app')); "
                "print(sorted(m for m in sys.modules if m.startswith('opentelemetry')), threading.active_count() - n)"
                % HERE)
        env = {k: v for k, v in os.environ.items() if not k.startswith("OTEL_")}
        out = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, env=env, check=True)
        self.assertEqual(out.stdout.strip(), "[] 0")

    def test_helpers_are_noops_when_off(self):
        self.assertFalse(t.enabled())
        with t.span("tool get_daily_status", tool="get_daily_status") as s:
            s.set(outcome="ok")
            s.error("internal")
        self.assertIsNone(t.inject())
        self.assertIsNone(t.extract_link("00-" + "a" * 32 + "-" + "b" * 16 + "-01"))
        t.metric_count("coach.tool.calls", tool="x")
        t.metric_observe("coach.tool.duration", 0.1)
        t.metric_gauge("coach.history.lag_days", 2)

        def f():
            return 1
        self.assertIs(t.bind_context(f), f)
        t.shutdown()

    def test_exception_inside_noop_span_passes_through(self):
        with self.assertRaises(KeyError):
            with t.span("tool x"):
                raise KeyError("x")

    def test_sdk_disabled_wins(self):
        env = {"OTEL_SDK_DISABLED": "true", "OTEL_EXPORTER_OTLP_HEADERS": "Authorization=Basic%20abc"}
        t.setup(cfg(tracing=True, otlp_endpoint="https://otlp.example"), env)
        self.assertFalse(t.enabled())

    def test_refused_env_keeps_it_off_and_leaves_no_header(self):
        env = {"OTEL_RESOURCE_ATTRIBUTES": "a=b", "OTEL_EXPORTER_OTLP_HEADERS": "Authorization=Basic%20abc"}
        with self.assertLogs("garmin_ext.telemetry", "WARNING") as cm:
            t.setup(cfg(tracing=True, otlp_endpoint="https://otlp.example"), env)
        self.assertFalse(t.enabled())
        self.assertIn("refused", cm.output[0])

    def test_refused_env_names(self):
        env = {"OTEL_PYTHON_ID_GENERATOR": "x", "OTEL_SERVICE_NAME": "y", "OTEL_METRICS_EXEMPLAR_FILTER": "z",
               "OTEL_TRACES_EXPORTER": "console", "OTEL_EXPORTER_OTLP_ENDPOINT": "https://ok.example", "PATH": "/"}
        self.assertEqual(t.refused_env(env), ["OTEL_METRICS_EXEMPLAR_FILTER", "OTEL_PYTHON_ID_GENERATOR",
                                              "OTEL_SERVICE_NAME", "OTEL_TRACES_EXPORTER"])

    def test_every_other_otlp_variable_is_refused(self):
        # The OTLP exporter reads these itself (per-signal headers are merged past take_headers).
        env = {"OTEL_EXPORTER_OTLP_ENDPOINT": "https://ok.example", "OTEL_EXPORTER_OTLP_HEADERS": "a=1",
               "OTEL_EXPORTER_OTLP_TRACES_HEADERS": "x=1", "OTEL_EXPORTER_OTLP_METRICS_HEADERS": "x=1",
               "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "https://elsewhere.example", "OTEL_EXPORTER_OTLP_TIMEOUT": "1"}
        self.assertEqual(t.refused_env(env), ["OTEL_EXPORTER_OTLP_METRICS_HEADERS",
                                              "OTEL_EXPORTER_OTLP_TIMEOUT", "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
                                              "OTEL_EXPORTER_OTLP_TRACES_HEADERS"])

    def test_per_signal_headers_keep_it_off_and_leave_the_environment(self):
        env = {"OTEL_EXPORTER_OTLP_HEADERS": "Authorization=Basic%20abcdefgh",
               "OTEL_EXPORTER_OTLP_TRACES_HEADERS": "x-extra=tracesecret2"}
        with self.assertLogs("garmin_ext.telemetry", "WARNING") as cm:
            t.setup(cfg(tracing=True, otlp_endpoint="https://otlp.example"), env)
        self.assertFalse(t.enabled())
        self.assertIn("refused", cm.output[0])
        self.assertEqual(env, {})


class Allowlist(unittest.TestCase):
    CONTRACT_KEYS = ("coach.caller", "coach.tool", "coach.error_group", "coach.mode", "coach.outcome",
                     "coach.answer_bytes_bucket", "coach.range_days", "coach.job.kind", "coach.job.status",
                     "coach.job.attempt", "coach.queue.wait_s", "coach.history.result_count",
                     "coach.history.channels", "coach.history.lag_days", "coach.remote_parent")

    def setUp(self):
        t.register_tools(["get_daily_status", "find_similar_weeks"])

    def test_contract_keys_are_allowed(self):
        for k in self.CONTRACT_KEYS:
            self.assertIn(k, t.ALLOWED_ATTRS)

    def test_every_alias_names_an_allowed_key(self):
        for full in t.ALIASES.values():
            self.assertIn(full, t.ALLOWED_ATTRS)

    def test_every_metric_key_has_a_check(self):
        for name, spec in t.METRICS.items():
            for k in spec[3]:
                self.assertTrue(k in t.ALLOWED_ATTRS or k in t._METRIC_ONLY, (name, k))

    def test_unknown_keys_and_bad_values_dropped(self):
        got = t.clean_attrs({"coach.caller": "tool", "coach.caller.x": "tool", "user.name": "bob",
                             "coach.range_days": 7, "coach.job.attempt": "3", "coach.remote_parent": 1,
                             "coach.history.channels": "vector,keyword", "coach.mode": "FULL",
                             "coach.queue.wait_s": float("nan"), "coach.history.lag_days": -1})
        self.assertEqual(got, {"coach.caller": "tool", "coach.range_days": 7,
                               "coach.history.channels": "vector,keyword"})

    def test_strict_raises_without_showing_the_value(self):
        with Strict(), self.assertRaises(ValueError) as cm:
            t.clean_attrs({"coach.tool.note": CANARIES["note"]})
        self.assertNotIn(CANARIES["note"], str(cm.exception))
        with Strict(), self.assertRaises(ValueError) as cm:
            t.clean_attrs({"coach.range_days": CANARIES["date"]})
        self.assertNotIn(CANARIES["date"], str(cm.exception))

    def test_request_id_allowlist(self):
        ok = t.clean_attrs({"jsonrpc.request.id": "42"})
        self.assertEqual(ok, {"jsonrpc.request.id": "42"})
        self.assertEqual(t.clean_attrs({"jsonrpc.request.id": "6f1c-9a"}), {"jsonrpc.request.id": "6f1c-9a"})
        for bad in (CANARIES["request_id"], "x" * 65, "a/b", "", "id:1"):
            self.assertEqual(t.clean_attrs({"jsonrpc.request.id": bad}), {}, bad)

    def test_tool_names_and_error_types(self):
        self.assertEqual(t.clean_attrs({"gen_ai.tool.name": CANARIES["tool"]}), {"gen_ai.tool.name": "other"})
        self.assertEqual(t.clean_attrs({"coach.tool": "find_similar_weeks"}), {"coach.tool": "find_similar_weeks"})
        self.assertEqual(t.clean_attrs({"error.type": "ValueError"}), {"error.type": "exception"})
        self.assertEqual(t.clean_attrs({"error.type": "-32602"}), {"error.type": "-32602"})
        self.assertEqual(t.clean_attrs({"error.type": "rate_limited"}), {"error.type": "rate_limited"})

    def test_json_usage_details(self):
        check = t.ALLOWED_ATTRS["langfuse.observation.usage_details"]
        self.assertEqual(check('{"output": 5, "input": 10}'), '{"input":10,"output":5}')
        for bad in ('{"input": "HRV 4217"}', '{"note": 1}', "[1]", "nope", '{"input": -1}', "{}"):
            self.assertIs(check(bad), t._DROP, bad)

    def test_span_names(self):
        cases = {
            "tools/call get_daily_status": "tools/call get_daily_status",
            "tools/call " + CANARIES["tool"]: "tools/call other",
            "tools/list": "tools/list",
            "prompts/get " + CANARIES["note"]: "prompts/get",
            "MCP send sampling/createMessage": "MCP send sampling/createMessage",
            "MCP send " + CANARIES["note"]: "MCP send other",
            "tool get_daily_status": "tool get_daily_status",
            "tool " + CANARIES["tool"]: "tool other",
            "source sleep": "source sleep",
            "source " + CANARIES["note"]: "source other",
            "garmin GET wellness-service": "garmin GET wellness-service",
            "garmin GET " + CANARIES["display"]: "garmin GET other",
            "garmin GET jane-runner-smith": "garmin GET other",       # a display-name-shaped segment
            "garmin GET latest": "garmin GET other",
            "job index_day": "job index_day",
            "job delete_everything": "job other",
            "history.index": "history.index",
            "chat claude-sonnet-5-5": "chat claude-sonnet-5-5",
            "chat " + CANARIES["email"]: "chat other",
            CANARIES["hrv"]: "span",
            "": "span",
            None: "span",
        }
        for raw, want in cases.items():
            self.assertEqual(t.clean_span_name(raw), want, raw)

    def test_bytes_bucket(self):
        self.assertEqual([t.bytes_bucket(n) for n in (0, 1023, 1024, 5000, 20000, 70000, 10**6, -1, True)],
                         ["lt_1k", "lt_1k", "1k_4k", "4k_16k", "16k_64k", "64k_256k", "ge_256k", "lt_1k", "lt_1k"])
        for b in ("lt_1k", "ge_256k"):
            self.assertEqual(t.clean_attrs({"coach.answer_bytes_bucket": b}), {"coach.answer_bytes_bucket": b})

    def test_traceparent_check(self):
        good = "00-" + "a" * 32 + "-" + "b" * 16 + "-01"
        self.assertTrue(t.valid_traceparent(good))
        self.assertTrue(t.valid_traceparent(good[:-2] + "03"))
        for bad in ("00-" + "0" * 32 + "-" + "b" * 16 + "-01", "01-" + good[3:], good + "x", good.upper(), None, 5):
            self.assertFalse(t.valid_traceparent(bad), bad)


class EndpointFamily(unittest.TestCase):
    def test_only_known_families_pass(self):
        check = t.ALLOWED_ATTRS["garmin.endpoint_family"]
        self.assertEqual(check("wellness-service"), "wellness-service")
        self.assertEqual(check("other"), "other")
        for bad in ("jane-runner-smith", "latest", "Wellness-service", "", None):
            self.assertIs(check(bad), t._DROP, bad)

    def test_list_is_what_garminconnect_calls(self):
        # Every family is a fixed Garmin service segment: lower case, ends in -service or -gateway (one
        # golf community path), never a name a user could choose.
        for name in t.GARMIN_ENDPOINT_FAMILIES:
            self.assertRegex(name, r"^[a-z]+(-service|-gateway)$|^gcs-golfcommunity$")


class Headers(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(t.parse_headers("Authorization=Basic%20abc,x-langfuse-ingestion-version=4"),
                         {"Authorization": "Basic abc", "x-langfuse-ingestion-version": "4"})
        for bad in (None, "", "novalue", "Author ization=x", "a="):
            self.assertEqual(t.parse_headers(bad), {}, bad)

    def test_every_header_the_check_passes_is_kept(self):
        """The header name "X_Scope_OrgID" passed feature_config but this used to return {} (every header dropped)."""
        import feature_config

        raw = "Authorization=Basic%20abc,X_Scope_OrgID=tenant1"
        self.assertEqual(feature_config.problems({"TRACING": "on", "OTEL_EXPORTER_OTLP_ENDPOINT": "https://o.example",
                                                  "OTEL_EXPORTER_OTLP_HEADERS": raw}), [])
        self.assertEqual(t.parse_headers(raw), {"Authorization": "Basic abc", "X_Scope_OrgID": "tenant1"})
        self.assertEqual(t.take_headers({}, raw=raw), {"Authorization": "Basic abc", "X_Scope_OrgID": "tenant1"})

    def test_take_removes_and_registers_every_part(self):
        decoded = "123456:" + CANARIES["otlp_token"]
        b64 = base64.b64encode(decoded.encode()).decode()
        env = {"OTEL_EXPORTER_OTLP_HEADERS": "Authorization=Basic%20" + b64, "OTHER": "1"}
        headers = t.take_headers(env)
        self.assertEqual(headers, {"Authorization": "Basic " + b64})
        self.assertNotIn("OTEL_EXPORTER_OTLP_HEADERS", env)
        for secret in ("Basic " + b64, b64, decoded, CANARIES["otlp_token"]):
            self.assertNotIn(secret, tool_errors.redact(f"failed with {secret} here"))

    def test_take_removes_and_registers_the_per_signal_headers(self):
        env = {"OTEL_EXPORTER_OTLP_HEADERS": "Authorization=Basic%20mainvalue1",
               "OTEL_EXPORTER_OTLP_TRACES_HEADERS": "x-scope=tracesecret1",
               "OTEL_EXPORTER_OTLP_METRICS_HEADERS": "x-scope=metricsecret1", "OTHER": "1"}
        self.assertEqual(t.take_headers(env), {"Authorization": "Basic mainvalue1"})
        self.assertEqual(env, {"OTHER": "1"})
        for secret in ("tracesecret1", "metricsecret1", "x-scope=tracesecret1"):
            self.assertNotIn(secret, tool_errors.redact(f"failed with {secret} here"))

    def test_take_prefers_the_config_value(self):
        env = {"OTEL_EXPORTER_OTLP_HEADERS": "a=1"}
        self.assertEqual(t.take_headers(env, "Authorization=Basic%20xyz"), {"Authorization": "Basic xyz"})
        self.assertEqual(env, {})

    def test_endpoint(self):
        for ok in ("https://otlp-gateway-prod-eu-west-2.grafana.net/otlp", "https://h.example:4318",
                   "http://127.0.0.1:4318", "http://localhost:9"):
            self.assertTrue(t.endpoint_ok(ok), ok)
        for bad in ("http://otlp.example", "https://u:p@h.example", "https://h.example/?k=v", "ftp://h", "", None,
                    "https://h.example/#x"):
            self.assertFalse(t.endpoint_ok(bad), bad)
        self.assertEqual(t.signal_url("https://h.example/otlp/", "traces"), "https://h.example/otlp/v1/traces")


class LogCollapse(unittest.TestCase):
    def record(self, msg, *args, level=logging.ERROR):
        return logging.LogRecord("opentelemetry.exporter.otlp.common.http", level, __file__, 1, msg, args, None)

    def test_one_line_per_kind_per_window_and_no_detail(self):
        now = [0.0]
        out = logging.getLogger("test_telemetry.collapse")
        f = t.CollapseExportErrors(every=600, clock=lambda: now[0], out=out)
        fail = "Failed to export %s batch code: %s, reason: %s"
        with self.assertLogs(out, "WARNING") as cm:
            for _ in range(5):
                self.assertFalse(f.filter(self.record(fail, "traces", 401, CANARIES["bearer"])))
            self.assertFalse(f.filter(self.record("Transient error %s encountered", CANARIES["display"],
                                                  level=logging.WARNING)))
            self.assertFalse(f.filter(self.record(fail, "traces", None, "ConnectionError(" + CANARIES["email"])))
            self.assertFalse(f.filter(self.record("Failed to export %s batch due to timeout, max retries or shutdown.",
                                                  "traces")))
            now[0] = 601
            f.filter(self.record(fail, "traces", 401, "x"))
        self.assertEqual(cm.output, [
            "WARNING:test_telemetry.collapse:telemetry: export failed: http_401",
            "WARNING:test_telemetry.collapse:telemetry: export failed: connection",
            "WARNING:test_telemetry.collapse:telemetry: export failed: timeout",
            "WARNING:test_telemetry.collapse:telemetry: export failed: http_401",
        ])
        self.assertEqual(f.counts["http_401"], 6)


# --- with the SDK ---------------------------------------------------------------------------------------

class _Capture:
    """A stdlib OTLP receiver on 127.0.0.1: keeps every request's path, headers and raw body."""

    def __init__(self, status=200, hang=False):
        self.requests = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                outer.requests.append((self.path, dict(self.headers), body))
                if hang:
                    time.sleep(30)
                self.send_response(status)
                self.send_header("Content-Type", "application/x-protobuf")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *a):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def body(self) -> bytes:
        return b"".join(b for _, _, b in self.requests)


class _MemMetrics:
    """A MetricExporter-like sink built lazily (needs the SDK)."""

    @staticmethod
    def make():
        from opentelemetry.sdk.metrics.export import MetricExporter, MetricExportResult

        class Mem(MetricExporter):
            def __init__(self):
                super().__init__()
                self.batches = []

            def export(self, metrics_data, timeout_millis=10000, **kw):
                self.batches.append(metrics_data)
                return MetricExportResult.SUCCESS

            def force_flush(self, timeout_millis=10000):
                return True

            def shutdown(self, timeout_millis=30000, **kw):
                return None
        return Mem()


@unittest.skipUnless(HAVE_SDK, SDK_REASON)
class SdkBase(unittest.TestCase):
    def setUp(self):
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

        t._reset_for_tests()
        t.register_tools(["get_daily_status", "get_weekly_status"])
        self.mem = InMemorySpanExporter()
        self.metrics = _MemMetrics.make()
        t.start([self.mem], [self.metrics], schedule_delay_ms=20, metric_interval_ms=3_600_000)
        self._mp = t._S.meter_provider
        self._tp = t._S.tracer_provider

    def tearDown(self):
        t._reset_for_tests()

    def flush(self):
        self._tp.force_flush(5000)
        self._mp.force_flush(5000)
        return list(self.mem.get_finished_spans())

    def by_name(self):
        return {s.name: s for s in self.flush()}


class SpansAndFilter(SdkBase):
    def test_our_span_has_only_allowed_attributes(self):
        with t.span("tool get_daily_status", {"coach.range_days": 7, "user.email": CANARIES["email"]},
                    tool="get_daily_status", caller="tool", bogus=CANARIES["hrv"]) as s:
            s.set(outcome="ok", answer_bytes_bucket=t.bytes_bucket(3000))
        got = self.by_name()["tool get_daily_status"]
        self.assertEqual(dict(got.attributes), {"coach.range_days": 7, "coach.tool": "get_daily_status",
                                                "coach.caller": "tool", "coach.outcome": "ok",
                                                "coach.answer_bytes_bucket": "1k_4k"})

    def test_strict_span_raises_on_unknown_key(self):
        with Strict(), self.assertRaises(ValueError):
            with t.span("tool x", {"user.name": "x"}):
                pass

    def test_sdk_style_span_is_rebuilt(self):
        from opentelemetry import trace
        from opentelemetry.trace import StatusCode

        tracer = trace.get_tracer("mcp-python-sdk")
        with tracer.start_as_current_span("tools/call " + CANARIES["tool"], kind=trace.SpanKind.SERVER,
                                          attributes={"jsonrpc.request.id": CANARIES["request_id"],
                                                      "gen_ai.tool.name": CANARIES["tool"],
                                                      "mcp.method.name": "tools/call",
                                                      "url.full": "https://x/" + CANARIES["display"]}) as sp:
            sp.add_event("note", {"text": CANARIES["note"]})
            sp.record_exception(ValueError(CANARIES["hrv"]))
            sp.set_status(StatusCode.ERROR, CANARIES["hrv"])
            sp.set_attribute("error.type", "ValueError")
        got = self.by_name()["tools/call other"]
        self.assertEqual(dict(got.attributes), {"gen_ai.tool.name": "other", "mcp.method.name": "tools/call",
                                                "error.type": "exception"})
        self.assertEqual(got.events, ())
        self.assertEqual(got.status.status_code, StatusCode.ERROR)
        self.assertIsNone(got.status.description)
        self.assertEqual(got.instrumentation_scope.name, "mcp-python-sdk")

    def test_exception_in_span_records_no_text(self):
        with self.assertRaises(RuntimeError):
            with t.span("tool get_daily_status"):
                raise RuntimeError(CANARIES["hrv"])
        got = self.by_name()["tool get_daily_status"]
        self.assertEqual(dict(got.attributes), {"error.type": "exception"})
        self.assertEqual(got.events, ())
        self.assertIsNone(got.status.description)

    def test_error_group(self):
        with t.span("tool get_daily_status") as s:
            s.error("rate_limited")
        got = self.by_name()["tool get_daily_status"]
        self.assertEqual(got.attributes["coach.error_group"], "rate_limited")

    def test_inbound_context_and_baggage_ignored(self):
        from opentelemetry import baggage
        from opentelemetry.propagate import extract

        foreign = "00-" + "c" * 32 + "-" + "d" * 16 + "-00"
        ctx = extract({"traceparent": foreign, "baggage": "k=" + CANARIES["baggage"]})
        self.assertIsNone(baggage.get_baggage("k", ctx))
        from opentelemetry import trace
        with trace.get_tracer("mcp-python-sdk").start_as_current_span("tools/call get_daily_status", context=ctx,
                                                                      kind=trace.SpanKind.SERVER):
            with t.span("tool get_daily_status"):
                pass
        spans = self.by_name()
        server, tool = spans["tools/call get_daily_status"], spans["tool get_daily_status"]
        self.assertNotEqual(server.context.trace_id, int("c" * 32, 16))
        self.assertIsNone(server.parent)
        self.assertTrue(server.attributes.get("coach.remote_parent"))
        self.assertNotIn("coach.remote_parent", tool.attributes)
        self.assertEqual(tool.parent.span_id, server.context.span_id)

    def test_sampled_flag_zero_is_still_recorded(self):
        with t.span("job index_day", parent="00-" + "e" * 32 + "-" + "f" * 16 + "-00"):
            pass
        self.assertIn("job index_day", self.by_name())

    def test_inject_then_child_and_link(self):
        with t.span("enqueue.daily", caller="enqueue"):
            tp = t.inject()
        self.assertTrue(t.valid_traceparent(tp))
        with t.span("job index_day", parent=tp, job_kind="index_day"):
            pass
        with t.span("job sweep", links=[t.extract_link(tp), t.extract_link("garbage")], job_kind="sweep"):
            pass
        spans = self.by_name()
        enq = spans["enqueue.daily"]
        self.assertEqual(spans["job index_day"].parent.span_id, enq.context.span_id)
        self.assertEqual(spans["job index_day"].context.trace_id, enq.context.trace_id)
        self.assertIsNone(spans["job sweep"].parent)
        self.assertEqual([lk.context.span_id for lk in spans["job sweep"].links], [enq.context.span_id])
        self.assertIsNone(t.extract_link("00-bad"))

    def test_bind_context_carries_parent_into_a_thread(self):
        with t.span("tool get_daily_status"):
            def work():
                with t.span("source sleep", source="sleep"):
                    pass
            th = threading.Thread(target=t.bind_context(work))
            th.start()
            th.join()
        spans = self.by_name()
        self.assertEqual(spans["source sleep"].parent.span_id, spans["tool get_daily_status"].context.span_id)

    def test_resource_is_built_here(self):
        with mock.patch.dict(os.environ, {"OTEL_RESOURCE_ATTRIBUTES": "x=" + CANARIES["resource"]}):
            t._reset_for_tests()
            from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

            self.mem = InMemorySpanExporter()
            t.start([self.mem], [], schedule_delay_ms=20)
            self._tp = t._S.tracer_provider
            self._mp = t._S.meter_provider
            with t.span("history.index"):
                pass
        res = self.by_name()["history.index"].resource.attributes
        self.assertEqual(set(res) - set(t.RESOURCE_ATTRS), set())
        self.assertEqual(res["service.name"], t.SERVICE_NAME)


class Metrics(SdkBase):
    def points(self):
        self.flush()
        out = {}
        for batch in self.metrics.batches:
            for rm in batch.resource_metrics:
                for sm in rm.scope_metrics:
                    for m in sm.metrics:
                        out.setdefault(m.name, []).extend(m.data.data_points)
        return out

    def test_exemplars_off_and_attributes_filtered(self):
        with t.span("tool get_daily_status"):
            t.metric_observe("coach.tool.duration", 0.3, tool="get_daily_status")
            t.metric_count("coach.tool.calls", attrs={"user.email": CANARIES["email"]}, tool="get_daily_status",
                           outcome="ok")
            t.metric_gauge("coach.history.lag_days", 2)
            t.metric_count("coach.secret.metric", 1)
        pts = self.points()
        self.assertEqual(set(pts), {"coach.tool.duration", "coach.tool.calls", "coach.history.lag_days"})
        for name, dps in pts.items():
            for dp in dps:
                self.assertEqual(list(dp.exemplars), [], name)
        self.assertEqual(dict(pts["coach.tool.calls"][0].attributes),
                         {"coach.tool": "get_daily_status", "coach.outcome": "ok"})
        self.assertEqual(list(pts["coach.tool.duration"][0].explicit_bounds), list(t.DURATION_BUCKETS))

    def test_filter_drops_point_with_foreign_keys(self):
        # Bypass the helper: write to the instrument directly, as a bug elsewhere might.
        t._S.instruments["coach.tool.calls"].add(1, {"coach.tool": "get_daily_status", "note": CANARIES["note"]})
        t.metric_count("coach.tool.calls", tool="get_weekly_status", outcome="ok")
        pts = self.points()["coach.tool.calls"]
        self.assertEqual([dict(p.attributes) for p in pts],
                         [{"coach.tool": "get_weekly_status", "coach.outcome": "ok"}])

    def test_strict_unknown_metric_raises(self):
        with Strict(), self.assertRaises(ValueError):
            t.metric_count("coach.not.a.metric")


class _grab_logs(logging.Handler):
    """Every line telemetry or OpenTelemetry writes that reaches the root logger (the process's output) while
    active. The mcp SDK's own log lines are out of scope here."""

    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines = []

    def emit(self, record):
        if record.name.startswith(("garmin_ext", "opentelemetry")):
            self.lines.append(record.getMessage())

    def __enter__(self):
        logging.getLogger().addHandler(self)
        return self.lines

    def __exit__(self, *exc):
        logging.getLogger().removeHandler(self)


@unittest.skipUnless(HAVE_SDK, SDK_REASON)
class WireCanary(unittest.TestCase):
    """setup() with a real OTLP/HTTP exporter to a local receiver; canaries planted everywhere a value could
    enter; the exact bytes sent must hold none of them, and every exported key must be allowlisted."""

    def setUp(self):
        t._reset_for_tests()
        self.rx = _Capture()

    def tearDown(self):
        t._reset_for_tests()
        self.rx.close()

    def test_no_canary_on_the_wire(self):
        from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceRequest
        from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

        token = base64.b64encode(("123456:" + CANARIES["otlp_token"]).encode()).decode()
        env = {"OTEL_EXPORTER_OTLP_HEADERS": "Authorization=Basic%20" + token, "GARMIN_AUTH": "key",
               "PATH": os.environ.get("PATH", "")}
        t.register_tools(["get_daily_status"])
        with mock.patch.dict(os.environ, env, clear=False), _grab_logs() as logs:
            t.setup(cfg(tracing=True, otlp_endpoint=self.rx.url + "/otlp"), os.environ)
            self.assertTrue(t.enabled())
            self.assertNotIn("OTEL_EXPORTER_OTLP_HEADERS", os.environ)
            self._plant()
            if HAVE_MCP:
                self._real_mcp_calls()
            t._S.meter_provider.force_flush(5000)
            t.shutdown(cap_s=5)
        raw = self.rx.body()
        self.assertTrue(raw)
        for name, value in CANARIES.items():
            self.assertNotIn(value.encode(), raw, name)
            self.assertNotIn(base64.b64encode(value.encode()), raw, name)
        for line in logs:
            for value in CANARIES.values():
                self.assertNotIn(value, line)
        self.assertIn(b"tool get_daily_status", raw)
        paths = {p for p, _, _ in self.rx.requests}
        self.assertEqual(paths, {"/otlp/v1/traces", "/otlp/v1/metrics"})
        self.assertEqual({h.get("authorization") or h.get("Authorization") for _, h, _ in self.rx.requests},
                         {"Basic " + token})

        keys, res_keys, names = set(), set(), set()
        for path, _, body in self.rx.requests:
            if path.endswith("/traces"):
                req = ExportTraceServiceRequest.FromString(body)
                for rs in req.resource_spans:
                    res_keys |= {a.key for a in rs.resource.attributes}
                    for ss in rs.scope_spans:
                        self.assertIn(ss.scope.name, t.SCOPES + ("other",))
                        for sp in ss.spans:
                            names.add(sp.name)
                            keys |= {a.key for a in sp.attributes}
                            self.assertEqual(len(sp.events), 0)
                            self.assertEqual(sp.status.message, "")
                            for lk in sp.links:
                                self.assertEqual(len(lk.attributes), 0)
            else:
                req = ExportMetricsServiceRequest.FromString(body)
                for rm in req.resource_metrics:
                    res_keys |= {a.key for a in rm.resource.attributes}
                    for sm in rm.scope_metrics:
                        for m in sm.metrics:
                            self.assertIn(m.name, t.METRICS)
                            self.assertFalse(m.name.startswith("otel."))
                            for dp in list(m.sum.data_points) + list(m.histogram.data_points) + list(
                                    m.gauge.data_points):
                                keys |= {a.key for a in dp.attributes}
                                self.assertEqual(len(dp.exemplars), 0)
        self.assertLessEqual(keys, set(t.ALLOWED_ATTRS) | set(t._METRIC_ONLY))
        self.assertLessEqual(res_keys, set(t.RESOURCE_ATTRS))
        if HAVE_MCP:
            self.assertIn("tools/call other", names)
            self.assertIn("tools/call get_daily_status", names)

    def _plant(self):
        c = CANARIES
        with t.span("tool get_daily_status", {"note": c["note"], "coach.range_days": c["date"]},
                    tool="get_daily_status", caller="tool", mode="key") as s:
            s.set({"user.display": c["display"], "coach.error.ref": c["hrv"]}, outcome="ok")
            with t.span("garmin GET " + c["display"], endpoint_family=c["display"], status_code=200):
                pass
            with t.span("source " + c["note"], source=c["hrv"]):
                pass
            tp = t.inject()
            with t.span("job index_day", parent=tp, links=[t.extract_link(tp)], job_kind="index_day"):
                pass
            t.metric_count("coach.tool.calls", attrs={"user.email": c["email"]}, tool="get_daily_status", outcome="ok")
            t.metric_count("coach.tool.calls", tool=c["tool"], outcome="ok")
            t.metric_observe("coach.tool.duration", 0.25, tool="get_daily_status")
            t.metric_observe("coach.tool.answer.size", 4217, tool="get_daily_status")
            t.metric_gauge("coach.history.lag_days", 1)
        with self.assertRaises(ValueError):
            with t.span("tool get_daily_status"):
                raise ValueError(f"{c['hrv']} {c['bearer']} {c['garmin_key']} {c['owner_secret']}")
        from opentelemetry import trace
        from opentelemetry.propagate import extract
        from opentelemetry.trace import StatusCode

        ctx = extract({"traceparent": "00-" + "1" * 32 + "-" + "2" * 16 + "-01", "baggage": "b=" + c["baggage"]})
        with trace.get_tracer("mcp-python-sdk").start_as_current_span(
                "tools/call " + c["tool"], context=ctx, kind=trace.SpanKind.SERVER,
                attributes={"jsonrpc.request.id": c["request_id"], "gen_ai.tool.name": c["tool"]}) as sp:
            sp.record_exception(RuntimeError(c["note"]))
            sp.add_event(c["email"])
            sp.set_status(StatusCode.ERROR, c["display"])
        with trace.get_tracer(c["resource"]).start_as_current_span(c["date"], attributes={"x": c["hrv"]}):
            pass

    def _real_mcp_calls(self):
        """The real mcp SDK server span through the in-memory client: arguments, results and errors carry
        canaries; the SDK must not put them on its span, and the filter must catch the tool name."""
        import asyncio

        from mcp.client import Client
        from mcp.server.mcpserver import MCPServer

        c = CANARIES
        server = MCPServer("canary")

        @server.tool()
        def get_daily_status(note: str = "") -> str:
            return f"{c['hrv']} {c['display']} {note}"

        @server.tool(name=c["tool"])
        def boom(note: str = "") -> str:
            raise RuntimeError(f"{c['owner_secret']} {note}")

        async def run():
            async with Client(server, mode="auto") as client:
                await client.call_tool("get_daily_status", {"note": c["note"]})
                try:
                    await client.call_tool(c["tool"], {"note": c["date"]})
                except Exception:  # noqa: BLE001 - only the span matters here
                    pass
        asyncio.run(run())


@unittest.skipUnless(HAVE_SDK, SDK_REASON)
class DeadBackend(unittest.TestCase):
    """A closed port and a black-hole receiver: span and metric calls cost the same as with telemetry off
    (p95 within +50 ms), export errors make one log line per kind, and shutdown returns within its cap."""

    N = 300

    def setUp(self):
        t._reset_for_tests()
        t.register_tools(["get_daily_status"])

    def tearDown(self):
        t._reset_for_tests()

    def work(self):
        times = []
        for _ in range(self.N):
            t0 = time.perf_counter()
            with t.span("tool get_daily_status", tool="get_daily_status") as s:
                with t.span("source sleep", source="sleep"):
                    pass
                s.set(outcome="ok")
            t.metric_count("coach.tool.calls", tool="get_daily_status", outcome="ok")
            t.metric_observe("coach.tool.duration", 0.01, tool="get_daily_status")
            times.append(time.perf_counter() - t0)
        return statistics.quantiles(times, n=20)[18]

    def start_to(self, url):
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.metrics import NoOpMeterProvider

        noop = NoOpMeterProvider()
        t.start([OTLPSpanExporter(endpoint=url + "/v1/traces", timeout=t.EXPORT_TIMEOUT_S, meter_provider=noop)],
                [OTLPMetricExporter(endpoint=url + "/v1/metrics", timeout=t.EXPORT_TIMEOUT_S, meter_provider=noop)],
                schedule_delay_ms=10, metric_interval_ms=200)

    def check(self, url):
        base = self.work()
        lines = []
        handler = logging.Handler()
        handler.emit = lambda r: lines.append(r.getMessage())
        tlog = logging.getLogger("garmin_ext.telemetry")
        tlog.addHandler(handler)
        try:
            self.start_to(url)
            t._COLLAPSE.last.clear()
            on = self.work()
            time.sleep(1.0)  # let the export thread fail a few times
            t0 = time.monotonic()
            t.shutdown(cap_s=2.0)
            took = time.monotonic() - t0
        finally:
            tlog.removeHandler(handler)
        self.assertLess(on - base, 0.050, (base, on))
        self.assertLess(took, 2.6)
        failed = [x for x in lines if x.startswith("telemetry: export failed:")]
        self.assertEqual(len(failed), len(set(failed)), failed)  # collapsed: one per kind
        return failed

    def test_closed_port(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        failed = self.check(f"http://127.0.0.1:{port}")
        self.assertTrue(failed)

    def test_black_hole(self):
        hole = socket.socket()
        hole.bind(("127.0.0.1", 0))
        hole.listen(64)  # accepts connections, never answers
        try:
            self.check(f"http://127.0.0.1:{hole.getsockname()[1]}")
        finally:
            hole.close()

    def test_refusing_backend(self):
        rx = _Capture(status=401)
        try:
            failed = self.check(rx.url)
        finally:
            rx.close()
        self.assertIn("telemetry: export failed: http_401", failed)


@unittest.skipUnless(HAVE_SDK, SDK_REASON)
class SelfCheck(unittest.TestCase):
    def test_selfcheck_passes(self):
        self.assertEqual(t.selfcheck(), [])


ROOT = os.path.dirname(HERE)
DASHBOARD = os.path.join(ROOT, "dashboards", "grafana", "coach-mcp.json")


def prometheus_names(name, spec):
    """The series names Grafana's OTLP-to-Prometheus conversion gives a METRICS entry."""
    base = name.replace(".", "_")
    suffix = {"s": "_seconds", "By": "_bytes", "d": "_days"}.get(spec[1], "")
    if suffix and not base.endswith(suffix):
        base += suffix
    if spec[0] == "counter":
        return {base + "_total"}
    if spec[0] == "histogram":
        return {base + "_bucket", base + "_sum", base + "_count"}
    return {base}


class Dashboard(unittest.TestCase):
    """dashboards/grafana/coach-mcp.json: no stack URL, no ids, and every series it queries exists."""

    def setUp(self):
        import json

        with open(DASHBOARD, encoding="utf-8") as f:
            self.text = f.read()
        self.data = json.loads(self.text)

    def walk(self, node):
        if isinstance(node, dict):
            yield node
            for v in node.values():
                yield from self.walk(v)
        elif isinstance(node, list):
            for v in node:
                yield from self.walk(v)

    def test_no_stack_url_or_ids(self):
        import re

        self.assertNotRegex(self.text, r"(?i)grafana\.net|grafana\.com/|https?://")
        self.assertNotIn("uid", self.data)
        self.assertNotIn("id", self.data)
        for node in self.walk(self.data):
            if "uid" in node:
                self.assertRegex(node["uid"], r"^\$\{DS_[A-Z]+\}$")
        self.assertFalse(re.search(r"[^\x20-\x7e\n]", self.text))

    def test_every_queried_series_is_a_metric(self):
        import re

        known, labels = set(), set()
        for name, spec in t.METRICS.items():
            known |= prometheus_names(name, spec)
            labels |= {k.replace(".", "_") for k in spec[3]}
        used = set()
        for node in self.walk(self.data):
            expr = node.get("expr")
            if isinstance(expr, str):
                used |= set(re.findall(r"\b((?:coach|gen_ai)_[a-z_]+)\b", expr))
        self.assertTrue(used & known)
        self.assertLessEqual(used, known | labels, sorted(used - known - labels))


if __name__ == "__main__":
    unittest.main()
