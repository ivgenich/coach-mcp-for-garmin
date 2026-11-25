"""Deep tests (release test phase): the feature switch matrix.

HISTORY_SEARCH x PLAN_TOOLS x PLAN_TRACKING x PLAN_WRITES (off, on, unset, case and space variants, invalid
values) x GARMIN_AUTH (full, key) x ROLE (app, worker, enqueue), checked against an oracle written from the
documented rules (feature_config's module docstring, SECURITY.md "How plan writes behave"):

- feature_config.problems(), load() and summary() for every combination (standard library);
- the exact --check-config output (launch._check_config_cli) for every combination (upstream);
- for every valid app combination: the allowed tools, every tool's effect annotations after patch(), and the
  server launch.main() really builds: served tools, hints, instructions and the Garmin write kinds the
  process admits (upstream);
- the default configuration against pinned lists and hashes taken from the main branch.

    cd ext && python -m unittest test_deep_config_matrix
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import io
import itertools
import os
import secrets
import sys
import types
import unittest
from unittest import mock

import feature_config
import garmin_guard
import garmin_paths
import server_instructions

from test_launch_support import (enabled_tools, fake_db_url, fake_modules, FULL, HAVE_UPSTREAM, key_env,
                                 KEY_MODE_TOOL_NAMES, load_launch, quiet, Stubs, UPSTREAM_MODULES, UpstreamCase)

SWITCHES = ("HISTORY_SEARCH", "PLAN_TOOLS", "PLAN_TRACKING", "PLAN_WRITES")
# None: unset. Every spelling a person might type; on/off match after strip() and lower().
SWITCH_VALUES = (None, "", "off", "OFF", " Off ", "on", "ON", " On\t", "yes", "1", "true", "onn", "o n", "\u043en")
ROLES = (None, "app", "worker", "enqueue", "APP", " Worker ")
AUTHS = ("full", "key", " KEY ", "Full")

# The default tool list (GARMIN_AUTH=full, every switch off), pinned from the main branch (1.0.5).
MAIN_DEFAULT_TOOLS = frozenset({
    "get_activities", "get_activities_by_date", "get_activity", "get_activity_splits", "get_activity_hr_in_timezones",
    "get_activity_power_in_timezones", "get_activity_fit_data", "get_scheduled_workouts", "get_workout_by_id",
    "get_courses", "get_course_details", "get_course_profile", "get_daily_status", "get_weekly_status", "get_stats",
    "get_sleep_summary", "get_sleep_summary_range", "get_hrv_trend", "get_body_battery", "get_weekly_stress",
    "get_training_readiness", "get_training_status", "get_training_load_trend", "get_vo2max_trend",
    "get_endurance_score", "get_lactate_threshold", "get_cycling_ftp", "get_heart_rate_zones",
    "get_race_predictions", "get_personal_record", "get_progress_summary_between_dates"})
# SHA-256 of main's INSTRUCTIONS and INSTRUCTIONS_KEY (1.0.5): the switches off must keep them word for word.
MAIN_INSTRUCTIONS_SHA256 = "67eb21f6a72ddc8acf66f3d61ab58a751bb55352b69d60b11187fe42b997eb3a"
MAIN_KEYMODE_INSTRUCTIONS_DIGEST = "1ed32c8deb27045fef1287e83e6d7b26d9889904e0190bb0ec82241a0115674f"

HISTORY_TOOLS = frozenset({"find_similar_weeks", "search_history"})
PLAN_READ = frozenset({"plan_assess", "plan_get", "plan_preview"})
REVIEW = frozenset({"plan_review_week"})
GARMIN_WRITES = frozenset({"plan_commit", "plan_cancel"})
APPLY = frozenset({"plan_apply_changes"})
FEEDBACK = frozenset({"log_feedback"})
WRITE_TOOLS = GARMIN_WRITES | APPLY | FEEDBACK
# (readOnly, destructive, idempotent, openWorld) as launch.py and SECURITY.md specify them.
WRITE_HINTS = {"plan_commit": (False, False, True, True), "plan_cancel": (False, True, True, True),
               "plan_apply_changes": (False, True, True, True), "log_feedback": (False, False, True, False)}


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def token_key():
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()


def switch(value):
    """The oracle's reading of a switch: True, False or None (invalid)."""
    text = (value or "").strip().lower()
    if text in ("", "off"):
        return False
    return True if text == "on" else None


