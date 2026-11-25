"""Tests for launch.py: the built-in sign-in: main() and --check-config with a network transport (SignInMain
runs main() with a stand-in for owner_auth and with the real one).

    cd ext && python -m unittest test_launch_signin

Shared helpers: test_launch_support.py.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import logging
import os
import secrets
import stat
import sys
import types
import unittest
from unittest import mock

import auth_http  # noqa: E402
import auth_store  # noqa: E402
import mcp2_compat  # noqa: E402
import server_instructions  # noqa: E402
import version  # noqa: E402
import tool_errors  # noqa: E402

from test_launch_support import (A2, A3, A5, A8, fake_secret, FULL, HAVE_UPSTREAM, KEY_VARS, load_launch,
    sign_in_env, Stubs, temp_dir, UPSTREAM_MODULES, UpstreamCase)
A12 = "GARMIN_MCP_TRANSPORT=sse cannot be used with the built-in sign-in: use streamable-http"
FRESH_START = "; deleting the file signs everyone out and starts fresh"
# Every path the sign-in serves.
SIGN_IN_PATHS = (
    "/authorize", "/token", "/register", "/revoke", "/.well-known/oauth-authorization-server",
    "/.well-known/oauth-protected-resource/mcp", "/mcp", "/consent", "/.well-known/oauth-protected-resource",
)
# Sign-in settings a test may set; any other value the test runner happens to
# have is cleared, so it cannot leak into a test.
SIGN_IN_VARS = ("GARMIN_MCP_TRANSPORT", "PUBLIC_URL", "RAILWAY_PUBLIC_DOMAIN", "RAILWAY_PROJECT_ID",
                "RAILWAY_ENVIRONMENT_ID", "RAILWAY_SERVICE_ID", "RAILWAY_DEPLOYMENT_ID", "OWNER_SECRET",
                "MCP_BEARER_TOKEN", "AUTH_DB", "ALLOWED_REDIRECT_URIS")


# --- The sign-in in main() ---------------------------------------------------
class Tagged:
    """ASGI middleware with one option, standing in for owner_auth's."""

    def __init__(self, app, tag):
        self.app, self.tag = app, tag

    async def __call__(self, scope, receive, send):
        await self.app(scope, receive, send)


class FakeOwnerAuth:
    """Stands in for owner_auth in sys.modules and records what launch.py
    calls. Its settings are real AuthSettings and its provider a bare
    object: MCPServer only stores the provider when it is built."""

    def __init__(self):
        from mcp.server.auth.settings import AuthSettings

        self.calls = []
        self.settings = AuthSettings(issuer_url="https://coach.example.com",
                                     resource_server_url="https://coach.example.com/mcp",
                                     validate_token_resource=True)
        self.provider = object()
        self.module = types.ModuleType("owner_auth")
        self.module.build = self.build
        self.module.asgi_middleware = self.asgi_middleware
        self.module.register_routes = self.register_routes

    def build(self, config):
        self.calls.append("build")
        self.config = config
        self.auth = types.SimpleNamespace(config=config, settings=self.settings, provider=self.provider)
        return self.auth

    def asgi_middleware(self, auth):
        self.calls.append("asgi_middleware")
        self.middleware_auth = auth
        return [(Tagged, {"tag": "registration filter"})]

    def register_routes(self, app, auth):
        self.calls.append("register_routes")
        self.routes_app, self.routes_auth = app, auth


