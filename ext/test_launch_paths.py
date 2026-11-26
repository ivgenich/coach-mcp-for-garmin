"""Tests for launch.py: the argument checks before Garmin and the Garmin request path guard (garmin_paths.py,
re-exported by launch.py), on its own and through the server.

    cd ext && python -m unittest test_launch_paths

Shared helpers: test_launch_support.py.
"""

from __future__ import annotations

import importlib.util
import json
import re
import socket
import unittest
from unittest import mock

import garmin_paths  # noqa: E402
import mcp2_compat  # noqa: E402
import tool_errors  # noqa: E402

from test_launch_support import (call_tools, captured_records, ERAS, fake_secret, HAVE_UPSTREAM, keep_tracing_hooks,
    LINE_RE, load_launch, quiet, RealServerCase, Stubs, tool_error_records, UpstreamCase)

# The property-based tests (PathGuardProperties) need Hypothesis; the full CI leg installs it.
HAVE_HYPOTHESIS = importlib.util.find_spec("hypothesis") is not None


class CallerValuesThroughTheServer(RealServerCase):
    """Caller values through the real server and upstream's own tools, with the
    Garmin login failed: a value never picks the group and never reaches the
    log."""

    def test_fit_data_ids(self):
        # A huge id is a whole number, so the call reaches Garmin and fails on the login. An id that int()
        # refuses is bad input with the number detail, even when it holds the login words.
        login = tool_errors.client_text(tool_errors.LOGIN)
        number = tool_errors.client_text(tool_errors.BAD_INPUT, detail=tool_errors.NUMBER_DETAIL)
        cases = (("1" * 47, login, tool_errors.LOGIN), ("x\ngarmin login", number, tool_errors.BAD_INPUT),
                 ("1:2", number, tool_errors.BAD_INPUT), ("rate limit " + "a" * 5000, number, tool_errors.BAD_INPUT))
        server = self.serve()
        for mode in ERAS:
            with captured_records() as records:
                results = call_tools(server, mode, [("get_activity_fit_data", {"activity_id": activity_id})
                                                    for activity_id, _, _ in cases])
            lines = tool_error_records(records)
            self.assertEqual(len(lines), len(cases))
            for (activity_id, text, group), result, record in zip(cases, results, lines):
                with self.subTest(mode=mode, activity_id=activity_id[:20]):
                    self.assertTrue(result.is_error)
                    self.assertEqual([block.text for block in result.content], [text])
                    self.assertEqual(LINE_RE.match(record.getMessage()).groups(), ("get_activity_fit_data", group))
                    self.assertNotIn("\n", record.getMessage())
                    self.assertIsNone(record.exc_info)

    def test_a_bad_limit_is_logged_by_field_name_only(self):
        value = fake_secret()
        server = self.serve()
        for mode in ERAS:
            with captured_records() as records:
                (result,) = call_tools(server, mode, [("get_activities", {"limit": value})])
            with self.subTest(mode=mode):
                self.assertTrue(result.is_error)
                self.assertEqual([block.text for block in result.content],
                                 [tool_errors.client_text(tool_errors.BAD_INPUT,
                                                          detail="limit must be a whole number")])
                (record,) = tool_error_records(records)
                self.assertEqual(record.getMessage(), "tool error: tool=get_activities group=bad_input: "
                                                      "ValidationError: limit must be a whole number")
                self.assertEqual([r.getMessage() for r in records if value in r.getMessage()], [])


# --- The Garmin path guard and the argument checks (security fix, 25 Nov 2026) ----------------------------

TRAVERSAL_ID = "x-/../../../userprofile-service/userprofile/personal-information"
CANONICAL_UUID = "0f8fad5b-d9cb-469f-a165-70867728950e"
# Each would reach a Garmin path as given: upstream's get_workout_by_id takes any id holding "-" for a UUID.
BAD_WORKOUT_IDS = (TRAVERSAL_ID, "x-%2f..", "x-/%2e%2e/a", "x-?a=1", "x-#f", "123\n", CANONICAL_UUID + "\n",
                   True, 0, -1, "1 2", "0123", 1.5, None, 10 ** 19, "١٢٣")