def oracle(env):
    """(error lines in order, the effective FeatureConfig fields) for an env whose other settings are valid
    (DATABASE_URL and HISTORY_TOKEN_KEY always set and valid), from the documented rules."""
    errors = []
    history = switch(env.get("HISTORY_SEARCH"))
    if history is None:
        errors.append(feature_config.ERR_H1)
        history = False
    role = (env.get("ROLE") or "").strip().lower() or "app"
    if role != "app" and not history:
        errors.append(feature_config.ERR_H5.format(role=role))
    auth = "key" if "".join((env.get("GARMIN_AUTH") or "").split()).lower() == "key" else "full"
    tools, writes, tracking = (switch(env.get(n)) for n in ("PLAN_TOOLS", "PLAN_WRITES", "PLAN_TRACKING"))
    if tools is None:
        errors.append(feature_config.ERR_P1)
        tools = False
    if writes is None:
        errors.append(feature_config.ERR_P2)
        writes = False
    if (tools or writes) and role != "app":
        errors.append(feature_config.ERR_P6)
    elif tools:
        if not history:
            errors.append(feature_config.ERR_P3)
        if auth != "full":
            errors.append(feature_config.ERR_P4)
    if writes and not tools:
        errors.append(feature_config.ERR_P5)
    if tracking is None:
        errors.append(feature_config.ERR_P_TRACKING_VALUE)
        tracking = False
    if tracking and not tools:
        errors.append(feature_config.ERR_P_TRACKING_NEEDS)
    plan = tools and history and auth == "full" and role == "app"
    return errors, {"history": history, "role": role, "garmin_auth": auth, "plan_tools": plan,
                    "plan_writes": plan and writes, "plan_tracking": plan and tracking}


def matrix(values=SWITCH_VALUES, roles=ROLES, auths=AUTHS):
    for combo in itertools.product(values, repeat=len(SWITCHES)):
        for role in roles:
            for auth in auths:
                env = {"GARMIN_AUTH": auth}
                env.update({name: v for name, v in zip(SWITCHES, combo) if v is not None})
                if role is not None:
                    env["ROLE"] = role
                yield env


