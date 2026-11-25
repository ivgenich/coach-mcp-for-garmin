"""Deep tests (release test phase): config/enabled-tools*.txt and GARMIN_ENABLED_TOOLS against the switches.

A baked list or GARMIN_ENABLED_TOOLS naming a write tool (this repo's or upstream's), a plan or history tool,
an unknown tool, a look-alike or a secret is an error that serves nothing; narrowing never adds a tool and
never takes away a switch's write tool or its guards.

    cd ext && python -m unittest test_deep_config_tools
"""

from __future__ import annotations

import base64
import os
import secrets
import unittest
from unittest import mock

import feature_config
import key_mode

from test_launch_support import (enabled_tools, fake_db_url, FULL, HAVE_UPSTREAM, key_env, KEY_MODE_TOOL_NAMES,
                                 load_launch, Stubs, UpstreamCase)

HERE = os.path.dirname(os.path.abspath(__file__))
KEY_FILE = os.path.join(HERE, os.pardir, "config", "enabled-tools-key.txt")
OWN_WRITES = frozenset({"plan_commit", "plan_cancel", "plan_apply_changes", "log_feedback"})
SWITCH_TOOLS = frozenset({"find_similar_weeks", "search_history", "plan_assess", "plan_preview", "plan_get",
                          "plan_review_week"}) | OWN_WRITES
NOT_SHOWN = "GARMIN_ENABLED_TOOLS: an entry is not a tool name (it is not shown)"


def token_key():
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()


class BakedFiles(unittest.TestCase):
    """The two files in config/ never name a switch tool, a write tool or a duplicate (standard library)."""

    def test_neither_file_names_a_switch_or_write_tool(self):
        launch = load_launch(self, Stubs().modules)
        with open(KEY_FILE, encoding="utf-8") as f:
            key_names = launch.parse_tool_list(f.read())
        for label, names in (("enabled-tools.txt", enabled_tools()), ("enabled-tools-key.txt", key_names)):
            with self.subTest(file=label):
                self.assertFalse(set(names) & SWITCH_TOOLS)
                self.assertFalse(set(names) & launch.OWN_WRITE_TOOLS)
                self.assertEqual(len(names), len(set(names)))
                self.assertTrue(all(launch.TOOL_NAME_SHAPE.fullmatch(n) for n in names))
        self.assertEqual(frozenset(key_names), KEY_MODE_TOOL_NAMES)
        self.assertLessEqual(frozenset(key_names), frozenset(enabled_tools()))

    def test_the_switch_sets_are_disjoint_where_they_must_be(self):
        launch = load_launch(self, Stubs().modules)
        self.assertFalse(launch.OWN_WRITE_TOOLS & launch.OWN_READ_TOOLS)
        self.assertEqual(launch.OWN_WRITE_TOOLS, OWN_WRITES)
        self.assertEqual(launch.GARMIN_WRITE_TOOLS, {"plan_commit", "plan_cancel", "plan_apply_changes"})
        self.assertEqual(set(launch.WRITE_EFFECTS), launch.GARMIN_WRITE_TOOLS)
        self.assertFalse(launch.NO_GARMIN_TOOLS & launch.GARMIN_WRITE_TOOLS)
        self.assertIn("plan_review_week", launch.OWN_READ_TOOLS)
        self.assertNotIn("plan_review_week", launch.NO_GARMIN_TOOLS)


