"""Tests for launch.py: this repo's tools join upstream's filtered app, only
read-only tools are ever registered, and with a network transport main() and
--check-config wire in the built-in sign-in.

    python -m unittest discover -s ext -p "test_launch*.py" -v

Many tests need upstream garmin_mcp (the image, or a venv with the pinned
commit) and are skipped without it; the read-only policy rules (ToolPolicy,
RegistrationGate, CheckConfig, ConfigFile) and the sign-in part of
check_config() (SignInCheckConfig) run on the standard library alone.
The status modules are replaced by stubs, so these tests pin launch.py's
wiring, not the tools' behaviour (their own tests cover that); RealModules
repeats the key checks with the real modules. SignInMain runs main() with a
stand-in for owner_auth and with the real one. Every secret here is a fake
made at run time.

Step 1.3 (the tool-error wrapper, the server instructions and the description
note): ToolCallOverride calls ReadOnlyFastMCP.call_tool() on a small server
with test tools; ServedTexts, LoginFailed and StatusToolsThroughTheServer run
the real launch.main() server with the real status modules, a Garmin login
that has failed and, for a Garmin 401, a fake Garmin HTTP session (no
network); CallToolHook covers gate()'s check of the SDK hook.

The other launch.py tests are in files of their own, along the modules
launch.py was split into: test_launch_paths.py (the argument checks and the
Garmin path guard), test_launch_answers.py (the description notes and answer
additions), test_launch_features.py (the optional features' startup),
test_launch_logging.py (the log lines and uvicorn's split),
test_launch_tracing.py (the tracing hooks and the call guard in call_tool()),
test_launch_signin.py and test_launch_key_mode.py. Their shared helpers
(Stubs, load_launch(), UpstreamCase, RealServerCase, log capture, fakes) are
in test_launch_support.py, which has no tests of its own.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import inspect
import io
import json
import logging
import os
import re
import secrets
import stat
import subprocess
import sys
import threading
import time
import types
import unittest
import zoneinfo
from unittest import mock

import mcp2_compat  # noqa: E402
import server_instructions  # noqa: E402
import version  # noqa: E402
import text_hygiene  # noqa: E402
import tool_errors  # noqa: E402

from test_launch_support import (A2, A3, A5, A8, ACTIVITY, answers, call_tools, captured_records,
    ConfigCommandRunner, DOCKERFILE_FILE, enabled_tools, ENABLED_TOOLS_FILE, ENTRYPOINT_FILE, ERAS, fake_secret,
    FakeServer, FULL, get_daily_status, handshake, HAVE_STATUS_MODULES, HAVE_UPSTREAM, HERE, KEY_VARS, LINE_RE,
    load_launch, LOGIN_TEXT, quiet, RATE_TEXT, RAW_ACTIVITY, read, READ_ONLY, RealServerCase, REMOVED, ROOT,
    sign_in_env, STATUS_MODULES, Stubs, temp_dir, tool_error_records, upstream_answer, UPSTREAM_MODULES,
    UpstreamCase)
# The status tools' arguments (the daily routine passes text_format; chats may
# pass the rest). FastMCP drops unknown arguments silently, so a renamed
# parameter would fail without an error.
EXPECTED_PARAMS = {
    "get_daily_status": {"date", "include_sources", "probe", "text_format"},
    "get_weekly_status": {"end_date", "include_sources", "text_format"},
}
# The sign-in's modules; the image needs all of them.
SIGN_IN_MODULES = ("auth_config.py", "auth_store.py", "auth_http.py", "owner_auth.py")


def build_app(enabled=(), all_modules=False):
    """A FastMCP app behind upstream's _ToolFilter, registered as main() does.

    `enabled` goes through upstream's own GARMIN_ENABLED_TOOLS parser; empty
    means no filter. Returns (fastmcp, filtered app, {name: listed tool}).
    """
    import garmin_mcp
    from mcp.server.fastmcp import FastMCP

    fastmcp = FastMCP("test")
    app = garmin_mcp._ToolFilter(fastmcp, garmin_mcp._parse_tool_set(",".join(enabled)), set())
    for name in UPSTREAM_MODULES if all_modules else ("courses",):
        app = getattr(garmin_mcp, name).register_tools(app)
    tools = {t.name: t for t in asyncio.run(fastmcp.list_tools())}
    return fastmcp, app, tools


@unittest.skipUnless(HAVE_UPSTREAM, "needs upstream garmin_mcp on mcp 2.x")
class PatchWithStubs(UpstreamCase):
    def patched(self, failing=None):
        stubs = Stubs(failing)
        load_launch(self, stubs.modules).patch()
        return stubs

    def test_registers_status_tools_beside_upstream_course_tools(self):
        self.patched()
        _, app, tools = build_app()
        self.assertLessEqual({"get_courses", "get_course_details"} | READ_ONLY, set(tools))
        self.assertEqual(app.unannotated_tool_names(), [])

    def test_annotations(self):
        self.patched()
        _, _, tools = build_app()
        self.assert_annotations(tools)
        # Every tool of this repo is read-only: patch() adds none to upstream's other sets.
        self.assertIs(self.annotations.ADDITIVE_TOOLS, self.upstream_additive)

    def test_selfchecks_run_in_patch_and_each_module_registers_once(self):
        stubs = self.patched()
        self.assertEqual(stubs.calls, ["daily_status.selfcheck", "weekly_status.selfcheck"])
        build_app()
        self.assertEqual(sorted(stubs.calls[2:]), sorted(m + ".register_tools" for m in STATUS_MODULES))

    def test_failing_selfcheck_fails_patch_and_leaves_upstream_alone(self):
        for module in ("daily_status", "weekly_status"):
            with self.subTest(module=module):
                with self.assertRaises(RuntimeError):
                    self.patched(failing=module)
                self.assertIs(self.courses.register_tools, self.upstream_register)
                self.assertIs(self.annotations.READ_ONLY_TOOLS, self.upstream_read_only)
                self.assertIs(self.annotations.ADDITIVE_TOOLS, self.upstream_additive)

    def test_upstream_tool_named_like_ours_fails_patch_and_leaves_upstream_alone(self):
        # The SDK would serve whichever registered first, under our read-only annotation.
        for name in sorted(READ_ONLY):
            taken = self.annotations.ALL_ANNOTATED_TOOLS | {name}
            with self.subTest(name=name), mock.patch.object(self.annotations, "ALL_ANNOTATED_TOOLS", taken):
                with self.assertRaisesRegex(RuntimeError, f"upstream now has a tool named {name}"):
                    self.patched()
                self.assertIs(self.courses.register_tools, self.upstream_register)
                self.assertIs(self.annotations.READ_ONLY_TOOLS, self.upstream_read_only)

    def test_status_tools_get_upstreams_client_at_call_time(self):
        stubs = self.patched()
        build_app()
        client = object()  # main() sets courses.garmin_client after patch()
        self.courses.garmin_client = client
        for module in ("daily_status", "weekly_status"):
            self.assertIs(stubs.get_client[module](), client)

    def test_allowlist_decides_whether_the_status_tools_register(self):
        self.patched()
        _, _, tools = build_app(enabled=["get_courses"])
        self.assertEqual(set(tools), {"get_courses"})
        _, _, tools = build_app(enabled=enabled_tools())
        self.assertLessEqual(READ_ONLY, set(tools))

    def test_enabled_tools_file_names_only_real_annotated_tools(self):
        self.patched()
        _, app, tools = build_app(enabled=enabled_tools(), all_modules=True)
        self.assertEqual(app.unknown_filter_names(), [])
        self.assertEqual(app.unannotated_tool_names(), [])
        self.assertEqual(set(tools), set(enabled_tools()))


@unittest.skipUnless(HAVE_UPSTREAM and HAVE_STATUS_MODULES,
                     "needs upstream garmin_mcp and ext/daily_status.py, weekly_status.py")
class RealModules(UpstreamCase):
    """The same wiring with the real modules: names, annotations, arguments."""

    def setUp(self):
        super().setUp()
        load_launch(self).patch()

    def test_allowlisted_app_serves_exactly_the_file(self):
        _, app, tools = build_app(enabled=enabled_tools(), all_modules=True)
        self.assertEqual(app.unknown_filter_names(), [])
        self.assertEqual(app.unannotated_tool_names(), [])
        self.assertEqual(set(tools), set(enabled_tools()))
        self.assert_annotations(tools)

    def test_status_tools_take_the_contract_arguments(self):
        _, _, tools = build_app()
        for name, params in EXPECTED_PARAMS.items():
            self.assertEqual(set(tools[name].input_schema.get("properties", {})), params, name)


@unittest.skipUnless(HAVE_UPSTREAM, "needs upstream garmin_mcp on mcp 2.x")
class ReadOnlyPolicy(UpstreamCase):
    """allowed_tools() and gate() against upstream's real classification."""

    def setUp(self):
        super().setUp()
        self.launch = load_launch(self, Stubs().modules)

    def test_unset_env_allows_the_baked_list_and_it_is_all_read_only(self):
        allowed = self.launch.allowed_tools({})
        self.assertEqual(allowed, frozenset(enabled_tools()))
        self.assertLessEqual(allowed, self.annotations.READ_ONLY_TOOLS | READ_ONLY)

    def test_no_upstream_write_tool_can_be_enabled(self):
        write = self.annotations.ADDITIVE_TOOLS | self.annotations.DESTRUCTIVE_TOOLS
        for name in sorted(write):
            with self.subTest(name=name):
                with self.assertRaises(self.launch.ConfigError) as cm:
                    self.launch.allowed_tools({"GARMIN_ENABLED_TOOLS": "get_stats," + name})
                self.assertIn(f"{name} is a write tool", str(cm.exception))

    def test_listed_tool_reclassified_as_write_fails_the_build_check(self):
        # As if an upstream bump listed one of our names as a write tool: the
        # Dockerfile's check (patch(), then allowed_tools()) must fail, not
        # serve it as read-only.
        destructive = self.annotations.DESTRUCTIVE_TOOLS | {"get_daily_status"}
        with mock.patch.object(self.annotations, "DESTRUCTIVE_TOOLS", destructive):
            self.launch.patch()
            with self.assertRaises(self.launch.ConfigError) as cm:
                self.launch.allowed_tools({})
        self.assertIn("config/enabled-tools.txt: get_daily_status is a write tool", str(cm.exception))

    def test_gate_refuses_what_upstreams_filter_lets_through(self):
        import garmin_mcp

        self.launch.patch()
        self.launch.gate({"get_courses", "get_daily_status"})
        fastmcp = garmin_mcp.FastMCP("test")
        app = garmin_mcp._ToolFilter(fastmcp, set(), set())  # upstream's filter: no limit
        with contextlib.redirect_stderr(io.StringIO()) as err:
            for name in UPSTREAM_MODULES:
                app = getattr(garmin_mcp, name).register_tools(app)
            fastmcp.add_tool(get_daily_status, name="delete_activity")  # the direct path too
        tools = {t.name for t in asyncio.run(fastmcp.list_tools())}
        self.assertEqual(tools, {"get_courses", "get_daily_status"})
        self.assertIn("refused to register tool 'delete_course'", err.getvalue())
        self.assertIn("refused to register tool 'delete_activity'", err.getvalue())


