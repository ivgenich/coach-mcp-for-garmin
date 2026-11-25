"""Tests for launch.py: key mode (GARMIN_AUTH=key): the tool policy, the config check, main() and the
server with Garmin's Personal API key.

    cd ext && python -m unittest test_launch_key_mode

Shared helpers: test_launch_support.py.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import os
import re
import secrets
import stat
import subprocess
import sys
import time
import unittest
from unittest import mock

import mcp2_compat  # noqa: E402
import server_instructions  # noqa: E402
import version  # noqa: E402
import tool_errors  # noqa: E402

from test_launch_support import (A2, A3, call_tools, captured_records, ConfigCommandRunner, DOCKERFILE_FILE,
    enabled_tools, ENTRYPOINT_FILE, ERAS, fake_key, handshake, HAVE_STATUS_MODULES, HAVE_UPSTREAM, key_env,
    KEY_MODE_TOOL_NAMES, load_launch, read, RealServerCase, ROOT, serve_key_mode, sign_in_env, Stubs, temp_dir,
    tool_error_records, UPSTREAM_MODULES, UpstreamCase)
AUTH_UNSET = ("GARMIN_AUTH is not set: set it to full (log in to Garmin once with your email and password; every "
              "tool) or key (a Garmin Personal API key; fewer tools and less data), then restart the server")
AUTH_INVALID = "GARMIN_AUTH must be full or key; the value set is neither (it is not shown)"
KEY_MISSING = ("GARMIN_AUTH=key needs GARMIN_PERSONAL_API_KEY: make a Personal API key in Garmin Connect on the web "
               "and set it as this variable")
KEY_UNUSED = ("GARMIN_PERSONAL_API_KEY is set but GARMIN_AUTH=full does not use it: delete the variable or leave it "
              "empty, or set GARMIN_AUTH=key")
NOT_IN_KEY_MODE = ("GARMIN_ENABLED_TOOLS: get_training_status is not available with GARMIN_AUTH=key; remove it, or "
                   "set GARMIN_AUTH=full")
E1 = ("EDGE_CLIENT_IP_HEADER is set but empty: delete the variable (Railway) or set CF-Connecting-IP (Docker "
      "Compose behind a Cloudflare Tunnel)")
E2 = "EDGE_CLIENT_IP_HEADER must be X-Real-IP or CF-Connecting-IP; the value set is neither"
KEY_OK = "launch: config OK ({} read-only tools, Garmin Personal API key)"


class KeyModeToolPolicy(unittest.TestCase):
    """resolve_tools(..., available=...): key mode narrows the list (stdlib)."""

    def setUp(self):
        self.launch = load_launch(self, Stubs().modules)
        self.baked = {"get_stats", "get_training_status", "get_daily_status"}
        self.read_only = self.baked | {"get_courses"}

    def resolve(self, env, available=frozenset({"get_stats", "get_daily_status"})):
        return self.launch.resolve_tools(env, self.baked, self.read_only, {"delete_activity"}, available)

    def test_available_narrows_the_list(self):
        self.assertEqual(self.resolve({}), (frozenset({"get_stats", "get_daily_status"}), []))
        self.assertEqual(self.resolve({"GARMIN_ENABLED_TOOLS": "GET_STATS"}), (frozenset({"get_stats"}), []))
        # Full mode (no `available`) is unchanged.
        self.assertEqual(self.resolve({}, None), (frozenset(self.baked), []))

    def test_a_tool_key_mode_lacks_is_its_own_line(self):
        allowed, errors = self.resolve({"GARMIN_ENABLED_TOOLS": "get_stats,get_training_status"})
        self.assertEqual((allowed, errors), (frozenset(), [NOT_IN_KEY_MODE]))
        # Other names keep their lines.
        allowed, errors = self.resolve({"GARMIN_ENABLED_TOOLS": "delete_activity,get_stat,get_courses"})
        self.assertEqual(len(errors), 3, errors)
        self.assertIn("delete_activity is a write tool", errors[0])


class KeyModeCheckConfig(unittest.TestCase):
    """check_config() and --check-config's lines with GARMIN_AUTH and EDGE_CLIENT_IP_HEADER, the tool
    policy stubbed: standard library only."""

    def setUp(self):
        self.launch = load_launch(self, Stubs().modules)
        patcher = mock.patch.object(self.launch, "allowed_tools", return_value=frozenset({"get_stats"}))
        self.allowed_tools = patcher.start()
        self.addCleanup(patcher.stop)

    def cli(self, env):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            code = self.launch._check_config_cli(env)
        return code, err.getvalue().splitlines()

    def test_garmin_auth_is_required(self):
        for value in (None, "", "   "):
            env = {"REPORT_TZ": "UTC"} if value is None else {"REPORT_TZ": "UTC", "GARMIN_AUTH": value}
            with self.subTest(value=value):
                self.assertEqual(self.cli(env), (1, ["launch: config error: " + AUTH_UNSET]))

    def test_an_invalid_mode_is_refused_without_showing_it(self):
        sentinel = fake_key()
        for value in ("junk", sentinel, "full key", "fulll"):
            with self.subTest(value=value):
                code, lines = self.cli({"REPORT_TZ": "UTC", "GARMIN_AUTH": value})
                self.assertEqual((code, lines), (1, ["launch: config error: " + AUTH_INVALID]))
        self.assertNotIn(sentinel, "".join(lines))

    def test_modes_ignore_case_and_spaces(self):
        cache = os.path.join(temp_dir(self), "garmin-key", "token.json")
        for value in ("full", " Full ", "FULL"):
            with self.subTest(value=value):
                self.assertEqual(self.cli({"REPORT_TZ": "UTC", "GARMIN_AUTH": value}),
                                 (0, ["launch: config OK (1 read-only tools)"]))
        for value in ("key", " KEY ", "Key"):
            with self.subTest(value=value):
                key = fake_key()
                env = {"REPORT_TZ": "UTC", "GARMIN_AUTH": value, "GARMIN_PERSONAL_API_KEY": key,
                       "GARMIN_KEY_TOKEN_CACHE": cache}
                code, lines = self.cli(env)
                self.assertEqual((code, lines), (0, [KEY_OK.format(1)]))
                self.assertNotIn(key, "".join(lines))
                self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(cache)).st_mode), 0o700)

    def test_key_settings(self):
        key = fake_key()
        self.assertEqual(self.cli({"REPORT_TZ": "UTC", "GARMIN_AUTH": "key"}),
                         (1, ["launch: config error: " + KEY_MISSING]))
        self.assertEqual(self.cli({"REPORT_TZ": "UTC", "GARMIN_AUTH": "full", "GARMIN_PERSONAL_API_KEY": key}),
                         (1, ["launch: config error: " + KEY_UNUSED]))
        # An empty or all-space key counts as unset in both modes.
        self.assertEqual(self.cli({"REPORT_TZ": "UTC", "GARMIN_AUTH": "full", "GARMIN_PERSONAL_API_KEY": "  "}),
                         (0, ["launch: config OK (1 read-only tools)"]))

    def test_order_tool_policy_mode_report_tz_edge_sign_in(self):
        self.allowed_tools.side_effect = self.launch.ConfigError(["a tool problem"])
        env = sign_in_env(os.path.join(temp_dir(self), "auth.sqlite"), PUBLIC_URL="http://example.com/mcp",
                          GARMIN_AUTH=None, EDGE_CLIENT_IP_HEADER="")
        env = {k: v for k, v in env.items() if v is not None}
        allowed, errors = self.launch.check_config(env)
        self.assertEqual(allowed, frozenset())
        self.assertEqual(errors[:2], ["a tool problem", AUTH_UNSET])
        self.assertTrue(errors[2].startswith("REPORT_TZ is not set"), errors)
        self.assertEqual(errors[3:], [E1, A2, A3])
        # The same order in key mode.
        env = dict(env, GARMIN_AUTH="key", EDGE_CLIENT_IP_HEADER="X-Forwarded-For")
        errors = self.launch.check_config(env)[1]
        self.assertEqual(errors[:2], ["a tool problem", KEY_MISSING])
        self.assertEqual(errors[3:], [E2, A2, A3])

    def test_edge_header_in_both_modes(self):
        sentinel = "TEST-ONLY-" + secrets.token_hex(6)
        for mode_env in ({"GARMIN_AUTH": "full"}, key_env(self)):
            for value, lines in (("X-Real-IP", []), (" CF-Connecting-IP ", []), ("", [E1]), (sentinel, [E2])):
                with self.subTest(mode=mode_env["GARMIN_AUTH"], value=value):
                    errors = self.launch.check_config({**mode_env, "REPORT_TZ": "UTC",
                                                       "EDGE_CLIENT_IP_HEADER": value})[1]
                    self.assertEqual(errors, lines)
                    self.assertNotIn(sentinel, "".join(errors))


@unittest.skipUnless(HAVE_UPSTREAM, "needs upstream garmin_mcp on mcp 2.x")
class KeyModeAllowedTools(UpstreamCase):
    """allowed_tools() against upstream's real classification."""

    def setUp(self):
        super().setUp()
        self.launch = load_launch(self, Stubs().modules)

    def test_key_mode_allows_exactly_the_key_mode_tools(self):
        import key_mode

        self.assertEqual(key_mode.KEY_MODE_TOOLS, KEY_MODE_TOOL_NAMES)
        self.assertEqual(self.launch.allowed_tools({"GARMIN_AUTH": " Key "}), KEY_MODE_TOOL_NAMES)
        self.assertLessEqual(KEY_MODE_TOOL_NAMES, set(enabled_tools()))
        # Full mode, and an unset or invalid mode (its own line comes from key_mode.check()), keep the 31.
        for env in ({"GARMIN_AUTH": "full"}, {}, {"GARMIN_AUTH": "junk"}):
            with self.subTest(env=env):
                self.assertEqual(self.launch.allowed_tools(env), frozenset(enabled_tools()))
                self.assertEqual(len(self.launch.allowed_tools(env)), 31)

    def test_enabled_tools_narrows_inside_key_mode(self):
        env = {"GARMIN_AUTH": "key", "GARMIN_ENABLED_TOOLS": "get_stats,get_daily_status"}
        self.assertEqual(self.launch.allowed_tools(env), {"get_stats", "get_daily_status"})
        with self.assertRaises(self.launch.ConfigError) as cm:
            self.launch.allowed_tools(dict(env, GARMIN_ENABLED_TOOLS="get_stats,get_training_status"))
        self.assertEqual(cm.exception.errors, [NOT_IN_KEY_MODE])


