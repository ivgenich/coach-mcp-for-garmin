"""Tests for owner_auth: the built-in single-owner sign-in (OAuth 2.1).

    python -m unittest test_owner_auth -v     (from ext/, on the mcp 2.x venv)

Every HTTP test drives the real SDK app, MCPServer(...).streamable_http_app()
with register_routes() and the RegistrationFilter, through starlette's
TestClient: no port is bound and nothing leaves the process. Skipped without
mcp 2.x (the standard-library leg).

Secrets are fake values made at run time. Assertion messages carry status and
error codes only, never a secret. Clocks are fake: they start at the current
time (rounded up to a whole second, so expiry arithmetic is exact) and only
move forward. Expiry tests check that the provider refuses on its own clock,
because the SDK checks the real time (F22).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib.metadata
import inspect
import json
import logging
import math
import os
import secrets
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from urllib.parse import parse_qs, quote, urlencode, urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import auth_config  # noqa: E402
import auth_store  # noqa: E402


def _mcp_major():
    try:
        return importlib.metadata.version("mcp").split(".", 1)[0]
    except importlib.metadata.PackageNotFoundError:
        return None


HAVE_MCP2 = _mcp_major() == "2"

if HAVE_MCP2:  # owner_auth and the SDK need mcp 2.x; the standard-library leg skips every test
    import owner_auth  # noqa: E402
    from mcp.server.auth.provider import RefreshToken, TokenError  # noqa: E402
    from mcp.server.mcpserver import MCPServer  # noqa: E402
    from mcp.shared.auth import InvalidRedirectUriError, OAuthClientInformationFull  # noqa: E402
    from starlette.testclient import TestClient  # noqa: E402

BASE = "http://127.0.0.1:8000"          # where TestClient sends requests (no socket is opened)
PUBLIC = "https://coach.example.com"    # PUBLIC_URL: what the metadata, the pages and the tokens name
RESOURCE = PUBLIC + "/mcp"
CALLBACK = "https://claude.ai/api/mcp/auth_callback"
LOOPBACK = "http://localhost:1/callback"
JWT_BEARER = "urn:ietf:params:oauth:grant-type:jwt-bearer"
FORM = "application/x-www-form-urlencoded"
EXPIRED_TEXT = "expired or was already used. Start again from your Claude app."
PAGE_HEADERS = {
    "cache-control": "no-store",
    "pragma": "no-cache",
    "x-frame-options": "DENY",
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
    "content-security-policy": "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'",
}
REDIRECT_HEADERS = {"cache-control": "no-store", "pragma": "no-cache", "referrer-policy": "no-referrer"}
MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
INITIALIZE = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                         "clientInfo": {"name": "owner-auth-test", "version": "0"}}}
CLIENT_SIDE_LOGGERS = ("httpx", "httpcore")   # the test client's own request lines (they carry URLs)
WWW_AUTHENTICATE = ('Bearer error="invalid_token", error_description="Authentication required", '
                    'resource_metadata="https://coach.example.com/.well-known/oauth-protected-resource/mcp"')


def fake_secret():
    return "TEST-ONLY-" + secrets.token_urlsafe(24)


def pkce():
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def query_of(url):
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query, keep_blank_values=True).items()}


def why(response):
    """A failure message without secrets: the status and the OAuth error code only."""
    try:
        data = response.json()
    except ValueError:
        data = None
    error = data.get("error") if isinstance(data, dict) else None
    return "status %d, error %r" % (response.status_code, error)


class FakeClock:
    """Starts at the current time, rounded up to a whole second, and only moves forward (F22)."""

    def __init__(self):
        self.now = float(math.ceil(time.time()))

    def __call__(self):
        return self.now

    def advance(self, seconds):
        if seconds < 0:
            raise ValueError("a fake clock only moves forward")
        self.now += seconds


class LogCapture(logging.Handler):
    """Every record as (logger name, level name, text with any traceback)."""

    def __init__(self):
        super().__init__(logging.DEBUG)
        self.records = []

    def emit(self, record):
        try:
            text = record.getMessage()
        except Exception:  # noqa: BLE001 - a bad format string is still searched
            text = "%s %r" % (record.msg, record.args)
        if record.exc_info:
            text += "\n" + logging.Formatter().formatException(record.exc_info)
        if record.stack_info:
            text += "\n" + record.stack_info
        self.records.append((record.name, record.levelname, text))

    def auth_lines(self, level=None):
        return [text for name, lvl, text in self.records
                if name == "garmin_ext.auth" and (level is None or lvl == level)]

    def server_text(self):
        return "\n".join(text for name, _, text in self.records if not name.startswith(CLIENT_SIDE_LOGGERS))


def capture_logs(test):
    """Route every log record to one LogCapture for this test, and none to stderr."""
    root = logging.getLogger()
    auth_log = logging.getLogger("garmin_ext.auth")
    saved = (root.handlers[:], root.level, auth_log.level, auth_log.disabled, auth_log.propagate,
             logging.root.manager.disable)
    capture = LogCapture()
    root.handlers = [capture]
    root.setLevel(logging.DEBUG)
    auth_log.setLevel(logging.DEBUG)
    auth_log.disabled = False
    auth_log.propagate = True
    logging.disable(logging.NOTSET)

    def restore():
        root.handlers = saved[0]
        root.setLevel(saved[1])
        auth_log.setLevel(saved[2])
        auth_log.disabled = saved[3]
        auth_log.propagate = saved[4]
        logging.disable(saved[5])

    test.addCleanup(restore)
    return capture


@unittest.skipUnless(HAVE_MCP2, "needs mcp 2.x")
class SignInCase(unittest.TestCase):
    """A fresh sign-in on its own temporary database for every test."""

    with_static_bearer = False
    extra_redirects = ()

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="owner-auth-test-")
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.db_path = os.path.join(self.tmp, "auth.sqlite")
        iterations = mock.patch.object(auth_store, "SECRET_FP_ITERATIONS", 1000)
        iterations.start()
        self.addCleanup(iterations.stop)
        self.logs = capture_logs(self)
        self.clock = FakeClock()
        self.owner_secret = fake_secret()
        self.bearer = fake_secret() if self.with_static_bearer else None
        self.auth = self.build()
        self.client = self.new_client()

    # --- the server ----------------------------------------------------------------------

    @property
    def provider(self):
        return self.auth.provider

    @property
    def store(self):
        return self.auth.provider.store

    def config(self, **changes):
        values = {"public_url": PUBLIC, "owner_secret": self.owner_secret, "static_bearer": self.bearer,
                  "extra_redirect_uris": tuple(self.extra_redirects), "db_path": self.db_path}
        values.update(changes)
        return auth_config.AuthConfig(**values)

    def build(self, **changes):
        auth = owner_auth.build(self.config(**changes), clock=self.clock)
        self.addCleanup(auth.provider.store.close)
        return auth

    def make_app(self, auth=None):
        auth = auth or self.auth
        server = MCPServer("owner-auth-test", auth=auth.settings, auth_server_provider=auth.provider)
        owner_auth.register_routes(server, auth)
        app = server.streamable_http_app(stateless_http=True, json_response=True)
        for cls, options in reversed(owner_auth.asgi_middleware(auth)):   # the first entry ends up outermost
            app.add_middleware(cls, **options)
        return app

    def new_client(self, auth=None):
        return TestClient(self.make_app(auth), base_url=BASE, follow_redirects=False)

    def mcp(self, token=None, *, auth=None, path="/mcp", headers=None):
        """POST initialize to /mcp on a freshly started app (its session manager runs once)."""
        sent = dict(MCP_HEADERS)
        if token is not None:
            sent["Authorization"] = "Bearer " + token
        sent.update(headers or {})
        with TestClient(self.make_app(auth), base_url=BASE) as client:
            return client.post(path, headers=sent, json=INITIALIZE)

    def assert_mcp(self, token, status, **kwargs):
        response = self.mcp(token, **kwargs)
        self.assertEqual(response.status_code, status, why(response))
        return response

    def sql(self, query, *args):
        db = sqlite3.connect(self.db_path)
        try:
            return db.execute(query, args).fetchall()
        finally:
            db.close()

    def run_async(self, coro):
        return asyncio.run(coro)

    # --- the OAuth flow, step by step ------------------------------------------------------

    def post_register(self, **fields):
        body = {"redirect_uris": [CALLBACK], "client_name": "Claude",
                "token_endpoint_auth_method": "client_secret_post",
                "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
                "scope": "offline_access"}
        body.update(fields)
        return self.client.post("/register", json={k: v for k, v in body.items() if v is not None})

    def register(self, **fields):
        response = self.post_register(**fields)
        self.assertEqual(response.status_code, 201, why(response))
        return response.json()

    def public_client(self, redirect=LOOPBACK, **fields):
        fields.setdefault("client_name", "Claude Code")
        return self.register(redirect_uris=[redirect], token_endpoint_auth_method="none", **fields)

    def authorize(self, client, *, redirect_uri=None, challenge=None, state=None, scope="offline_access",
                  resource=RESOURCE, method="S256"):
        params = {"response_type": "code", "client_id": client["client_id"],
                  "redirect_uri": redirect_uri or client["redirect_uris"][0], "code_challenge": challenge,
                  "code_challenge_method": method, "state": state, "scope": scope, "resource": resource}
        return self.client.get("/authorize", params={k: v for k, v in params.items() if v is not None})

    def start(self, client, *, redirect_uri=None, **kwargs):
        verifier, challenge = pkce()
        state = "state-" + secrets.token_urlsafe(16)
        redirect_uri = redirect_uri or client["redirect_uris"][0]
        response = self.authorize(client, redirect_uri=redirect_uri, challenge=challenge, state=state, **kwargs)
        self.assertEqual(response.status_code, 302, why(response))
        location = response.headers["location"]
        self.assertTrue(location.startswith(PUBLIC + "/consent?req="), "not sent to the consent page")
        return {"client": client, "verifier": verifier, "challenge": challenge, "state": state,
                "redirect_uri": redirect_uri, "pending": query_of(location)["req"]}

    def post_consent(self, pending, *, secret=None, action="approve", site="same-origin", **fields):
        form = {"req": pending, "username": "owner"}
        if secret is not None:
            form["secret"] = secret
        if action is not None:
            form["action"] = action
        form.update(fields)
        headers = {"Sec-Fetch-Site": site} if site is not None else {}
        return self.client.post("/consent", data=form, headers=headers)

    def approve(self, flow):
        response = self.post_consent(flow["pending"], secret=self.owner_secret)
        self.assertEqual(response.status_code, 303, why(response))
        flow["code"] = query_of(response.headers["location"])["code"]
        return flow

    def exchange(self, flow, **overrides):
        client = flow["client"]
        form = {"grant_type": "authorization_code", "code": flow["code"], "redirect_uri": flow["redirect_uri"],
                "code_verifier": flow["verifier"], "client_id": client["client_id"],
                "client_secret": client.get("client_secret")}
        form.update(overrides)
        return self.client.post("/token", data={k: v for k, v in form.items() if v is not None})

    def sign_in(self, client=None, **kwargs):
        flow = self.approve(self.start(client or self.register(), **kwargs))
        response = self.exchange(flow)
        self.assertEqual(response.status_code, 200, why(response))
        flow["tokens"] = response.json()
        return flow

    def refresh(self, client, refresh_token, *, http=None, **extra):
        form = {"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": client["client_id"]}
        if client.get("client_secret"):
            form["client_secret"] = client["client_secret"]
        form.update(extra)
        return (http or self.client).post("/token", data=form)

    def rotate(self, client, refresh_token):
        response = self.refresh(client, refresh_token)
        self.assertEqual(response.status_code, 200, why(response))
        return response.json()

    def revoke(self, client, token, **extra):
        form = {"token": token, "client_id": client["client_id"], "client_secret": client.get("client_secret", "")}
        form.update(extra)
        return self.client.post("/revoke", data=form)

    def grant_reasons(self):
        return [row[0] for row in self.sql("SELECT revoked_reason FROM grants ORDER BY created_at")]

    def dcr_line(self):
        lines = [line for line in self.logs.auth_lines("INFO") if line.startswith("dcr: request ")]
        self.assertTrue(lines, "no dcr: request line")
        return lines[-1]


# --- discovery -----------------------------------------------------------------------------------


class Discovery(SignInCase):
    """AS metadata, both resource metadata documents and the 401 (S1-S4)."""

    def test_authorization_server_metadata_is_exact(self):
        response = self.client.get("/.well-known/oauth-authorization-server")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "issuer": PUBLIC,
            "authorization_endpoint": PUBLIC + "/authorize",
            "token_endpoint": PUBLIC + "/token",
            "registration_endpoint": PUBLIC + "/register",
            "scopes_supported": ["offline_access"],
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "token_endpoint_auth_methods_supported": ["client_secret_post", "client_secret_basic"],
            "revocation_endpoint": PUBLIC + "/revoke",
            "revocation_endpoint_auth_methods_supported": ["client_secret_post", "client_secret_basic"],
            "code_challenge_methods_supported": ["S256"],
        })

    def test_both_resource_metadata_documents_are_the_same(self):
        expected = {"resource": RESOURCE, "authorization_servers": [PUBLIC], "bearer_methods_supported": ["header"]}
        at_path = self.client.get("/.well-known/oauth-protected-resource/mcp")
        at_root = self.client.get("/.well-known/oauth-protected-resource")
        for response in (at_path, at_root):
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), expected)
            self.assertEqual(response.headers["cache-control"], "public, max-age=3600")
        self.assertEqual(at_root.content, at_path.content)

    def test_mcp_without_a_token_gets_401_with_resource_metadata(self):
        response = self.assert_mcp(None, 401)
        self.assertEqual(response.headers["www-authenticate"], WWW_AUTHENTICATE)

    def test_public_url_spelling_is_the_sdks(self):
        # F15 end to end: PUBLIC_URL as typed, normalized by auth_config, equals what the SDK serves.
        for typed, origin in (("HTTPS://Coach.Example.COM:443/", PUBLIC),
                              ("https://coach.example.com:8443", "https://coach.example.com:8443"),
                              ("http://LOCALHOST:80", "http://localhost"),
                              ("http://[::1]:8340", "http://[::1]:8340")):
            with self.subTest(typed=typed):
                db_path = os.path.join(self.tmp, "f15-%d.sqlite" % len(typed))
                config, errors = auth_config.config_from_environ({
                    "GARMIN_MCP_TRANSPORT": "streamable-http", "PUBLIC_URL": typed,
                    "OWNER_SECRET": self.owner_secret, "AUTH_DB": db_path})
                self.assertEqual(errors, [])
                auth = owner_auth.build(config, clock=self.clock)
                self.addCleanup(auth.provider.store.close)
                client = self.new_client(auth)
                metadata = client.get("/.well-known/oauth-authorization-server").json()
                self.assertEqual(metadata["issuer"], origin)
                self.assertEqual(metadata["token_endpoint"], origin + "/token")
                self.assertEqual(client.get("/.well-known/oauth-protected-resource").json()["resource"],
                                 origin + "/mcp")

    def test_an_issuer_the_sdk_would_respell_is_refused(self):
        with self.assertRaises(RuntimeError) as caught:
            owner_auth.build(self.config(public_url="https://Coach.example.com"), clock=self.clock)
        self.assertIn("PUBLIC_URL", str(caught.exception))
        self.assertNotIn(self.owner_secret, str(caught.exception))


# --- registration ----------------------------------------------------------------------------------


class Registration(SignInCase):
    """Dynamic client registration: the body filter, the allowlist and the logs."""

    def test_a_claude_like_body_is_narrowed(self):
        # S5: claude.ai lists the jwt-bearer grant and may ask for a scope we do not know.
        client = self.register(grant_types=["authorization_code", "refresh_token", JWT_BEARER],
                               scope="claudeai offline_access")
        self.assertEqual(client["grant_types"], ["authorization_code", "refresh_token"])
        self.assertEqual(client["scope"], "offline_access")
        self.assertEqual(client["token_endpoint_auth_method"], "client_secret_post")
        self.assertEqual(client["redirect_uris"], [CALLBACK])
        self.assertTrue(client.get("client_secret"))
        stored = json.loads(self.store.get_client(client["client_id"]))
        self.assertEqual(stored["grant_types"], ["authorization_code", "refresh_token"])
        line = self.dcr_line()
        self.assertIn("client_name='Claude' auth_method='client_secret_post'", line)
        self.assertIn("redirect_hosts=['https://claude.ai']", line)
        self.assertIn("dropped=['grant " + JWT_BEARER + "', 'scope claudeai'] rewritten=[]", line)
        self.assertIn("dcr: registered client %s (Claude), auth method client_secret_post, 1 clients stored"
                      % client["client_id"], self.logs.auth_lines("INFO"))

    def test_basic_auth_method_becomes_post(self):
        # F2: the SDK needs client_id in the /token form, so a Basic-only client could never refresh.
        client = self.register(token_endpoint_auth_method="client_secret_basic")
        self.assertEqual(client["token_endpoint_auth_method"], "client_secret_post")
        self.assertTrue(client.get("client_secret"))
        self.assertIn("rewritten=['client_secret_basic->client_secret_post']", self.dcr_line())
        self.sign_in(client)

    def test_public_loopback_client_gets_no_secret_key_at_all(self):
        # S6 (F2): the 201 of a "none" client has no client_secret key, not even null.
        response = self.client.post("/register", json={"redirect_uris": ["http://localhost:1/callback"],
                                                       "token_endpoint_auth_method": "none",
                                                       "client_name": "Claude Code"})
        self.assertEqual(response.status_code, 201, why(response))
        body = response.json()
        self.assertNotIn("client_secret", body)
        self.assertNotIn("client_secret_expires_at", body)
        self.assertEqual(body["token_endpoint_auth_method"], "none")
        self.assertEqual(body["grant_types"], ["authorization_code", "refresh_token"])
        self.assertEqual(body["scope"], "offline_access")

    def test_grants_become_exactly_the_two(self):
        # F10, S9.
        for asked in (["authorization_code", "client_credentials"], ["authorization_code"],
                      ["refresh_token", "implicit", "authorization_code", JWT_BEARER]):
            with self.subTest(asked=asked):
                self.assertEqual(self.register(grant_types=asked)["grant_types"],
                                 ["authorization_code", "refresh_token"])
        self.assertIn("rewritten=['grant_types']", [l for l in self.logs.auth_lines() if "dcr: request" in l][1])

    def test_grants_without_authorization_code_are_left_to_the_sdk(self):
        response = self.post_register(grant_types=["client_credentials"])
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "invalid_client_metadata")

    def test_scope_keeps_only_valid_tokens(self):
        self.assertEqual(self.register(scope="claudeai profile")["scope"], "offline_access")   # field dropped: default
        self.assertEqual(self.register(scope="offline_access offline_access")["scope"], "offline_access")
        self.assertEqual(self.register(scope="")["scope"], "offline_access")

    def test_non_ascii_survives_the_rewrite(self):
        name = "Zoë’s Claude ✓"
        self.assertEqual(self.register(client_name=name, grant_types=["authorization_code"])["client_name"], name)

    def test_allowed_redirects(self):
        for uri in (CALLBACK, "https://claude.com/api/mcp/auth_callback", LOOPBACK, "http://127.0.0.1:33418/cb",
                    "http://[::1]/x", "http://localhost", "HTTPS://CLAUDE.AI:443/api/mcp/auth_callback"):
            with self.subTest(uri=uri):
                self.register(redirect_uris=[uri])

    def test_refused_redirects(self):
        # S7 plus near misses: exact https strings only; http only on loopback; never a fragment or user info.
        for uri in ("https://evil.example/cb", "http://example.com/cb", "javascript:alert(1)", "cursor://x/cb",
                    "http://localhost:1/callback#fragment", "http://localhost:1/callback#",
                    "http://user@localhost:1/callback", "https://user:pw@claude.ai/api/mcp/auth_callback",
                    CALLBACK + "?x=1", CALLBACK + "/", CALLBACK + "#", "http://claude.ai/api/mcp/auth_callback",
                    "http://127.0.0.2/cb", "https://localhost/cb", "http://localhost.evil.example/cb"):
            with self.subTest(uri=uri):
                response = self.post_register(redirect_uris=[uri])
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json(), {"error": "invalid_redirect_uri",
                                                   "error_description": owner_auth.REDIRECT_HELP})
        response = self.post_register(redirect_uris=[CALLBACK, "https://evil.example/cb"])
        self.assertEqual(response.json()["error"], "invalid_redirect_uri")
        self.assertEqual(self.store.counts(), (0, 0))

    def test_a_body_over_64_kib_gets_413(self):
        # S8: declared by Content-Length.
        body = json.dumps({"redirect_uris": [CALLBACK], "client_name": "x" * 70000})
        response = self.client.post("/register", content=body, headers={"Content-Type": "application/json"})
        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.json(), {"error": "invalid_client_metadata",
                                           "error_description": "registration request too large"})
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(self.store.counts(), (0, 0))

    def test_a_chunked_body_over_64_kib_without_a_length_gets_413(self):
        # F17: the bytes actually streamed are counted.
        def chunks():
            yield b'{"redirect_uris": ["' + CALLBACK.encode() + b'"], "client_name": "'
            for _ in range(70):
                yield b"x" * 1024
            yield b'"}'
        response = self.client.post("/register", content=chunks(), headers={"Content-Type": "application/json"})
        self.assertNotIn("content-length", response.request.headers)
        self.assertEqual(response.status_code, 413)
        self.assertEqual(self.store.counts(), (0, 0))

    def test_a_body_of_exactly_64_kib_passes(self):
        head = '{"redirect_uris": ["%s"], "client_name": "Claude", "software_id": "' % CALLBACK
        body = head + "x" * (65536 - len(head) - 2) + '"}'
        self.assertEqual(len(body.encode()), 65536)
        response = self.client.post("/register", content=body, headers={"Content-Type": "application/json"})
        self.assertEqual(response.status_code, 201, why(response))

    def test_unparseable_bodies_reach_the_sdk_unchanged(self):
        # F17: deep nesting (RecursionError in json.loads), non-JSON and non-objects get the SDK's 400, not a 500.
        for body in (b"[" * 30000 + b"]" * 30000, b'{"a": ' + b"[" * 30000 + b"]" * 30000 + b"}",
                     b"not json", b"[1, 2]", b'"text"', b"\xff\xfe", b""):
            with self.subTest(size=len(body)):
                response = self.client.post("/register", content=body, headers={"Content-Type": "application/json"})
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()["error"], "invalid_client_metadata")

    def test_other_requests_pass_untouched(self):
        self.assertEqual(self.client.get("/register").status_code, 405)
        self.assertEqual(self.client.post("/token", data={"grant_type": "refresh_token"}).status_code, 401)
        self.assertFalse([l for l in self.logs.auth_lines() if l.startswith("dcr: request")])


@unittest.skipUnless(HAVE_MCP2, "needs mcp 2.x")
class RegistrationFilterAsgi(unittest.TestCase):
    """RegistrationFilter as a bare ASGI middleware, message by message (F17)."""

    def drive(self, messages, *, path="/register", method="POST", headers=(), max_body=64, scope_type="http",
              read_after=False):
        queue = list(messages)
        receive_calls = []
        sent = []
        seen = []

        async def receive():
            receive_calls.append(1)
            return queue.pop(0) if queue else {"type": "http.disconnect"}

        async def send(message):
            sent.append(message)

        async def app(scope, receive, send):
            got = []
            while True:
                message = await receive()
                got.append(message)
                if message["type"] != "http.request" or not message.get("more_body", False):
                    break
            if read_after:
                got.append(await receive())
            seen.append({"scope": scope, "messages": got})
            await send({"type": "http.response.start", "status": 204, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        middleware = owner_auth.RegistrationFilter(app, allowed_grants=auth_config.GRANT_TYPES,
                                                   valid_scopes=auth_config.VALID_SCOPES, max_body=max_body)
        scope = {"type": scope_type, "method": method, "path": path, "headers": list(headers)}
        with mock.patch.object(owner_auth.log, "info"):
            asyncio.run(middleware(scope, receive, send))
        return seen, sent, len(receive_calls), scope

    def test_a_streamed_body_stops_being_read_past_the_limit(self):
        chunks = [{"type": "http.request", "body": b"x" * 10, "more_body": True} for _ in range(100)]
        seen, sent, reads, _ = self.drive(chunks, max_body=64)
        self.assertEqual(seen, [])
        self.assertEqual(sent[0]["status"], 413)
        self.assertEqual(reads, 7)   # 70 bytes is the first count over 64

    def test_a_declared_length_over_the_limit_is_refused_unread(self):
        seen, sent, reads, _ = self.drive([], headers=[(b"content-length", b"65")], max_body=64)
        self.assertEqual((seen, sent[0]["status"], reads), ([], 413, 0))

    def test_the_body_is_replayed_once_then_receive_passes_through(self):
        body = json.dumps({"redirect_uris": [CALLBACK], "grant_types": ["authorization_code"]}).encode()
        messages = [{"type": "http.request", "body": body[:20], "more_body": True},
                    {"type": "http.request", "body": body[20:], "more_body": False},
                    {"type": "http.disconnect", "marker": "real"}]
        seen, sent, _, _ = self.drive(messages, headers=[(b"content-length", str(len(body)).encode()),
                                                         (b"transfer-encoding", b"chunked")],
                                      max_body=4096, read_after=True)
        replayed, after = seen[0]["messages"]
        self.assertFalse(replayed["more_body"])
        self.assertEqual(json.loads(replayed["body"])["grant_types"], ["authorization_code", "refresh_token"])
        self.assertEqual(after, {"type": "http.disconnect", "marker": "real"})
        headers = dict(seen[0]["scope"]["headers"])
        self.assertEqual(headers[b"content-length"], str(len(replayed["body"])).encode())
        self.assertNotIn(b"transfer-encoding", headers)
        self.assertEqual(sent[0]["status"], 204)

    def test_a_body_cut_short_is_forwarded_unchanged(self):
        messages = [{"type": "http.request", "body": b'{"redirect', "more_body": True}, {"type": "http.disconnect"}]
        seen, _, _, _ = self.drive(messages, max_body=4096)
        self.assertEqual(seen[0]["messages"], [{"type": "http.request", "body": b'{"redirect', "more_body": True},
                                               {"type": "http.disconnect"}])

    def test_other_scopes_and_paths_pass_through(self):
        for kwargs in ({"method": "GET"}, {"path": "/token"}, {"path": "/register/"}, {"scope_type": "websocket"}):
            with self.subTest(**kwargs):
                messages = [{"type": "http.request", "body": b"x" * 100, "more_body": False}]
                seen, _, _, scope = self.drive(messages, max_body=8, **kwargs)
                self.assertIs(seen[0]["scope"], scope)
                self.assertEqual(seen[0]["messages"][0]["body"], b"x" * 100)


class ExtraRedirects(SignInCase):
    """ALLOWED_REDIRECT_URIS entries match after pydantic's normalization."""

    extra_redirects = ("https://Cb.Example.com:443/oauth/callback", "https://bad host.example/x")

    def test_an_extra_entry_matches_its_normalized_spelling(self):
        client = self.register(redirect_uris=["https://cb.example.com/oauth/callback"])
        self.sign_in(client)

    def test_an_entry_pydantic_cannot_parse_is_ignored_with_a_warning(self):
        self.assertIn("sign-in: ALLOWED_REDIRECT_URIS entry 2 is not a valid URL; ignored",
                      self.logs.auth_lines("WARNING"))
        self.assertNotIn("bad host", self.logs.server_text())