@unittest.skipUnless(HAVE_UPSTREAM, "needs upstream garmin_mcp on mcp 2.x")
class MainServesOnlyReadOnlyTools(UpstreamCase):
    """launch.main() through upstream's main() up to app.run(), with the Garmin
    login and the server loop stubbed out: what a client would be offered."""

    def serve(self, env=None, bypass_upstream_filter=False):
        """(served {name: tool} or None, stderr, exit code or None)."""
        import garmin_mcp
        from mcp.server.mcpserver import MCPServer

        env = dict(env or {})
        launch = load_launch(self, Stubs().modules)
        self.addCleanup(setattr, sys, "stdout", sys.stdout)  # upstream's main() wraps it
        for name in UPSTREAM_MODULES:  # and configures each module's client
            mod = getattr(garmin_mcp, name)
            self.addCleanup(setattr, mod, "garmin_client", getattr(mod, "garmin_client", None))
        served = []
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.dict(
                os.environ, {"GARMIN_MCP_TRANSPORT": "stdio", "GARMIN_MCP_STATELESS_HTTP": "false", **FULL, **env}))
            for key in ("GARMIN_ENABLED_TOOLS", "GARMIN_DISABLED_TOOLS") + KEY_VARS[1:]:
                if key not in env:
                    os.environ.pop(key, None)
            stack.enter_context(mock.patch.object(garmin_mcp._PendingGarminClient, "start", lambda self, login: None))
            stack.enter_context(mock.patch.object(MCPServer, "run", lambda self, *a, **k: served.append(self)))
            if bypass_upstream_filter:  # as if upstream stopped reading GARMIN_ENABLED_TOOLS
                stack.enter_context(mock.patch.object(garmin_mcp, "_resolve_tool_filters", lambda: (set(), set())))
            err = stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
            try:
                launch.main([])
            except SystemExit as exc:
                return None, err.getvalue(), exc.code
        (server,) = served
        self.server = server
        return {t.name: t for t in asyncio.run(server.list_tools())}, err.getvalue(), None

    def assert_read_only(self, tools):
        for name, tool in tools.items():
            self.assertTrue(tool.annotations and tool.annotations.read_only_hint, name)

    def test_unset_env_serves_exactly_the_baked_list(self):
        tools, _, code = self.serve()
        self.assertIsNone(code)
        self.assertEqual(set(tools), set(enabled_tools()))
        self.assert_read_only(tools)

    def test_server_info_names_this_project_not_garmin(self):
        # upstream's main() passes "Garmin Connect v1.0", Garmin's product name.
        _, _, code = self.serve()
        self.assertIsNone(code)
        self.assertEqual(self.server.name, "coach-mcp-for-garmin")
        # serverInfo.version is this project's (ext/version.py), not the mcp SDK's.
        import version

        self.assertEqual(self.server.version, version.VERSION)

    def test_env_narrows_the_list(self):
        tools, _, code = self.serve({"GARMIN_ENABLED_TOOLS": "get_stats,get_daily_status"})
        self.assertIsNone(code)
        self.assertEqual(set(tools), {"get_stats", "get_daily_status"})

    def test_bad_env_stops_the_server_before_it_serves(self):
        for env, needle in (({"GARMIN_ENABLED_TOOLS": "get_stats,delete_activity"}, "delete_activity is a write tool"),
                            ({"GARMIN_ENABLED_TOOLS": "get_stat"}, "get_stat is an unknown tool"),
                            ({"GARMIN_DISABLED_TOOLS": "get_stats"}, "GARMIN_DISABLED_TOOLS is no longer supported")):
            with self.subTest(env=env):
                tools, err, code = self.serve(env)
                self.assertEqual((tools, code), (None, 1))
                self.assertIn(needle, err)

    def test_gate_holds_if_upstreams_filter_lets_everything_through(self):
        tools, err, code = self.serve(bypass_upstream_filter=True)
        self.assertIsNone(code)
        self.assertEqual(set(tools), set(enabled_tools()))
        self.assert_read_only(tools)
        self.assertIn("refused to register tool 'delete_activity'", err)


class CheckConfigCommand(ConfigCommandRunner, unittest.TestCase):
    """`python launch.py --check-config`, as entrypoint.sh runs it before Caddy."""

    def test_bad_sign_in_settings_one_line_each_and_no_secret(self):
        db = os.path.join(temp_dir(self), "auth.sqlite")
        owner_secret = fake_secret(6)  # too short (A5)
        bearer = "TEST ONLY " + secrets.token_urlsafe(24)  # spaces (A8)
        env = sign_in_env(db, REPORT_TZ="Europe/London", PUBLIC_URL="http://example.com/mcp",  # A2 and A3
                          OWNER_SECRET=owner_secret, MCP_BEARER_TOKEN=bearer)
        code, out, err = self.run_command(env)
        self.assertEqual(code, 1)
        self.assertEqual(err.splitlines(), ["launch: config error: " + line for line in (A2, A3, A5, A8)])
        self.assertEqual(out, "")
        self.assert_no_secret(out + err, owner_secret, bearer)
        self.assertFalse(os.path.exists(db), "AUTH_DB is opened only once the settings are valid")

    def test_good_sign_in_settings_pass_and_create_auth_db_0600(self):
        db = os.path.join(temp_dir(self), "data", "auth.sqlite")  # its directory is missing too
        env = sign_in_env(db, REPORT_TZ="Europe/London", MCP_BEARER_TOKEN=fake_secret())
        code, out, err = self.run_command(env)
        self.assertEqual((code, err.splitlines(), out),
                         (0, [f"launch: config OK ({len(set(enabled_tools()))} read-only tools)"], ""))
        self.assertEqual(stat.S_IMODE(os.stat(db).st_mode), 0o600)
        self.assert_no_secret(out + err, env["OWNER_SECRET"], env["MCP_BEARER_TOKEN"])

    def test_good_config_exits_0(self):
        code, lines = self.run_check(REPORT_TZ="Europe/London")
        self.assertEqual((code, lines), (0, [f"launch: config OK ({len(set(enabled_tools()))} read-only tools)"]))

    def test_bad_config_exits_1_with_one_line_per_problem(self):
        code, lines = self.run_check(GARMIN_ENABLED_TOOLS="delete_activity")
        self.assertEqual(code, 1)
        self.assertEqual(len(lines), 2, lines)  # the write tool, then the missing REPORT_TZ
        self.assertTrue(all(line.startswith("launch: config error: ") for line in lines), lines)
        self.assertIn("delete_activity is a write tool", lines[0])
        self.assertIn("REPORT_TZ", lines[1])

    def test_a_newline_in_the_variable_stays_one_line(self):
        # The file's one-per-line list pasted into the variable.
        code, lines = self.run_check(REPORT_TZ="Europe/London", GARMIN_ENABLED_TOOLS="get_stats\nget_daily_status")
        self.assertEqual((code, len(lines)), (1, 1), lines)
        self.assertIn(r"GARMIN_ENABLED_TOOLS: 'get_stats\nget_daily_status' is an unknown tool", lines[0])


# --- Read-only policy, standard library only ---------------------------------
SAMPLE_READ_ONLY = frozenset({"get_stats", "get_activities", "get_user_profile", "get_daily_status"})
SAMPLE_WRITE = frozenset({"delete_activity", "set_fit_download_dir"})
SAMPLE_BAKED = ["get_stats", "get_activities", "get_daily_status"]