GOOD_WORKOUT_IDS = (123, "123", CANONICAL_UUID)
# date.fromisoformat() on Python 3.12 accepts the first three; the regex refuses all of them.
BAD_DATES = ("20261101/x", "20261101?x", "20261101#x", "2026-W01-1", "20260101", "2026-02-30", "2026-11-01\n",
             "٢٠٢٦-11-01", 20261101)
BAD_PATHS = (TRAVERSAL_ID, "workout-service/fbt-adaptive/x-%2f..", "a/%2e%2e/b", "workout-service/x-?a=1",
             "workout-service/x-#f", "workout-service/123\n", "a//b", "a\\b", "a/./b", "./a", "a/.", "a\tb",
             "a/\u00a0b", "a/\uff0e\uff0e/b", "a/\uff0fb", "a/\u2024\u2024/b", "a/\x7f", "", "metrics-service/metrics/maxmet/daily/20261101?x/2026-11-10")
GOOD_PATHS = ("/course-service/course/1", "workout-service/workout/123",
              f"workout-service/fbt-adaptive/{CANONICAL_UUID}",
              "/biometric-service/stats/lactateThresholdSpeed/range/2026-01-01/2026-02-01"
              "?sport=RUNNING&aggregation=daily&aggregationStrategy=LATEST",
              "/wellness-service/wellness/dailyEvents?calendarDate=2026-11-01",
              # garminconnect puts the owner's display name in some paths: a space or a letter such as "Í"
              # must pass (requests percent-encodes them; neither changes the path's structure).
              "/usersummary-service/usersummary/daily/My Name", "/personalrecord-service/personalrecord/prs/Ívar",
              "/personalrecord-service/personalrecord/prs/E\u0301va")


class RefusedPath:
    def assert_refused(self):
        """The guard's refusal. By its text, not its class: the guard on garminconnect's Client may be the one
        an earlier test's fresh import of launch installed (patch() never wraps it twice)."""
        return self.assertRaisesRegex(tool_errors.KnownFailure, "^" + re.escape(self.launch.PATH_REFUSED_TEXT) + "$")