class LogLines(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


@contextlib.contextmanager
def captured_logs(name="garmin_ext"):
    """The messages logged under `name` at INFO or above, kept away from the
    root logger's handlers."""
    logger = logging.getLogger(name)
    handler, saved = LogLines(), (logger.level, logger.propagate)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.addHandler(handler)
    try:
        yield handler.lines
    finally:
        logger.removeHandler(handler)
        logger.setLevel(saved[0])
        logger.propagate = saved[1]


def asgi_request(app, method, path, client_ip, extra_headers=(), body=b""):
    """One request to an ASGI app in process, as Caddy (a loopback peer)
    forwards it for `client_ip`: (status, headers). `extra_headers` are
    (name, value) text pairs added after Caddy's."""

    async def go():
        sent = []
        chunks = [{"type": "http.request", "body": body, "more_body": False}]

        async def receive():
            if chunks:
                return chunks.pop()
            return {"type": "http.disconnect"}

        async def send(message):
            sent.append(message)

        headers = [(b"host", b"127.0.0.1:8000"), (b"x-client-ip", client_ip.encode())]
        headers += [(name.lower().encode("latin-1"), value.encode("latin-1")) for name, value in extra_headers]
        if body:
            headers.append((b"content-length", str(len(body)).encode()))
        scope = {
            "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"}, "http_version": "1.1",
            "method": method, "scheme": "http", "path": path, "raw_path": path.encode(), "root_path": "",
            "query_string": b"", "headers": headers,
            "client": ("127.0.0.1", 50000), "server": ("127.0.0.1", 8000),
        }
        await app(scope, receive, send)
        return sent

    start = next(m for m in asyncio.run(go()) if m["type"] == "http.response.start")
    return start["status"], dict(start["headers"])


def uvicorn_stack(app, environ):
    """The ASGI app uvicorn serves for `app`, built as the SDK builds it
    (uvicorn.Config with its defaults, so proxy headers on) under `environ`,
    the environment main() handed to the server. Nothing is bound; log_config
    is None only to leave the test run's loggers alone."""
    import uvicorn

    with mock.patch.dict(os.environ):
        os.environ.pop("FORWARDED_ALLOW_IPS", None)
        if "FORWARDED_ALLOW_IPS" in environ:
            os.environ["FORWARDED_ALLOW_IPS"] = environ["FORWARDED_ALLOW_IPS"]
        config = uvicorn.Config(app, host="127.0.0.1", port=8331, log_level="info", log_config=None)
        config.load()
    return config.loaded_app