class ToolPolicy(unittest.TestCase):
    """launch.resolve_tools(): the rules, on sample tool sets."""

    def setUp(self):
        self.launch = load_launch(self, Stubs().modules)

    def resolve(self, baked=SAMPLE_BAKED, **env):
        return self.launch.resolve_tools(env, baked, SAMPLE_READ_ONLY, SAMPLE_WRITE)

    def assert_refused(self, result, *needles):
        allowed, errors = result
        self.assertEqual(allowed, frozenset(), "any error must allow nothing")
        self.assertEqual(len(errors), len(needles), errors)
        for error, needle in zip(errors, needles):
            self.assertIn(needle, error)

    def test_unset_or_blank_env_means_the_baked_list(self):
        for env in ({}, {"GARMIN_ENABLED_TOOLS": ""}, {"GARMIN_ENABLED_TOOLS": "  "}, {"GARMIN_DISABLED_TOOLS": ""}):
            with self.subTest(env=env):
                self.assertEqual(self.resolve(**env), (frozenset(SAMPLE_BAKED), []))

    def test_env_narrows_case_insensitively(self):
        self.assertEqual(self.resolve(GARMIN_ENABLED_TOOLS=" get_stats , GET_DAILY_STATUS,"),
                         (frozenset({"get_stats", "get_daily_status"}), []))

    def test_write_tool_in_env_is_an_error(self):
        self.assert_refused(self.resolve(GARMIN_ENABLED_TOOLS="get_stats,delete_activity"),
                            "GARMIN_ENABLED_TOOLS: delete_activity is a write tool")

    def test_unknown_name_in_env_is_an_error(self):
        self.assert_refused(self.resolve(GARMIN_ENABLED_TOOLS="get_stat"), "get_stat is an unknown tool")

    def test_env_cannot_add_a_read_only_tool_the_file_leaves_out(self):
        self.assert_refused(self.resolve(GARMIN_ENABLED_TOOLS="get_user_profile"),
                            "get_user_profile is not in config/enabled-tools.txt")

    def test_env_naming_no_tools_is_an_error(self):
        self.assert_refused(self.resolve(GARMIN_ENABLED_TOOLS=" , ,"), "names no tools")

    def test_unprintable_names_are_quoted_on_one_line(self):
        # A newline must not split one problem over two lines, nor an invisible
        # character make a correct-looking name read as unknown.
        for raw, shown in (("get_stats\nget_daily_status", r"'get_stats\nget_daily_status'"),
                           ("get_stats\u200b", r"'get_stats\u200b'")):
            with self.subTest(raw=raw):
                result = self.resolve(GARMIN_ENABLED_TOOLS=raw)
                self.assert_refused(result, f"GARMIN_ENABLED_TOOLS: {shown} is an unknown tool")
                self.assertTrue(result[1][0].isprintable(), result[1])
        self.assert_refused(self.resolve(baked=SAMPLE_BAKED + ["\ufeffget_user_profile"]),
                            r"config/enabled-tools.txt: '\ufeffget_user_profile' is not a known read-only tool")

    def test_an_entry_not_shaped_like_a_tool_name_is_not_shown(self):
        # A secret pasted into the variable by mistake must not reach the
        # deploy log; a real tool-name typo still is shown (above).
        secret = "-".join(["Zq3", "x9F_kP2v", "L8wR4tYh", "7nB1mC6d"])  # built, so no scanner reads it as a real key
        for raw in (secret, f"get_stats,{secret}", f"{secret},0123456789abcdef0123456789abcdef"):
            with self.subTest(raw=raw):
                result = self.resolve(GARMIN_ENABLED_TOOLS=raw)
                self.assert_refused(result, "GARMIN_ENABLED_TOOLS: an entry is not a tool name (it is not shown)")
                self.assertNotIn(secret.lower(), result[1][0])
                self.assertNotIn("0123456789", result[1][0])
        # Mixed case without a dash: lower-cased it has the shape of a tool
        # name, so the upper-case letter alone must hide it (review C1).
        mixed = "_".join(["kP2vL8wR", "tYh7nB1mC6dS0aEq"])
        for raw in (mixed, "Purple_Monkey_Dishwasher_Staple"):
            with self.subTest(raw=raw):
                result = self.resolve(GARMIN_ENABLED_TOOLS=f"get_stats,{raw}")
                self.assert_refused(result, "GARMIN_ENABLED_TOOLS: an entry is not a tool name (it is not shown)")
                self.assertNotIn(raw.lower(), result[1][0])
        # A tool-shaped typo next to it keeps its own line, with the name.
        self.assert_refused(self.resolve(GARMIN_ENABLED_TOOLS=f"get_stat,{secret}"),
                            "get_stat is an unknown tool", "an entry is not a tool name (it is not shown)")

    def test_disabled_tools_is_refused(self):
        self.assert_refused(self.resolve(GARMIN_DISABLED_TOOLS="get_stats"), "GARMIN_DISABLED_TOOLS is no longer supported")

    def test_file_naming_a_write_or_unknown_tool_is_an_error(self):
        self.assert_refused(self.resolve(baked=SAMPLE_BAKED + ["set_fit_download_dir", "get_nothing"]),
                            "get_nothing is not a known read-only tool", "set_fit_download_dir is a write tool")

    def test_empty_file_is_an_error(self):
        self.assert_refused(self.resolve(baked=[]), "lists no tools")

    def test_reads_the_repo_file_as_these_tests_do(self):
        self.assertEqual(self.launch.ENABLED_TOOLS_FILE, os.path.normpath(ENABLED_TOOLS_FILE))
        self.assertEqual(self.launch.read_enabled_tools_file(), enabled_tools())
        with self.assertRaises(self.launch.ConfigError):
            self.launch.read_enabled_tools_file(os.path.join(HERE, "no-such-file.txt"))


class RegistrationGate(unittest.TestCase):
    def test_only_allowed_names_register_on_either_path(self):
        def get_stats():
            return "{}"

        def delete_activity():
            return "{}"

        launch = load_launch(self, Stubs().modules)
        server = launch.read_only_server_class(FakeServer, {"get_stats"})()
        with contextlib.redirect_stderr(io.StringIO()) as err:
            server.tool()(get_stats)
            server.tool()(delete_activity)
            server.add_tool(delete_activity)
            server.tool(name="delete_activity")(get_stats)  # the registered name decides
            server.add_tool(delete_activity, name="get_stats")
        self.assertEqual(server.added, ["get_stats", "get_stats"])
        self.assertEqual(err.getvalue().count("refused to register tool 'delete_activity'"), 3)


class CheckConfig(unittest.TestCase):
    """--check-config's output and exit code, with the tool policy stubbed."""

    def setUp(self):
        self.launch = load_launch(self, Stubs().modules)

    def run_cli(self, env, side_effect=None):
        env = {key: value for key, value in {**FULL, **env}.items() if value is not None}
        with mock.patch.object(self.launch, "allowed_tools", return_value=frozenset({"get_stats"}),
                               side_effect=side_effect), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            code = self.launch._check_config_cli(env)
        return code, err.getvalue().splitlines()

    def test_ok(self):
        self.assertEqual(self.run_cli({"REPORT_TZ": "Europe/London"}), (0, ["launch: config OK (1 read-only tools)"]))

    def test_report_tz_is_required_and_must_be_a_real_zone(self):
        for env in ({}, {"REPORT_TZ": " "}, {"REPORT_TZ": "Mars/Olympus_Mons"}):
            with self.subTest(env=env):
                code, lines = self.run_cli(env)
                self.assertEqual((code, len(lines)), (1, 1), lines)
                self.assertTrue(lines[0].startswith("launch: config error: REPORT_TZ"), lines)

    def test_every_problem_gets_its_own_line(self):
        code, lines = self.run_cli({}, side_effect=self.launch.ConfigError(["first", "second"]))
        self.assertEqual(code, 1)
        self.assertEqual(lines[:2], ["launch: config error: first", "launch: config error: second"])
        self.assertEqual(len(lines), 3)  # and the REPORT_TZ one

    def test_a_check_that_cannot_run_fails_closed_in_one_line(self):
        code, lines = self.run_cli({"REPORT_TZ": "Europe/London"}, side_effect=RuntimeError("no upstream"))
        self.assertEqual((code, lines), (1, ["launch: config error: the check could not run: RuntimeError: no upstream"]))