@unittest.skipUnless(HAVE_UPSTREAM, "needs upstream garmin_mcp on mcp 2.x")
class KeyModeCheckConfigCommand(ConfigCommandRunner, unittest.TestCase):
    """`python launch.py --check-config` in key mode, in a child process."""

    def test_key_mode_ok_line_and_no_key_anywhere(self):
        env = key_env(self, REPORT_TZ="Europe/London")
        code, out, err = self.run_command(env)
        self.assertEqual((code, err.splitlines(), out), (0, [KEY_OK.format(13)], ""))
        self.assertNotIn(env["GARMIN_PERSONAL_API_KEY"], out + err)
        self.assertNotIn(env["GARMIN_PERSONAL_API_KEY"][3:], out + err)

    def test_garmin_auth_unset_stops_the_server(self):
        code, lines = self.run_check(REPORT_TZ="Europe/London", GARMIN_AUTH=None)
        self.assertEqual((code, lines), (1, ["launch: config error: " + AUTH_UNSET]))

    def test_a_tool_key_mode_lacks(self):
        env = key_env(self, REPORT_TZ="Europe/London", GARMIN_ENABLED_TOOLS="get_stats,get_training_status")
        code, lines = self.run_check(**env)
        self.assertEqual((code, lines), (1, ["launch: config error: " + NOT_IN_KEY_MODE]))