class RemovedExtraRedirect(SignInCase):
    """A client registered while ALLOWED_REDIRECT_URIS listed its URI gets no
    code there once the owner removes the entry and restarts; it stays stored."""

    extra_redirects = ("https://cb.example.com/oauth/callback",)

    def restart_without_the_entry(self):
        self.auth = self.build(extra_redirect_uris=())
        self.client = self.new_client()

    def test_the_removed_uri_gets_400_without_a_location(self):
        client = self.register(redirect_uris=["https://cb.example.com/oauth/callback", CALLBACK])
        self.start(client)                                   # allowed today
        self.restart_without_the_entry()
        pending_before = self.sql("SELECT COUNT(*) FROM pending")[0][0]
        for redirect_uri in ("https://cb.example.com/oauth/callback", "https://CB.example.com:443/oauth/callback"):
            with self.subTest(redirect_uri=redirect_uri):
                response = self.authorize(client, redirect_uri=redirect_uri, challenge=pkce()[1], state="s-r13")
                self.assertEqual(response.status_code, 400, why(response))
                self.assertNotIn("location", response.headers)
                self.assertEqual((response.json()["error"], response.json()["error_description"]),
                                 ("invalid_request", owner_auth.REDIRECT_NO_LONGER_ALLOWED))
        self.assertEqual(self.sql("SELECT COUNT(*) FROM pending")[0][0], pending_before)
        self.assertIsNotNone(self.store.get_client(client["client_id"]))   # not deleted
        self.sign_in(client, redirect_uri=CALLBACK)                         # its built-in callback still works

    def test_the_only_registered_uri_is_refused_when_omitted_too(self):
        client = self.register(redirect_uris=["https://cb.example.com/oauth/callback"])
        self.restart_without_the_entry()
        params = {"response_type": "code", "client_id": client["client_id"], "code_challenge": pkce()[1],
                  "code_challenge_method": "S256", "state": "s-r13", "resource": RESOURCE}
        response = self.client.get("/authorize", params=params)
        self.assertEqual(response.status_code, 400, why(response))
        self.assertNotIn("location", response.headers)