class FeatureConfigMatrix(unittest.TestCase):
    """problems(), load() and summary() for every combination (about 230,000), against the oracle."""

    def setUp(self):
        self.base = {"DATABASE_URL": fake_db_url(), "HISTORY_TOKEN_KEY": token_key()}

    def test_every_combination(self):
        wrong = []
        count = 0
        for env in matrix(SWITCH_VALUES[:8], ROLES[:4], AUTHS[:2]):
            env = {**self.base, **env}
            count += 1
            want_errors, want = oracle(env)
            got_errors = feature_config.problems(env)
            cfg = feature_config.load(env)
            got = {name: getattr(cfg, name) for name in want}
            if got_errors != want_errors or got != want:
                wrong.append((sorted((k, v) for k, v in env.items() if k in SWITCHES + ("ROLE", "GARMIN_AUTH")),
                              got_errors, want_errors, got, want))
            # Never a write without the whole chain, whatever the errors say.
            if cfg.plan_writes and not (cfg.plan_tools and cfg.history and cfg.garmin_auth == "full"
                                        and cfg.role == "app"):
                wrong.append(("plan_writes without its chain", env))
            if cfg.plan_tracking and not cfg.plan_tools:
                wrong.append(("plan_tracking without plan_tools", env))
        self.assertGreater(count, 20000)
        self.assertEqual(wrong[:5], [])

    def test_spellings_roles_and_auths(self):
        wrong = []
        for env in matrix(SWITCH_VALUES, ROLES, AUTHS):
            if sum(1 for n in SWITCHES if env.get(n) not in (None, "", "off", "on")) > 1:
                continue   # every odd spelling alone, with every other switch in its plain forms
            env = {**self.base, **env}
            want_errors, want = oracle(env)
            cfg = feature_config.load(env)
            if feature_config.problems(env) != want_errors or {n: getattr(cfg, n) for n in want} != want:
                wrong.append(sorted(env.items()))
        self.assertEqual(wrong[:5], [])

    def test_error_lines_never_carry_a_value(self):
        marker = "TEST-ONLY-" + secrets.token_hex(8)
        for name in SWITCHES + ("ROLE",):
            with self.subTest(name=name):
                lines = feature_config.problems({**self.base, "HISTORY_SEARCH": "on", name: marker})
                self.assertTrue(lines)
                self.assertNotIn(marker, "\n".join(lines))
                self.assertNotIn(marker.lower(), "\n".join(lines))

    def test_summary(self):
        on = {**self.base, "GARMIN_AUTH": "full", "HISTORY_SEARCH": "on", "PLAN_TOOLS": "on"}
        cases = (
            ({"GARMIN_AUTH": "full"}, ""),
            ({**self.base, "GARMIN_AUTH": "full", "HISTORY_SEARCH": "on"}, "history search on"),
            (on, "history search on, plan tools on"),
            ({**on, "PLAN_WRITES": "on"}, "history search on, plan tools on, plan writes on"),
            ({**on, "PLAN_TRACKING": "ON"}, "history search on, plan tools on, plan tracking on"),
            ({**on, "PLAN_WRITES": " on", "PLAN_TRACKING": "on "},
             "history search on, plan tools on, plan writes on, plan tracking on"),
            ({**on, "ROLE": "worker", "PLAN_TOOLS": "off"}, "ROLE=worker, history search on"),
            ({**on, "GARMIN_AUTH": "key"}, "history search on"),   # an invalid plan switch is off in load()
        )
        for env, want in cases:
            with self.subTest(env=sorted(env)):
                self.assertEqual(feature_config.load(env).summary(), want)

    def test_the_plan_writes_note_only_with_the_whole_chain(self):
        on = {**self.base, "GARMIN_AUTH": "full", "HISTORY_SEARCH": "on", "PLAN_TOOLS": "on", "PLAN_WRITES": "on"}
        self.assertIn(feature_config.NOTE_PLAN_WRITES, feature_config.load(on).notes)
        for change in ({"PLAN_TOOLS": "off"}, {"HISTORY_SEARCH": "off"}, {"GARMIN_AUTH": "key"},
                       {"ROLE": "worker"}, {"PLAN_WRITES": "off"}, {"PLAN_WRITES": "yes"}):
            with self.subTest(change=sorted(change.items())):
                self.assertNotIn(feature_config.NOTE_PLAN_WRITES, feature_config.load({**on, **change}).notes)

    def test_a_crashing_parse_is_one_fixed_line_and_everything_off(self):
        class Boom(dict):
            def get(self, *args):
                raise RuntimeError("TEST-ONLY secret value")

        self.assertEqual(feature_config.problems(Boom()),
                         ["the feature settings could not be checked: RuntimeError"])
        cfg = feature_config.load(Boom())
        self.assertEqual((cfg.history, cfg.plan_tools, cfg.plan_writes, cfg.plan_tracking), (False,) * 4)


class DefaultsPinnedToMain(unittest.TestCase):
    """The default configuration equals main's: the baked list and the instruction texts (standard library)."""

    def test_baked_list(self):
        self.assertEqual(frozenset(enabled_tools()), MAIN_DEFAULT_TOOLS)
        self.assertEqual(len(enabled_tools()), len(MAIN_DEFAULT_TOOLS))   # no duplicates

    def test_instructions_are_mains_word_for_word(self):
        self.assertEqual(sha(server_instructions.INSTRUCTIONS), MAIN_INSTRUCTIONS_SHA256)
        self.assertEqual(sha(server_instructions.INSTRUCTIONS_KEY), MAIN_KEYMODE_INSTRUCTIONS_DIGEST)

    def test_the_plan_text_differs_only_where_documented(self):
        plan = server_instructions.INSTRUCTIONS_PLAN
        self.assertNotIn("No tool changes any data", plan)
        self.assertIn("Only plan_commit and plan_cancel change data", plan)
        self.assertIn("After a failed write, call plan_get before writing again.", plan)
        self.assertLessEqual(len(plan), 2048)
        self.assertTrue(plan.isascii())
        # The data rule is kept word for word.
        rule = server_instructions.INSTRUCTIONS.split("\n\n")[3]
        self.assertIn(rule, plan)

    def test_the_default_check_config_line_is_mains(self):
        launch = load_launch(self, Stubs().modules)
        with mock.patch.object(launch, "allowed_tools", return_value=MAIN_DEFAULT_TOOLS), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            code = launch._check_config_cli({**FULL, "REPORT_TZ": "UTC"})
        self.assertEqual((code, err.getvalue()), (0, "launch: config OK (31 read-only tools)\n"))


