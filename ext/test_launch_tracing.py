"""Tests for launch.py: the tracing hooks and the call guard inside call_tool().

    cd ext && python -m unittest test_launch_tracing

Shared helpers: test_launch_support.py.
"""

from __future__ import annotations

import asyncio
import threading
import time
import unittest

import mcp2_compat  # noqa: E402
import tool_errors  # noqa: E402
import garmin_guard  # noqa: E402
import status_common  # noqa: E402

from test_launch_support import (call_tools, captured_records, FakeTelemetry, HAVE_UPSTREAM, keep_tracing_hooks,
    load_launch, LOGIN_TEXT, quiet, RATE_TEXT, RAW_ACTIVITY, Stubs, tool_error_records, upstream_answer)


class TracingHooks(unittest.TestCase):
    """_install_tracing_hooks(): Garmin threads keep the trace context, Garmin requests get "garmin GET
    <family>" CLIENT spans and metrics, status sources get "source <name>" spans and a counter. Fake
    telemetry and fake clients; standard library only, but the upstream parts skip without upstream."""

    def setUp(self):
        self.launch = load_launch(self, Stubs().modules)
        keep_tracing_hooks(self)
        self.tel = FakeTelemetry()
        self.launch._TELEMETRY = self.tel
        self.launch._KEY_MODE = False

    def names(self, kind):
        return [call[1:] for call in self.tel.calls if call[0] == kind]

    def test_endpoint_family_is_the_first_segment_only(self):
        family = self.launch._endpoint_family
        self.assertEqual(family("/wellness-service/wellness/dailySleepData/Some.Name"), "wellness-service")
        self.assertEqual(family("usersummary-service?x=1"), "usersummary-service")
        for path in ("/12345/x", "/A-Service/x", "", None, "/ab/x", "/" + "a" * 50):
            self.assertEqual(family(path), "other", path)

    def test_endpoint_family_is_a_known_service_or_other(self):
        family = self.launch._endpoint_family
        # A lowercase-hyphen display name as the first segment must never reach a span name.
        for path in ("/jane-runner-smith/stats", "jane-runner-smith", "/latest/jane-runner-smith",
                     "/proxy/wellness-service/x", "/wellness-servicex/x"):
            self.assertEqual(family(path), "other", path)
        for name in ("usersummary-service", "activitylist-service", "hrv-service", "course-service",
                     "workout-service", "sleep-service", "trainingplan-service"):
            self.assertEqual(family(f"/{name}/x/1"), name)

    def test_endpoint_families_match_telemetry(self):
        import telemetry

        self.assertEqual(self.launch.GARMIN_ENDPOINT_FAMILIES, telemetry.GARMIN_ENDPOINT_FAMILIES)

    def test_source_spans_and_the_sources_counter(self):
        sc = status_common

        def fine():
            return {"x": 1}

        def broken():
            raise ConnectionError("API Error 404 - Not Found")

        def stalled():
            time.sleep(1.0)

        self.launch._trace_status_sources()
        self.launch._trace_status_sources()  # idempotent
        self.assertEqual(sc.fetch_all.__wrapped__.__name__, "fetch_all")
        self.assertFalse(getattr(sc.fetch_all.__wrapped__, "_coach_traced", False))
        quiet(self, "garmin_ext")
        calls = {"sleep": sc.call(fine), "hrv": sc.call(broken), "Bad Name": sc.call(fine),
                 "stats": sc.call(stalled), "gear": sc.call(fine)}
        results = asyncio.run(sc.fetch_all(calls, timeout_s=0.2, skip={"gear"}))
        self.assertEqual(set(results), set(calls))
        self.assertTrue(results["sleep"]["ok"])
        counted = {c[2]["source"]: c[2]["outcome"] for c in self.names("count") if c[0] == "coach.status.sources"}
        self.assertEqual(counted, {"sleep": "ok", "hrv": "missing", "other": "ok", "stats": "timeout",
                                   "gear": "skipped"})
        spans = sorted(c[0] for c in self.names("span"))
        self.assertEqual(spans, ["source hrv", "source other", "source sleep", "source stats"])
        self.assertIn(("span.set", {"outcome": "missing", "error_group": "no_data"}), self.tel.calls)

    def test_sources_are_untouched_while_tracing_is_off(self):
        sc = status_common
        self.launch._trace_status_sources()
        self.launch._TELEMETRY = None
        results = asyncio.run(sc.fetch_all({"sleep": sc.call(lambda: 1)}))
        self.assertEqual(results["sleep"]["data"], 1)
        self.assertEqual(self.tel.calls, [])

    def traced_client(self, outcomes):
        """A class shaped like garminconnect's Client, its _run_request wrapped as the hook wraps it."""
        seen = []

        class Response:
            def __init__(self, status):
                self.status_code = status

        class Client:
            def _run_request(self, method, path, **kwargs):
                seen.append((method, path))
                outcome = outcomes.pop(0)
                if isinstance(outcome, Exception):
                    raise outcome
                return Response(outcome)

        Client._run_request = self.launch._traced_run_request(Client._run_request)
        return Client(), seen

    def test_garmin_get_spans_and_request_metrics(self):
        client, seen = self.traced_client([200, ConnectionError("API Error 429 - Too Many Requests")])
        self.assertEqual(client._run_request("GET", "/wellness-service/wellness/x/Some.Name").status_code, 200)
        with self.assertRaises(ConnectionError):
            client._run_request("GET", "metrics-service/y")
        self.assertEqual(len(seen), 2)
        self.assertEqual(self.names("span"), [
            ("garmin GET wellness-service", {"http.request.method": "GET", "caller": "tool",
                                             "endpoint_family": "wellness-service", "kind": "client"}),
            ("garmin GET metrics-service", {"http.request.method": "GET", "caller": "tool",
                                            "endpoint_family": "metrics-service", "kind": "client"})])
        self.assertIn(("span.set", {"status_code": 200}), self.tel.calls)
        self.assertIn(("span.error", "rate_limited"), self.tel.calls)
        counts = [c[2] for c in self.names("count") if c[0] == "coach.garmin.requests"]
        self.assertEqual(counts, [{"caller": "tool", "outcome": "ok", "mode": "full"},
                                  {"caller": "tool", "outcome": "error", "mode": "full",
                                   "error_group": "rate_limited"}])
        durations = [c[1] for c in self.names("observe") if c[0] == "coach.garmin.request.duration"]
        self.assertEqual(durations, [{"caller": "tool", "mode": "full"}] * 2)
        text = repr(self.tel.calls)
        self.assertNotIn("Some.Name", text)
        self.assertNotIn("429", text)

    def test_other_methods_count_but_get_no_get_span(self):
        client, _ = self.traced_client([200])
        client._run_request("POST", "graphql-gateway/graphql", json={"query": "query{x}"})
        self.assertEqual(self.names("span"), [])
        self.assertEqual([c[2]["outcome"] for c in self.names("count")], ["ok"])

    def test_requests_pass_straight_through_while_tracing_is_off(self):
        self.launch._TELEMETRY = None
        client, seen = self.traced_client([204])
        self.assertEqual(client._run_request("GET", "/x-service/y").status_code, 204)
        self.assertEqual(self.tel.calls, [])

    @unittest.skipUnless(HAVE_UPSTREAM, "needs upstream garmin_mcp and garminconnect")
    def test_install_wraps_upstream_threads_and_the_client_once(self):
        import garmin_mcp
        from garminconnect import client as gc_client

        self.launch._install_tracing_hooks()
        self.launch._install_tracing_hooks()
        self.assertIsInstance(garmin_mcp.threading, self.launch._BoundThreading)
        self.assertIs(garmin_mcp.threading._real, threading)
        wrapped = gc_client.Client._run_request
        self.assertTrue(wrapped._coach_traced)
        self.assertFalse(getattr(wrapped.__wrapped__, "_coach_traced", False))
        done = []
        thread = garmin_mcp.threading.Thread(target=lambda: done.append(1), name="garmin-call:x", daemon=True)
        thread.start()
        thread.join(5)
        self.assertEqual(done, [1])
        self.assertIn(("bind_context",), self.tel.calls)
        self.assertIs(garmin_mcp.threading.Event, threading.Event)

    @unittest.skipUnless(HAVE_UPSTREAM, "needs upstream garmin_mcp and garminconnect")
    def test_key_mode_leaves_the_garminconnect_client_alone(self):
        from garminconnect import client as gc_client

        before = gc_client.Client._run_request
        self.launch._KEY_MODE = True
        self.launch._install_tracing_hooks()
        self.assertIs(gc_client.Client._run_request, before)