class ExtraRedirectOriginOnly(SignInCase):
    """V2-3: an ALLOWED_REDIRECT_URIS entry with no path signs in, whether the
    client registers and sends it with or without the "/"."""

    extra_redirects = ("https://app.example.com",)

    def test_registered_without_the_slash(self):
        client = self.register(redirect_uris=["https://app.example.com"])
        self.assertEqual(client["redirect_uris"], ["https://app.example.com"])
        flow = self.sign_in(client)
        self.assertTrue(flow["tokens"]["access_token"])

    def test_registered_without_and_sent_with_the_slash(self):
        client = self.register(redirect_uris=["https://app.example.com"])
        self.sign_in(client, redirect_uri="https://app.example.com/")

    def test_registered_with_the_slash(self):
        self.sign_in(self.register(redirect_uris=["https://app.example.com/"]))

    def test_a_path_is_still_refused(self):
        client = self.register(redirect_uris=["https://app.example.com"])
        verifier, challenge = pkce()
        response = self.authorize(client, redirect_uri="https://app.example.com/cb", challenge=challenge, state="s")
        self.assertEqual(response.status_code, 400, why(response))


class Eviction(SignInCase):
    """MAX_CLIENTS with eviction: idle clients first (tier 1), then 7-day idle ones (F11)."""

    def test_an_idle_client_is_evicted_at_the_cap(self):
        with mock.patch.object(auth_config, "MAX_CLIENTS", 3):
            first = self.register()
            for _ in range(2):
                self.clock.advance(1)
                self.register()
            self.clock.advance(1)
            self.register()
        self.assertIsNone(self.store.get_client(first["client_id"]))
        self.assertEqual(self.store.counts()[0], 3)
        self.assertIn("dcr: evicted idle client " + first["client_id"], self.logs.auth_lines("INFO"))

    def test_signed_in_clients_go_only_after_7_idle_days(self):
        with mock.patch.object(auth_config, "MAX_CLIENTS", 2):
            older = self.sign_in()
            self.clock.advance(60)
            newer = self.sign_in()
            for wait in (0, 6 * 86400):
                self.clock.advance(wait)
                response = self.post_register()
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json(), {"error": "invalid_client_metadata",
                                                   "error_description": "too many registered clients; try again later"})
            self.clock.advance(86400 + 120)
            self.register()
        gone = older["client"]["client_id"]
        self.assertIsNone(self.store.get_client(gone))
        self.assertIn("token: grant revoked for client %s (client evicted)" % gone, self.logs.auth_lines("WARNING"))
        self.assertIn("dcr: evicted idle client " + gone, self.logs.auth_lines("INFO"))
        self.assertEqual(self.refresh(older["client"], older["tokens"]["refresh_token"]).status_code, 401)
        self.rotate(newer["client"], newer["tokens"]["refresh_token"])

    def test_strangers_signing_in_cannot_push_out_an_idle_owner(self):
        # V1-3: registering and opening a consent request need no secret, so a
        # stranger's clients go at tier 1 before the owner's grant is touched.
        with mock.patch.object(auth_config, "MAX_CLIENTS", 3):
            owner = self.sign_in()
            self.clock.advance(8 * 86400)                  # the owner has not used Claude for 8 days
            strangers = []
            for _ in range(5):
                strangers.append(self.register())
                self.start(strangers[-1])                  # a consent request nobody approves
                self.clock.advance(1)
        self.assertIsNotNone(self.store.get_client(owner["client"]["client_id"]))
        self.assertEqual([self.store.get_client(c["client_id"]) is not None for c in strangers],
                         [False, False, False, True, True])
        self.assertFalse([line for line in self.logs.auth_lines("WARNING") if "client evicted" in line])
        self.rotate(owner["client"], owner["tokens"]["refresh_token"])


# --- authorization --------------------------------------------------------------------------------