class PathRules(RefusedPath, unittest.TestCase):
    """launch.garmin_path_ok() and refused_argument(), the rules alone. Standard library only."""

    def test_launch_serves_the_same_guard(self):
        launch = load_launch(self, Stubs().modules)
        self.assertIs(launch.garmin_path_ok, garmin_paths.garmin_path_ok)
        self.assertIs(launch.check_garmin_path, garmin_paths.check_garmin_path)
        self.assertIs(launch.refused_argument, garmin_paths.refused_argument)
        self.assertIs(launch.GarminPathRefused, garmin_paths.GarminPathRefused)

    def setUp(self):
        self.launch = load_launch(self, Stubs().modules)

    def test_bad_paths_are_refused_and_plain_ones_pass(self):
        for path in BAD_PATHS + (None, b"a/b", 7):
            with self.subTest(path=path):
                self.assertFalse(self.launch.garmin_path_ok(path))
                with self.assert_refused():
                    self.launch.check_garmin_path(path)
        for path in GOOD_PATHS:
            with self.subTest(path=path):
                self.assertTrue(self.launch.garmin_path_ok(path))

    def test_only_garminconnects_own_query_names_may_follow_a_question_mark(self):
        self.assertFalse(self.launch.garmin_path_ok("a/b?a=1"))
        self.assertFalse(self.launch.garmin_path_ok("a/b?sport=RUNNING&x=1"))
        self.assertFalse(self.launch.garmin_path_ok("a/b?sport="))
        self.assertFalse(self.launch.garmin_path_ok("a/b?sport=a/b"))
        self.assertTrue(self.launch.garmin_path_ok("a/b?start=0&limit=20"))

    def test_the_refusal_is_bad_input_raised_wrapped_or_answered(self):
        refused = self.launch.GarminPathRefused()
        self.assertNotIn("..", str(refused))
        self.assertEqual(tool_errors.classify_exception(refused)[:2], (tool_errors.BAD_INPUT, tool_errors.GENERIC_DETAIL))
        try:
            try:
                raise refused
            except ValueError as exc:
                raise ConnectionError(f"Connection error: {exc}") from exc
        except ConnectionError as wrapped:
            self.assertEqual(tool_errors.classify_exception(wrapped)[0], tool_errors.BAD_INPUT)
        self.assertEqual(tool_errors.classify_text(f"Error retrieving workout: Connection error: {refused}"),
                         (tool_errors.BAD_INPUT, tool_errors.GENERIC_DETAIL))

    def test_argument_checks(self):
        refused = self.launch.refused_argument
        for value in BAD_WORKOUT_IDS:
            with self.subTest(workout_id=value):
                self.assertEqual(refused("get_workout_by_id", {"workout_id": value}),
                                 ("workout_id", tool_errors.GENERIC_DETAIL))
        for value in GOOD_WORKOUT_IDS + (CANONICAL_UUID.upper(), 10 ** 19 - 1):
            with self.subTest(workout_id=value):
                self.assertIsNone(refused("get_workout_by_id", {"workout_id": value, "raw": True}))
        good = {"start_date": "2026-11-01", "end_date": "2026-11-10"}
        self.assertIsNone(refused("get_vo2max_trend", good))
        for field in good:
            for value in BAD_DATES:
                with self.subTest(field=field, value=value):
                    self.assertEqual(refused("get_vo2max_trend", {**good, field: value}),
                                     (field, tool_errors.DATE_DETAIL))
        # plan_preview's course_id goes into the course read's path: digits only (rules v2 race pacing)
        preview = {"race_date": "2027-03-05"}
        for value in ("", "0", "4242", 4242, 0, "1" * 15):
            with self.subTest(course_id=value):
                self.assertIsNone(refused("plan_preview", {**preview, "course_id": value}))
                if value not in ("", "0", 0):
                    self.assertTrue(garmin_paths.garmin_path_ok(f"course-service/course/{value}"))
        for value in (TRAVERSAL_ID, "../1", "-1", "01", "1" * 16, 10 ** 15, -1, True, 1.5, None):
            with self.subTest(course_id=value):
                self.assertEqual(refused("plan_preview", {**preview, "course_id": value}),
                                 ("course_id", tool_errors.GENERIC_DETAIL))
        # Absent arguments are the SDK's to report; other tools have no row.
        self.assertIsNone(refused("get_workout_by_id", {}))
        self.assertIsNone(refused("get_stats", {"date": TRAVERSAL_ID}))
        self.assertIsNone(refused("get_workout_by_id", None))


def fake_garmin(test):
    """A real garminconnect.Garmin whose HTTP session records (method, url, kwargs) and answers 200 {} (no
    network: a connection attempt fails the test), with the path guard installed on garminconnect's Client
    until the test ends."""
    import garminconnect
    from garminconnect.client import Client as GarminClient

    class Response:
        status_code = 200
        text = "{}"
        content = b"{}"

        def json(self):
            return {}

    sent = []

    class Session:
        def request(self, method, url, headers=None, **kwargs):
            sent.append((method, url, kwargs))
            return Response()

    def refuse(*args, **kwargs):
        raise AssertionError("a test tried to reach the network")

    keep_tracing_hooks(test)  # restores Client._run_request
    for patcher in (mock.patch.object(GarminClient, "_fresh_api_session", lambda client: Session()),
                    mock.patch.object(GarminClient, "_refresh_session", lambda client: None),
                    mock.patch.object(socket.socket, "connect", refuse),
                    mock.patch.object(socket, "getaddrinfo", refuse)):
        patcher.start()
        test.addCleanup(patcher.stop)
    garmin = garminconnect.Garmin()
    garmin.client.di_token = fake_secret(24)
    garmin.display_name, garmin.unit_system = "MyName", "metric"
    return garmin, sent