@unittest.skipUnless(HAVE_UPSTREAM, "needs upstream garmin_mcp on mcp 2.x")
class KeyModeMain(UpstreamCase):
    """launch.main() in key mode with the status modules stubbed and key_mode.install() recorded: the tools,
    the instructions, the version, and the key out of os.environ before install() runs."""

    def serve(self, env):
        return serve_key_mode(self, env)

    def test_key_mode_serves_the_key_tools_and_texts(self):
        env = key_env(self, GARMIN_PERSONAL_API_BASE_URL="")
        launch, stubs, server, installed, err, code = self.serve(env)
        self.assertIsNone(code, err)
        self.assertEqual({t.name for t in asyncio.run(server.list_tools())}, KEY_MODE_TOOL_NAMES)
        self.assertTrue(launch._KEY_MODE)
        self.assertEqual(stubs.key_mode, {"daily_status": True, "weekly_status": True})
        self.assertEqual(server.instructions, server_instructions.INSTRUCTIONS_KEY)
        self.assertEqual((server.name, server.version), ("coach-mcp-for-garmin", version.VERSION))
        self.assertEqual(mcp2_compat._server_overrides, {"name": "coach-mcp-for-garmin", "version": version.VERSION,
                                                         "instructions": server_instructions.INSTRUCTIONS_KEY})
        ((key, cache_file, seen, _),) = installed
        self.assertEqual((key, cache_file), (env["GARMIN_PERSONAL_API_KEY"], env["GARMIN_KEY_TOKEN_CACHE"]))
        # Gone from the environment before install() runs; the cache path stays (internal, not a secret).
        self.assertIsNone(seen["GARMIN_PERSONAL_API_KEY"])
        self.assertIsNone(seen["GARMIN_PERSONAL_API_BASE_URL"])
        self.assertEqual(seen["GARMIN_KEY_TOKEN_CACHE"], env["GARMIN_KEY_TOKEN_CACHE"])
        self.assertNotIn(env["GARMIN_PERSONAL_API_KEY"], err)

    def test_logging_is_set_up_before_install(self):
        # Else install()'s INFO line "garmin key mode: on, token cache <path>" (4.11) is dropped.
        env = key_env(self)
        _, _, _, installed, err, code = self.serve(env)
        self.assertIsNone(code, err)
        self.assertEqual(len(installed), 1)
        self.assertIn("logging", self.order)
        self.assertLess(self.order.index("logging"), self.order.index("install"))

    def test_full_mode_passes_full_to_the_status_tools(self):
        launch, stubs, server, installed, err, code = self.serve({"GARMIN_AUTH": "full"})
        self.assertIsNone(code, err)
        self.assertFalse(launch._KEY_MODE)
        self.assertEqual(installed, [])
        self.assertEqual(stubs.key_mode, {"daily_status": False, "weekly_status": False})
        self.assertEqual(server.instructions, server_instructions.INSTRUCTIONS)
        self.assertEqual(len(asyncio.run(server.list_tools())), 31)

    def test_main_refuses_a_missing_mode_or_key(self):
        for env, line in (({}, AUTH_UNSET), ({"GARMIN_AUTH": "key"}, KEY_MISSING),
                          ({"GARMIN_AUTH": "nope"}, AUTH_INVALID)):
            with self.subTest(env=env):
                _, stubs, server, installed, err, code = self.serve(env)
                self.assertEqual((server, code, installed), (None, 1, []))
                self.assertEqual([x for x in err.splitlines() if x.startswith("launch:")],
                                 ["launch: config error: " + line])
                self.assertEqual(stubs.calls, [], "nothing is patched before the mode is known")

    def test_patch_refuses_an_unknown_mode(self):
        launch = load_launch(self, Stubs().modules)
        with self.assertRaises(ValueError):
            launch.patch(garmin_auth="nope")