class Authorize(SignInCase):
    """/authorize: resource binding, PKCE format, bounds, lenient scope, pending caps (S10, S18-S20)."""

    def setUp(self):
        super().setUp()
        self.claude = self.register()

    def pending_count(self, client_id=None):
        if client_id is None:
            return self.sql("SELECT COUNT(*) FROM pending")[0][0]
        return self.sql("SELECT COUNT(*) FROM pending WHERE client_id = ?", client_id)[0][0]

    def assert_error_redirect(self, response, error, state, description=None):
        self.assertEqual(response.status_code, 302, why(response))
        location = response.headers["location"]
        self.assertTrue(location.startswith(CALLBACK + "?"), "the error does not go back to the client")
        query = query_of(location)
        self.assertEqual(query["error"], error)
        self.assertEqual(query.get("state"), state)
        if description is not None:
            self.assertEqual(query["error_description"], description)

    def test_a_valid_request_goes_to_the_consent_page(self):
        verifier, challenge = pkce()
        response = self.authorize(self.claude, challenge=challenge, state="s-10")
        self.assertEqual(response.status_code, 302, why(response))
        self.assertEqual(response.headers["cache-control"], "no-store")
        pending = query_of(response.headers["location"])["req"]
        self.assertGreaterEqual(len(pending), 43)
        row = self.store.get_pending(auth_store.hash_secret(pending))
        self.assertEqual((row["client_id"], row["redirect_uri"], row["state"], row["code_challenge"], row["scopes"],
                          row["resource"], row["redirect_uri_explicit"]),
                         (self.claude["client_id"], CALLBACK, "s-10", challenge, "offline_access", RESOURCE, 1))
        self.assertEqual(row["expires_at"] - row["created_at"], auth_config.PENDING_TTL)
        self.assertEqual(self.sql("SELECT COUNT(*) FROM pending WHERE pending_hash = ?", pending)[0][0], 0)
        self.assertIn("authorize: client %s, redirect host claude.ai" % self.claude["client_id"],
                      self.logs.auth_lines("INFO"))

    def test_resource_is_bound_to_this_server(self):
        for resource in (None, RESOURCE, RESOURCE + "/", "HTTPS://COACH.example.com:443/mcp"):
            with self.subTest(resource=resource):
                flow = self.start(self.claude, resource=resource)
                row = self.store.get_pending(auth_store.hash_secret(flow["pending"]))
                self.assertEqual(row["resource"], RESOURCE)
        for resource in ("https://evil.example/mcp", PUBLIC, PUBLIC + "/mcp2", PUBLIC + "/MCP", PUBLIC + "/mcp?x=1",
                         "http://coach.example.com/mcp", "not a url", ""):
            with self.subTest(resource=resource):
                response = self.authorize(self.claude, challenge=pkce()[1], state="s-18", resource=resource)
                self.assert_error_redirect(response, "invalid_target", "s-18", "unknown resource")
        self.assertEqual(self.pending_count(), 4)

    def test_challenge_must_be_s256_shaped(self):
        # S19: plain, missing or malformed PKCE.
        for name, kwargs in (("plain", {"method": "plain", "challenge": pkce()[1]}), ("missing", {}),
                             ("42 characters", {"challenge": "a" * 42}), ("44 characters", {"challenge": "a" * 44}),
                             ("padding", {"challenge": "a" * 42 + "="}), ("plus", {"challenge": "a" * 42 + "+"}),
                             ("slash", {"challenge": "a" * 42 + "/"}), ("space", {"challenge": "a" * 42 + " "})):
            with self.subTest(name=name):
                response = self.authorize(self.claude, state="s-19", **kwargs)
                self.assert_error_redirect(response, "invalid_request", "s-19")
                self.assertNotIn("/consent", response.headers["location"])
        self.assertEqual(self.pending_count(), 0)

    def test_state_and_scope_bounds(self):
        # F12: refused before anything is stored.
        self.start(self.claude)
        response = self.authorize(self.claude, challenge=pkce()[1], state="s" * 1024)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/consent?req=", response.headers["location"])
        self.start(self.claude, scope="a" * 512)
        for kwargs in ({"state": "s" * 1025}, {"state": "s-12", "scope": "a" * 513},
                       {"state": "s-12", "scope": " ".join(["x"] * 300)}):
            with self.subTest(size=len(kwargs.get("scope", kwargs["state"]))):
                response = self.authorize(self.claude, challenge=pkce()[1], **kwargs)
                self.assert_error_redirect(response, "invalid_request", kwargs["state"], "state or scope too long")
        self.assertEqual(self.pending_count(), 3)

    def test_scope_is_lenient(self):
        # D2: claude.ai was seen asking for "claudeai"; it is echoed, not checked.
        flow = self.sign_in(self.claude, scope="claudeai")
        self.assertEqual(flow["tokens"]["scope"], "claudeai")
        flow = self.sign_in(self.claude, scope=None)
        self.assertNotIn("scope", flow["tokens"])

    def test_an_unregistered_redirect_gets_400_without_a_location(self):
        # S20.
        response = self.authorize(self.claude, redirect_uri="https://evil.example/cb", challenge=pkce()[1],
                                  state="s-20")
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("location", response.headers)

    def test_an_unknown_client_gets_400(self):
        response = self.authorize({"client_id": "nobody", "redirect_uris": [CALLBACK]}, challenge=pkce()[1])
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("location", response.headers)

    def test_a_client_keeps_its_newest_five_pending_requests(self):
        # F5: past MAX_PENDING_PER_CLIENT the client's own oldest goes.
        flows = []
        for _ in range(auth_config.MAX_PENDING_PER_CLIENT):
            flows.append(self.start(self.claude))
            self.clock.advance(1)
        flows.append(self.start(self.claude))
        self.assertEqual(self.client.get("/consent", params={"req": flows[0]["pending"]}).status_code, 400)
        for flow in flows[1:]:
            self.assertEqual(self.client.get("/consent", params={"req": flow["pending"]}).status_code, 200)
        self.assertEqual(self.pending_count(self.claude["client_id"]), auth_config.MAX_PENDING_PER_CLIENT)

    def test_a_full_pending_table_refuses_and_deletes_nothing(self):
        # F5: another client's rows are never deleted to make room.
        other = self.register()
        with mock.patch.object(auth_config, "MAX_PENDING", 3):
            for _ in range(3):
                self.start(self.claude)
            response = self.authorize(other, challenge=pkce()[1], state="s-full")
            self.assert_error_redirect(response, "temporarily_unavailable", "s-full",
                                       "too many sign-ins in progress; try again in a few minutes")
            response = self.authorize(self.claude, challenge=pkce()[1], state="s-own")
            self.assert_error_redirect(response, "temporarily_unavailable", "s-own")
        self.assertEqual(self.pending_count(self.claude["client_id"]), 3)
        self.assertEqual(self.pending_count(other["client_id"]), 0)


class Loopback(SignInCase):
    """Claude Code: an http loopback redirect matches on any port, everything else exact (D4, S21, S22, F15)."""

    def test_any_port_on_the_registered_path(self):
        cli = self.public_client("http://localhost:1/callback")
        self.assertNotIn("client_secret", cli)
        flow = self.start(cli, redirect_uri="http://localhost:54321/callback")
        page = self.client.get("/consent", params={"req": flow["pending"]})
        self.assertEqual(page.status_code, 200)
        self.assertIn('<p class="warn">This sign-in returns to a program on this computer (<b>localhost:54321</b>), '
                      "such as Claude Code. Approve only if you started it here just now.</p>", page.text)
        self.assertNotIn("your browser goes back to", page.text)
        self.assertNotIn("form-action", page.headers["content-security-policy"])
        response = self.post_consent(flow["pending"], secret=self.owner_secret)
        self.assertEqual(response.status_code, 303)
        self.assertTrue(response.headers["location"].startswith("http://localhost:54321/callback?"))
        flow["code"] = query_of(response.headers["location"])["code"]
        response = self.exchange(flow)
        self.assertEqual(response.status_code, 200, why(response))
        self.assert_mcp(response.json()["access_token"], 200)

    def test_another_path_host_or_shape_is_refused(self):
        cli = self.public_client("http://localhost:1/callback")
        for uri in ("http://localhost:54321/other", "http://127.0.0.1:54321/callback", "http://[::1]:54321/callback",
                    "http://localhost:54321/callback#x", "http://localhost:54321/callback?x=1",
                    "http://user@localhost:54321/callback", "https://localhost:54321/callback",
                    "http://localhost:54321/Callback"):
            with self.subTest(uri=uri):
                response = self.authorize(cli, redirect_uri=uri, challenge=pkce()[1], state="s-22")
                self.assertEqual(response.status_code, 400)
                self.assertNotIn("location", response.headers)

    def test_an_empty_path_is_slash(self):
        # F15: a registered "http://localhost" has no path; a requested one with a port has "/".
        cli = self.public_client("http://localhost")
        for uri in ("http://localhost:5555", "http://localhost:5555/"):
            with self.subTest(uri=uri):
                flow = self.approve(self.start(cli, redirect_uri=uri))
                response = self.exchange(flow)
                self.assertEqual(response.status_code, 200, why(response))

    def test_ipv6_and_ipv4_loopback(self):
        for registered, used in (("http://[::1]:1/cb", "http://[::1]:6000/cb"),
                                 ("http://127.0.0.1/cb", "http://127.0.0.1:6001/cb")):
            with self.subTest(registered=registered):
                self.sign_in(self.public_client(registered), redirect_uri=used)


@unittest.skipUnless(HAVE_MCP2, "needs mcp 2.x")
class ClientModel(unittest.TestCase):
    """LoopbackAwareClient on its own."""

    def client(self, *uris):
        return owner_auth.LoopbackAwareClient(client_id="c", redirect_uris=list(uris), scope="offline_access")

    def test_scope_is_echoed_unchecked(self):
        client = self.client(CALLBACK)
        self.assertIsNone(client.validate_scope(None))
        self.assertEqual(client.validate_scope("claudeai  offline_access"), ["claudeai", "offline_access"])
        self.assertEqual(client.validate_scope(""), [])

    def test_https_redirects_match_exactly(self):
        from pydantic import AnyUrl
        client = self.client(CALLBACK)
        self.assertEqual(str(client.validate_redirect_uri(AnyUrl(CALLBACK))), CALLBACK)
        self.assertEqual(str(client.validate_redirect_uri(None)), CALLBACK)
        with self.assertRaises(InvalidRedirectUriError):
            client.validate_redirect_uri(AnyUrl("https://claude.com/api/mcp/auth_callback"))

    def test_a_registered_uri_the_allowlist_refuses_is_refused(self):
        from pydantic import AnyUrl
        client = self.client("https://app.example.com/cb", "http://localhost:1/callback")
        client._redirect_allowed = lambda uri: uri.scheme == "http"   # stands in for redirect_allowed()
        with self.assertRaises(InvalidRedirectUriError):
            client.validate_redirect_uri(AnyUrl("https://app.example.com/cb"))
        wanted = AnyUrl("http://localhost:4242/callback")
        self.assertIs(client.validate_redirect_uri(wanted), wanted)

    def test_loopback_returns_the_requested_uri(self):
        from pydantic import AnyUrl
        client = self.client("http://localhost:1/callback")
        wanted = AnyUrl("http://localhost:4242/callback")
        self.assertIs(client.validate_redirect_uri(wanted), wanted)

    def test_a_registered_origin_without_a_path_matches_its_slash_form(self):
        # V2-3: the client model keeps the empty path; /authorize parses it as "/".
        from pydantic import AnyUrl
        client = self.client("https://app.example.com")
        self.assertEqual(str(client.redirect_uris[0]), "https://app.example.com")
        wanted = AnyUrl("https://app.example.com")
        self.assertEqual(str(wanted), "https://app.example.com/")
        self.assertIs(client.validate_redirect_uri(wanted), wanted)
        self.assertEqual(str(client.validate_redirect_uri(AnyUrl("HTTPS://App.Example.com:443/"))),
                         "https://app.example.com/")
        for other in ("https://app.example.com/cb", "https://app.example.com:8443/", "https://app.example.com/?x=1",
                      "http://app.example.com/", "https://other.example.com/"):
            with self.subTest(other=other), self.assertRaises(InvalidRedirectUriError):
                client.validate_redirect_uri(AnyUrl(other))