@unittest.skipUnless(HAVE_UPSTREAM, "needs upstream garmin_mcp and garminconnect")
class PathGuardOnTheClient(RefusedPath, UpstreamCase):
    """The guard on garminconnect's Client._run_request refuses a bad path whoever builds it, before any
    request leaves, and leaves params= alone."""

    def setUp(self):
        super().setUp()
        self.launch = load_launch(self, Stubs().modules)
        self.garmin, self.sent = fake_garmin(self)
        quiet(self, "garminconnect", "mcp", "garmin_ext")
        self.launch._install_path_guard()

    def test_every_api_method_refuses_a_bad_path_and_sends_nothing(self):
        from garminconnect import GarminConnectConnectionError

        calls = (lambda p: self.garmin.client.connectapi(p), lambda p: self.garmin.client.download(p),
                 lambda p: self.garmin.client.request("GET", "connectapi", p),
                 lambda p: self.garmin.client.post("connectapi", p, json={}))
        for path in BAD_PATHS:
            for call in calls:
                with self.subTest(path=path), self.assert_refused():
                    call(path)
            # Garmin's own wrappers re-raise it as a connection error, which still classifies as bad input.
            for wrapper in (self.garmin.connectapi, self.garmin.download):
                with self.subTest(path=path), self.assertRaises(GarminConnectConnectionError) as cm:
                    wrapper(path)
                self.assertEqual(tool_errors.classify_exception(cm.exception)[0], tool_errors.BAD_INPUT)
        self.assertEqual(self.sent, [])

    def test_plain_paths_pass_and_params_are_untouched(self):
        params = {"q": "../x?%#", "startDate": "2026-11-01"}
        self.garmin.connectapi("/course-service/course/1", params=params)
        self.garmin.get_workout_by_id(123)
        (first, second) = self.sent
        self.assertTrue(first[1].endswith("/course-service/course/1"))
        self.assertEqual(first[2]["params"], {"q": "../x?%#", "startDate": "2026-11-01"})
        self.assertTrue(second[1].endswith("/workout-service/workout/123"))

    def test_install_is_idempotent_and_survives_the_tracing_wrapper(self):
        from garminconnect.client import Client as GarminClient

        guarded = GarminClient._run_request
        self.launch._install_path_guard()
        self.assertIs(GarminClient._run_request, guarded)
        self.launch._trace_garmin_requests()
        self.assertTrue(GarminClient._run_request._coach_traced)
        self.launch._install_path_guard()
        self.assertTrue(GarminClient._run_request._coach_traced)  # not wrapped twice
        self.assertTrue(self.launch._path_guard_installed(GarminClient._run_request))
        with self.assert_refused():
            self.garmin.client.connectapi(TRAVERSAL_ID)
        self.assertEqual(self.sent, [])

    def test_patch_installs_it_in_full_mode_only(self):
        from garminconnect.client import Client as GarminClient

        original = GarminClient.__dict__["_run_request"]
        while getattr(original, "__wrapped__", None) is not None:
            original = original.__wrapped__
        GarminClient._run_request = original
        self.launch.patch(garmin_auth="key")
        self.assertFalse(self.launch._path_guard_installed(GarminClient._run_request))
        self.launch.patch()
        self.assertTrue(self.launch._path_guard_installed(GarminClient._run_request))

    def test_a_moved_hook_fails_the_build_check(self):
        from garminconnect.client import Client as GarminClient

        def _run_request(self, method, url, **kwargs):  # a bump that renamed the parameter
            return None

        # The installed garminconnect still has the hook (under the guard setUp installed): a bump that drops
        # or renames Client._run_request fails here, loudly, as well as in the build check below.
        self.assertIn("_run_request", vars(GarminClient))
        self.assertTrue(self.launch._path_guard_installed(GarminClient._run_request))
        self.launch._check_path_guard_hook()
        hook = GarminClient.__dict__["_run_request"]
        for renamed in (None, "_send_request"):  # the method gone, or there under another name
            with self.subTest(renamed=renamed):
                delattr(GarminClient, "_run_request")
                try:
                    if renamed is not None:
                        setattr(GarminClient, renamed, hook)
                    with self.assertRaisesRegex(RuntimeError, "path guard needs a new hook"):
                        self.launch._check_path_guard_hook()
                    with self.assertRaisesRegex(RuntimeError, "path guard needs a new hook"):
                        self.launch._install_path_guard()
                finally:
                    if renamed is not None:
                        delattr(GarminClient, renamed)
                    GarminClient._run_request = hook
        with mock.patch.object(GarminClient, "_run_request", _run_request):
            with self.assertRaisesRegex(RuntimeError, "path guard needs a new hook"):
                self.launch._check_path_guard_hook()
        with mock.patch.object(GarminClient, "connectapi", lambda self, path, **kw: None):
            with self.assertRaisesRegex(RuntimeError, "path guard needs a new hook"):
                self.launch._check_path_guard_hook()

    def test_an_upstream_tool_that_builds_a_bad_path_answers_bad_input(self):
        garmin = self.garmin

        def catches(workout_id: str) -> str:
            """As upstream's tools: the failure becomes an "Error ...: ..." answer."""
            try:
                return json.dumps(garmin.connectapi(f"workout-service/fbt-adaptive/{workout_id}"))
            except Exception as e:  # noqa: BLE001 - upstream's pattern
                return f"Error retrieving workout: {str(e)}"

        def raises(workout_id: str) -> str:
            """A tool that lets the failure out."""
            return json.dumps(garmin.connectapi(f"workout-service/fbt-adaptive/{workout_id}"))

        FastMCP = mcp2_compat.install().FastMCP
        server = self.launch.read_only_server_class(FastMCP, {"catches", "raises"})("test")
        server.tool(name="catches", structured_output=False)(catches)
        server.tool(name="raises", structured_output=False)(raises)
        guard = mock.Mock()
        guard.cooldown.return_value = (None, None)
        with mock.patch.object(self.launch, "_GUARD", guard):
            results = call_tools(server, "legacy", [("catches", {"workout_id": TRAVERSAL_ID}),
                                                    ("raises", {"workout_id": "x-/%2e%2e/a"})])
        expected = tool_errors.client_text(tool_errors.BAD_INPUT, detail=tool_errors.GENERIC_DETAIL)
        for result in results:
            self.assertTrue(result.is_error)
            self.assertEqual([block.text for block in result.content], [expected])
        self.assertEqual(self.sent, [])
        outcomes = [c.args[1] for c in guard.after_outcome.call_args_list]
        self.assertEqual(outcomes, [tool_errors.BAD_INPUT] * 2)