class ResolveTools(unittest.TestCase):
    """resolve_tools() alone (standard library): the baked list and GARMIN_ENABLED_TOOLS."""

    READ = frozenset({"get_stats", "get_activity", "plan_get"})
    WRITE = frozenset({"delete_activity", "plan_commit", "log_feedback"})

    def setUp(self):
        self.launch = load_launch(self, Stubs().modules)

    def resolve(self, environ, baked=("get_stats", "get_activity"), available=None):
        return self.launch.resolve_tools(environ, baked, self.READ, self.WRITE, available)

    def test_baked_list_errors(self):
        for baked, word in ((["get_stats", "plan_commit"], "a write tool"), (["get_stats", "delete_activity"],
                            "a write tool"), (["get_stats", "made_up_tool"], "not a known read-only tool"),
                            (["get_stats", "log_feedback"], "a write tool")):
            with self.subTest(baked=baked):
                allowed, errors = self.resolve({}, baked)
                self.assertEqual(allowed, frozenset())
                self.assertEqual(len(errors), 1)
                self.assertIn(word, errors[0])
        allowed, errors = self.resolve({}, [])
        self.assertEqual((allowed, errors), (frozenset(), ["config/enabled-tools.txt lists no tools"]))

    def test_enabled_tools_errors(self):
        cases = {
            "plan_commit": "plan_commit is a write tool", "PLAN_COMMIT": "plan_commit is a write tool",
            " plan_commit ": "plan_commit is a write tool", "delete_activity": "delete_activity is a write tool",
            "log_feedback": "log_feedback is a write tool", "plan_get": "plan_get is not in config/enabled-tools.txt",
            "made_up_tool": "made_up_tool is an unknown tool", "Made_Up_Tool": NOT_SHOWN,
            "plan_c\u043emmit": NOT_SHOWN, "plan_commit\u200b": "'plan_commit\\u200b' is an unknown tool", "sk-" + "A" * 40: NOT_SHOWN,
            "get_stats;rm -rf /": NOT_SHOWN, "a" * 300 + "_b": NOT_SHOWN,
        }
        for value, want in cases.items():
            with self.subTest(value=repr(value)[:40]):
                allowed, errors = self.resolve({"GARMIN_ENABLED_TOOLS": "get_stats," + value})
                self.assertEqual(allowed, frozenset())
                self.assertTrue(any(want in e for e in errors), errors)
                if want == NOT_SHOWN:
                    self.assertNotIn(value.strip().lower(), "\n".join(errors))

    def test_blank_entries(self):
        for value in (",", " , ,", ",,,"):
            with self.subTest(value=value):
                allowed, errors = self.resolve({"GARMIN_ENABLED_TOOLS": value})
                self.assertEqual(allowed, frozenset())
                self.assertEqual(errors, ["GARMIN_ENABLED_TOOLS names no tools; unset it to expose the whole list"])
        for value in ("", "   ", None):
            with self.subTest(value=value):
                env = {} if value is None else {"GARMIN_ENABLED_TOOLS": value}
                self.assertEqual(self.resolve(env), (frozenset({"get_stats", "get_activity"}), []))

    def test_narrowing_only_narrows(self):
        self.assertEqual(self.resolve({"GARMIN_ENABLED_TOOLS": "GET_STATS, get_stats ,get_stats"}),
                         (frozenset({"get_stats"}), []))
        allowed, errors = self.resolve({"GARMIN_ENABLED_TOOLS": "get_stats"}, available={"get_activity"})
        self.assertEqual(allowed, frozenset())
        self.assertEqual(errors, [key_mode.ERR_TOOL_NOT_IN_KEY_MODE.format(name="get_stats")])

    def test_disabled_tools_is_refused_whatever_its_value(self):
        for value in ("get_stats", "x", " plan_commit "):
            with self.subTest(value=value):
                allowed, errors = self.resolve({"GARMIN_DISABLED_TOOLS": value})
                self.assertEqual(allowed, frozenset())
                self.assertTrue(errors[0].startswith("GARMIN_DISABLED_TOOLS is no longer supported"))
        self.assertEqual(self.resolve({"GARMIN_DISABLED_TOOLS": "  "})[1], [])


