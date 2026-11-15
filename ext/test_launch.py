"""Tests for launch.py: this repo's tools join upstream's filtered app, and
only read-only tools are ever registered.

    python -m unittest discover -s ext -p "test_launch*.py" -v

Many tests need upstream garmin_mcp (the image, or a venv with the pinned
commit) and are skipped without it; the read-only policy rules (ToolPolicy,
RegistrationGate, CheckConfig, ConfigFile) run on the standard library alone.
The status modules are replaced by stubs, so these tests pin launch.py's
wiring, not the tools' behaviour (their own tests cover that); RealModules
repeats the key checks with the real modules.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import importlib.metadata
import importlib.util
import io
import os
import re
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

import mcp2_compat  # noqa: E402

# On mcp 2.x, serve garmin_mcp's `from mcp.server.fastmcp import FastMCP` from
# the adapter, as the image's .pth file does; a no-op on mcp 1.x.
mcp2_compat.register_finder()

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, os.pardir)
ENABLED_TOOLS_FILE = os.path.join(ROOT, "config", "enabled-tools.txt")
STATUS_MODULES = ("daily_status", "weekly_status")
# launch.patch() installs the mcp 2.x adapter and refuses any other mcp (the
# image pins mcp 2.3.0), so under mcp 1.x the tests that call it are skipped.
HAVE_UPSTREAM = (importlib.util.find_spec("garmin_mcp") is not None
                 and mcp2_compat._mcp_major() == "2")
HAVE_STATUS_MODULES = all(os.path.exists(os.path.join(HERE, m + ".py")) for m in STATUS_MODULES)

READ_ONLY = {"get_course_profile", "get_daily_status", "get_weekly_status"}
# Tools that are gone; none may come back by accident.
REMOVED = {"send_daily_status_now"}
# The status tools' arguments (the daily routine passes text_format; chats may
# pass the rest). FastMCP drops unknown arguments silently, so a renamed
# parameter would fail without an error.
EXPECTED_PARAMS = {
    "get_daily_status": {"date", "include_sources", "probe", "text_format"},
    "get_weekly_status": {"end_date", "include_sources", "text_format"},
}
# The modules upstream's main() registers, in its order.
UPSTREAM_MODULES = (
    "activity_management", "health_wellness", "user_profile", "devices",
    "gear_management", "weight_management", "challenges", "training", "workouts",
    "data_management", "womens_health", "nutrition", "workout_builders", "courses",
    "activity_analysis", "calendar_events",
)


def enabled_tools():
    """config/enabled-tools.txt, parsed the way scripts/deploy.sh parses it."""
    names = []
    with open(ENABLED_TOOLS_FILE, encoding="utf-8") as f:
        for line in f:
            name = "".join(line.split("#", 1)[0].split())
            if name:
                names.append(name)
    return names


# Stub tools: the contract's names and signatures.
def get_daily_status(date: str = "", include_sources: bool = False, probe: bool = False,
                     text_format: str = "") -> str:
    """Stub."""
    return "{}"


def get_weekly_status(end_date: str = "", include_sources: bool = False, text_format: str = "") -> str:
    """Stub."""
    return "{}"


class Stubs:
    """Stand-ins for the status modules; they record what launch.py calls."""

    def __init__(self, failing=None):
        self.calls = []
        self.get_client = {}
        self.modules = {
            "daily_status": self._status_module("daily_status", get_daily_status, failing),
            "weekly_status": self._status_module("weekly_status", get_weekly_status, failing),
        }

    def _status_module(self, name, tool, failing):
        mod = types.ModuleType(name)

        def selfcheck():
            self.calls.append(name + ".selfcheck")
            if name == failing:
                raise RuntimeError(name + ": an upstream helper is missing")

        def register_tools(app, get_client):
            self.calls.append(name + ".register_tools")
            self.get_client[name] = get_client
            app.tool()(tool)
            return app

        mod.selfcheck, mod.register_tools = selfcheck, register_tools
        return mod


def load_launch(test, modules=None):
    """Import ext/launch.py afresh, seeing `modules` (None: the real files).

    sys.modules is restored when the test ends, so no stub leaks into other
    test modules.
    """
    names = ("launch",) + STATUS_MODULES
    saved = {n: sys.modules.get(n) for n in names}

    def restore():
        for n, mod in saved.items():
            if mod is None:
                sys.modules.pop(n, None)
            else:
                sys.modules[n] = mod

    test.addCleanup(restore)
    sys.modules.pop("launch", None)
    if modules:
        sys.modules.update(modules)
    return importlib.import_module("launch")


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


class UpstreamCase(unittest.TestCase):
    """Saves and restores the upstream globals patch() changes."""

    def setUp(self):
        import garmin_mcp
        from garmin_mcp import courses, tool_annotations

        self.courses, self.annotations = courses, tool_annotations
        self.upstream_register = courses.register_tools
        self.upstream_read_only = tool_annotations.READ_ONLY_TOOLS
        self.upstream_additive = tool_annotations.ADDITIVE_TOOLS
        # launch.gate() replaces garmin_mcp.FastMCP.
        saved = (courses.register_tools, courses.garmin_client,
                 tool_annotations.READ_ONLY_TOOLS, tool_annotations.ADDITIVE_TOOLS, garmin_mcp.FastMCP)

        def restore():
            (courses.register_tools, courses.garmin_client,
             tool_annotations.READ_ONLY_TOOLS, tool_annotations.ADDITIVE_TOOLS, garmin_mcp.FastMCP) = saved

        self.addCleanup(restore)
        self.addCleanup(mcp2_compat.set_server_overrides)  # launch.main() sets the server name

    def assert_annotations(self, tools):
        for name in READ_ONLY:
            ann = tools[name].annotations
            self.assertIsNotNone(ann, name)
            # mcp-types 2.x attributes are snake_case; the wire keeps readOnlyHint.
            self.assertTrue(ann.read_only_hint, name)
            self.assertFalse(ann.destructive_hint, name)
        for name in REMOVED:
            self.assertNotIn(name, tools)


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
                os.environ, {"GARMIN_MCP_TRANSPORT": "stdio", "GARMIN_MCP_STATELESS_HTTP": "false", **env}))
            for key in ("GARMIN_ENABLED_TOOLS", "GARMIN_DISABLED_TOOLS"):
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
        self.assertEqual(self.server.version, importlib.metadata.version("mcp"))

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


@unittest.skipUnless(HAVE_UPSTREAM, "needs upstream garmin_mcp on mcp 2.x")
class CheckConfigCommand(unittest.TestCase):
    """`python launch.py --check-config`, as entrypoint.sh runs it before Caddy."""

    def run_check(self, **env):
        with tempfile.TemporaryDirectory() as home:
            base = {"PATH": os.environ.get("PATH", ""), "HOME": home, "GARMINTOKENS": os.path.join(home, "tokens")}
            proc = subprocess.run([sys.executable, os.path.join(HERE, "launch.py"), "--check-config"],
                                  env={**base, **env}, stdin=subprocess.DEVNULL, capture_output=True,
                                  text=True, timeout=120)
        return proc.returncode, proc.stderr.splitlines()

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


class FakeServer:
    """MCPServer's two registration paths: tool() registers through add_tool()."""

    def __init__(self):
        self.added = []

    def add_tool(self, fn, name=None, **kwargs):
        self.added.append(name or fn.__name__)

    def tool(self, name=None, **kwargs):
        def decorator(fn):
            self.add_tool(fn, name=name, **kwargs)
            return fn

        return decorator


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
        with open(DOCKERIGNORE_FILE, encoding="utf-8") as f:
            patterns = [line.strip() for line in f if line.strip() and not line.startswith("#")]
        self.assertFalse(any(docker_ignores(p, "config/enabled-tools.txt") for p in patterns))

    def test_entrypoint_checks_config_before_anything_starts(self):
        code = [line for line in read(ENTRYPOINT_FILE).splitlines() if not line.lstrip().startswith("#")]

        def first(needle):
            return next(i for i, line in enumerate(code) if needle in line)

        check = first("launch.py --check-config")
        self.assertLess(check, first("while :"), "must run before the supervisor loop, which restarts forever")
        self.assertLess(check, first("exec caddy"))
        self.assertRegex(code[check] + code[check + 1], r"\|\|\s*die ")

    def test_no_deny_list_and_report_tz_is_required(self):
        env_example = read(ENV_EXAMPLE_FILE)
        self.assertNotIn("GARMIN_DISABLED_TOOLS", env_example)
        self.assertRegex(env_example, r"(?m)^REPORT_TZ=\S+")
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

        import logging

        logging.disable(logging.INFO)  # the SDK logs each failed call at INFO
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


DOCKERIGNORE_FILE = os.path.join(ROOT, ".dockerignore")
DOCKERFILE_FILE = os.path.join(ROOT, "Dockerfile")
ENTRYPOINT_FILE = os.path.join(ROOT, "entrypoint.sh")
ENV_EXAMPLE_FILE = os.path.join(ROOT, ".env.example")
DEPLOY_FILE = os.path.join(ROOT, "scripts", "deploy.sh")


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


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