# --- upstream: --check-config, the allowed tools and their annotations ---------------------------------------

def expected_tools(cfg, key):
    """The tool set the documented rules give a valid app configuration."""
    base = KEY_MODE_TOOL_NAMES if key else MAIN_DEFAULT_TOOLS
    tools = set(base)
    if cfg["history"]:
        tools |= HISTORY_TOOLS
    if cfg["plan_tools"]:
        tools |= PLAN_READ
        if cfg["plan_tracking"]:
            tools |= REVIEW | FEEDBACK
        if cfg["plan_writes"]:
            tools |= GARMIN_WRITES
            if cfg["plan_tracking"]:
                tools |= APPLY
    return frozenset(tools)


def expected_ok_line(cfg, key):
    tools = expected_tools(cfg, key)
    summary = ", ".join(part for part, on in (
        ("history search on", cfg["history"]), ("plan tools on", cfg["plan_tools"]),
        ("plan writes on", cfg["plan_writes"]), ("plan tracking on", cfg["plan_tracking"])) if on)
    extra = f"; {summary}" if summary else ""
    if tools & FEEDBACK:
        extra = f"; log_feedback writes only the app's database{extra}"
    reads = len(tools - WRITE_TOOLS)
    if key:
        return f"launch: config OK ({reads} read-only tools, Garmin Personal API key{extra})"
    writes = len(tools & (GARMIN_WRITES | APPLY))
    if writes:
        return f"launch: config OK ({reads} read-only tools and {writes} plan write tools{extra})"
    return f"launch: config OK ({reads} read-only tools{extra})"


@unittest.skipUnless(HAVE_UPSTREAM, "needs upstream garmin_mcp on mcp 2.x")
class CheckConfigMatrix(UpstreamCase):
    """launch._check_config_cli() for HISTORY_SEARCH x PLAN_TOOLS x PLAN_TRACKING x PLAN_WRITES (unset, off,
    on, a case variant, an invalid value) x GARMIN_AUTH x ROLE: the exact output and exit status."""

    VALUES = (None, "off", "On", "maybe")

    def setUp(self):
        super().setUp()
        self.launch = load_launch(self, Stubs().modules)
        self.key = key_env(self)
        self.base = {"REPORT_TZ": "UTC", "DATABASE_URL": fake_db_url(), "HISTORY_TOKEN_KEY": token_key()}

    def env_for(self, env):
        out = {**self.base, **env}
        if out["GARMIN_AUTH"].strip().lower() == "key":
            out.update({k: v for k, v in self.key.items() if k != "GARMIN_AUTH"})
        return out

    def run_cli(self, env):
        with mock.patch.object(self.launch, "_has_module", return_value=True), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            code = self.launch._check_config_cli(env)
        return code, err.getvalue().splitlines()

    def test_every_combination(self):
        wrong = []
        for env in matrix(self.VALUES, (None, "worker", "enqueue"), ("full", "key")):
            env = self.env_for(env)
            errors, cfg = oracle(env)
            code, lines = self.run_cli(env)
            if errors:
                want = (1, ["launch: config error: " + line for line in errors])
            elif cfg["role"] != "app":
                want = (0, [f"launch: config OK (ROLE={cfg['role']}, history search on)"])
            else:
                want = (0, [expected_ok_line(cfg, cfg["garmin_auth"] == "key")])
            if (code, lines) != want:
                wrong.append((sorted((k, v) for k, v in env.items() if k in SWITCHES + ("ROLE", "GARMIN_AUTH")),
                              (code, lines), want))
        self.assertEqual(wrong[:3], [])

    def test_a_feature_without_its_packages(self):
        env = self.env_for({"GARMIN_AUTH": "full", "HISTORY_SEARCH": "on", "PLAN_TOOLS": "on", "PLAN_WRITES": "on"})
        with mock.patch.object(self.launch, "_has_module", return_value=False), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            code = self.launch._check_config_cli(env)
        self.assertEqual((code, err.getvalue().splitlines()), (1, ["launch: config error: " + feature_config.ERR_H11]))