class ConfigFile(unittest.TestCase):
    """Standard library only: runs without upstream too."""

    def test_extra_tools_listed_once(self):
        names = enabled_tools()
        self.assertEqual(len(names), len(set(names)), "duplicate names")
        self.assertLessEqual(READ_ONLY, set(names))
        self.assertEqual(REMOVED & set(names), set())

    def test_launch_annotates_exactly_the_extra_tools(self):
        launch = load_launch(self, Stubs().modules)
        self.assertEqual(set(launch.EXTRA_READ_ONLY_TOOLS), READ_ONLY)
        self.assertFalse(hasattr(launch, "EXTRA_ADDITIVE_TOOLS"))

    def test_garmin_token_files_stay_out_of_the_build_context(self):
        with open(DOCKERIGNORE_FILE, encoding="utf-8") as f:
            patterns = [line.strip() for line in f if line.strip() and not line.startswith("#")]
        for path in ("garmin.token", "ext/garmin.token", "config/a/b.token", "garminconnect_base64",
                     "ext/garminconnect_base64.txt", "scripts/old.garminconnect_base64.bak",
                     "tokens/oauth1_token.json", "garmin_tokens.json"):
            with self.subTest(path=path):
                self.assertTrue(any(docker_ignores(p, path) for p in patterns), path)
        self.assertFalse(any(docker_ignores(p, "ext/daily_status.py") for p in patterns))

    def test_image_bakes_the_tool_list_where_launch_reads_it(self):
        text = read(DOCKERFILE_FILE)
        copies = {tuple(line.split()[1:]) for line in text.splitlines() if line.startswith("COPY ")}
        self.assertIn(("ext/", "/opt/garmin-ext/"), copies)
        # launch.py reads ../config/enabled-tools.txt relative to itself.
        baked = os.path.normpath(os.path.join("/opt/garmin-ext", os.pardir, "config", "enabled-tools.txt"))
        self.assertIn(("config/enabled-tools.txt", baked), copies)
        # The build check validates the baked list, so the file must be there first.
        self.assertLess(text.index("COPY config/enabled-tools.txt"), text.index("launch.gate(launch.allowed_tools({}))"))
        self.assertIn("launch.patch()", text)
        # The build check, a command not a comment, also builds a throwaway
        # sign-in on the installed mcp.
        (check,) = [line for line in text.splitlines()
                    if "launch.gate(launch.allowed_tools({}))" in line and not line.lstrip().startswith("#")]
        self.assertIn("launch.patch()", check)
        self.assertIn("owner_auth.selfcheck()", check)
        # Key mode's build check and the version it prints.
        self.assertIn("key_mode.selfcheck()", check)
        self.assertIn("print('coach-mcp-for-garmin', version.VERSION)", check)
        with open(DOCKERIGNORE_FILE, encoding="utf-8") as f:
            patterns = [line.strip() for line in f if line.strip() and not line.startswith("#")]
        self.assertFalse(any(docker_ignores(p, "config/enabled-tools.txt") for p in patterns))
        for name in SIGN_IN_MODULES:
            with self.subTest(module=name):
                self.assertTrue(os.path.exists(os.path.join(HERE, name)), name)
                self.assertFalse(any(docker_ignores(p, "ext/" + name) for p in patterns), name)

    def test_image_ships_the_tool_error_modules(self):
        # Step 1.3's modules and the modules launch.py was split into ship through COPY ext/ (no Dockerfile
        # change). launch.py imports each part at the top, and each step 1.3 module is imported at the top of
        # launch.py or of one of its parts, so the Dockerfile's `import launch` fails the build without it.
        with open(DOCKERIGNORE_FILE, encoding="utf-8") as f:
            patterns = [line.strip() for line in f if line.strip() and not line.startswith("#")]
        launch_source = read(os.path.join(HERE, "launch.py"))
        for name in LAUNCH_PARTS:
            with self.subTest(module=name):
                self.assertTrue(os.path.exists(os.path.join(HERE, name)), name)
                self.assertFalse(any(docker_ignores(p, "ext/" + name) for p in patterns), name)
                self.assertRegex(launch_source, rf"(?m)^import {name[:-3]}\b")
        sources = launch_source + "".join(read(os.path.join(HERE, name)) for name in LAUNCH_PARTS)
        for name in TOOL_ERROR_MODULES:
            with self.subTest(module=name):
                self.assertTrue(os.path.exists(os.path.join(HERE, name)), name)
                self.assertFalse(any(docker_ignores(p, "ext/" + name) for p in patterns), name)
                self.assertRegex(sources, rf"(?m)^import {name[:-3]}\b")

    def test_entrypoint_checks_config_before_anything_starts(self):
        code = [line for line in read(ENTRYPOINT_FILE).splitlines() if not line.lstrip().startswith("#")]

        def first(needle):
            return next(i for i, line in enumerate(code) if needle in line)

        check = first("launch.py --check-config")
        self.assertLess(check, first("while :"), "must run before the supervisor loop, which restarts forever")
        self.assertLess(check, first("caddy run"))
        self.assertRegex(code[check] + code[check + 1], r"\|\|\s*die ")

    def test_entrypoint_gives_the_secrets_to_python_and_none_to_caddy(self):
        text = read(ENTRYPOINT_FILE)
        code = [line.strip() for line in text.splitlines() if not line.lstrip().startswith("#")]
        # launch.py does the sign-in: neither of its two runs drops a secret.
        self.assertNotIn("env -u MCP_BEARER_TOKEN python", text)
        runs = [line for line in code if "/opt/garmin-ext/launch.py" in line]
        self.assertEqual(len(runs), 2, runs)  # --check-config, then the server
        for line in runs:
            self.assertRegex(line, r"^\(?run_as app python /opt/garmin-ext/launch\.py\b")
        # Caddy, the edge, starts without either secret, and env keeps it PID 1.
        (caddy,) = [line for line in code if "caddy run" in line]
        self.assertTrue(caddy.startswith("exec env "), caddy)
        words = caddy.split("caddy run", 1)[0].split()
        unset = {words[i + 1] for i, word in enumerate(words[:-1]) if word == "-u"}
        self.assertLessEqual({"OWNER_SECRET", "MCP_BEARER_TOKEN", "GARMIN_PERSONAL_API_KEY"}, unset)
        # EDGE_CLIENT_IP_HEADER reaches Caddy (the Caddyfile reads it).
        self.assertNotIn("EDGE_CLIENT_IP_HEADER", unset)
        # MCP_BEARER_TOKEN is optional now; launch.py --check-config checks it.
        self.assertNotIn("MCP_BEARER_TOKEN is not set", text)

    def test_entrypoint_warns_once_about_what_a_missing_volume_loses(self):
        # The /data block of entrypoint.sh, run by sh with a stand-in mount
        # table and log(), for each place GARMINTOKENS and AUTH_DB can point.
        text = read(ENTRYPOINT_FILE)
        mounts = os.path.join(temp_dir(self), "mounts")
        # Key mode keeps only a 24-hour access token on /data: its Garmin tokens are never "lost".
        block = text[text.index('lost=""'):text.index('has_tokens() {')].replace("/proc/mounts", mounts)
        warning = "WARNING: no volume is mounted at /data; {} will be lost on the next deploy.\n"
        cases = (
            ("no", {"GARMINTOKENS": "/data/tokens"}, "Garmin tokens and sign-ins"),
            ("no", {"GARMINTOKENS": "/data/tokens", "AUTH_DB": "/data/auth.sqlite"}, "Garmin tokens and sign-ins"),
            ("no", {"GARMINTOKENS": "/data/tokens", "AUTH_DB": "/srv/auth.sqlite"}, "Garmin tokens"),
            ("no", {"GARMINTOKENS": "/srv/tokens"}, "sign-ins"),
            ("no", {"GARMINTOKENS": "/srv/tokens", "AUTH_DB": "/srv/auth.sqlite"}, None),
            ("yes", {"GARMINTOKENS": "/data/tokens"}, "sign-ins"),
            ("yes", {"GARMINTOKENS": "/data/tokens", "AUTH_DB": "/srv/auth.sqlite"}, None),
        )
        for mounted in (False, True):
            with open(mounts, "w", encoding="utf-8") as f:
                f.write("overlay / overlay rw 0 0\n" + ("/dev/vda1 /data ext4 rw 0 0\n" if mounted else ""))
            for key_mode, env, lost in cases:
                script = f'set -eu\nlog() {{ echo "$*"; }}\nkey_mode={key_mode}\n' + block
                with self.subTest(mounted=mounted, key_mode=key_mode, env=env):
                    proc = subprocess.run(["sh", "-c", script], env={"PATH": os.environ.get("PATH", ""), **env},
                                          stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
                    self.assertEqual(proc.returncode, 0, proc.stderr)
                    self.assertEqual(proc.stdout, warning.format(lost) if lost and not mounted else "")

    def test_no_deny_list_and_report_tz_is_required(self):
        env_example = read(ENV_EXAMPLE_FILE)
        self.assertNotIn("GARMIN_DISABLED_TOOLS", env_example)
        # Empty on purpose: the server stops with "REPORT_TZ is not set" until it is filled in.
        self.assertRegex(env_example, r"(?m)^REPORT_TZ=$")
        self.assertRegex(env_example, r"(?m)^GARMIN_AUTH=$")
        self.assertRegex(env_example, r"(?m)^# GARMIN_PERSONAL_API_KEY=$")
        self.assertNotIn("GARMIN_KEY_TOKEN_CACHE", env_example)
        # deploy.sh no longer sets any tool list: the image carries it.
        self.assertNotRegex(read(DEPLOY_FILE), r"variable set[^\n]*GARMIN_(DISABLED|ENABLED)_TOOLS")


LOGIN_FAILED = "Garmin login failed. Run 'garmin-mcp-auth' to authenticate, then restart the server."


def failing_sync(date: str = "") -> str:
    """Raises like upstream's proxy does before login."""
    raise RuntimeError(LOGIN_FAILED)


async def failing_async(date: str = "") -> str:
    """Raises like upstream's proxy does before login."""
    raise RuntimeError(LOGIN_FAILED)


def deliberate(date: str = "") -> str:
    """Raises ToolError itself."""
    from mcp.server.mcpserver.exceptions import ToolError

    raise ToolError("bad date")


@unittest.skipUnless(HAVE_UPSTREAM, "needs upstream garmin_mcp on mcp 2.x")
class ToolErrorText(unittest.TestCase):
    """mcp 2.x alone answers a tool exception with a bare "Error executing tool
    <name>"; the adapter keeps the message, as mcp 1.x did."""

    def call(self, fn):
        import garmin_mcp
        from mcp import Client
        from mcp.server.fastmcp import FastMCP

        fastmcp = FastMCP("test")
        app = garmin_mcp._ToolFilter(fastmcp, set(), set())
        app.tool()(fn)

        async def run():
            async with Client(fastmcp) as client:
                return await client.call_tool(fn.__name__, {"date": "2026-11-12"})

        logging.disable(logging.ERROR)  # the SDK logs each failed call at INFO, the adapter at DEBUG
        self.addCleanup(logging.disable, logging.NOTSET)
        result = asyncio.run(run())
        self.assertTrue(result.is_error)
        return result.content[0].text

    def test_sync_tool_keeps_message(self):
        self.assertEqual(self.call(failing_sync), f"Error executing tool failing_sync: {LOGIN_FAILED}")

    def test_async_tool_keeps_message(self):
        self.assertEqual(self.call(failing_async), f"Error executing tool failing_async: {LOGIN_FAILED}")

    def test_tool_error_unchanged(self):
        self.assertEqual(self.call(deliberate), "Error executing tool deliberate: bad date")

    def test_wrapping_keeps_name_schema_and_function(self):
        from mcp.server.fastmcp import FastMCP
        from mcp.server.mcpserver import MCPServer

        wrapped, plain = FastMCP("a"), MCPServer("b")
        self.assertIs(wrapped.tool()(failing_async), failing_async)
        plain.tool()(failing_async)
        (w,), (p,) = asyncio.run(wrapped.list_tools()), asyncio.run(plain.list_tools())
        self.assertEqual(w.model_dump(), p.model_dump())
# Upstream range tools that catch each day's failure and answer "No ... found"
# as a normal result. Pinned, so the gap stays visible until it is closed.
HIDDEN_FAILURES = {
    "get_hrv_trend": "No HRV data found between ",
    "get_sleep_summary_range": "No sleep data found between ",
    "get_training_load_trend": "No training load data found between ",
}
# get_vo2max_trend hides a failed Garmin call the same way, but not a failed
# login: it looks each client method up with getattr(client, name, None),
# which raises upstream's login error before any call.
HIDDEN_GARMIN_ERRORS = dict(HIDDEN_FAILURES, get_vo2max_trend="No VO2 max data found between ")
# A failed first Garmin call ends these two reports with an error.
STATUS_TOOLS = ("get_daily_status", "get_weekly_status")
INTERNAL_TEXT_RE = re.compile(r"Error: Internal server error \(ref ([0-9a-f]{8})\)\. .+")
HOOK_ERROR = ("launch: mcp's MCPServer no longer sends tools/call through call_tool(name, arguments, context); "
              "the tool-error wrapper in read_only_server_class() needs a new hook")
# The tool-error modules: launch.py imports them, so the image must ship them.
TOOL_ERROR_MODULES = ("text_hygiene.py", "tool_errors.py", "server_instructions.py")
# The modules launch.py was split into (it re-exports their names); the image needs all of them.
LAUNCH_PARTS = ("garmin_paths.py", "tool_answers.py", "feature_startup.py", "launch_logging.py")

# Failure texts as upstream answers them.
NOT_FOUND_TEXT = ("Error retrieving activity 14295551: Garmin Connect request failed: HTTP error: API Error 404 - "
                  "Not Found. Garmin Connect may be unreachable; check your network connection or try again later.")
LIMIT_TEXT = "Error retrieving activities: limit cannot exceed 1000"
STALL_TEXT = ("Error retrieving stats: Garmin request 'get_stats' did not return within 90s and was abandoned. This "
              "is usually a transient stall on Garmin's side. (Adjust with GARMIN_MCP_CALL_TIMEOUT.)")
DATE_TEXT = "Error: date must be YYYY-MM-DD, got '2026-02-30'"
BOOM = "boom https://example.invalid/u/MyName"
ODD = "Something odd happened at https://example.invalid/u/MyName"
SECOND_BLOCK = json.dumps({"activityName": "Easy" + chr(0x202E) + " ride"})


async def raises_boom() -> str:
    """Crashes with a message that holds a URL and a display name."""
    raise RuntimeError(BOOM)


async def typed(activity_id: int) -> str:
    """Takes a whole-number id."""
    return str(activity_id)


async def protocol_error() -> str:
    """Raises the SDK's protocol error."""
    from mcp.shared.exceptions import MCPError

    raise MCPError(-32602, "a protocol error")


def returns_error_result():
    """Answers a CallToolResult already flagged as an error."""
    from mcp_types import CallToolResult, TextContent

    return CallToolResult(content=[TextContent(type="text", text=ODD)], is_error=True)


def two_blocks():
    """Answers two text blocks that both need cleaning, and one that is no text."""
    from mcp_types import CallToolResult, ImageContent, TextContent

    return CallToolResult(content=[TextContent(type="text", text=RAW_ACTIVITY),
                                   ImageContent(type="image", data="AAAA", mime_type="image/png"),
                                   TextContent(type="text", text=SECOND_BLOCK)])


def required_arguments(tool):
    """Values for a listed tool's required arguments (R1b probe 7's args_for)."""
    arguments = {}
    for name in tool.input_schema.get("required", []):
        if (tool.input_schema["properties"][name].get("type") or "string") == "integer":
            arguments[name] = 1
        else:
            arguments[name] = "2026-11-01" if "start" in name else "2026-11-10" if "date" in name else "1"
    return arguments


def exception_chain(exc):
    """exc, then what it was raised from or while handling."""
    chain = []
    while exc is not None and all(exc is not seen for seen in chain):
        chain.append(exc)
        exc = exc.__cause__ or exc.__context__
    return chain


@contextlib.contextmanager
def caught_by_the_override():
    """The exceptions ReadOnlyFastMCP.call_tool() caught, in order: it hands
    each one to tool_errors.classify_exception(), which still runs."""
    caught, classify = [], tool_errors.classify_exception

    def recording(exc):
        caught.append(exc)
        return classify(exc)

    with mock.patch.object(tool_errors, "classify_exception", recording):
        yield caught


def today_in(zone_name):
    """Today in the zone `zone_name` (UTC for None), as YYYY-MM-DD."""
    zone = zoneinfo.ZoneInfo(zone_name) if zone_name else datetime.timezone.utc
    return datetime.datetime.now(zone).date().isoformat()


@unittest.skipUnless(HAVE_UPSTREAM, "needs mcp 2.x and upstream garmin_mcp")
class ToolCallOverride(unittest.TestCase):
    """ReadOnlyFastMCP.call_tool() on a small server built over the adapter's
    FastMCP, with test tools, called through the official client: each kind
    of failure gets its group's fixed text and one log line, and upstream
    answers are cleaned off the event loop."""

    def setUp(self):
        self.launch = load_launch(self, Stubs().modules)
        FastMCP = mcp2_compat.install().FastMCP
        ours = sorted(self.launch.EXTRA_READ_ONLY_TOOLS)
        allowed = {"answers", "structured", "raises_boom", "typed", "protocol_error", "returns_error_result",
                   "failing_async", "get_activity", "get_sleep_summary", "two_blocks", *ours}
        self.server = self.launch.read_only_server_class(FastMCP, allowed)("test")
        tool = self.server.tool
        tool(name="get_sleep_summary", structured_output=False)(answers)
        # As upstream registers its tools: no output schema.
        for fn in (answers, raises_boom, typed, protocol_error, returns_error_result, failing_async, two_blocks):
            tool(structured_output=False)(fn)
        tool(name="structured", structured_output=True)(answers)
        for name in ("get_activity", *ours):
            tool(name=name, structured_output=False)(upstream_answer)
        quiet(self, "mcp", "garmin_ext")

    def call(self, mode, calls):
        return call_tools(self.server, mode, calls)

    def test_returned_failure_texts_get_the_fixed_text_of_their_group(self):
        cases = ((NOT_FOUND_TEXT, tool_errors.NO_DATA, None, logging.INFO),
                 (LOGIN_TEXT, tool_errors.LOGIN, None, logging.WARNING),
                 (RATE_TEXT, tool_errors.RATE_LIMITED, None, logging.WARNING),
                 (LIMIT_TEXT, tool_errors.BAD_INPUT, "limit is too large: at most 1000", logging.INFO),
                 (STALL_TEXT, tool_errors.UNAVAILABLE, None, logging.WARNING))
        for mode in ERAS:
            with captured_records() as records:
                results = self.call(mode, [("answers", {"text": text}) for text, _, _, _ in cases])
            lines = tool_error_records(records)
            self.assertEqual(len(lines), len(cases))
            for (text, group, detail, level), result, record in zip(cases, results, lines):
                with self.subTest(mode=mode, group=group):
                    self.assertEqual(result.is_error, group != tool_errors.NO_DATA)
                    self.assertEqual([block.text for block in result.content],
                                     [tool_errors.client_text(group, detail=detail)])
                    self.assertIsNone(result.structured_content)
                    self.assertEqual(record.getMessage(),
                                     f"tool error: tool=answers group={group}: {tool_errors.redact(text)}")
                    self.assertEqual(record.levelno, level)
                    self.assertIsNone(record.exc_info)
            self.assertFalse(any("rate limit:" in r.getMessage() for r in records), mode)

    def test_a_crash_is_an_internal_error_with_a_ref_in_the_text_and_the_log(self):
        for mode in ERAS:
            with caught_by_the_override() as caught, captured_records() as records:
                (result,) = self.call(mode, [("raises_boom", {})])
            with self.subTest(mode=mode):
                self.assertTrue(result.is_error)
                (text,) = [block.text for block in result.content]
                ref = INTERNAL_TEXT_RE.fullmatch(text).group(1)
                self.assertEqual(text, tool_errors.client_text(tool_errors.INTERNAL, ref=ref))
                for leak in ("boom", "example.invalid", "MyName", "RuntimeError", "Traceback", "Error executing"):
                    self.assertNotIn(leak, text)
                (record,) = tool_error_records(records)
                self.assertEqual(record.levelno, logging.ERROR)
                message = record.getMessage()
                self.assertTrue(message.startswith(f"tool error: ref={ref} tool=raises_boom group=internal: "), message)
                self.assertIn("RuntimeError: " + BOOM, message)
                # The record carries the exception the override caught (the SDK's ToolError around the
                # crash), not the root of its chain; logging prints the whole chain from there.
                (exc,) = caught
                self.assertIs(record.exc_info[1], exc)
                self.assertEqual(type(exc).__name__, "ToolError")
                self.assertTrue(any(isinstance(e, RuntimeError) and str(e) == BOOM for e in exception_chain(exc)))
                trace = logging.Formatter().formatException(record.exc_info)
                self.assertIn("ToolError: Error executing tool raises_boom: " + BOOM, trace)
                self.assertIn("RuntimeError: " + BOOM, trace)
                self.assertEqual([r for r in records if r.levelno >= logging.ERROR], [record])

    def test_a_returned_error_result_is_answered_with_a_fixed_text(self):
        with captured_records() as records:
            (result,) = self.call("legacy", [("returns_error_result", {})])
        (text,) = [block.text for block in result.content]
        ref = INTERNAL_TEXT_RE.fullmatch(text).group(1)
        self.assertTrue(result.is_error)
        self.assertNotIn("example.invalid", text)
        (record,) = tool_error_records(records)
        self.assertEqual(record.getMessage(), f"tool error: ref={ref} tool=returns_error_result group=internal: {ODD}")
        self.assertIsNone(record.exc_info)  # nothing was raised

    def test_invalid_arguments_name_the_field(self):
        for mode in ERAS:
            with captured_records() as records:
                (result,) = self.call(mode, [("typed", {"activity_id": "abc"})])
            with self.subTest(mode=mode):
                self.assertTrue(result.is_error)
                self.assertEqual([block.text for block in result.content],
                                 ["Error: Invalid input: activity_id must be a whole number. "
                                  "Fix the arguments and call the tool again."])
                (record,) = tool_error_records(records)
                self.assertEqual((record.levelno, LINE_RE.match(record.getMessage()).groups()),
                                 (logging.INFO, ("typed", "bad_input")))

    def test_an_unknown_tool_is_invalid_input(self):
        for mode in ERAS:
            with captured_records() as records:
                (result,) = self.call(mode, [("no_such_tool", {})])
            with self.subTest(mode=mode):
                self.assertTrue(result.is_error)
                unknown = tool_errors.client_text(tool_errors.BAD_INPUT, detail=tool_errors.DETAIL_UNKNOWN_TOOL)
                self.assertEqual([block.text for block in result.content], [unknown])
                (record,) = tool_error_records(records)
                self.assertEqual(LINE_RE.match(record.getMessage()).groups(), ("no_such_tool", "bad_input"))

    def test_the_log_line_is_redacted_and_never_holds_rate_limit_colon(self):
        # garminconnect's own 429 wording ("Web proxy rate limit: ...", GC/__init__.py:374) with a bearer
        # token and an email address: the line carries redact()'s text, never the original.
        text = ("Error retrieving stats: Garmin rate limit hit: Web proxy rate limit: Bearer " + fake_secret()
                + " for a@b.example")
        with captured_records() as records:
            (result,) = self.call("legacy", [("answers", {"text": text})])
        self.assertTrue(result.is_error)
        self.assertEqual([block.text for block in result.content], [tool_errors.client_text(tool_errors.RATE_LIMITED)])
        (record,) = tool_error_records(records)
        message = record.getMessage()
        self.assertEqual(message, f"tool error: tool=answers group=rate_limited: {tool_errors.redact(text)}")
        self.assertNotEqual(tool_errors.redact(text), text)
        for kept in ("rate limit -", "Bearer <redacted>", "<email>"):
            self.assertIn(kept, message)
        for leak in ("rate limit:", "TEST-ONLY-", "a@b.example"):
            self.assertNotIn(leak, message)
        self.assertEqual([r.getMessage() for r in records if "rate limit:" in r.getMessage()], [])

    def test_an_unknown_tool_name_is_logged_on_one_line(self):
        # The name is caller text: its newline and bidi override never reach the log line.
        name = "no_such_tool" + chr(0x202E) + "\nx"
        for mode in ERAS:
            with captured_records() as records:
                (result,) = self.call(mode, [(name, {})])
            with self.subTest(mode=mode):
                self.assertTrue(result.is_error)
                self.assertEqual([block.text for block in result.content],
                                 [tool_errors.client_text(tool_errors.BAD_INPUT, detail=tool_errors.DETAIL_UNKNOWN_TOOL)])
                (record,) = tool_error_records(records)
                message = record.getMessage()
                self.assertTrue(message.startswith("tool error: tool=no_such_tool x group=bad_input: "), repr(message))
                self.assertNotIn("\n", message)
                self.assertNotIn(chr(0x202E), message)

    def test_a_bad_typed_value_is_logged_by_field_name_only(self):
        # The pydantic error's own text holds input_value=<the value>; the log line holds
        # "ValidationError: <field> must be ...", never the value.
        value = fake_secret()
        for mode in ERAS:
            with captured_records() as records:
                (result,) = self.call(mode, [("typed", {"activity_id": value})])
            with self.subTest(mode=mode):
                self.assertTrue(result.is_error)
                (record,) = tool_error_records(records)
                self.assertEqual(record.getMessage(), "tool error: tool=typed group=bad_input: "
                                                      "ValidationError: activity_id must be a whole number")
                self.assertEqual([r.getMessage() for r in records if value in r.getMessage()], [])
                self.assertNotIn(value, result.content[0].text)

    def test_a_huge_unknown_tool_name_answers_fast_and_logs_a_short_name(self):
        # redact() used to run in quadratic time on a long name, on the event loop. A 1 MB name now
        # answers at once, and the log keeps at most 64 characters of it.
        name = "a" * (1 << 20)
        started = time.monotonic()
        with captured_records() as records:
            (result,) = self.call("legacy", [(name, {})])
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 5.0)  # was hours before the fix; under 0.1 s here, with room for a busy machine
        self.assertTrue(result.is_error)
        self.assertEqual([block.text for block in result.content],
                         [tool_errors.client_text(tool_errors.BAD_INPUT, detail=tool_errors.DETAIL_UNKNOWN_TOOL)])
        (record,) = tool_error_records(records)
        message = record.getMessage()
        tool = message[len("tool error: tool="):message.index(" group=bad_input: ")]
        self.assertTrue(message.startswith("tool error: tool="), message[:80])
        self.assertLessEqual(len(tool), 65)
        self.assertTrue(tool.endswith(text_hygiene.ELLIPSIS), tool)

    def test_raised_failures_that_are_not_internal_log_no_traceback(self):
        # Only an internal error's line carries exc_info. These three are raised, so the
        # override has an exception in hand: the login error as upstream's proxy raises it, invalid
        # arguments and an unknown tool.
        for mode in ERAS:
            with captured_records() as records:
                results = self.call(mode, [("failing_async", {}), ("typed", {"activity_id": "abc"}),
                                           ("no_such_tool", {})])
            with self.subTest(mode=mode):
                self.assertEqual([result.is_error for result in results], [True, True, True])
                self.assertEqual([result.content[0].text for result in results],
                                 [tool_errors.client_text(tool_errors.LOGIN),
                                  tool_errors.client_text(tool_errors.BAD_INPUT,
                                                          detail="activity_id must be a whole number"),
                                  tool_errors.client_text(tool_errors.BAD_INPUT, detail=tool_errors.DETAIL_UNKNOWN_TOOL)])
                lines = tool_error_records(records)
                self.assertEqual([(r.levelno, LINE_RE.match(r.getMessage()).groups()) for r in lines],
                                 [(logging.WARNING, ("failing_async", "login")), (logging.INFO, ("typed", "bad_input")),
                                  (logging.INFO, ("no_such_tool", "bad_input"))])
                self.assertEqual([r.exc_info for r in lines], [None, None, None])

    def test_a_protocol_error_stays_a_protocol_error(self):
        from mcp.shared.exceptions import MCPError

        for mode in ERAS:
            with self.subTest(mode=mode), captured_records() as records:
                (result,) = self.call(mode, [("protocol_error", {})])
                self.assertIsInstance(result, MCPError)
                self.assertEqual((result.code, result.message), (-32602, "a protocol error"))
                self.assertEqual(tool_error_records(records), [])

    def test_a_bug_in_the_wrapper_is_an_internal_error(self):
        bug = RuntimeError("classifier bug")
        with mock.patch.object(tool_errors, "classify_text", side_effect=bug), captured_records() as records:
            (result,) = self.call("legacy", [("answers", {"text": LOGIN_TEXT})])
        (text,) = [block.text for block in result.content]
        ref = INTERNAL_TEXT_RE.fullmatch(text).group(1)
        self.assertTrue(result.is_error)
        self.assertNotIn("classifier bug", text)
        (record,) = tool_error_records(records)
        self.assertEqual(record.levelno, logging.ERROR)
        self.assertEqual(record.getMessage(), f"tool error: ref={ref} tool=answers group=internal: "
                                              "tool-error wrapper failed: RuntimeError: classifier bug")
        self.assertIs(record.exc_info[1], bug)

    def test_upstream_answers_are_cleaned_in_a_worker_thread(self):
        seen = []
        clean = text_hygiene.clean_tool_answer

        def recording(text):
            seen.append(threading.current_thread().name)
            return clean(text)

        for mode in ERAS:
            seen.clear()
            with mock.patch.object(text_hygiene, "clean_tool_answer", recording):
                (result,) = self.call(mode, [("get_activity", {})])
            with self.subTest(mode=mode):
                self.assertEqual(len(seen), 1)
                self.assertNotEqual(seen[0], "MainThread")
                self.assertNotEqual(seen[0], threading.current_thread().name)  # the event loop's thread
                self.assertFalse(result.is_error)
                (text,) = [block.text for block in result.content]
                self.assertEqual(text, clean(RAW_ACTIVITY))
                data = json.loads(text)
                self.assertEqual(data["activityName"], "Tempo run")
                self.assertNotIn("\n", data["description"])
                # Numbers, ids, timestamps and the URL are untouched; indent=2 is kept.
                self.assertEqual({k: v for k, v in data.items() if k not in ("activityName", "description")},
                                 {k: v for k, v in ACTIVITY.items() if k not in ("activityName", "description")})
                self.assertTrue(text.startswith('{\n  "activityId": 14295551,'), text[:40])

    def test_every_text_block_is_cleaned(self):
        for mode in ERAS:
            (result,) = self.call(mode, [("two_blocks", {})])
            with self.subTest(mode=mode):
                self.assertFalse(result.is_error)
                first, image, second = result.content
                self.assertEqual(first.text, text_hygiene.clean_tool_answer(RAW_ACTIVITY))
                self.assertEqual((image.type, image.data), ("image", "AAAA"))
                self.assertEqual(second.text, text_hygiene.clean_tool_answer(SECOND_BLOCK))
                self.assertNotIn(chr(0x202E), second.text)

    def test_a_clean_answer_comes_back_as_is(self):
        (result,) = self.call("legacy", [("answers", {"text": "No activities found at index 0"})])
        self.assertFalse(result.is_error)
        self.assertEqual([block.text for block in result.content], ["No activities found at index 0"])

    def test_our_three_tools_are_never_cleaned(self):
        calls = []

        def recording(text):
            calls.append(text)
            return text_hygiene.ELLIPSIS

        names = sorted(self.launch.EXTRA_READ_ONLY_TOOLS)
        with mock.patch.object(text_hygiene, "clean_tool_answer", recording):
            results = self.call("legacy", [(name, {}) for name in names] + [("get_activity", {})])
        self.assertEqual(calls, [RAW_ACTIVITY])  # only the upstream-named tool
        for name, result in zip(names, results):
            with self.subTest(tool=name):
                self.assertFalse(result.is_error)
                self.assertEqual([block.text for block in result.content], [RAW_ACTIVITY])

    def test_an_empty_sleep_summary_is_answered_as_no_data(self):
        """Live test B3: a night that had not synced yet came back as an empty summary, with no words at all."""
        all_null = json.dumps({"sleep_seconds": None, "sleep_score": None, "sleep_start": None}, indent=2)
        night = json.dumps({"sleep_seconds": 25920, "sleep_score": None})
        for mode in ERAS:
            with captured_records() as records:
                results = self.call(mode, [("get_sleep_summary", {"text": "{}"}),
                                           ("get_sleep_summary", {"text": all_null}),
                                           ("get_sleep_summary", {"text": night}),
                                           ("get_sleep_summary", {"text": "No sleep summary found for 2026-11-18"}),
                                           ("answers", {"text": "{}"})])
            empty, nulls, real, upstream_words, other_tool = results
            for result in (empty, nulls):
                with self.subTest(mode=mode):
                    self.assertFalse(result.is_error)
                    self.assertEqual([block.text for block in result.content], [tool_errors.NO_SLEEP_TEXT])
            self.assertEqual([block.text for block in real.content], [night])
            self.assertEqual([block.text for block in upstream_words.content],
                             ["No sleep summary found for 2026-11-18"])
            self.assertEqual([block.text for block in other_tool.content], ["{}"])  # only this one tool
            lines = tool_error_records(records)
            self.assertEqual([record.getMessage() for record in lines],
                             ["tool error: tool=get_sleep_summary group=no_data: " + tool_errors.NO_SLEEP_LOG] * 2)
            self.assertEqual({record.levelno for record in lines}, {logging.INFO})

    def test_a_structured_tool_keeps_its_result_form(self):
        # No tool this server serves has an output schema (ServedTexts pins it);
        # the override still keeps the form a structured tool answers in.
        results = self.call("legacy", [("structured", {"text": RAW_ACTIVITY}),
                                       ("structured", {"text": NOT_FOUND_TEXT}),
                                       ("structured", {"text": LOGIN_TEXT})])
        cleaned, no_data, login = results
        self.assertEqual(cleaned.structured_content, {"result": cleaned.content[0].text})
        self.assertEqual(cleaned.content[0].text, text_hygiene.clean_tool_answer(RAW_ACTIVITY))
        self.assertFalse(no_data.is_error)
        self.assertEqual(no_data.structured_content, {"result": tool_errors.client_text(tool_errors.NO_DATA)})
        self.assertEqual([block.text for block in no_data.content], [tool_errors.client_text(tool_errors.NO_DATA)])
        self.assertTrue(login.is_error)
        self.assertIsNone(login.structured_content)
        self.assertEqual([block.text for block in login.content], [tool_errors.client_text(tool_errors.LOGIN)])

    def test_the_date_detail_gives_today_in_report_tz(self):
        # Kiritimati (UTC+14) and Pago Pago (UTC-11) are 25 hours apart, so
        # their dates always differ: only the zone's own date can pass both.
        for setting, zone in (("Pacific/Kiritimati", "Pacific/Kiritimati"), ("Pacific/Pago_Pago", "Pacific/Pago_Pago"),
                              (None, None), ("Mars/Olympus_Mons", None)):
            with self.subTest(REPORT_TZ=setting), mock.patch.dict(os.environ):
                os.environ.pop("REPORT_TZ", None)
                if setting:
                    os.environ["REPORT_TZ"] = setting
                days = {today_in(zone)}
                (result,) = self.call("legacy", [("answers", {"text": DATE_TEXT})])
                days.add(today_in(zone))
                self.assertTrue(result.is_error)
                self.assertIn(result.content[0].text,
                              {tool_errors.client_text(tool_errors.BAD_INPUT, detail=tool_errors.DATE_DETAIL, today=day)
                               for day in days})

    def test_free_text_tools_get_the_note(self):
        tools = {t.name: t for t in asyncio.run(self.server.list_tools())}
        note = "\n\n" + server_instructions.FREE_TEXT_NOTE
        self.assertEqual(tools["get_activity"].description, upstream_answer.__doc__ + note)
        self.assertEqual(tools["answers"].description, answers.__doc__)