class HttpError(Exception):
    """Named like Garmin's client's HttpError (garmin_personal_api), which key mode reads for its status code
    only; its message holds Garmin's answer body, which must reach no log line."""

    def __init__(self, status_code, body):
        super().__init__(f"POST https://garmin.invalid/token returned HTTP {status_code}: {body}")
        self.status_code = status_code


@unittest.skipUnless(HAVE_UPSTREAM and HAVE_STATUS_MODULES,
                     "needs upstream garmin_mcp and ext/daily_status.py, weekly_status.py")
class KeyModeServer(RealServerCase):
    """The real launch.main() server in key mode with the real key_mode.install() and a fake authenticator
    that refuses the key (Garmin's token endpoint answering 401): no network, no Garmin client library needed."""

    def setUp(self):
        super().setUp()
        import garmin_mcp

        self.addCleanup(setattr, garmin_mcp, "init_api", garmin_mcp.init_api)
        self.original_init_api = garmin_mcp.init_api
        self.env = key_env(self, GARMIN_PERSONAL_API_BASE_URL="")
        os.environ.update(self.env)  # RealServerCase's patch.dict restores os.environ
        self.body = "TEST-ONLY-body-" + secrets.token_hex(8)
        self.token_requests = []

    def serve_key(self):
        import key_mode

        test, real_install = self, key_mode.install

        class Authenticator:
            def obtain_token(self, api_key):
                test.token_requests.append(api_key == test.env["GARMIN_PERSONAL_API_KEY"])
                raise HttpError(401, test.body)

        def install(key, cache_file=None):
            return real_install(key, cache_file, authenticator=Authenticator(), http=object())

        # Unlike RealServerCase.serve(), upstream's own login lambda runs: it calls the init_api key_mode installed.
        with mock.patch.object(key_mode, "install", install):
            return self.serve_with_login()

    def serve_with_login(self):
        """RealServerCase.serve() without its own login stub."""
        import garmin_mcp
        from mcp.server.mcpserver import MCPServer

        self.courses.register_tools, self.annotations.READ_ONLY_TOOLS, garmin_mcp.FastMCP = self.pristine
        self.launch = load_launch(self)
        self.addCleanup(setattr, sys, "stdout", sys.stdout)
        for name in UPSTREAM_MODULES:
            module = getattr(garmin_mcp, name)
            self.addCleanup(setattr, module, "garmin_client", getattr(module, "garmin_client", None))
        served = []
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(MCPServer, "run", lambda server, *a, **k: served.append(server)))
            stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
            self.launch.main([])
            pending = garmin_mcp.activity_management.garmin_client._client
            deadline = time.monotonic() + 30
            while pending.login_state() == "pending" and time.monotonic() < deadline:
                time.sleep(0.01)
        self.pending = pending
        (server,) = served
        return server

    def test_refused_key(self):
        import garmin_mcp

        with captured_records("garmin_ext") as startup:
            server = self.serve_key()
        key = self.env["GARMIN_PERSONAL_API_KEY"]
        self.assertIsNot(garmin_mcp.init_api, self.original_init_api)
        self.assertEqual(self.token_requests, [True])
        # /readyz says failed (upstream's _readiness reads this state).
        self.assertEqual(self.pending.login_state(), "failed")
        self.assertEqual(garmin_mcp._readiness(self.pending), (503, {"status": "failed"}))
        # The key and the base-URL variable left os.environ; the cache folder is private.
        self.assertNotIn("GARMIN_PERSONAL_API_KEY", os.environ)
        self.assertNotIn("GARMIN_PERSONAL_API_BASE_URL", os.environ)
        cache_dir = os.path.dirname(self.env["GARMIN_KEY_TOKEN_CACHE"])
        self.assertEqual(stat.S_IMODE(os.stat(cache_dir).st_mode), 0o700)
        login = tool_errors.client_text(tool_errors.LOGIN, key_mode=True)
        self.assertNotEqual(login, tool_errors.client_text(tool_errors.LOGIN))
        self.assertTrue(login.startswith("Error: Garmin login missing or expired."))
        for mode in ERAS:
            with self.subTest(mode=mode):
                result, info, tools = handshake(server, mode)
                self.assertEqual(result.instructions, server_instructions.INSTRUCTIONS_KEY)
                self.assertEqual((info.name, info.version), ("coach-mcp-for-garmin", version.VERSION))
                self.assertEqual(set(tools), KEY_MODE_TOOL_NAMES)
                with captured_records() as records:
                    answered = call_tools(server, mode, [("get_stats", {"date": "2026-11-10"}),
                                                         ("get_daily_status", {}), ("get_weekly_status", {})])
                for result in answered:
                    self.assertTrue(result.is_error)
                    self.assertEqual([block.text for block in result.content], [login])
                lines = [r.getMessage() for r in tool_error_records(records)]
                self.assertEqual(len(lines), 3, lines)
                self.assertTrue(all(" group=login: " in line for line in lines), lines)
                for record in records + startup:
                    text = record.getMessage()
                    self.assertNotIn(key, text)
                    self.assertNotIn(self.body, text)
        # The 15-minute latch: no second token request for the calls above.
        self.assertEqual(self.token_requests, [True])
        self.assertTrue(any("Garmin refused the Personal API key (HTTP 401)" in r.getMessage() for r in startup))