class ArgumentChecksThroughTheServer(RealServerCase):
    """launch.main()'s server with upstream's real get_workout_by_id and get_vo2max_trend over a fake Garmin
    session: a bad argument answers bad input before the call guard and before Garmin; good ones reach
    Garmin."""

    def setUp(self):
        super().setUp()
        keep_tracing_hooks(self)  # main()'s patch() installs the path guard on garminconnect's Client
        from uvicorn import config as uvicorn_config

        self.uvicorn_config = uvicorn_config.LOGGING_CONFIG
        self.uvicorn_before = json.dumps(self.uvicorn_config, sort_keys=True, default=repr)
        self.server = self.serve()
        self.garmin_answering(200, {})
        self.guard = mock.Mock()
        self.guard.cooldown.return_value = (None, None)
        patcher = mock.patch.object(self.launch, "_GUARD", self.guard)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_bad_workout_ids_never_reach_the_guard_or_garmin(self):
        expected = tool_errors.client_text(tool_errors.BAD_INPUT, detail=tool_errors.GENERIC_DETAIL)
        with captured_records() as records:
            results = call_tools(self.server, "legacy",
                                 [("get_workout_by_id", {"workout_id": value}) for value in BAD_WORKOUT_IDS])
        for value, result in zip(BAD_WORKOUT_IDS, results):
            with self.subTest(workout_id=value):
                self.assertTrue(result.is_error)
                self.assertEqual([block.text for block in result.content], [expected])
        self.assertEqual(self.garmin_requests, [])
        self.guard.before_call.assert_not_called()
        self.guard.after_outcome.assert_not_called()
        lines = [r.getMessage() for r in tool_error_records(records)]
        self.assertEqual(len(lines), len(BAD_WORKOUT_IDS))
        self.assertTrue(all("group=bad_input" in line for line in lines), lines)
        self.assertFalse(any("userprofile" in line for line in lines), lines)

    def test_good_workout_ids_reach_garmin(self):
        results = call_tools(self.server, "legacy",
                             [("get_workout_by_id", {"workout_id": value}) for value in GOOD_WORKOUT_IDS])
        expected = tool_errors.client_text(tool_errors.BAD_INPUT, detail=tool_errors.GENERIC_DETAIL)
        for result in results:
            self.assertNotEqual([block.text for block in result.content], [expected])
        self.assertEqual(len(self.garmin_requests), 3)
        self.assertTrue(self.garmin_requests[0].endswith("/workout-service/workout/123"))
        self.assertTrue(self.garmin_requests[1].endswith("/workout-service/workout/123"))
        self.assertTrue(self.garmin_requests[2].endswith(f"/workout-service/fbt-adaptive/{CANONICAL_UUID}"))
        self.assertEqual(self.guard.before_call.call_count, 3)

    def test_vo2max_dates(self):
        expected = tool_errors.client_text(tool_errors.BAD_INPUT, detail=tool_errors.DATE_DETAIL,
                                            today=self.launch._today_iso())
        calls = [("get_vo2max_trend", {"start_date": value, "end_date": "2026-11-10"}) for value in BAD_DATES]
        calls += [("get_vo2max_trend", {"start_date": "2026-11-01", "end_date": value}) for value in BAD_DATES]
        for (_, arguments), result in zip(calls, call_tools(self.server, "legacy", calls)):
            with self.subTest(arguments=arguments):
                self.assertTrue(result.is_error)
                self.assertEqual([block.text for block in result.content], [expected])
        self.assertEqual(self.garmin_requests, [])
        self.guard.before_call.assert_not_called()
        (result,) = call_tools(self.server, "legacy",
                               [("get_vo2max_trend", {"start_date": "2026-11-01", "end_date": "2026-11-10"})])
        self.assertNotEqual([block.text for block in result.content], [expected])
        self.assertTrue(self.garmin_requests)
        self.assertTrue(all(".." not in url and "?x" not in url for url in self.garmin_requests))

    def test_stdio_leaves_uvicorns_log_config_alone(self):
        self.assertEqual(json.dumps(self.uvicorn_config, sort_keys=True, default=repr), self.uvicorn_before)