@unittest.skipUnless(HAVE_UPSTREAM, "needs upstream garmin_mcp on mcp 2.x")
class AllowedToolsWithLists(UpstreamCase):
    """allowed_tools() with a baked file and GARMIN_ENABLED_TOOLS that try to add what only a switch may."""

    def setUp(self):
        super().setUp()
        self.launch = load_launch(self, Stubs().modules)
        self.base = {**FULL, "REPORT_TZ": "UTC", "DATABASE_URL": fake_db_url(), "HISTORY_TOKEN_KEY": token_key()}
        self.all_on = {**self.base, "HISTORY_SEARCH": "on", "PLAN_TOOLS": "on", "PLAN_WRITES": "on",
                       "PLAN_TRACKING": "on"}

    def allowed(self, env, baked=None):
        if baked is None:
            return self.launch.allowed_tools(env)
        with mock.patch.object(self.launch, "read_enabled_tools_file", return_value=list(baked)):
            return self.launch.allowed_tools(env)

    def errors(self, env, baked=None):
        with self.assertRaises(self.launch.ConfigError) as cm:
            self.allowed(env, baked)
        return cm.exception.errors

    def test_the_baked_file_cannot_add_a_switch_or_write_tool(self):
        upstream_writes = sorted(self.annotations.ADDITIVE_TOOLS | self.annotations.DESTRUCTIVE_TOOLS)
        self.assertTrue(upstream_writes)
        for name in sorted(SWITCH_TOOLS) + upstream_writes[:5] + ["made_up_tool"]:
            for env in (self.base, self.all_on):
                if env is self.all_on and name in SWITCH_TOOLS - OWN_WRITES:
                    continue   # with its switch on, a read tool in the file is just listed twice
                with self.subTest(name=name, on=env is self.all_on):
                    errors = self.errors(env, enabled_tools() + [name])
                    self.assertEqual(len(errors), 1, errors)
                    self.assertIn(f"config/enabled-tools.txt: {name} is ", errors[0])

    def test_enabled_tools_cannot_name_a_write_tool_in_any_spelling(self):
        upstream_writes = sorted(self.annotations.ADDITIVE_TOOLS | self.annotations.DESTRUCTIVE_TOOLS)[:5]
        for name in sorted(OWN_WRITES) + upstream_writes:
            for spelled in (name, name.upper(), f" {name} ", f"get_stats,{name}", f"{name},{name}"):
                with self.subTest(spelled=spelled):
                    errors = self.errors({**self.all_on, "GARMIN_ENABLED_TOOLS": spelled})
                    self.assertIn(f"GARMIN_ENABLED_TOOLS: {name} is a write tool", "\n".join(errors))

    def test_enabled_tools_cannot_name_a_switch_tool_whose_switch_is_off(self):
        cases = (({}, "search_history"), ({"HISTORY_SEARCH": "on"}, "plan_get"),
                 ({"HISTORY_SEARCH": "on", "PLAN_TOOLS": "on"}, "plan_review_week"),
                 ({"HISTORY_SEARCH": "on", "PLAN_TOOLS": "on", "GARMIN_AUTH": "key"}, "plan_get"))
        for switches, name in cases:
            env = {**self.base, **switches}
            if env["GARMIN_AUTH"] == "key":
                env.update(key_env(self))
                env["PLAN_TOOLS"] = "off"
            with self.subTest(switches=sorted(switches), name=name):
                errors = self.errors({**env, "GARMIN_ENABLED_TOOLS": f"get_stats,{name}"})
                self.assertIn(f"GARMIN_ENABLED_TOOLS: {name} is an unknown tool", "\n".join(errors))

    def test_narrowing_keeps_every_switch_write_tool(self):
        for narrowed in ("get_stats", "plan_get", "search_history", "plan_review_week", "get_stats,plan_assess"):
            with self.subTest(narrowed=narrowed):
                allowed = self.allowed({**self.all_on, "GARMIN_ENABLED_TOOLS": narrowed})
                self.assertEqual(allowed, frozenset(narrowed.split(",")) | OWN_WRITES)

    def test_a_write_tool_in_upstreams_sets_is_never_added_by_a_switch(self):
        """If upstream ever named one of this repo's write tools, the switch must not serve upstream's."""
        import garmin_mcp.tool_annotations as annotations

        self.launch._UPSTREAM_WRITE.clear()
        with mock.patch.object(annotations, "DESTRUCTIVE_TOOLS", annotations.DESTRUCTIVE_TOOLS | {"plan_cancel"}):
            allowed = self.allowed(self.all_on)
        self.launch._UPSTREAM_WRITE.clear()
        self.assertNotIn("plan_cancel", allowed)
        self.assertIn("plan_commit", allowed)

    def test_check_config_names_the_bad_entries_and_serves_nothing(self):
        import contextlib
        import io

        secret = "TEST-ONLY-" + secrets.token_urlsafe(24)
        env = {**self.all_on, "GARMIN_ENABLED_TOOLS": f"plan_commit,{secret},get_stats"}
        with mock.patch.object(self.launch, "_has_module", return_value=True), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            code = self.launch._check_config_cli(env)
        out = err.getvalue()
        self.assertEqual(code, 1)
        self.assertIn("GARMIN_ENABLED_TOOLS: plan_commit is a write tool", out)
        self.assertIn(NOT_SHOWN, out)
        self.assertNotIn(secret, out)
        self.assertNotIn(secret.lower(), out)
        self.assertNotIn("config OK", out)
        self.assertEqual(feature_config.problems(env), [])