# --- the consent page ---------------------------------------------------------------------------------


class Consent(SignInCase):
    """GET and POST /consent: every rule in contract 6.3.5 (S11-S17, F3, F16)."""

    def setUp(self):
        super().setUp()
        self.claude = self.register()
        self.flow = self.start(self.claude)
        self.pending = self.flow["pending"]

    def get_page(self, pending=None):
        return self.client.get("/consent", params={"req": self.pending if pending is None else pending})

    def assert_page(self, response, status):
        self.assertEqual(response.status_code, status, why(response))
        for name, value in PAGE_HEADERS.items():
            self.assertEqual(response.headers.get(name), value, name)
        self.assertTrue(response.headers["content-type"].startswith("text/html"))
        for name in ("cross-origin-opener-policy", "set-cookie"):
            self.assertNotIn(name, response.headers)
        self.assertNotIn("<script", response.text.lower())
        self.assertNotIn("http://", response.text)
        self.assertNotIn("https://", response.text)

    def assert_redirect(self, response, starts_with=CALLBACK + "?"):
        self.assertEqual(response.status_code, 303, why(response))
        for name, value in REDIRECT_HEADERS.items():
            self.assertEqual(response.headers.get(name), value, name)
        self.assertTrue(response.headers["location"].startswith(starts_with), "wrong redirect target")
        return query_of(response.headers["location"])

    def test_the_page(self):
        response = self.get_page()
        self.assert_page(response, 200)
        text = response.text
        for expected in ("You are signing in to <b>coach.example.com</b>.",
                         "App: <b>Claude</b> (not verified)",
                         "After you approve, your browser goes back to <b>claude.ai</b>.",
                         "Started less than a minute ago.",
                         "This gives read-only access to your Garmin data on this server.",
                         "Approve only a sign-in you started yourself just now. "
                         "If someone sent you this link, press Deny.",
                         '<form method="post" action="/consent">',
                         '<input type="hidden" name="req" value="%s">' % self.pending,
                         'name="username" value="owner" autocomplete="username" readonly',
                         'type="password" name="secret" autocomplete="current-password" required',
                         '<button type="submit" name="action" value="deny" formnovalidate>'):
            self.assertIn(expected, text)
        self.assertLess(text.index('value="approve"'), text.index('value="deny"'))   # F16: Enter approves
        self.assertNotIn('class="warn"', text)
        self.assertNotIn("form-action", response.headers["content-security-policy"])   # F3

    def test_started_minutes_ago(self):
        for wait, text in ((59, "less than a minute"), (1, "1 minute"), (65, "2 minutes")):
            self.clock.advance(wait)
            self.assertIn("Started %s ago." % text, self.get_page().text)

    def test_the_client_name_is_escaped_cut_and_flattened(self):
        name = "<script>alert(1)</script>" + "x" * 200 + "\n\u202eevil"
        flow = self.start(self.register(client_name=name))
        response = self.get_page(flow["pending"])
        self.assert_page(response, 200)
        self.assertIn("<b>&lt;script&gt;alert(1)&lt;/script&gt;" + "x" * 55 + "</b> (not verified)", response.text)
        self.assertNotIn("x" * 56, response.text)
        flow = self.start(self.register(client_name="Cla\u202eude\nCode"))
        self.assertIn("<b>Cla ude Code</b>", self.get_page(flow["pending"]).text)
        flow = self.start(self.register(client_name=None))
        self.assertIn("<b>an app with no name</b>", self.get_page(flow["pending"]).text)

    def test_an_unknown_or_missing_request_gets_400(self):
        # S17.
        for params in ({"req": "unknown"}, {"req": ""}, {}):
            with self.subTest(params=params):
                response = self.client.get("/consent", params=params)
                self.assert_page(response, 400)
                self.assertIn(EXPIRED_TEXT, response.text)

    def test_an_expired_request_is_refused_on_the_providers_clock(self):
        self.clock.advance(auth_config.PENDING_TTL)
        self.assert_page(self.get_page(), 400)
        response = self.post_consent(self.pending, secret=self.owner_secret)
        self.assert_page(response, 400)
        self.assertIn(EXPIRED_TEXT, response.text)
        self.assertEqual(self.sql("SELECT COUNT(*) FROM codes")[0][0], 0)

    def test_approve(self):
        # S14, then S15: single use.
        response = self.post_consent(self.pending, secret=self.owner_secret)
        query = self.assert_redirect(response)
        self.assertEqual(sorted(query), ["code", "state"])   # no iss (D13)
        self.assertEqual(query["state"], self.flow["state"])
        self.assertTrue(query["code"].startswith("ac_"))
        self.assertEqual(self.sql("SELECT COUNT(*) FROM codes WHERE code_hash = ?",
                                  auth_store.hash_secret(query["code"]))[0][0], 1)
        self.assertEqual(self.sql("SELECT COUNT(*) FROM codes WHERE code_hash = ?", query["code"])[0][0], 0)
        self.assertIn("consent: approved for client " + self.claude["client_id"], self.logs.auth_lines("INFO"))
        self.assert_page(self.post_consent(self.pending, secret=self.owner_secret), 400)
        self.assert_page(self.get_page(), 400)

    def test_deny(self):
        # S16: no secret needed.
        query = self.assert_redirect(self.post_consent(self.pending, action="deny"))
        self.assertEqual(query, {"error": "access_denied", "state": self.flow["state"]})
        self.assertIn("consent: denied for client " + self.claude["client_id"], self.logs.auth_lines("INFO"))
        self.assert_page(self.get_page(), 400)
        self.assertEqual(self.sql("SELECT COUNT(*) FROM codes")[0][0], 0)

    def test_three_wrong_secrets_end_the_request(self):
        # S12, F16.
        wrong = fake_secret()
        for n in (1, 2):
            response = self.post_consent(self.pending, secret=wrong)
            self.assert_page(response, 403)
            self.assertIn('<p class="error" role="alert">Wrong secret.</p>', response.text)
            self.assertIn('<input type="hidden" name="req" value="%s">' % self.pending, response.text)
            self.assertNotIn(wrong, response.text)
            self.assertIn("consent: wrong owner secret (%d of 3)" % n, self.logs.auth_lines("WARNING"))
        query = self.assert_redirect(self.post_consent(self.pending, secret=wrong))
        self.assertEqual(query, {"error": "access_denied", "state": self.flow["state"]})
        self.assertIn("consent: wrong owner secret (3 of 3)", self.logs.auth_lines("WARNING"))
        self.assert_page(self.get_page(), 400)
        self.assert_page(self.post_consent(self.pending, secret=self.owner_secret), 400)

    def test_two_wrong_secrets_then_the_right_one(self):
        for _ in range(2):
            self.assert_page(self.post_consent(self.pending, secret=fake_secret()), 403)
        self.assertIn("code", self.assert_redirect(self.post_consent(self.pending, secret=self.owner_secret)))

    def test_a_cross_site_post_is_refused_and_the_request_stays_usable(self):
        # S13 (D5).
        for site in ("cross-site", "same-site", "Cross-Site"):
            with self.subTest(site=site):
                self.assert_page(self.post_consent(self.pending, secret=self.owner_secret, site=site), 403)
        self.assertEqual(self.logs.auth_lines("WARNING"), [])
        self.assertIn("code", self.assert_redirect(self.post_consent(self.pending, secret=self.owner_secret,
                                                                      site="none")))
        flow = self.start(self.claude)
        self.assertIn("code", self.assert_redirect(self.post_consent(flow["pending"], secret=self.owner_secret,
                                                                      site=None)))

    def test_a_missing_action_with_a_secret_approves(self):
        # F16: a scripted submit, or Enter in a browser that sends no button.
        self.assertIn("code", self.assert_redirect(self.post_consent(self.pending, secret=self.owner_secret,
                                                                      action=None)))

    def test_other_actions_are_refused(self):
        for kwargs in ({"action": None, "secret": ""}, {"action": None}, {"action": "maybe", "secret": "x"},
                       {"action": "", "secret": "x"}):
            with self.subTest(**kwargs):
                self.assert_page(self.post_consent(self.pending, **kwargs), 400)
        self.assertEqual(self.logs.auth_lines("WARNING"), [])
        self.approve(self.flow)

    def test_size_limits(self):
        # S41, direct leg: 8 KiB by Content-Length or by what actually arrives.
        def form(n):
            return urlencode({"req": self.pending, "username": "owner", "secret": "w" * n, "action": "approve"})
        n = 8192 - len(form(0))
        self.assertEqual(len(form(n)), 8192)
        headers = {"Content-Type": FORM, "Sec-Fetch-Site": "same-origin"}
        self.assert_page(self.client.post("/consent", content=form(n), headers=headers), 403)
        self.assert_page(self.client.post("/consent", content=form(n + 1), headers=headers), 413)
        self.assert_page(self.client.post("/consent", content=b"req=x&secret=" + b"a" * 10000, headers=headers), 413)

        def chunks():
            yield b"req=x&secret="
            for _ in range(10):
                yield b"a" * 1000
        response = self.client.post("/consent", content=chunks(), headers=headers)
        self.assertNotIn("content-length", response.request.headers)
        self.assert_page(response, 413)

    def test_only_a_form_post_is_accepted(self):
        body = urlencode({"req": self.pending, "secret": self.owner_secret, "action": "approve"})
        for content_type in ("application/json", "multipart/form-data; boundary=x", "text/plain", None):
            with self.subTest(content_type=content_type):
                headers = {"Sec-Fetch-Site": "same-origin"}
                if content_type:
                    headers["Content-Type"] = content_type
                self.assert_page(self.client.post("/consent", content=body, headers=headers), 400)
        headers = {"Sec-Fetch-Site": "same-origin", "Content-Type": FORM + "; charset=UTF-8"}
        self.assert_redirect(self.client.post("/consent", content=body, headers=headers))

    def test_too_many_fields_are_refused(self):
        fields = {"f%d" % i: "x" for i in range(8)}
        self.assert_page(self.post_consent(self.pending, secret=self.owner_secret, **fields), 400)
        self.approve(self.flow)

    def test_oauth_parameters_come_from_the_stored_request(self):
        response = self.post_consent(self.pending, secret=self.owner_secret, redirect_uri="https://evil.example/cb",
                                     state="forged", code_challenge="x" * 43)
        query = self.assert_redirect(response)
        self.assertEqual(query["state"], self.flow["state"])

    def test_a_non_ascii_secret_does_not_crash(self):
        self.assert_page(self.post_consent(self.pending, secret="pässwörd ✓"), 403)
        headers = {"Content-Type": FORM, "Sec-Fetch-Site": "same-origin"}
        body = b"req=" + self.pending.encode() + b"&action=approve&secret="
        # Invalid UTF-8, percent-escaped and then raw; the raw one is the third wrong secret.
        self.assert_page(self.client.post("/consent", content=body + b"%FF%FE", headers=headers), 403)
        self.assert_redirect(self.client.post("/consent", content=body + b"\xff\xfe\xc3", headers=headers))
        self.assertIn("consent: wrong owner secret (3 of 3)", self.logs.auth_lines("WARNING"))

    def test_a_client_gone_mid_form_gets_400(self):
        app = self.make_app()
        messages = [{"type": "http.request", "body": b"req=" + self.pending.encode() + b"&sec", "more_body": True},
                    {"type": "http.disconnect"}]
        sent = []

        async def receive():
            return messages.pop(0) if messages else {"type": "http.disconnect"}

        async def send(message):
            sent.append(message)

        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
                 "scheme": "http", "path": "/consent", "raw_path": b"/consent", "query_string": b"",
                 "root_path": "", "headers": [(b"host", b"127.0.0.1:8000"), (b"content-type", FORM.encode())],
                 "client": ("127.0.0.1", 50000), "server": ("127.0.0.1", 8000)}
        asyncio.run(app(scope, receive, send))
        self.assertEqual(sent[0]["status"], 400)
        self.assertEqual(self.client.get("/consent", params={"req": self.pending}).status_code, 200)