class ServedTexts(RealServerCase):
    """What a client sees in both eras: the instructions, the server name and
    the tool list."""

    def test_instructions_and_server_name_in_both_eras(self):
        server = self.serve()
        for mode in ERAS:
            with self.subTest(mode=mode):
                result, info, _ = handshake(server, mode)
                self.assertEqual(type(result).__name__, "InitializeResult" if mode == "legacy" else "DiscoverResult")
                self.assertEqual(result.instructions, server_instructions.INSTRUCTIONS)
                self.assertEqual(info.name, "coach-mcp-for-garmin")
                self.assertEqual(info.version, version.VERSION)

    def test_tool_list_annotations_and_descriptions_in_both_eras(self):
        server = self.serve()
        with mock.patch.object(server_instructions, "FREE_TEXT_TOOLS", frozenset()):
            plain = self.serve()
        note = "\n\n" + server_instructions.FREE_TEXT_NOTE
        served = {}
        for mode in ERAS:
            tools, without = handshake(server, mode)[2], handshake(plain, mode)[2]
            served[mode] = {name: tool.description for name, tool in tools.items()}
            self.assertEqual(set(tools), set(enabled_tools()))
            self.assertEqual((len(tools), set(without)), (31, set(tools)))
            for name, tool in sorted(tools.items()):
                with self.subTest(mode=mode, tool=name):
                    self.assertIs(tool.annotations.read_only_hint, True)
                    self.assertIsNone(tool.output_schema)
                    self.assertNotIn(server_instructions.FREE_TEXT_NOTE, without[name].description)
                    if name in server_instructions.FREE_TEXT_TOOLS:
                        self.assertTrue(tool.description.endswith(note), tool.description[-80:])
                        self.assertEqual(tool.description[:-len(note)],
                                         inspect.cleandoc(without[name].description).rstrip())
                    else:
                        self.assertEqual(tool.description, without[name].description)
        self.assertEqual(served["legacy"], served["auto"])
        # Claude Code before v2.1.296 cut a tool description after 2,048 characters, and claude.ai, which drops
        # the server instructions, may cut too (unverified). With its docstring's indentation,
        # get_activities_by_date's description was 2,075 characters and lost the end of the note there; without
        # it, it is 1,867. No noted description may cross that line.
        noted = server_instructions.FREE_TEXT_TOOLS | set(self.launch.TOOL_NOTES)
        late = {name for name in noted if name in tools and len(served["legacy"][name]) > 2048}
        self.assertEqual(late, set())
        # Each tool note is served, right before the data note on a free-text tool.
        for name, text in self.launch.TOOL_NOTES.items():
            with self.subTest(tool=name):
                self.assertIn(name, tools)
                tail = note if name in server_instructions.FREE_TEXT_TOOLS else ""
                self.assertTrue(served["legacy"][name].endswith("\n\n" + text + tail), name)