class KeyModeFiles(unittest.TestCase):
    """The key-mode lines of entrypoint.sh, the Dockerfile and the pins; standard library only."""

    def test_entrypoint_mode_case(self):
        text = read(ENTRYPOINT_FILE)
        start = text.index('case "$(printf')
        block = text[start:text.index("esac", start) + 4]
        for value, expected in (("key", "yes"), (" KEY ", "yes"), ("Key\n", "yes"), ("full", "no"), ("", "no")):
            with self.subTest(value=value):
                proc = subprocess.run(["sh", "-c", 'set -eu\n' + block + '\nprintf %s "$key_mode"'],
                                      env={"PATH": os.environ.get("PATH", ""), "GARMIN_AUTH": value},
                                      stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
                self.assertEqual((proc.returncode, proc.stdout), (0, expected), proc.stderr)

    def test_entrypoint_leaves_the_token_cache_alone(self):
        text = read(ENTRYPOINT_FILE)
        self.assertNotIn("GARMIN_KEY_TOKEN_CACHE", text)
        self.assertNotIn("garmin-key", text)
        code = [line.strip() for line in text.splitlines() if not line.lstrip().startswith("#")]
        chmods = [line for line in code if "chmod " in line]
        self.assertEqual(len(chmods), 1, chmods)
        self.assertIn('(run_as app chmod -- 700 "$GARMINTOKENS" </dev/null)', chmods[0])

    def test_dockerfile_installs_the_client_by_hash(self):
        text = read(DOCKERFILE_FILE)
        self.assertIn("COPY config/garmin-key-client.txt /tmp/garmin-key-client.txt", text)
        self.assertIn("pip install --require-hashes --no-deps -r /tmp/garmin-key-client.txt", text)
        install_c = text[text.index("pip install"):text.index("--require-hashes")]
        self.assertNotIn("garmin-connect-personal-api-python", install_c)
        self.assertNotIn("HEALTHCHECK", text)
        lines = text.splitlines()
        i = lines.index("# hadolint ignore=DL3064")
        env_block = []
        for line in lines[i + 1:]:
            env_block.append(line)
            if not line.endswith("\\"):
                break
        env_text = "\n".join(env_block)
        self.assertTrue(env_text.startswith("ENV "), env_text)
        self.assertIn("GARMINTOKENS_BASE64=/data/tokens/garmin_tokens.base64", env_text)
        self.assertIn("GARMIN_KEY_TOKEN_CACHE=/data/garmin-key/token.json", env_text)
        # The ignore line covers only the next instruction: no other ENV names a TOKEN or KEY variable.
        others = text.replace(env_text, "")
        self.assertNotRegex(others, r"(?m)^ENV [^\n]*(TOKEN|KEY)")

    def test_hash_pin_matches_constraints(self):
        with open(os.path.join(ROOT, "config", "garmin-key-client.txt"), encoding="utf-8") as f:
            reqs = [line.strip() for line in f if line.strip() and not line.lstrip().startswith("#")]
        (req,) = reqs
        match = re.fullmatch(r"garmin-connect-personal-api-python==(\S+) --hash=sha256:([0-9a-f]{64})", req)
        self.assertIsNotNone(match, req)
        constraints = read(os.path.join(ROOT, "constraints.txt"))
        self.assertRegex(constraints, rf"(?m)^garmin-connect-personal-api-python=={re.escape(match.group(1))}$")
        self.assertRegex(constraints, r"(?m)^certifi==2026\.10\.23$")
        self.assertRegex(constraints, r"(?m)^garminconnect==0\.3\.2$")


if __name__ == "__main__":
    unittest.main()