# --- tokens ---------------------------------------------------------------------------------------------


class Codes(SignInCase):
    """/token with authorization codes: single use, reuse revokes (AZ-4, F13), expiry on our clock."""

    def setUp(self):
        super().setUp()
        self.claude = self.register()

    def test_exchange(self):
        # S23.
        flow = self.approve(self.start(self.claude))
        response = self.exchange(flow)
        self.assertEqual(response.status_code, 200, why(response))
        self.assertEqual((response.headers["cache-control"], response.headers["pragma"]), ("no-store", "no-cache"))
        tokens = response.json()
        self.assertEqual((tokens["token_type"], tokens["expires_in"], tokens["scope"]),
                         ("Bearer", 3600, "offline_access"))
        self.assertTrue(tokens["access_token"].startswith("at_"))
        self.assertTrue(tokens["refresh_token"].startswith("rt_"))
        self.assertIn("token: code exchanged for client %s (auth method client_secret_post)"
                      % self.claude["client_id"], self.logs.auth_lines("INFO"))
        rows = dict(self.sql("SELECT token_hash, kind FROM tokens"))
        self.assertEqual(rows, {auth_store.hash_secret(tokens["access_token"]): "access",
                                auth_store.hash_secret(tokens["refresh_token"]): "refresh"})
        self.assert_mcp(tokens["access_token"], 200)

    def test_a_replayed_code_revokes_its_grant(self):
        # S24.
        flow = self.sign_in(self.claude)
        response = self.exchange(flow)
        self.assertEqual((response.status_code, response.json()["error"]), (400, "invalid_grant"))
        self.assert_mcp(flow["tokens"]["access_token"], 401)
        self.assertEqual(self.refresh(self.claude, flow["tokens"]["refresh_token"]).status_code, 400)
        self.assertEqual(self.grant_reasons(), ["authorization code reuse"])
        self.assertIn("token: grant revoked for client %s (authorization code reuse)" % self.claude["client_id"],
                      self.logs.auth_lines("WARNING"))

    def test_losing_the_exchange_race_revokes(self):
        # F13: two exchanges that both passed load_authorization_code.
        flow = self.approve(self.start(self.claude))

        async def race():
            client = await self.provider.get_client(self.claude["client_id"])
            code = await self.provider.load_authorization_code(client, flow["code"])
            first = await self.provider.exchange_authorization_code(client, code)
            try:
                await self.provider.exchange_authorization_code(client, code)
            except TokenError as exc:
                return first, exc
            return first, None

        first, error = self.run_async(race())
        self.assertIsNotNone(error)
        self.assertEqual((error.error, error.error_description), ("invalid_grant", "authorization code already used"))
        self.assertIsNone(self.run_async(self.provider.load_access_token(first.access_token)))
        self.assertEqual(self.grant_reasons(), ["authorization code reuse"])

    def test_a_wrong_verifier_or_client_secret_is_refused(self):
        # S25, S26.
        flow = self.approve(self.start(self.claude))
        response = self.exchange(flow, code_verifier=pkce()[0])
        self.assertEqual((response.status_code, response.json()["error"]), (400, "invalid_grant"))
        response = self.exchange(flow, client_secret="x" * 64)
        self.assertEqual((response.status_code, response.json()["error"]), (401, "invalid_client"))
        response = self.exchange(flow, redirect_uri=CALLBACK + "x")
        self.assertEqual((response.status_code, response.json()["error"]), (400, "invalid_request"))
        self.assertEqual(self.exchange(flow).status_code, 200)   # a refused attempt does not use the code

    def test_another_clients_code_is_unknown_and_revokes_nothing(self):
        other = self.register()
        flow = self.approve(self.start(self.claude))
        stolen = dict(flow, client=other)
        response = self.exchange(stolen)
        self.assertEqual((response.status_code, response.json()["error"]), (400, "invalid_grant"))
        self.assertEqual(self.exchange(flow).status_code, 200)
        self.assertEqual(self.grant_reasons(), [None])

    def test_an_expired_code_is_refused_on_the_providers_clock(self):
        flow = self.approve(self.start(self.claude))
        self.clock.advance(auth_config.CODE_TTL)
        response = self.exchange(flow)   # the SDK's own check uses the real time and lets it through
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"error": "invalid_grant", "error_description": "authorization code has expired"})
        self.assertEqual(self.sql("SELECT COUNT(*) FROM grants")[0][0], 0)


class Refresh(SignInCase):
    """Refresh rotation, the F4 replay window, reuse revocation, expiry and scope (S28-S31)."""

    def setUp(self):
        super().setUp()
        self.claude = self.register()
        self.flow = self.sign_in(self.claude)
        self.rt1 = self.flow["tokens"]["refresh_token"]

    def warnings(self):
        return self.logs.auth_lines("WARNING")

    def test_rotation(self):
        # S28.
        t2 = self.rotate(self.claude, self.rt1)
        self.assertNotEqual(t2["access_token"], self.flow["tokens"]["access_token"])
        self.assertNotEqual(t2["refresh_token"], self.rt1)
        self.assertEqual((t2["expires_in"], t2["scope"], t2["token_type"]), (3600, "offline_access", "Bearer"))
        parent = self.sql("SELECT parent_hash FROM tokens WHERE token_hash = ?",
                          auth_store.hash_secret(t2["refresh_token"]))[0][0]
        self.assertEqual(parent, auth_store.hash_secret(self.rt1))
        self.assertIn("token: refresh for client " + self.claude["client_id"], self.logs.auth_lines("INFO"))
        self.assert_mcp(t2["access_token"], 200)

    def test_a_replay_within_120_s_returns_the_same_pair(self):
        # F4 rule 5: at 31 s and at 119 s (and at the 120 s edge), and the grant stays alive.
        t2 = self.rotate(self.claude, self.rt1)
        for wait, age in ((31, 31), (88, 119), (1, 120)):
            self.clock.advance(wait)
            again = self.rotate(self.claude, self.rt1)
            self.assertEqual((again["access_token"], again["refresh_token"]), (t2["access_token"], t2["refresh_token"]))
            self.assertEqual(again["expires_in"], 3600 - age)
            self.assertIn("token: refresh token reused within %ds; returned the same pair for client %s"
                          % (age, self.claude["client_id"]), self.warnings())
        self.assertEqual(self.grant_reasons(), [None])
        self.assert_mcp(t2["access_token"], 200)
        t3 = self.rotate(self.claude, t2["refresh_token"])
        self.assertNotEqual(t3["refresh_token"], t2["refresh_token"])

    def test_three_concurrent_refreshes_get_the_same_pair(self):
        # F4 rule 5: each thread has its own TestClient, so each request runs on its own event loop.
        barrier = threading.Barrier(3)
        results = [None] * 3

        def worker(i):
            http = TestClient(self.client.app, base_url=BASE, follow_redirects=False)
            barrier.wait()
            try:
                results[i] = self.refresh(self.claude, self.rt1, http=http)
            except BaseException as exc:  # noqa: BLE001 - reported below
                results[i] = exc

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual([getattr(r, "status_code", type(r).__name__) for r in results], [200, 200, 200])
        pairs = {(r.json()["access_token"], r.json()["refresh_token"]) for r in results}
        self.assertEqual(len(pairs), 1)
        self.assertEqual(self.sql("SELECT COUNT(*) FROM tokens WHERE parent_hash = ?",
                                  auth_store.hash_secret(self.rt1))[0][0], 1)
        self.assertEqual(self.grant_reasons(), [None])

    def test_loads_that_interleave_with_a_rotation(self):
        # F4 rule 4: all three loads see an unused token before any exchange runs.
        async def interleave():
            client = await self.provider.get_client(self.claude["client_id"])
            loaded = [await self.provider.load_refresh_token(client, self.rt1) for _ in range(3)]
            return await asyncio.gather(*(self.provider.exchange_refresh_token(client, rt, rt.scopes)
                                          for rt in loaded))

        tokens = self.run_async(interleave())
        self.assertEqual(len({(t.access_token, t.refresh_token) for t in tokens}), 1)
        self.assertEqual(self.grant_reasons(), [None])

    def test_using_rt2_then_replaying_rt1_revokes(self):
        # S30, F4 rule 3.
        t2 = self.rotate(self.claude, self.rt1)
        t3 = self.rotate(self.claude, t2["refresh_token"])
        response = self.refresh(self.claude, self.rt1)
        self.assertEqual((response.status_code, response.json()["error"]), (400, "invalid_grant"))
        self.assertEqual(self.grant_reasons(), ["refresh token reuse"])
        self.assertIn("token: grant revoked for client %s (refresh token reuse)" % self.claude["client_id"],
                      self.warnings())
        self.assert_mcp(t3["access_token"], 401)
        self.assertEqual(self.refresh(self.claude, t3["refresh_token"]).status_code, 400)

    def test_a_replay_at_121_s_revokes(self):
        t2 = self.rotate(self.claude, self.rt1)
        self.clock.advance(121)
        self.assertEqual(self.refresh(self.claude, self.rt1).status_code, 400)
        self.assertEqual(self.grant_reasons(), ["refresh token reuse"])
        self.assert_mcp(t2["access_token"], 401)
        self.assertEqual(self.refresh(self.claude, t2["refresh_token"]).status_code, 400)

    def test_a_replay_after_a_restart_revokes(self):
        # The replay map lives in memory only (F4 rule 3).
        t2 = self.rotate(self.claude, self.rt1)
        restarted = self.build()
        http = self.new_client(restarted)
        self.assertEqual(self.refresh(self.claude, self.rt1, http=http).status_code, 400)
        self.assertEqual(self.grant_reasons(), ["refresh token reuse"])
        self.assert_mcp(t2["access_token"], 401, auth=restarted)

    def test_a_replay_by_another_client_revokes_nothing(self):
        other = self.register()
        t2 = self.rotate(self.claude, self.rt1)
        response = self.refresh(other, self.rt1)
        self.assertEqual((response.status_code, response.json()["error"]), (400, "invalid_grant"))
        self.assertEqual(self.grant_reasons(), [None])
        self.assertEqual(self.rotate(self.claude, self.rt1)["refresh_token"], t2["refresh_token"])

    def test_an_unknown_refresh_token_is_refused(self):
        response = self.refresh(self.claude, "rt_" + secrets.token_urlsafe(32))
        self.assertEqual((response.status_code, response.json()["error"]), (400, "invalid_grant"))
        self.assertEqual(self.grant_reasons(), [None])

    def test_the_replay_map_is_bounded(self):
        with mock.patch.object(owner_auth, "REPLAY_MAX_ENTRIES", 2):
            flows = [self.flow]
            for _ in range(2):
                self.clock.advance(1)   # grants are listed by creation time
                flows.append(self.sign_in(self.claude))
            pairs = [self.rotate(self.claude, f["tokens"]["refresh_token"]) for f in flows]
            self.assertEqual(len(self.provider._replay), 2)
            self.assertEqual(self.refresh(self.claude, self.rt1).status_code, 400)   # the oldest entry was dropped
            again = self.rotate(self.claude, flows[2]["tokens"]["refresh_token"])
        self.assertEqual(again["refresh_token"], pairs[2]["refresh_token"])
        self.assertEqual(self.grant_reasons(), ["refresh token reuse", None, None])

    def test_a_refresh_token_expires_after_30_days(self):
        # F22: the SDK's real-time check would pass; the provider refuses on its own clock.
        self.clock.advance(auth_config.REFRESH_TTL)
        self.assertGreater(self.sql("SELECT expires_at FROM tokens WHERE kind = 'refresh'")[0][0], time.time())

        async def load():
            client = await self.provider.get_client(self.claude["client_id"])
            return await self.provider.load_refresh_token(client, self.rt1)

        # Checked before any write: the store's tidy-up deletes expired rows, which
        # would hide a provider that forgot its own expiry check.
        self.assertEqual(self.sql("SELECT COUNT(*) FROM tokens WHERE kind = 'refresh'")[0][0], 1)
        self.assertIsNone(self.run_async(load()))
        response = self.refresh(self.claude, self.rt1)
        self.assertEqual((response.status_code, response.json()["error"]), (400, "invalid_grant"))

    def test_the_replay_window_belongs_to_one_client(self):
        # F4 rule 2, behind the SDK's own client check: even a direct call never
        # hands this client's pair to another client, and revokes nothing.
        t2 = self.rotate(self.claude, self.rt1)
        other = self.register()

        async def exchange():
            client = await self.provider.get_client(other["client_id"])
            token = RefreshToken(token=self.rt1, client_id=other["client_id"], scopes=["offline_access"])
            return await self.provider.exchange_refresh_token(client, token, ["offline_access"])

        with self.assertRaises(TokenError) as caught:
            self.run_async(exchange())
        self.assertEqual(caught.exception.error, "invalid_grant")
        self.assertEqual(self.grant_reasons(), [None])
        self.assertEqual(self.rotate(self.claude, self.rt1)["refresh_token"], t2["refresh_token"])

    def test_refresh_slides(self):
        self.clock.advance(29 * 86400)
        t2 = self.rotate(self.claude, self.rt1)
        self.clock.advance(29 * 86400)
        self.rotate(self.claude, t2["refresh_token"])

    def test_an_access_token_expires_after_an_hour(self):
        # F22.
        access = self.flow["tokens"]["access_token"]
        self.clock.advance(auth_config.ACCESS_TTL)
        self.assert_mcp(access, 401)
        self.assertIsNone(self.run_async(self.provider.load_access_token(access)))
        self.rotate(self.claude, self.rt1)

    def test_a_scope_the_grant_does_not_hold_is_refused(self):
        # F18, S31: the SDK's invalid_scope; the refused request does not use the token.
        response = self.refresh(self.claude, self.rt1, scope="other")
        self.assertEqual((response.status_code, response.json()["error"]), (400, "invalid_scope"))
        self.assertEqual(self.rotate(self.claude, self.rt1)["scope"], "offline_access")

    def test_a_narrower_access_scope_keeps_the_refresh_scopes(self):
        flow = self.sign_in(self.claude, scope="offline_access claudeai")
        response = self.refresh(self.claude, flow["tokens"]["refresh_token"], scope="claudeai")
        self.assertEqual(response.status_code, 200, why(response))
        t2 = response.json()
        self.assertEqual(t2["scope"], "claudeai")
        t3 = self.rotate(self.claude, t2["refresh_token"])
        self.assertEqual(t3["scope"], "offline_access claudeai")