def counting(text_holder):
    def answers_counted(text: str) -> str:
        """Answers `text` and counts the call."""
        text_holder.append(text)
        return text
    return answers_counted


@unittest.skipUnless(HAVE_UPSTREAM, "needs mcp 2.x and upstream garmin_mcp")
class GuardAndTracingInCallTool(unittest.TestCase):
    """call_tool() with the guard main() builds and a stand-in telemetry module."""

    def setUp(self):
        self.launch = load_launch(self, Stubs().modules)
        FastMCP = mcp2_compat.install().FastMCP
        self.calls = []
        allowed = {"answers_counted", "search_history", "partial_report"}
        self.server = self.launch.read_only_server_class(FastMCP, allowed)("test")
        self.server.tool(structured_output=False)(counting(self.calls))
        self.server.tool(name="search_history", structured_output=False)(upstream_answer)
        self.later_failure = None

        @mcp2_compat.server_loop
        async def partial_report() -> str:
            """A report whose first source answers and whose second fails with `later_failure` (if set)."""
            def first():
                return {"ok": 1}

            def second():
                if self.later_failure is not None:
                    raise self.later_failure
                return {"ok": 2}

            results = await status_common.fetch_all(
                {"first": status_common.call(first), "second": status_common.call(second)}, first="first")
            return "answered: " + ",".join(sorted(k for k, v in results.items() if v["ok"]))

        self.server.tool(name="partial_report", structured_output=False)(partial_report)
        self.launch._GUARD = garmin_guard.GarminGuard(zone="UTC")
        quiet(self, "mcp", "garmin_ext")

    def test_a_rate_limit_on_a_later_report_source_starts_the_cooldown(self):
        from garminconnect import GarminConnectTooManyRequestsError

        self.later_failure = GarminConnectTooManyRequestsError("Too many requests (429)")
        results = call_tools(self.server, "auto", [("partial_report", {}), ("answers_counted", {"text": "fine"})])
        self.assertEqual(results[0].content[0].text, "answered: first")  # the report still answers
        self.assertFalse(results[0].is_error)
        self.assertEqual(results[1].content[0].text, tool_errors.client_text(tool_errors.RATE_LIMITED))
        self.assertEqual(self.calls, [])  # the guard refused the next live call
        self.assertEqual(self.launch._GUARD.cooldown()[1], garmin_guard.RATE_LIMITED)

    def test_a_partial_answer_never_ends_a_login_cooldown(self):
        from garminconnect import GarminConnectAuthenticationError

        guard = self.launch._GUARD
        guard.after_outcome(garmin_guard.TOOL, garmin_guard.LOGIN)
        self.later_failure = GarminConnectAuthenticationError("Authentication failed")
        (result,) = call_tools(self.server, "auto", [("partial_report", {})])
        self.assertEqual(result.content[0].text, "answered: first")
        self.assertEqual(guard.cooldown()[1], garmin_guard.LOGIN)
        self.later_failure = None
        (result,) = call_tools(self.server, "auto", [("partial_report", {})])
        self.assertEqual(result.content[0].text, "answered: first,second")
        self.assertEqual(guard.cooldown(), (None, None))  # a whole answer ends it, as before

    def test_the_guard_outcome_of_a_call(self):
        outcome = self.launch._guard_outcome
        self.assertIsNone(outcome(None, set()))
        self.assertEqual(outcome(None, {"rate_limited"}), "rate_limited")
        self.assertEqual(outcome(None, {"login", "rate_limited"}), "rate_limited")
        self.assertEqual(outcome("unavailable", {"login"}), "login")
        self.assertEqual(outcome("login", {"rate_limited"}), "login")  # the call's own cooldown group first
        self.assertEqual(outcome("bad_input", set()), "bad_input")

    def test_a_rate_limit_stops_live_retries_without_calling_garmin(self):
        rate, fine = tool_errors.client_text(tool_errors.RATE_LIMITED), "fine"
        with captured_records() as records:
            results = call_tools(self.server, "auto", [("answers_counted", {"text": RATE_TEXT}),
                                                       ("answers_counted", {"text": fine}),
                                                       ("search_history", {})])
        self.assertEqual([r.content[0].text for r in results[:2]], [rate, rate])
        self.assertEqual(self.calls, [RATE_TEXT])  # the second call never ran
        self.assertEqual(results[2].content[0].text, RAW_ACTIVITY)  # the history tools never call Garmin
        lines = [r.getMessage() for r in tool_error_records(records)]
        self.assertEqual(lines[1], "tool error: tool=answers_counted group=rate_limited: "
                                   "Garmin call refused by the call guard: rate_limited")
        self.assertEqual(self.launch._GUARD.live_in_flight, 0)

    def test_login_never_blocks_a_retry(self):
        call_tools(self.server, "auto", [("answers_counted", {"text": LOGIN_TEXT}),
                                         ("answers_counted", {"text": "fine"})])
        self.assertEqual(self.calls, [LOGIN_TEXT, "fine"])

    def test_spans_and_metrics(self):
        fake = FakeTelemetry()
        self.launch._TELEMETRY = fake
        call_tools(self.server, "auto", [("answers_counted", {"text": "fine"}),
                                         ("answers_counted", {"text": RATE_TEXT})])
        spans = [c for c in fake.calls if c[0] == "span"]
        self.assertEqual(spans, [("span", "tool answers_counted", {"tool": "answers_counted", "caller": "tool",
                                                                   "mode": "full"})] * 2)
        sets = [c[1] for c in fake.calls if c[0] == "span.set"]
        self.assertEqual(sets, [{"outcome": "ok", "answer_bytes_bucket": "lt_1k"},
                                {"outcome": "error", "answer_bytes_bucket": "lt_1k"},
                                {"error_group": "rate_limited"}])
        self.assertIn(("span.error", "rate_limited"), fake.calls)
        counts = [c[3] for c in fake.calls if c[0] == "count"]
        self.assertEqual(counts, [{"tool": "answers_counted", "outcome": "ok", "mode": "full"},
                                  {"tool": "answers_counted", "outcome": "error", "mode": "full",
                                   "error_group": "rate_limited"}])
        gauges = [(c[2], c[3]) for c in fake.calls if c[0] == "gauge"]
        self.assertEqual(gauges, [(0, {}), (1, {"error_group": "rate_limited"})])

    def test_broken_telemetry_never_breaks_a_call(self):
        self.launch._TELEMETRY = FakeTelemetry(fail=True)
        (result,) = call_tools(self.server, "auto", [("answers_counted", {"text": "fine"})])
        self.assertEqual(result.content[0].text, "fine")

    def test_without_main_there_is_no_guard(self):
        self.launch._GUARD = None
        call_tools(self.server, "auto", [("answers_counted", {"text": RATE_TEXT}),
                                         ("answers_counted", {"text": "fine"})])
        self.assertEqual(self.calls, [RATE_TEXT, "fine"])


if __name__ == "__main__":
    unittest.main()