@unittest.skipUnless(HAVE_UPSTREAM, "needs upstream garmin_mcp on mcp 2.x")
class AllowedToolsAndHints(UpstreamCase):
    """For every valid app combination: allowed_tools() and every allowed tool's annotations after patch()."""

    def setUp(self):
        super().setUp()
        self.key = key_env(self)
        self.base = {"REPORT_TZ": "UTC", "DATABASE_URL": fake_db_url(), "HISTORY_TOKEN_KEY": token_key()}
        a = self.annotations
        self.saved = (a.READ_ONLY_TOOLS, a.ADDITIVE_TOOLS, a.DESTRUCTIVE_TOOLS, a.IDEMPOTENT_TOOLS,
                      a.LOCAL_ONLY_TOOLS, self.courses.register_tools)

    def restore(self):
        a = self.annotations
        (a.READ_ONLY_TOOLS, a.ADDITIVE_TOOLS, a.DESTRUCTIVE_TOOLS, a.IDEMPOTENT_TOOLS, a.LOCAL_ONLY_TOOLS,
         self.courses.register_tools) = self.saved

    def valid_envs(self):
        for env in matrix(("off", "on"), (None,), ("full", "key")):
            env = {**self.base, **env}
            if env["GARMIN_AUTH"] == "key":
                env.update({k: v for k, v in self.key.items() if k != "GARMIN_AUTH"})
            errors, cfg = oracle(env)
            if not errors:
                yield env, cfg

    def test_every_valid_combination(self):
        seen = 0
        for env, cfg in self.valid_envs():
            seen += 1
            key = cfg["garmin_auth"] == "key"
            with self.subTest(env=sorted((k, v) for k, v in env.items() if k in SWITCHES + ("GARMIN_AUTH",))):
                self.restore()
                launch = load_launch(self, Stubs().modules)
                allowed = launch.allowed_tools(env)
                self.assertEqual(allowed, expected_tools(cfg, key))
                launch._FEATURES = feature_config.load(env)
                launch.patch(garmin_auth="key" if key else "full")
                for name in sorted(allowed):
                    hints = self.annotations.annotations_for(name)
                    self.assertIsNotNone(hints, name)       # nothing served unannotated
                    if name in WRITE_TOOLS:
                        got = (hints.read_only_hint, hints.destructive_hint, hints.idempotent_hint,
                               hints.open_world_hint)
                        self.assertEqual(got, WRITE_HINTS[name], name)
                    else:
                        self.assertTrue(hints.read_only_hint, name)
                        self.assertFalse(hints.destructive_hint, name)
                # A write tool the switches did not turn on is not even annotated (never read-only).
                for name in WRITE_TOOLS - allowed:
                    hints = self.annotations.annotations_for(name)
                    self.assertTrue(hints is None or not hints.read_only_hint, name)
                self.assertFalse(WRITE_TOOLS & self.annotations.READ_ONLY_TOOLS)
        self.assertEqual(seen, 8)   # full: 6 (history off; on; +plan; x writes x tracking), key: 2

    def test_default_equals_main(self):
        launch = load_launch(self, Stubs().modules)
        self.assertEqual(launch.allowed_tools({**FULL}), MAIN_DEFAULT_TOOLS)
        for spelled in ({"HISTORY_SEARCH": "off"}, {"PLAN_TOOLS": "OFF", "PLAN_WRITES": "", "PLAN_TRACKING": " off "}):
            with self.subTest(spelled=sorted(spelled.items())):
                self.assertEqual(launch.allowed_tools({**FULL, **spelled}), MAIN_DEFAULT_TOOLS)
        launch._FEATURES = feature_config.load(FULL)
        launch.patch()
        self.assertIs(self.annotations.ADDITIVE_TOOLS, self.upstream_additive)
        for name in WRITE_TOOLS:
            self.assertIsNone(self.annotations.annotations_for(name))

    def test_invalid_switches_never_turn_anything_on(self):
        launch = load_launch(self, Stubs().modules)
        on = {**self.base, **FULL, "HISTORY_SEARCH": "on", "PLAN_TOOLS": "on"}
        for name in ("PLAN_WRITES", "PLAN_TRACKING", "PLAN_TOOLS"):
            for value in ("yes", "1", "true", "enabled", "on!", "\u043en"):
                with self.subTest(name=name, value=value):
                    allowed = launch.allowed_tools({**on, name: value})
                    self.assertFalse(WRITE_TOOLS & allowed)