# Characters that make paths interesting to the guard: separators, dots, the refused parts, whitespace, a
# letter (Í), a combining accent, and lookalikes of "." and "/".
PATHISH = list("ab1Z/.-_?=&%#\\: \t\n") + ["\u00cd", "\u0301", "\uff0e", "\uff0f", "\u2024", "\x7f", "\u00a0", "\u200b"]
GARMIN_HOST = "connectapi.garmin.com"


@unittest.skipUnless(HAVE_HYPOTHESIS, "needs hypothesis")
class PathGuardProperties(unittest.TestCase):
    """Property-based tests of the path guard and the argument checks (garmin_paths, which launch.py
    re-exports): generated paths and arguments instead of the fixed lists above."""

    def check(self, strategy, prop, examples=300):
        """Run `prop` on `strategy`'s examples: derandomized, so a CI run is repeatable, and with no example
        database (Hypothesis still caches Unicode tables in a git-ignored .hypothesis/ folder)."""
        from hypothesis import given, settings

        settings(max_examples=examples, deadline=None, database=None, derandomize=True)(given(strategy)(prop))()

    def strategies(self):
        from hypothesis import strategies as st

        return st

    def plain_routes(self):
        """Routes garminconnect builds: segments of letters, digits, "-" and "_" (some with a display name:
        letters and spaces), joined by single slashes, with or without a leading one."""
        st = self.strategies()
        word = st.from_regex(r"[A-Za-z0-9_-]{1,20}", fullmatch=True)
        name = st.text(alphabet=st.characters(categories=("Lu", "Ll", "Lo")), min_size=1, max_size=10)
        segment = st.one_of(word, st.builds(lambda a, b: f"{a} {b}", name, name), name)
        return st.builds(lambda lead, parts: lead + "/".join(parts), st.sampled_from(("", "/")),
                         st.lists(segment, min_size=1, max_size=8))

    def pathish(self):
        st = self.strategies()
        return st.one_of(st.text(alphabet=st.sampled_from(PATHISH), max_size=40), self.plain_routes(), st.text())

    def test_plain_routes_and_garminconnects_own_queries_pass(self):
        st = self.strategies()
        value = st.from_regex(r"[A-Za-z0-9_.:-]{1,64}", fullmatch=True)
        pair = st.builds(lambda n, v: f"{n}={v}", st.sampled_from(sorted(garmin_paths._PATH_QUERY_NAMES)), value)
        query = st.one_of(st.just(""), st.lists(pair, min_size=1, max_size=4).map(lambda ps: "?" + "&".join(ps)))

        def prop(route, query):
            # A query value may hold ".." or "//"? Not with this alphabet: ".." is refused anywhere in the path.
            path = route + query
            if ".." in path:
                self.assertFalse(garmin_paths.garmin_path_ok(path))
                return
            self.assertTrue(garmin_paths.garmin_path_ok(path), path)
            garmin_paths.check_garmin_path(path)

        self.check(st.tuples(self.plain_routes(), query), lambda args: prop(*args))

    def test_a_refused_part_anywhere_refuses_the_path(self):
        st = self.strategies()
        bad = st.sampled_from(garmin_paths._PATH_REFUSED_PARTS)

        def prop(args):
            before, part, after = args
            path = before + part + after
            self.assertFalse(garmin_paths.garmin_path_ok(path), path)
            with self.assertRaises(garmin_paths.GarminPathRefused):
                garmin_paths.check_garmin_path(path)

        self.check(st.tuples(self.pathish(), bad, self.pathish()), prop)

    def test_a_dot_segment_anywhere_refuses_the_path(self):
        st = self.strategies()

        def prop(args):
            route, index, dot = args
            parts = route.split("/")
            parts.insert(index % (len(parts) + 1), dot)
            path = "/".join(parts)
            self.assertFalse(garmin_paths.garmin_path_ok(path), path)

        self.check(st.tuples(self.plain_routes(), st.integers(0, 20), st.sampled_from((".", ".."))), prop)

    def test_a_control_or_lookalike_character_refuses_the_path(self):
        st = self.strategies()
        odd = st.one_of(st.characters(categories=("Cc", "Cf", "Zl", "Zp")),
                        st.sampled_from(("\t", "\n", "\u00a0", "\u2007", "\uff0e", "\uff0f", "\u2024", "\u2215")))

        def prop(args):
            route, index, ch = args
            index %= len(route) + 1
            path = route[:index] + ch + route[index:]
            self.assertFalse(garmin_paths.garmin_path_ok(path), repr(path))

        self.check(st.tuples(self.plain_routes(), st.integers(0, 200), odd), prop)

    def test_an_accepted_path_keeps_the_url_garminconnect_builds(self):
        """The property the guard exists for: whatever path passes, the URL garminconnect builds from it
        (f"{connectapi}/{path.lstrip('/')}") keeps Garmin's host, has no fragment and no dot segment, and
        after requests prepares it (percent-encoding, urllib3's dot-segment removal) names the same route."""
        import urllib.parse

        try:
            from requests.models import PreparedRequest
        except ImportError:  # the check below then runs without requests' own preparation
            PreparedRequest = None

        def prop(path):
            ok = garmin_paths.garmin_path_ok(path)
            self.assertIsInstance(ok, bool)
            if not ok:
                return
            url = f"https://{GARMIN_HOST}/{path.lstrip('/')}"
            parts = urllib.parse.urlsplit(url)
            self.assertEqual((parts.scheme, parts.netloc, parts.fragment), ("https", GARMIN_HOST, ""), repr(path))
            route = path.partition("?")[0].lstrip("/")
            self.assertEqual(parts.path, "/" + route, repr(path))
            self.assertFalse({".", ".."} & set(route.split("/")), repr(path))
            self.assertNotIn("", route.split("/")[:-1], repr(path))
            if PreparedRequest is not None:
                prepared = PreparedRequest()
                prepared.prepare_url(url, None)
                sent = urllib.parse.urlsplit(prepared.url)
                self.assertEqual(sent.netloc, GARMIN_HOST, repr(path))
                self.assertEqual(urllib.parse.unquote(sent.path), "/" + route, repr(path))

        self.check(self.pathish(), prop, examples=600)

    def test_never_raises_and_check_agrees_for_any_value(self):
        st = self.strategies()
        values = st.one_of(self.pathish(), st.binary(max_size=20), st.integers(), st.none(), st.floats(),
                           st.lists(st.text(max_size=5), max_size=3))

        def prop(value):
            ok = garmin_paths.garmin_path_ok(value)
            self.assertIsInstance(ok, bool)
            if ok:
                self.assertIsInstance(value, str)
                garmin_paths.check_garmin_path(value)
            else:
                with self.assertRaises(garmin_paths.GarminPathRefused) as cm:
                    garmin_paths.check_garmin_path(value)
                self.assertEqual(str(cm.exception), garmin_paths.PATH_REFUSED_TEXT)  # never the path itself

        self.check(values, prop)

    def test_the_guarded_run_request_calls_garmin_only_for_accepted_paths(self):
        calls = []

        def original(client, method, path, *args, **kwargs):
            calls.append((client, method, path, args, kwargs))
            return "answer"

        guarded = garmin_paths.path_guarded_run_request(original)
        self.assertTrue(garmin_paths.path_guard_installed(guarded))
        self.assertFalse(garmin_paths.path_guard_installed(original))
        client = object()
        params = {"q": "../x?%#"}  # params= is not part of the path and passes untouched

        def prop(path):
            calls.clear()
            if garmin_paths.garmin_path_ok(path):
                self.assertEqual(guarded(client, "GET", path, "connectapi", params=params), "answer")
                self.assertEqual(calls, [(client, "GET", path, ("connectapi",), {"params": params})])
            else:
                with self.assertRaises(garmin_paths.GarminPathRefused):
                    guarded(client, "GET", path, params=params)
                self.assertEqual(calls, [])

        self.check(self.pathish(), prop)

    def test_accepted_arguments_build_paths_the_guard_accepts(self):
        """refused_argument() and the path guard agree: a workout id or date the argument check lets through
        builds an upstream path the guard lets through too, and ids out of range are refused."""
        st = self.strategies()
        uuid = st.uuids().map(str)
        ids = st.one_of(st.integers(), st.integers(1, garmin_paths._ID_MAX).map(str), uuid,
                        uuid.map(str.upper), self.pathish(), st.booleans(), st.floats())
        dates = st.one_of(st.dates().map(lambda d: d.isoformat()), self.pathish(), st.integers())

        def id_prop(value):
            refused = garmin_paths.refused_argument("get_workout_by_id", {"workout_id": value})
            if isinstance(value, int) and not isinstance(value, bool):
                self.assertEqual(refused is None, 0 < value <= garmin_paths._ID_MAX, value)
            if refused is None:
                kind = "fbt-adaptive" if "-" in str(value) else "workout"
                self.assertTrue(garmin_paths.garmin_path_ok(f"workout-service/{kind}/{value}"), repr(value))
            else:
                self.assertEqual(refused, ("workout_id", tool_errors.GENERIC_DETAIL))

        def date_prop(value):
            refused = garmin_paths.refused_argument("get_vo2max_trend", {"start_date": value, "end_date": value})
            if refused is None:
                path = f"metrics-service/metrics/maxmet/daily/{value}/{value}"
                self.assertTrue(garmin_paths.garmin_path_ok(path), repr(value))
            else:
                self.assertEqual(refused, ("start_date", tool_errors.DATE_DETAIL))

        self.check(ids, id_prop)
        self.check(dates, date_prop)


if __name__ == "__main__":
    unittest.main()