class Revoke(SignInCase):
    """/revoke (S32): the whole grant goes; other clients' tokens stay."""

    def setUp(self):
        super().setUp()
        self.claude = self.register()
        self.flow = self.sign_in(self.claude)
        self.tokens = self.flow["tokens"]

    def assert_revoked(self):
        self.assert_mcp(self.tokens["access_token"], 401)
        self.assertEqual(self.refresh(self.claude, self.tokens["refresh_token"]).status_code, 400)
        self.assertEqual(self.grant_reasons(), ["revoked by client"])
        self.assertIn("token: grant revoked for client %s (revoked by client)" % self.claude["client_id"],
                      self.logs.auth_lines("WARNING"))

    def test_revoking_the_refresh_token(self):
        response = self.revoke(self.claude, self.tokens["refresh_token"])
        self.assertEqual(response.status_code, 200)
        self.assertEqual((response.headers["cache-control"], response.content), ("no-store", b""))
        self.assert_revoked()

    def test_revoking_the_access_token(self):
        self.assertEqual(self.revoke(self.claude, self.tokens["access_token"]).status_code, 200)
        self.assert_revoked()

    def test_revoking_with_a_refresh_hint(self):
        response = self.revoke(self.claude, self.tokens["refresh_token"], token_type_hint="refresh_token")
        self.assertEqual(response.status_code, 200)
        self.assert_revoked()

    def test_an_unknown_token_is_200_and_revokes_nothing(self):
        self.assertEqual(self.revoke(self.claude, "rt_" + secrets.token_urlsafe(32)).status_code, 200)
        self.assertEqual(self.grant_reasons(), [None])

    def test_another_clients_tokens_are_not_revoked(self):
        other = self.register()
        for token in self.tokens["access_token"], self.tokens["refresh_token"]:
            self.assertEqual(self.revoke(other, token).status_code, 200)
        self.assertEqual(self.grant_reasons(), [None])
        self.assert_mcp(self.tokens["access_token"], 200)

    def test_a_public_client_with_an_empty_secret(self):
        cli = self.public_client()
        flow = self.sign_in(cli)
        self.assertEqual(self.revoke(cli, flow["tokens"]["refresh_token"], client_secret="").status_code, 200)
        self.assert_mcp(flow["tokens"]["access_token"], 401)


# --- the static bearer and the audience check -------------------------------------------------------------


class StaticBearer(SignInCase):
    """The opt-in MCP_BEARER_TOKEN header (S33-S35, F21)."""

    with_static_bearer = True

    def test_the_right_value_is_accepted(self):
        self.assert_mcp(self.bearer, 200)

    def test_a_wrong_value_gets_401_with_resource_metadata(self):
        for token in (self.bearer + "x", self.bearer[:-1], self.bearer.lower(), " " + self.bearer, ""):
            with self.subTest(length=len(token)):
                self.assertEqual(self.assert_mcp(token, 401).headers["www-authenticate"], WWW_AUTHENTICATE)

    def test_a_non_ascii_header_value_does_not_crash(self):
        for raw in (b"Bearer \xff\xfe" + self.bearer.encode(), b"Bearer " + self.bearer.encode() + b"\xe9"):
            self.assert_mcp(None, 401, headers={"Authorization": raw})

    def test_a_token_in_the_query_is_ignored(self):
        self.assert_mcp(None, 401, path="/mcp?access_token=" + self.bearer)

    def test_the_access_token_it_maps_to(self):
        token = self.run_async(self.provider.load_access_token(self.bearer))
        self.assertEqual((token.client_id, token.scopes, token.expires_at, token.resource, token.subject),
                         ("static-bearer", [], None, RESOURCE, "owner"))

    def test_it_survives_an_owner_secret_change(self):
        # F21: rotating OWNER_SECRET signs out OAuth clients, not the static bearer.
        changed = self.build(owner_secret=fake_secret())
        self.assert_mcp(self.bearer, 200, auth=changed)

    def test_revoking_it_does_nothing(self):
        client = self.register()
        self.assertEqual(self.revoke(client, self.bearer).status_code, 200)
        self.run_async(self.provider.revoke_token(self.run_async(self.provider.load_access_token(self.bearer))))
        self.assert_mcp(self.bearer, 200)

    def test_repr_hides_the_secrets(self):
        for text in (repr(self.auth), repr(self.auth.config), str(self.auth)):
            self.assertNotIn(self.bearer, text)
            self.assertNotIn(self.owner_secret, text)


class NoStaticBearer(SignInCase):

    def test_any_bearer_value_gets_401(self):
        self.assertIsNone(self.run_async(self.provider.load_access_token(fake_secret())))
        self.assert_mcp(fake_secret(), 401)


class TokenResource(SignInCase):
    """A token is accepted only for this server's /mcp (validate_token_resource)."""

    def mint(self, resource, revoked=False):
        token = "at_" + secrets.token_urlsafe(32)
        grant_id, now = secrets.token_hex(16), self.clock()
        db = sqlite3.connect(self.db_path)
        try:
            with db:
                db.execute("INSERT INTO grants (grant_id, client_id, scopes, resource, created_at, last_used_at, "
                           "revoked_at, revoked_reason) VALUES (?, 'minted', '', ?, ?, ?, ?, ?)",
                           (grant_id, resource, now, now, now if revoked else None,
                            "revoked by client" if revoked else None))
                db.execute("INSERT INTO tokens (token_hash, kind, grant_id, client_id, scopes, created_at, expires_at) "
                           "VALUES (?, 'access', ?, 'minted', '', ?, ?)",
                           (auth_store.hash_secret(token), grant_id, now, now + 3600))
        finally:
            db.close()
        return token

    def test_this_resource_in_any_spelling_is_accepted(self):
        for resource in (RESOURCE, "https://COACH.example.com:443/mcp/"):
            with self.subTest(resource=resource):
                self.assert_mcp(self.mint(resource), 200)

    def test_another_resource_or_none_gets_401(self):
        for resource in ("https://other.example/mcp", "", PUBLIC, RESOURCE + "/x", "http://coach.example.com/mcp"):
            with self.subTest(resource=resource):
                self.assert_mcp(self.mint(resource), 401)
        token = self.run_async(self.provider.load_access_token(self.mint("")))
        self.assertIsNone(token.resource)

    def test_a_revoked_grant_gets_401(self):
        self.assert_mcp(self.mint(RESOURCE, revoked=True), 401)