class LoginFailed(RealServerCase):
    """With the Garmin login failed, every tool answers the fixed login text
    (isError, one block) and logs one WARNING line, except the range tools
    that hide a failed day as a normal "No ... found" answer (Garmin's 401
    included)."""

    def call_everything(self, server, skip=()):
        """{mode: ({tool: CallToolResult}, log records)} for every served tool but `skip`."""
        tools = handshake(server, "legacy")[2]
        self.assertEqual(set(tools), set(enabled_tools()))
        calls = [(name, required_arguments(tool)) for name, tool in sorted(tools.items()) if name not in skip]
        out = {}
        for mode in ERAS:
            with captured_records() as records:
                results = call_tools(server, mode, calls)
            out[mode] = dict(zip((name for name, _ in calls), results)), records
        return out

    def assert_login_answers(self, answered, records, hidden):
        login = tool_errors.client_text(tool_errors.LOGIN)
        for name, result in sorted(answered.items()):
            with self.subTest(tool=name):
                (text,) = [block.text for block in result.content]
                if name in hidden:
                    self.assertFalse(result.is_error)
                    self.assertTrue(text.startswith(hidden[name]), text)
                else:
                    self.assertTrue(result.is_error)
                    self.assertEqual(text, login)
                    self.assertIsNone(result.structured_content)
        failed = {name: tool_errors.LOGIN for name in answered if name not in hidden}
        self.assert_one_line_per_failure(records, failed)
        self.assertEqual({r.levelno for r in tool_error_records(records)}, {logging.WARNING} if failed else set())

    def test_upstream_tools_and_the_course_tool(self):
        for mode, (answered, records) in self.call_everything(self.serve(), skip=STATUS_TOOLS).items():
            with self.subTest(mode=mode):
                self.assertEqual(len(answered), 29)
                self.assertEqual(sum(result.is_error for result in answered.values()), 26)
                self.assert_login_answers(answered, records, HIDDEN_FAILURES)

    def test_status_tools(self):
        # A failed first call ends the report with the login error.
        server = self.serve()
        for mode in ERAS:
            with captured_records() as records:
                answered = dict(zip(STATUS_TOOLS, call_tools(server, mode, [(name, {}) for name in STATUS_TOOLS])))
            with self.subTest(mode=mode):
                self.assert_login_answers(answered, records, {})

    def test_a_garmin_401_reads_as_the_login_text(self):
        server = self.serve()
        self.garmin_answering(401, {"message": "Unauthorized"})
        everything = self.call_everything(server, skip=STATUS_TOOLS)
        for mode, (answered, records) in everything.items():
            with self.subTest(mode=mode):
                self.assertEqual(sum(result.is_error for result in answered.values()), 25)
                self.assert_login_answers(answered, records, HIDDEN_GARMIN_ERRORS)
        # Every call reached Garmin (the fake session) at least once, in both eras.
        self.assertGreaterEqual(len(self.garmin_requests), 2 * 29)

    def test_a_garmin_401_stops_the_status_tools_with_the_login_text(self):
        server = self.serve()
        self.garmin_answering(401, {"message": "Unauthorized"})
        for mode in ERAS:
            del self.garmin_requests[:]
            with captured_records() as records:
                answered = dict(zip(STATUS_TOOLS, call_tools(server, mode, [(name, {}) for name in STATUS_TOOLS])))
            with self.subTest(mode=mode):
                self.assert_login_answers(answered, records, {})
                self.assertGreaterEqual(len(self.garmin_requests), 2)