@unittest.skipUnless(HAVE_UPSTREAM, "needs upstream garmin_mcp on mcp 2.x")
class SignInMain(UpstreamCase):
    """launch.main() with a network transport, through upstream's main() up
    to app.run(), with the Garmin login and the server loop stubbed out."""

    def setUp(self):
        super().setUp()
        self.launch = load_launch(self, Stubs().modules)
        self.db = os.path.join(temp_dir(self), "auth.sqlite")
        # main() puts the query-string filter on uvicorn's access logger.
        access = logging.getLogger("uvicorn.access")
        self.addCleanup(setattr, access, "filters", list(access.filters))
        saved = sys.modules.get("owner_auth")
        self.addCleanup(self.restore_owner_auth, saved)
        self.addCleanup(self.forget_auth)

    @staticmethod
    def restore_owner_auth(saved):
        if saved is None:
            sys.modules.pop("owner_auth", None)
        else:
            sys.modules["owner_auth"] = saved

    def forget_auth(self):
        auth, self.launch._AUTH = self.launch._AUTH, None
        store = getattr(getattr(auth, "provider", None), "store", None)
        if store is not None:  # the real owner_auth keeps its database open
            store.close()

    def serve(self, env, owner_auth=None):
        """(served server or None, stderr, exit code or None, log lines)."""
        import garmin_mcp
        from mcp.server.mcpserver import MCPServer

        if owner_auth is not None:
            sys.modules["owner_auth"] = owner_auth
        self.addCleanup(setattr, sys, "stdout", sys.stdout)  # upstream's main() wraps it
        for name in UPSTREAM_MODULES:  # and configures each module's client
            mod = getattr(garmin_mcp, name)
            self.addCleanup(setattr, mod, "garmin_client", getattr(mod, "garmin_client", None))
        served = []
        self.run_environ = None

        def run(server, *args, **kwargs):
            served.append(server)
            self.run_environ = dict(os.environ)  # what uvicorn.Config would read

        base = {"GARMIN_MCP_STATELESS_HTTP": "true", "GARMIN_MCP_HOST": "127.0.0.1", "GARMIN_MCP_PORT": "8331",
                **FULL}
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.dict(os.environ, {**base, **env}))
            for key in ("GARMIN_ENABLED_TOOLS", "GARMIN_DISABLED_TOOLS", "FORWARDED_ALLOW_IPS") + SIGN_IN_VARS \
                    + KEY_VARS[1:]:
                if key not in env:
                    os.environ.pop(key, None)
            stack.enter_context(mock.patch.object(garmin_mcp._PendingGarminClient, "start", lambda self, login: None))
            stack.enter_context(mock.patch.object(MCPServer, "run", run))
            err = stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
            logs = stack.enter_context(captured_logs())
            try:
                self.launch.main([])
            except SystemExit as exc:
                self.assertEqual(served, [])
                return None, err.getvalue(), exc.code, logs
        (server,) = served
        return server, err.getvalue(), None, logs

    def test_main_registers_the_sign_in_secrets_for_redaction(self):
        bearer = fake_secret()
        env = sign_in_env(self.db, MCP_BEARER_TOKEN=bearer)
        _server, err, code, _logs = self.serve(env, FakeOwnerAuth().module)
        self.assertIsNone(code, err)
        for value in (env["OWNER_SECRET"], bearer):
            self.assertIn(value, tool_errors._SECRETS)
            self.assertNotIn(value, tool_errors.redact(f"a line with {value} in it"))

    def test_main_hands_the_sign_in_to_the_server(self):
        fake = FakeOwnerAuth()
        bearer = fake_secret()
        env = sign_in_env(self.db, MCP_BEARER_TOKEN=bearer)
        server, err, code, logs = self.serve(env, fake.module)
        self.assertIsNone(code, err)
        self.assertEqual(fake.calls, ["build", "asgi_middleware", "register_routes"])
        config = fake.config
        self.assertEqual((config.public_url, config.db_path, config.static_bearer),
                         ("https://coach.example.com", self.db, bearer))
        self.assertIs(self.launch._AUTH, fake.auth)
        self.assertIs(fake.middleware_auth, fake.auth)
        # The overrides carry the instructions and the sign-in's settings and provider into the server.
        self.assertEqual(mcp2_compat._server_overrides,
                         {"name": "coach-mcp-for-garmin", "version": version.VERSION,
                          "instructions": server_instructions.INSTRUCTIONS,
                          "auth": fake.settings, "auth_server_provider": fake.provider})
        self.assertEqual(server.instructions, server_instructions.INSTRUCTIONS)
        self.assertEqual(server.settings.auth, fake.settings)
        self.assertIs(server._auth_server_provider, fake.provider)
        # The rate limit first (outermost), then owner_auth's middleware.
        self.assertEqual(mcp2_compat._asgi_middleware,
                         [(auth_http.SignInRateLimit, {}), (Tagged, {"tag": "registration filter"})])
        # The courses hook adds the sign-in's routes to the app upstream's main() built.
        self.assertIs(fake.routes_auth, fake.auth)
        self.assertIs(fake.routes_app._app, server)
        self.assertTrue(any(isinstance(f, auth_http.DropQueryString) for f in logging.getLogger("uvicorn.access").filters))
        self.assertIn("sign-in: on, issuer https://coach.example.com, static bearer on", logs)
        for value in (bearer, env["OWNER_SECRET"]):
            self.assertNotIn(value, err + "\n".join(logs))

    def test_bad_sign_in_settings_exit_1_before_anything_is_built(self):
        fake = FakeOwnerAuth()
        env = sign_in_env(self.db, PUBLIC_URL="http://example.com/mcp")
        server, err, code, _ = self.serve(env, fake.module)
        self.assertEqual((server, code), (None, 1))
        lines = [line for line in err.splitlines() if line.startswith("launch:")]
        self.assertEqual(lines, ["launch: config error: " + A2, "launch: config error: " + A3])
        self.assertEqual(fake.calls, [])
        self.assertIsNone(self.launch._AUTH)
        self.assertFalse(os.path.exists(self.db))
        self.assertNotIn(env["OWNER_SECRET"], err)

    def test_stdio_has_no_sign_in_and_clears_left_over_middleware(self):
        fake = FakeOwnerAuth()
        mcp2_compat.set_asgi_middleware((Tagged, {"tag": "left over"}))
        server, err, code, _ = self.serve({"GARMIN_MCP_TRANSPORT": "stdio", "PUBLIC_URL": "http://example.com/mcp",
                                           **FULL}, fake.module)
        self.assertIsNone(code, err)
        self.assertEqual(fake.calls, [])
        self.assertIsNone(self.launch._AUTH)
        self.assertIsNone(server.settings.auth)
        self.assertEqual(mcp2_compat._server_overrides,
                         {"name": "coach-mcp-for-garmin", "version": version.VERSION,
                          "instructions": server_instructions.INSTRUCTIONS})
        self.assertEqual(mcp2_compat._asgi_middleware, [])

    def test_main_with_the_real_owner_auth(self):
        import owner_auth
        from mcp.server.auth.middleware.auth_context import AuthContextMiddleware
        from starlette.middleware.authentication import AuthenticationMiddleware

        env = sign_in_env(self.db)
        with mock.patch.object(auth_store, "SECRET_FP_ITERATIONS", 1000):
            server, err, code, logs = self.serve(env)
        self.assertIsNone(code, err)
        self.assertIsInstance(self.launch._AUTH, owner_auth.OwnerAuth)
        self.assertEqual(stat.S_IMODE(os.stat(self.db).st_mode), 0o600)
        app = server.streamable_http_app()
        self.assertLessEqual(set(SIGN_IN_PATHS), {getattr(route, "path", None) for route in app.routes})
        # The adapter's hook: the rate limit outermost, then owner_auth's
        # registration filter, Origin check and iss parameter, all outside the
        # SDK's own authentication.
        self.assertEqual([m.cls for m in app.user_middleware],
                         [auth_http.SignInRateLimit, owner_auth.RegistrationFilter,
                          owner_auth.OriginCheck, owner_auth.IssuerParameter,
                          AuthenticationMiddleware, AuthContextMiddleware])
        self.assertEqual(app.user_middleware[1].kwargs, owner_auth.asgi_middleware(self.launch._AUTH)[0][1])
        # Upstream's async tools take the tool loop on this path too.
        tool = server._tool_manager.get_tool("get_activities")
        self.assertIs(getattr(tool.fn, mcp2_compat.TOOL_LOOP_MARKER, None), True)
        # The served app limits /authorize to 30 requests per 10 minutes per IP.
        with captured_logs() as lines, contextlib.redirect_stderr(io.StringIO()):
            answers = [asgi_request(app, "GET", "/authorize", "203.0.113.9") for _ in range(31)]
            other_caller = asgi_request(app, "GET", "/authorize", "203.0.113.10")
        self.assertEqual({status for status, _ in answers[:30]}, {400})  # no client_id
        status, headers = answers[30]
        self.assertEqual(status, 429)
        self.assertIn(b"retry-after", headers)
        self.assertEqual(other_caller[0], 400)
        limited = [line for line in lines if line.startswith("rate limit:")]
        self.assertEqual(len(limited), 1, limited)
        self.assertRegex(limited[0], r"^rate limit: GET /authorize \(per-ip\) from 203\.0\.113\.9, retry in \d+s$")
        self.assertIn("sign-in: on, issuer https://coach.example.com, static bearer off", logs)
        self.assertNotIn(env["OWNER_SECRET"], err + "\n".join(logs))

    def real_app(self):
        """main() with the real owner_auth: the app the SDK would serve."""
        with mock.patch.object(auth_store, "SECRET_FP_ITERATIONS", 1000):
            server, err, code, _ = self.serve(sign_in_env(self.db))
        self.assertIsNone(code, err)
        return server.streamable_http_app()

    def test_callers_behind_an_edge_hop_keep_their_own_buckets(self):
        # V1-1: Caddy's X-Forwarded-For ends with the edge's address. uvicorn,
        # as the SDK starts it, must not put that in place of the peer (Caddy),
        # or client_ip() ignores X-Client-IP and every caller shares a bucket.
        served = uvicorn_stack(self.real_app(), self.run_environ)
        hop = [("X-Forwarded-For", "100.64.0.7")]
        with captured_logs() as lines, contextlib.redirect_stderr(io.StringIO()):
            callers = [asgi_request(served, "POST", "/consent", "203.0.113.%d" % n, hop)[0] for n in range(1, 22)]
            one = [asgi_request(served, "POST", "/consent", "203.0.113.99", hop)[0] for _ in range(21)]
        self.assertEqual(set(callers), {400})                    # 21 callers, none limited
        self.assertEqual((set(one[:20]), one[20]), ({400}, 429))  # one caller still is
        limited = [line for line in lines if line.startswith("rate limit:")]
        self.assertEqual(limited, [line for line in limited if " from 203.0.113.99, " in line])
        self.assertEqual(len(limited), 1, limited)
        self.assertEqual(self.run_environ.get("FORWARDED_ALLOW_IPS"), "")

    def test_methods_no_client_needs_get_405_before_the_sdk(self):
        # V1-2: HEAD /authorize reads the form as POST does, and an OPTIONS that
        # is not a CORS preflight reaches the /register handler.
        app = self.real_app()
        form = ("response_type=code&client_id=x&code_challenge_method=S256&code_challenge=" + "c" * 43
                + "&redirect_uri=https%3A%2F%2Fclaude.ai%2Fapi%2Fmcp%2Fauth_callback").encode()
        registration = json.dumps({"redirect_uris": ["https://claude.ai/api/mcp/auth_callback"],
                                   "client_name": "Claude", "token_endpoint_auth_method": "client_secret_post",
                                   "grant_types": ["authorization_code", "refresh_token"],
                                   "response_types": ["code"]}).encode()
        with captured_logs(), contextlib.redirect_stderr(io.StringIO()):
            head = asgi_request(app, "HEAD", "/authorize", "203.0.113.20",
                                [("Content-Type", "application/x-www-form-urlencoded")], form)
            options = asgi_request(app, "OPTIONS", "/register", "203.0.113.20",
                                   [("Content-Type", "application/json")], registration)
            preflight = asgi_request(app, "OPTIONS", "/register", "203.0.113.20",
                                     [("Origin", "https://app.example.com"), ("Access-Control-Request-Method", "POST")])
            registered = asgi_request(app, "POST", "/register", "203.0.113.20",
                                      [("Content-Type", "application/json")], registration)
        self.assertEqual((head[0], head[1].get(b"allow")), (405, b"GET, POST"))
        self.assertEqual((options[0], options[1].get(b"allow")), (405, b"POST"))
        self.assertEqual(preflight[0], 200)             # the SDK's CORS layer answers it
        self.assertEqual(registered[0], 201)            # the same body by POST registers
        self.assertEqual(self.launch._AUTH.provider.store.counts()[0], 1)