class OwnerSecretChange(SignInCase):
    """D10: a new OWNER_SECRET at startup revokes every grant, pending request and unused code."""

    def test_a_new_secret_revokes_everything(self):
        claude = self.register()
        signed_in = self.sign_in(claude)
        waiting = self.start(claude)
        unused = self.approve(self.start(claude))
        new_secret = fake_secret()
        restarted = self.build(owner_secret=new_secret)
        self.assertIn("sign-in: the owner secret changed; every sign-in was revoked", self.logs.auth_lines("WARNING"))
        self.assertEqual(self.grant_reasons(), ["owner secret changed"])
        http = self.new_client(restarted)
        self.assert_mcp(signed_in["tokens"]["access_token"], 401, auth=restarted)
        self.assertEqual(self.refresh(claude, signed_in["tokens"]["refresh_token"], http=http).status_code, 400)
        self.assertEqual(http.get("/consent", params={"req": waiting["pending"]}).status_code, 400)
        self.assertEqual(self.sql("SELECT COUNT(*) FROM codes WHERE used_at IS NULL")[0][0], 0)
        old_secret, self.client = self.owner_secret, http
        flow = self.start(claude)
        self.assertEqual(self.post_consent(flow["pending"], secret=old_secret).status_code, 403)
        self.owner_secret = new_secret
        self.sign_in(claude)
        exchange = self.exchange(dict(unused, client=claude))
        self.assertEqual((exchange.status_code, exchange.json()["error"]), (400, "invalid_grant"))

    def test_the_same_secret_revokes_nothing(self):
        flow = self.sign_in()
        restarted = self.build()
        self.assertNotIn("sign-in: the owner secret changed; every sign-in was revoked", self.logs.auth_lines())
        self.assertIn("sign-in: database ready (1 clients, 1 live grants)", self.logs.auth_lines("INFO"))
        self.assert_mcp(flow["tokens"]["access_token"], 200, auth=restarted)


# --- wiring and the build check ----------------------------------------------------------------------------


class Wiring(SignInCase):
    """build(), register_routes(), asgi_middleware() and the provider's shape."""

    def test_build_logs_the_database_state(self):
        self.assertIn("sign-in: database ready (0 clients, 0 live grants)", self.logs.auth_lines("INFO"))

    def test_settings(self):
        settings = self.auth.settings
        self.assertEqual((str(settings.issuer_url), str(settings.resource_server_url)), (PUBLIC, RESOURCE))
        self.assertIs(settings.validate_token_resource, True)
        self.assertIsNone(settings.required_scopes)
        self.assertFalse(settings.identity_assertion_enabled)
        options = settings.client_registration_options
        self.assertEqual((options.enabled, options.valid_scopes, options.default_scopes,
                          options.client_secret_expiry_seconds), (True, ["offline_access"], ["offline_access"], None))
        self.assertTrue(settings.revocation_options.enabled)

    def test_asgi_middleware(self):
        self.assertEqual(owner_auth.asgi_middleware(self.auth), [(owner_auth.RegistrationFilter, {
            "allowed_grants": ("authorization_code", "refresh_token"), "valid_scopes": ("offline_access",),
            "max_body": 65536})])

    def test_register_routes_adds_two_routes_bound_to_their_auth(self):
        calls = []

        class FakeApp:
            def custom_route(self, path, methods):
                calls.append((path, methods))
                return lambda handler: handler

        owner_auth.register_routes(FakeApp(), self.auth)
        self.assertEqual(calls, [("/consent", ["GET", "POST"]), ("/.well-known/oauth-protected-resource", ["GET"])])
        other = self.build(public_url="https://other.example.com", db_path=os.path.join(self.tmp, "other.sqlite"))
        self.assertEqual(self.new_client(other).get("/.well-known/oauth-protected-resource").json()["resource"],
                         "https://other.example.com/mcp")
        self.assertEqual(self.client.get("/.well-known/oauth-protected-resource").json()["resource"], RESOURCE)

    def test_build_closes_its_store_when_it_fails(self):
        opened = []

        class Recording(auth_store.AuthStore):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                opened.append(self)

        with mock.patch.object(owner_auth, "AuthStore", Recording), \
                mock.patch.object(owner_auth, "AuthSettings", side_effect=ValueError("refused")):
            with self.assertRaises(ValueError):
                owner_auth.build(self.config(), clock=self.clock)
        self.assertEqual(len(opened), 1)
        with self.assertRaises(sqlite3.ProgrammingError):
            opened[0].counts()

    def test_the_provider_has_every_protocol_method(self):
        from mcp.server.auth.provider import OAuthAuthorizationServerProvider
        names = [n for n, v in vars(OAuthAuthorizationServerProvider).items() if not n.startswith("_") and callable(v)]
        self.assertIn("exchange_identity_assertion", names)
        for name in names:
            self.assertTrue(inspect.iscoroutinefunction(getattr(self.provider, name)), name)

    def test_the_identity_assertion_grant_is_refused(self):
        with self.assertRaises(TokenError) as caught:
            self.run_async(self.provider.exchange_identity_assertion(None, None))
        self.assertEqual(caught.exception.error, "unsupported_grant_type")

    def test_a_damaged_client_record_reads_as_unknown(self):
        secret_like = fake_secret()
        db = sqlite3.connect(self.db_path)
        with db:
            db.execute("INSERT INTO clients (client_id, info_json, created_at, last_used_at) VALUES ('bad', ?, 0, 0)",
                       ('{"client_id": "bad", "client_secret": "%s", "redirect_uris": 7}' % secret_like,))
        db.close()
        self.assertIsNone(self.run_async(self.provider.get_client("bad")))
        response = self.client.post("/token", data={"grant_type": "refresh_token", "refresh_token": "x",
                                                     "client_id": "bad", "client_secret": secret_like})
        self.assertEqual(response.status_code, 401)
        self.assertNotIn(secret_like, self.logs.server_text())


class SelfCheck(SignInCase):
    """selfcheck(): the Docker build check (6.3.7)."""

    def test_it_passes_quietly(self):
        before = len(self.logs.records)
        owner_auth.selfcheck()
        self.assertEqual([r for r in self.logs.records[before:] if r[0] == "garmin_ext.auth"], [])

    def test_it_touches_no_network(self):
        def refuse(*args, **kwargs):
            raise AssertionError("selfcheck reached for the network")

        with mock.patch("socket.socket.connect", refuse), mock.patch("socket.socket.bind", refuse), \
                mock.patch("socket.getaddrinfo", refuse), mock.patch("socket.create_connection", refuse):
            owner_auth.selfcheck()

    def test_it_passes_in_a_fresh_interpreter_with_no_output(self):
        code = "import owner_auth; owner_auth.selfcheck()"
        result = subprocess.run([sys.executable, "-c", code], cwd=HERE, capture_output=True, text=True, timeout=120,
                                env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))

    def test_a_missing_route_fails_with_one_line(self):
        with mock.patch.object(owner_auth, "SELFCHECK_PATHS", owner_auth.SELFCHECK_PATHS + ("/missing",)):
            with self.assertRaisesRegex(RuntimeError, r"^owner_auth selfcheck: not routed: /missing$"):
                owner_auth.selfcheck()

    def test_a_missing_sdk_method_fails(self):
        with mock.patch.object(OAuthClientInformationFull, "validate_scope", None):
            with self.assertRaisesRegex(RuntimeError, r"^owner_auth selfcheck: OAuthClientInformationFull has no "
                                                      r"validate_scope\(\)$"):
                owner_auth.selfcheck()

    def test_other_errors_are_wrapped(self):
        with mock.patch.object(owner_auth, "build", side_effect=OSError("no room")):
            with self.assertRaisesRegex(RuntimeError, r"^owner_auth selfcheck: OSError: no room$"):
                owner_auth.selfcheck()


# --- logs --------------------------------------------------------------------------------------------------


class NoSecretsInLogs(SignInCase):
    """A full in-process flow, every server-side log record captured at DEBUG: no secret appears (section 8, F23)."""

    with_static_bearer = True

    def test_a_full_flow_logs_no_secret(self):
        seen = [self.owner_secret, self.bearer]
        basic_pairs = []

        def remember(flow):
            seen.extend(v for k, v in flow.items() if k in ("verifier", "challenge", "state", "pending", "code"))
            seen.extend(flow.get("tokens", {}).get(k) for k in ("access_token", "refresh_token"))

        claude = self.register(token_endpoint_auth_method="client_secret_basic",
                               grant_types=["authorization_code", "refresh_token", JWT_BEARER],
                               scope="claudeai offline_access")
        cli = self.public_client()
        for client in (claude, cli):
            if client.get("client_secret"):
                seen.append(client["client_secret"])
                basic_pairs.append(client["client_id"] + ":" + client["client_secret"])

        flow = self.start(claude)
        self.client.get("/consent", params={"req": flow["pending"]})
        self.post_consent(flow["pending"], secret=fake_secret())
        self.post_consent(flow["pending"], secret=self.owner_secret, site="cross-site")
        self.approve(flow)
        flow["tokens"] = self.exchange(flow).json()
        remember(flow)
        t2 = self.rotate(claude, flow["tokens"]["refresh_token"])
        self.clock.advance(5)
        self.rotate(claude, flow["tokens"]["refresh_token"])   # a replay inside the window
        t3 = self.rotate(claude, t2["refresh_token"])
        self.refresh(claude, flow["tokens"]["refresh_token"])   # reuse: revokes
        seen.extend([t2["access_token"], t2["refresh_token"], t3["access_token"], t3["refresh_token"]])

        local = self.sign_in(cli, redirect_uri="http://localhost:43210/callback")
        remember(local)
        self.assert_mcp(local["tokens"]["access_token"], 200)
        self.exchange(local)   # code replay: revokes
        self.revoke(cli, local["tokens"]["refresh_token"])

        fresh = self.sign_in(claude)
        remember(fresh)
        self.revoke(claude, fresh["tokens"]["access_token"])

        denied = self.start(claude)
        remember(denied)
        self.post_consent(denied["pending"], action="deny")
        stale = self.start(claude)
        remember(stale)
        for _ in range(3):
            secret = fake_secret()
            seen.append(secret)
            self.post_consent(stale["pending"], secret=secret)
        self.assert_mcp(self.bearer, 200)
        self.assert_mcp(self.bearer + "x", 401)
        self.post_register(redirect_uris=["https://evil.example/cb"])
        self.client.post("/register", content=b"[" * 30000 + b"]" * 30000, headers={"Content-Type": "application/json"})

        text = self.logs.server_text()
        squeezed = "".join(text.split())
        forms = []
        for value in filter(None, seen):
            forms += [value, quote(value, safe=""), json.dumps(value)[1:-1]]
        forms += [base64.b64encode(pair.encode()).decode() for pair in basic_pairs]
        hits = sum(1 for form in forms if len(form) >= 8 and (form in text or form in squeezed))
        self.assertEqual(hits, 0, "%d secret value(s) in the server logs" % hits)
        self.assertGreater(len(seen), 25)
        for expected in ("dcr: request ", "dcr: registered client ", "authorize: client ", "consent: approved for client ",
                         "consent: denied for client ", "consent: wrong owner secret (1 of 3)",
                         "consent: wrong owner secret (3 of 3)", "token: code exchanged for client ",
                         "token: refresh for client ", "token: refresh token reused within 5s",
                         "token: grant revoked for client ", "(refresh token reuse)", "(authorization code reuse)",
                         "(revoked by client)"):
            self.assertIn(expected, text)


if __name__ == "__main__":
    unittest.main()