class StatusToolsThroughTheServer(RealServerCase):
    """This repo's tools through the real server (the login case is
    LoginFailed.test_status_tools)."""

    def test_an_impossible_date_is_invalid_input_with_today(self):
        # Refused before any Garmin call: with the login failed, a call would answer the login text.
        server = self.serve()
        for mode in ERAS:
            days = {today_in(None)}
            with captured_records() as records:
                (result,) = call_tools(server, mode, [("get_daily_status", {"date": "2026-02-30"})])
            days.add(today_in(None))
            with self.subTest(mode=mode):
                self.assertTrue(result.is_error)
                (text,) = [block.text for block in result.content]
                self.assertIn(text, {tool_errors.client_text(tool_errors.BAD_INPUT, detail=tool_errors.DATE_DETAIL,
                                                             today=day) for day in days})
                self.assertNotIn("2026-02-30", text)
                self.assert_one_line_per_failure(records, {"get_daily_status": tool_errors.BAD_INPUT})

    def test_a_crash_in_the_daily_status_is_an_internal_error_with_its_traceback(self):
        # A bug is not caught by the tool; the override logs its traceback under a ref.
        server = self.serve()

        async def broken(*args, **kwargs):
            raise KeyError("no-such-key")

        with mock.patch.object(self.launch.daily_status, "build_daily_status", broken), \
                caught_by_the_override() as caught, captured_records() as records:
            (result,) = call_tools(server, "legacy", [("get_daily_status", {})])
        self.assertTrue(result.is_error)
        (text,) = [block.text for block in result.content]
        ref = INTERNAL_TEXT_RE.fullmatch(text).group(1)
        self.assertEqual(text, tool_errors.client_text(tool_errors.INTERNAL, ref=ref))
        self.assertNotIn("no-such-key", text)
        self.assert_one_line_per_failure(records, {"get_daily_status": tool_errors.INTERNAL})
        (record,) = tool_error_records(records)
        self.assertEqual(record.levelno, logging.ERROR)
        self.assertTrue(record.getMessage().startswith(f"tool error: ref={ref} tool=get_daily_status group=internal: "))
        # The exception the override caught (the SDK's ToolError), not the KeyError at the root.
        (exc,) = caught
        self.assertIs(record.exc_info[1], exc)
        self.assertEqual(type(exc).__name__, "ToolError")
        trace = logging.Formatter().formatException(record.exc_info)
        self.assertIn("ToolError: Error executing tool get_daily_status", trace)
        self.assertIn("KeyError: 'no-such-key'", trace)

    def test_a_course_without_geo_points_is_no_data(self):
        server = self.serve()
        paths = []

        def connectapi(path):
            paths.append(path)
            return {"courseId": 7, "courseName": "Loop"}

        self.courses.garmin_client = types.SimpleNamespace(client=types.SimpleNamespace(connectapi=connectapi))
        for mode in ERAS:
            with captured_records() as records:
                (result,) = call_tools(server, mode, [("get_course_profile", {"course_id": 7})])
            with self.subTest(mode=mode):
                self.assertFalse(result.is_error)
                self.assertEqual([block.text for block in result.content],
                                 [tool_errors.client_text(tool_errors.NO_DATA)])
                self.assertIsNone(result.structured_content)
                self.assert_one_line_per_failure(records, {"get_course_profile": tool_errors.NO_DATA})
                self.assertEqual([r.levelno for r in tool_error_records(records)], [logging.INFO])
        self.assertEqual(paths, ["/course-service/course/7"] * 2)