class SignInCheckConfig(unittest.TestCase):
    """check_config() with the sign-in: on for any transport but stdio, its
    errors after the tool and REPORT_TZ ones, and AUTH_DB opened only when
    the settings are valid. Standard library only (the tool policy is
    stubbed; auth_config and auth_store are stdlib modules)."""

    def setUp(self):
        self.launch = load_launch(self, Stubs().modules)
        patcher = mock.patch.object(self.launch, "allowed_tools", return_value=frozenset({"get_stats"}))
        self.allowed_tools = patcher.start()
        self.addCleanup(patcher.stop)
        self.db = os.path.join(temp_dir(self), "auth.sqlite")

    def check(self, env, writable=()):
        """(check_config(env), the check_writable mock, which returns `writable`)."""
        env = {key: value for key, value in {**FULL, **env}.items() if value is not None}
        with mock.patch.object(self.launch.auth_store, "check_writable", return_value=list(writable)) as opened:
            return self.launch.check_config(env), opened

    def test_no_sign_in_lines_without_a_network_transport(self):
        bad = {"REPORT_TZ": "Europe/London", "PUBLIC_URL": "http://example.com/mcp", "OWNER_SECRET": fake_secret(6),
               "MCP_BEARER_TOKEN": "not a token", "AUTH_DB": "relative/auth.sqlite"}
        for transport in (None, "", "stdio", " STDIO "):
            env = dict(bad) if transport is None else dict(bad, GARMIN_MCP_TRANSPORT=transport)
            with self.subTest(transport=transport):
                result, opened = self.check(env)
                self.assertEqual(result, (frozenset({"get_stats"}), []))
                opened.assert_not_called()

    def test_sign_in_errors_come_after_the_tool_and_report_tz_errors_in_order(self):
        self.allowed_tools.side_effect = self.launch.ConfigError(["a tool problem"])
        env = sign_in_env(self.db, PUBLIC_URL="http://example.com/mcp", OWNER_SECRET=fake_secret(6),
                          MCP_BEARER_TOKEN="TEST ONLY " + secrets.token_urlsafe(24))  # no REPORT_TZ
        (allowed, errors), opened = self.check(env)
        self.assertEqual(allowed, frozenset())
        self.assertEqual(errors[0], "a tool problem")
        self.assertTrue(errors[1].startswith("REPORT_TZ"), errors)
        self.assertEqual(errors[2:], [A2, A3, A5, A8])
        opened.assert_not_called()

    def test_sse_is_refused_with_the_sign_in(self):
        (allowed, errors), opened = self.check(sign_in_env(self.db, GARMIN_MCP_TRANSPORT="sse", REPORT_TZ="UTC"))
        self.assertEqual(errors, [A12])
        opened.assert_not_called()

    def test_auth_db_is_checked_only_when_the_settings_are_valid(self):
        env = sign_in_env(self.db, REPORT_TZ="UTC")
        result, opened = self.check(env)
        self.assertEqual(result, (frozenset({"get_stats"}), []))
        opened.assert_called_once_with(self.db)
        line = f"AUTH_DB: cannot open {self.db}: OSError: no{FRESH_START}"
        (_, errors), opened = self.check(env, writable=[line])
        self.assertEqual(errors, [line])
        opened.assert_called_once_with(self.db)

    def test_the_real_auth_db_check(self):
        # Valid settings: the file is created 0600, as the server would.
        self.assertEqual(self.launch.check_config(sign_in_env(self.db, REPORT_TZ="UTC")),
                         (frozenset({"get_stats"}), []))  # sign_in_env sets GARMIN_AUTH=full
        self.assertEqual(stat.S_IMODE(os.stat(self.db).st_mode), 0o600)
        # A path that cannot be a database file: one A13 line.
        blocked = os.path.join(self.db, "auth.sqlite")  # under a regular file
        allowed, errors = self.launch.check_config(sign_in_env(blocked, REPORT_TZ="UTC"))
        self.assertEqual(len(errors), 1, errors)
        self.assertTrue(errors[0].startswith(f"AUTH_DB: cannot open {blocked}: "), errors)
        self.assertTrue(errors[0].endswith(FRESH_START), errors)


if __name__ == "__main__":
    unittest.main()