# --- upstream: the server launch.main() builds ---------------------------------------------------------------

def fake_guard_pg():
    module = types.ModuleType("garmin_guard_pg")
    module.PgSharedState = lambda dsn, tz, live=False: garmin_guard.MemoryState()
    return module


@unittest.skipUnless(HAVE_UPSTREAM, "needs upstream garmin_mcp on mcp 2.x")
class ServedMatrix(UpstreamCase):
    """launch.main([]) for every valid app combination, with the real status, history and plan tool modules
    (nothing reaches Postgres or Garmin: the stores are built lazily): the served tools and hints, the
    instructions, upstream's own warnings and the Garmin write kinds this process admits."""

    def setUp(self):
        super().setUp()
        import plan_tracking

        quiet(self, "mcp", "garmin_mcp", "garminconnect", "garmin_ext")
        fake_modules(self, garmin_guard_pg=fake_guard_pg())
        self.addCleanup(garmin_paths.allow_plan_writes, ())
        self.addCleanup(plan_tracking.configure, None)
        self.base = {"REPORT_TZ": "UTC", "DATABASE_URL": fake_db_url(), "HISTORY_TOKEN_KEY": token_key()}
        import garmin_mcp

        a = self.annotations
        self.pristine = (self.courses.register_tools, garmin_mcp.FastMCP, a.READ_ONLY_TOOLS, a.ADDITIVE_TOOLS,
                         a.DESTRUCTIVE_TOOLS, a.IDEMPOTENT_TOOLS, a.LOCAL_ONLY_TOOLS)

    def serve(self, env):
        import garmin_mcp
        import key_mode
        from mcp.server.mcpserver import MCPServer

        # Each main() starts from upstream as imported: patch() and gate() wrap what the last run left.
        a = self.annotations
        (self.courses.register_tools, garmin_mcp.FastMCP, a.READ_ONLY_TOOLS, a.ADDITIVE_TOOLS,
         a.DESTRUCTIVE_TOOLS, a.IDEMPOTENT_TOOLS, a.LOCAL_ONLY_TOOLS) = self.pristine
        garmin_paths.allow_plan_writes(())
        launch = load_launch(self)
        self.addCleanup(setattr, sys, "stdout", sys.stdout)
        for name in UPSTREAM_MODULES:
            module = getattr(garmin_mcp, name)
            self.addCleanup(setattr, module, "garmin_client", getattr(module, "garmin_client", None))
        served = []
        full_env = {"GARMIN_MCP_TRANSPORT": "stdio", "GARMIN_MCP_STATELESS_HTTP": "false", **env}
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.dict(os.environ, full_env))
            for name in ("GARMIN_ENABLED_TOOLS", "GARMIN_DISABLED_TOOLS", "GARMIN_PERSONAL_API_KEY",
                         "GARMIN_KEY_TOKEN_CACHE", "ROLE", "HISTORY_FORGET", "TRACING") + SWITCHES:
                if name not in env:
                    os.environ.pop(name, None)
            stack.enter_context(mock.patch.object(garmin_mcp._PendingGarminClient, "start", lambda s, login: None))
            stack.enter_context(mock.patch.object(MCPServer, "run", lambda s, *a, **k: served.append(s)))
            stack.enter_context(mock.patch.object(key_mode, "install", lambda *a, **k: None))
            stack.enter_context(mock.patch.object(garmin_guard, "install_refresh_lock", lambda *a, **k: True))
            stack.enter_context(mock.patch.object(garmin_guard, "install_atomic_token_dump", lambda *a, **k: True))
            for name in ("_start_token_publisher", "_start_catch_up", "_start_token_cleanup"):
                stack.enter_context(mock.patch.object(launch, name, lambda *a, **k: None))
            err = stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
            launch.main([])
            kinds = garmin_paths.allowed_write_kinds()
        (server,) = served
        return launch, server, err.getvalue(), kinds

    def combos(self):
        full = {"GARMIN_AUTH": "full"}
        history = {**full, "HISTORY_SEARCH": "on"}
        plan = {**history, "PLAN_TOOLS": "on"}
        key = {"GARMIN_AUTH": "key"}
        yield full
        yield history
        for writes, tracking in itertools.product(("off", "on"), repeat=2):
            yield {**plan, "PLAN_WRITES": writes, "PLAN_TRACKING": tracking}
        yield key
        yield {**key, "HISTORY_SEARCH": "on"}

    def test_every_valid_combination(self):
        for combo in self.combos():
            env = {**self.base, **combo}
            if combo["GARMIN_AUTH"] == "key":
                env.update({k: v for k, v in key_env(self).items() if k != "GARMIN_AUTH"})
            errors, cfg = oracle(env)
            self.assertEqual(errors, [])
            key = cfg["garmin_auth"] == "key"
            with self.subTest(combo=sorted(combo.items())):
                launch, server, stderr, kinds = self.serve(env)
                tools = {t.name: t for t in asyncio.run(server.list_tools())}
                self.assertEqual(set(tools), expected_tools(cfg, key))
                self.assertNotIn("unclassified", stderr)
                self.assertNotIn("not found and ignored", stderr)
                self.assertNotIn("refused to register", stderr)
                for name, tool in tools.items():
                    ann = tool.annotations
                    self.assertIsNotNone(ann, name)
                    if name in WRITE_TOOLS:
                        self.assertEqual((ann.read_only_hint, ann.destructive_hint, ann.idempotent_hint,
                                          ann.open_world_hint), WRITE_HINTS[name], name)
                    else:
                        self.assertTrue(ann.read_only_hint, name)
                        self.assertFalse(ann.destructive_hint, name)
                if key:
                    want = server_instructions.INSTRUCTIONS_KEY
                elif cfg["plan_writes"]:
                    want = (server_instructions.INSTRUCTIONS_PLAN_TRACKING if cfg["plan_tracking"]
                            else server_instructions.INSTRUCTIONS_PLAN)
                elif cfg["plan_tracking"]:   # log_feedback writes the app's database
                    want = server_instructions.INSTRUCTIONS_TRACKING
                else:
                    want = server_instructions.INSTRUCTIONS
                self.assertEqual(server.instructions, want)
                self.assertEqual(kinds, frozenset(garmin_paths.WRITE_PATHS) if cfg["plan_writes"] else frozenset())
                if not cfg["plan_tools"]:
                    self.assertEqual(sha(server.instructions),
                                     MAIN_KEYMODE_INSTRUCTIONS_DIGEST if key else MAIN_INSTRUCTIONS_SHA256)
                garmin_paths.allow_plan_writes(())

    def test_narrowing_keeps_the_write_tools_and_their_guards(self):
        """GARMIN_ENABLED_TOOLS narrows the read tools only: the switch-on write tools stay, with their hints,
        the plan instructions and the admitted write kinds."""
        env = {**self.base, "GARMIN_AUTH": "full", "HISTORY_SEARCH": "on", "PLAN_TOOLS": "on", "PLAN_WRITES": "on",
               "PLAN_TRACKING": "on", "GARMIN_ENABLED_TOOLS": "plan_get,get_stats"}
        launch, server, stderr, kinds = self.serve(env)
        tools = {t.name: t for t in asyncio.run(server.list_tools())}
        self.assertEqual(set(tools), {"plan_get", "get_stats"} | GARMIN_WRITES | APPLY | FEEDBACK)
        self.assertEqual(server.instructions, server_instructions.INSTRUCTIONS_PLAN_TRACKING)
        self.assertEqual(kinds, frozenset(garmin_paths.WRITE_PATHS))
        for name in WRITE_TOOLS:
            self.assertFalse(tools[name].annotations.read_only_hint)
        # The argument checks still run for a narrowed-in write tool.
        result = asyncio.run(server.call_tool("plan_commit", {"plan_id": "x", "commit_token": "x"}))
        self.assertTrue(result.is_error)
        self.assertTrue(result.content[0].text.startswith("Error: Invalid input"))