@unittest.skipUnless(HAVE_UPSTREAM, "needs upstream garmin_mcp on mcp 2.x")
class CallToolHook(UpstreamCase):
    """gate() fails the build when the SDK no longer sends tools/call through
    MCPServer.call_tool(name, arguments, context), the method the tool-error
    wrapper overrides, and leaves upstream's
    server class alone."""

    def setUp(self):
        super().setUp()
        import garmin_mcp
        from mcp.server.mcpserver import MCPServer

        self.launch = load_launch(self, Stubs().modules)
        self.garmin_mcp, self.MCPServer, self.base = garmin_mcp, MCPServer, garmin_mcp.FastMCP

    def assert_build_fails(self):
        with self.assertRaises(RuntimeError) as cm:
            self.launch.gate({"get_stats"})
        self.assertEqual(str(cm.exception), HOOK_ERROR)
        self.assertIs(self.garmin_mcp.FastMCP, self.base)

    def test_the_installed_sdk_passes(self):
        self.launch.gate({"get_stats"})
        self.assertIn("call_tool", self.garmin_mcp.FastMCP.__dict__)
        self.assertTrue(issubclass(self.garmin_mcp.FastMCP, self.base))

    def test_a_missing_call_tool_fails_the_build(self):
        hook = self.MCPServer.__dict__["call_tool"]
        self.addCleanup(setattr, self.MCPServer, "call_tool", hook)
        del self.MCPServer.call_tool
        self.assert_build_fails()

    def test_renamed_parameters_fail_the_build(self):
        async def call_tool(self, tool_name, arguments, context=None):  # pragma: no cover - never called
            return None

        with mock.patch.object(self.MCPServer, "call_tool", call_tool):
            self.assert_build_fails()

    def test_a_handler_that_no_longer_calls_it_fails_the_build(self):
        real = inspect.getsource

        def getsource(obj):
            return real(obj).replace("self.call_tool(", "self._tool_manager.call_tool(")

        with mock.patch("inspect.getsource", getsource):
            self.assert_build_fails()

    def test_source_that_cannot_be_read_fails_the_build(self):
        with mock.patch("inspect.getsource", side_effect=OSError("could not get source code")):
            self.assert_build_fails()


DOCKERIGNORE_FILE = os.path.join(ROOT, ".dockerignore")
ENV_EXAMPLE_FILE = os.path.join(ROOT, ".env.example")
DEPLOY_FILE = os.path.join(ROOT, "scripts", "deploy.sh")


def docker_ignores(pattern: str, path: str) -> bool:
    """Docker's .dockerignore matching, enough for these patterns: "**/" spans
    zero or more directories, "*" and "?" stay inside one, and a pattern that
    matches a directory also covers everything under it."""
    pattern = pattern.rstrip("/")
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
        elif pattern[i] == "*":
            out, i = out + "[^/]*", i + 1
        elif pattern[i] == "?":
            out, i = out + "[^/]", i + 1
        else:
            out, i = out + re.escape(pattern[i]), i + 1
    return re.fullmatch(out + "(?:/.*)?", path.rstrip("/")) is not None


if __name__ == "__main__":
    unittest.main()
