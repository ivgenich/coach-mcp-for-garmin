#!/usr/bin/env python3
"""Check the built-in sign-in of a running coach-mcp-for-garmin server.

The server is its own OAuth 2.1 authorization server for Claude: discovery,
client registration, the owner's consent page (it asks for OWNER_SECRET),
tokens, refresh and revocation. It also accepts the optional static bearer
header (MCP_BEARER_TOKEN). This script plays the Claude app's part against it,
including many requests that must be refused. Standard library only, so any
python3 3.10 or newer runs it:

    python3 scripts/test-signin.py --live --base https://your-app.up.railway.app
    read -rs MCP_BEARER_TOKEN && export MCP_BEARER_TOKEN      # optional, for S33
    python3 scripts/test-signin.py --base http://127.0.0.1:8000

--base is the server's origin, exactly as PUBLIC_URL (no path). The script
asks for OWNER_SECRET at a prompt that does not echo; it also takes it from
the environment (scripts/e2e-local.py passes it that way), never as an
argument. MCP_BEARER_TOKEN is optional and comes only from the environment;
S33 needs it.

Every check prints one line: PASS, FAIL or INFO, the check ID (S1-S46, as in
the build contract, plus S47 and S48, added after review), and a short
detail with status codes and yes/no only.
The script never prints a secret, a token, a code, a state or a page link.
Exit status 0 means no FAIL.

  --live            a real deployment: only the smoke subset (S1-S6,
                    S10, S11, S14, S23, S27, S28, S32, S33, and S46 with
                    --run-protocol). It registers three clients, signs in once
                    (twice with --run-protocol) and revokes those sign-ins
                    (S32, S46).
  (default)         a local server you started yourself: every check. It
                    fills the client table and the sign-in rate limits, so
                    restart the server before you run it again.
  --via-caddy       --base is the local Caddy edge in front of the server. Adds
                    the edge checks (S39, S41's Caddy cap, S42, S43) and expects
                    410 for the old token URL (S40).
  --run-protocol    S46: run scripts/test-protocol.py against <base>/mcp with a
                    fresh OAuth access token in its environment (needs mcp
                    2.3.0 in --protocol-python, default: this python). S46
                    always revokes the sign-in it made.
  --protocol-args STR
                    extra arguments for scripts/test-protocol.py in S46, as
                    one shell-quoted string, e.g. "--expect-version 1.0.0
                    --no-preview"
  --tools-file F    tool names S27 expects (default: config/enabled-tools.txt;
                    key mode: config/enabled-tools-key.txt); also sets the
                    default for --expect-tools
  --report F        also write each check's ID, status and detail as JSON to F
                    (mode 0600)
  --secrets-out F   write every secret value this run saw (JSON, mode 0600) for
                    the log grep in scripts/e2e-local.py. Refused with --live.

Every request carries X-Real-IP and X-Client-IP with a documentation address
(203.0.113.n), a new one for each check, so the sign-in rate limits that one
check fills never touch another. Checks run in the contract's order: S1-S35,
then S47 and S48, then S40-S46, then the rate-limit checks S36-S39 last (S38
fills the client table).

  S47  (local only) callers behind one edge hop keep their own rate-limit
       buckets: 21 callers whose POST /consent carries X-Forwarded-For with
       one shared address get no 429; one caller doing it 21 times does.
  S48  (local only) HEAD /authorize and OPTIONS on /register, /token and
       /revoke get 405 with an Allow header; a CORS preflight OPTIONS
       /register gets the SDK's 200 direct and 405 through Caddy (which
       drops Origin), and registers nothing either way.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import html
import http.client
import json
import os
import re
import secrets
import shlex
import ssl
import subprocess
import sys
import time
from collections import Counter
from urllib.parse import parse_qs, urlencode, urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
ENABLED_TOOLS_FILE = os.path.normpath(os.path.join(HERE, os.pardir, "config", "enabled-tools.txt"))
PROTOCOL_SCRIPT = os.path.join(HERE, "test-protocol.py")

CLAUDE_CALLBACK = "https://claude.ai/api/mcp/auth_callback"
JWT_BEARER = "urn:ietf:params:oauth:grant-type:jwt-bearer"
GRANTS = ["authorization_code", "refresh_token"]
SCOPE = "offline_access"
AUTH_METHODS = ["client_secret_post", "client_secret_basic"]
# The consent page's Content-Security-Policy, as a set of directives (F3: no form-action).
CONSENT_CSP = {"default-src 'none'", "style-src 'unsafe-inline'", "base-uri 'none'", "frame-ancestors 'none'"}
CONSENT_HEADERS = {
    "pragma": "no-cache",
    "x-frame-options": "deny",
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
}
LOOPBACK_REGISTERED = "http://localhost:1/callback"
LOOPBACK_USED = "http://localhost:54321/callback"
PROTOCOL_VERSION = "2025-06-18"
INIT_PARAMS = {
    "protocolVersion": PROTOCOL_VERSION,
    "capabilities": {},
    "clientInfo": {"name": "coach-mcp-test-signin", "version": "1"},
}
ANTHROPIC_IP = "160.79.104.9"  # inside 160.79.104.0/21, Anthropic's published egress range
EDGE_HOP = "100.64.0.7"  # S47: stands in for Railway's edge, the last X-Forwarded-For hop
REGISTER_LIMIT, CONSENT_LIMIT, AUTHORIZE_LIMIT, WIDE_REGISTER = 20, 20, 30, 25
BULK_CLIENTS, BULK_PER_IP = 205, 10
TOOL_CALL_BUDGET = 20.0  # seconds, S45
LOGIN_PREFIX = "Error: Garmin login missing or expired."  # S45: the start of step 1.3's fixed login text

ORDER = (
    "S1 S2 S3 S4 S5 S5b S6 S7 S8 S9 S10 S11 S12 S13 S14 S15 S16 S17 S18 S19 S20 S21 S22 S23 S24 S25 S26 S27 "
    "S28 S29 S30 S31 S32 S33 S34 S35 S47 S48 S40 S41 S42 S43 S44 S45 S46 S36 S37 S38 S39"
).split()
LIVE = set("S1 S2 S3 S4 S5 S5b S6 S10 S11 S14 S23 S27 S28 S32 S33".split())
DEFAULT = object()


class NotRun(Exception):
    """A check could not run because an earlier step gave it nothing to use."""


class Broken(Exception):
    """A helper step failed; the text names the step and its status, never a value."""


def yn(flag) -> str:
    return "yes" if flag else "no"


def error_code(value) -> str:
    """An OAuth error code (a short [a-z_] word) as is; anything else as '-'."""
    return value if isinstance(value, str) and re.fullmatch(r"[a-z_]{1,40}", value) else "-"


def tally(statuses) -> str:
    """[400, 400, 429] -> '400x2, 429x1' in first-seen order."""
    counts = Counter(statuses)
    return ", ".join(f"{s}x{counts[s]}" for s in dict.fromkeys(statuses))


def page_text(body: str) -> str:
    """Visible text of an HTML page: tags removed, entities decoded, spaces collapsed."""
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", body)).split())


def tool_names(path: str = ENABLED_TOOLS_FILE):
    """The names in config/enabled-tools.txt (text after '#', whitespace and
    blank lines ignored, as ext/launch.py reads it), or None if it is missing."""
    try:
        with open(path, encoding="utf-8") as f:
            names = {"".join(line.split("#", 1)[0].split()) for line in f}
    except OSError:
        return None
    names.discard("")
    return names


class Resp:
    def __init__(self, status: int, headers, body: bytes):
        self.status = status
        self.headers = headers  # http.client.HTTPMessage: case-insensitive
        self.body = body

    def header(self, name: str) -> str:
        return self.headers.get(name) or ""

    def has(self, name: str) -> bool:
        return self.headers.get(name) is not None

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "replace")

    def json(self):
        try:
            return json.loads(self.body)
        except ValueError:
            return None

    def obj(self) -> dict:
        j = self.json()
        return j if isinstance(j, dict) else {}

    @property
    def error(self) -> str:
        """The OAuth `error` code of a JSON error body, or '-'."""
        return error_code(self.obj().get("error"))

    def rpc(self):
        """The JSON-RPC answer: a JSON body, or the last result/error message of an SSE body."""
        if "text/event-stream" in self.header("content-type"):
            last = None
            for line in self.text.splitlines():
                if line.startswith("data:") and line[5:].strip():
                    try:
                        msg = json.loads(line[5:].strip())
                    except ValueError:
                        continue
                    if isinstance(msg, dict) and ("result" in msg or "error" in msg):
                        last = msg
            return last or {}
        return self.obj()


class Pending:
    """One /authorize request waiting on the consent page."""

    def __init__(self, req: str, state: str, verifier: str, redirect_uri: str, consent_url: str):
        self.req, self.state, self.verifier = req, state, verifier
        self.redirect_uri, self.consent_url = redirect_uri, consent_url


class SignInCheck:
    def __init__(self, args, owner_secret: str, static_bearer):
        self.args = args
        self.base = args.base
        self.mcp_url = self.base + "/mcp"
        parts = urlsplit(self.base)
        self.host = parts.netloc  # what the consent page names (AuthConfig.host)
        self.https = parts.scheme == "https"
        self.owner_secret = owner_secret
        self.static_bearer = static_bearer
        self.results = []
        self.details = []  # (id, status, scrubbed detail) for --report
        self.secrets = {}
        self.credentials = []  # [client_id, client_secret] of every confidential client
        self.edge = []  # (status, Server header?, Via header?) of every response (S43)
        self.next_ip = 1
        self.keep("owner_secret", owner_secret)
        self.keep("static_bearer", static_bearer)
        self.prm = None  # S2: the path PRM JSON
        self.client = None  # S5: the claude.ai-like confidential client
        self.loop_client = None  # S6: the Claude Code-like public loopback client
        self.main = None  # S10: the main sign-in's pending request
        self.main_code = None  # S14
        self.main_tokens = None  # S23
        self.rotated = None  # S28: (old refresh token, new token JSON)
        self.live_token = None  # S31: an access token whose grant stays alive
        self.expect_names = tool_names(args.tools_file)
        label = os.path.relpath(os.path.abspath(args.tools_file), os.path.join(HERE, os.pardir))
        self.tools_label = os.path.basename(label) if label.startswith(os.pardir) else label

    # --- output -----------------------------------------------------------------

    def keep(self, kind: str, value) -> None:
        if isinstance(value, str) and value:
            bucket = self.secrets.setdefault(kind, [])
            if value not in bucket:
                bucket.append(value)

    def scrub(self, text: str) -> str:
        """Defence in depth: any secret value that reached `text` becomes <redacted>."""
        for values in self.secrets.values():
            for v in values:
                if len(v) >= 8 and v in text:
                    text = text.replace(v, "<redacted>")
        return text

    def say(self, status: str, cid: str, detail: str) -> None:
        detail = self.scrub(detail)
        self.results.append((cid, status))
        self.details.append((cid, status, detail))
        print(f"{status} {cid} {detail}", flush=True)

    def ip(self) -> str:
        if self.next_ip > 254:
            raise RuntimeError("out of 203.0.113.x addresses")
        n, self.next_ip = self.next_ip, self.next_ip + 1
        return f"203.0.113.{n}"

    # --- HTTP -------------------------------------------------------------------

    def http(self, method, target, *, ip, headers=None, body=None, form=None, json_body=None,
             client_ip_header=None, timeout=None) -> Resp:
        """One request on a fresh connection; redirects are never followed."""
        if target.startswith(self.base + "/"):
            target = target[len(self.base):]
        if not target.startswith("/"):
            raise Broken(f"refusing a request outside --base ({method})")
        hdrs = {"User-Agent": "coach-mcp-test-signin/1", "X-Real-IP": ip, "X-Client-IP": client_ip_header or ip}
        if json_body is not None:
            body = json.dumps(json_body).encode("utf-8")
            hdrs["Content-Type"] = "application/json"
        elif form is not None:
            body = urlencode(form).encode("ascii")
            hdrs["Content-Type"] = "application/x-www-form-urlencoded"
        hdrs.update(headers or {})
        parts = urlsplit(self.base)
        port = parts.port or (443 if self.https else 80)
        wait = timeout or self.args.timeout
        if self.https:
            conn = http.client.HTTPSConnection(parts.hostname, port, timeout=wait, context=ssl.create_default_context())
        else:
            conn = http.client.HTTPConnection(parts.hostname, port, timeout=wait)
        try:
            conn.putrequest(method, target, skip_accept_encoding=True)
            for k, v in hdrs.items():
                conn.putheader(k, v)
            if body is not None:
                conn.putheader("Content-Length", str(len(body)))
            conn.endheaders()
            if body:
                try:
                    conn.send(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # an early answer (413) closes the connection; read it below
            resp = conn.getresponse()
            try:
                data = resp.read()
            except (http.client.IncompleteRead, ConnectionResetError) as e:
                # An early 413 closes with the request body unread; a proxy in
                # front (Caddy) may then cut the response body. The status line
                # arrived, so keep it; any other status still fails.
                if resp.status != 413:
                    raise
                data = e.partial if isinstance(e, http.client.IncompleteRead) else b""
            r = Resp(resp.status, resp.headers, data)
        finally:
            conn.close()
        self.edge.append((r.status, r.has("server"), r.has("via")))
        return r

    def mcp(self, token, method, params, *, ip, req_id=1, path="/mcp", session=None, timeout=None) -> Resp:
        msg = {"jsonrpc": "2.0", "method": method}
        if req_id is not None:
            msg["id"] = req_id
        if params is not None:
            msg["params"] = params
        headers = {"Accept": "application/json, text/event-stream"}
        if method != "initialize":
            headers["Mcp-Protocol-Version"] = PROTOCOL_VERSION
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if session:
            headers["Mcp-Session-Id"] = session
        return self.http("POST", path, ip=ip, headers=headers, json_body=msg, timeout=timeout)

    def mcp_session(self, token, ip):
        """initialize, notifications/initialized, tools/list. Returns the three responses."""
        init = self.mcp(token, "initialize", INIT_PARAMS, ip=ip)
        sid = init.header("mcp-session-id") or None  # only if the server runs stateful
        note = self.mcp(token, "notifications/initialized", None, ip=ip, req_id=None, session=sid)
        tl = self.mcp(token, "tools/list", {}, ip=ip, req_id=2, session=sid)
        return init, note, tl

    def initialize_ok(self, token, ip, path="/mcp") -> Resp:
        return self.mcp(token, "initialize", INIT_PARAMS, ip=ip, path=path)

    @staticmethod
    def rpc_ok(r: Resp) -> bool:
        return r.status == 200 and "result" in r.rpc()

    # --- OAuth helpers --------------------------------------------------------------

    def pkce(self):
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        self.keep("code_verifier", verifier)
        self.keep("code_challenge", challenge)
        return verifier, challenge

    def new_state(self) -> str:
        state = secrets.token_urlsafe(16)
        self.keep("state", state)
        return state

    def register(self, body: dict, ip: str):
        r = self.http("POST", "/register", ip=ip, json_body=body)
        j = r.obj()
        if j.get("client_secret"):
            self.keep("client_secret", j["client_secret"])
            if j.get("client_id"):
                self.credentials.append([j["client_id"], j["client_secret"]])
        return r, j

    @staticmethod
    def dcr_body(redirect_uris, method="client_secret_post", name="test-signin", **extra) -> dict:
        body = {"client_name": name, "redirect_uris": list(redirect_uris), "grant_types": list(GRANTS),
                "response_types": ["code"]}
        if method is not None:
            body["token_endpoint_auth_method"] = method
        body.update(extra)
        return body

    def authorize(self, client_id, redirect_uri, challenge, state, *, ip, resource=DEFAULT, scope=SCOPE,
                  method="S256", omit_challenge=False) -> Resp:
        params = [("response_type", "code"), ("client_id", client_id), ("redirect_uri", redirect_uri)]
        if not omit_challenge:
            params.append(("code_challenge", challenge))
        params.append(("code_challenge_method", method))
        if state is not None:
            params.append(("state", state))
        if scope:
            params.append(("scope", scope))
        resource = self.mcp_url if resource is DEFAULT else resource
        if resource is not None:
            params.append(("resource", resource))
        return self.http("GET", "/authorize?" + urlencode(params), ip=ip)

    def consent_req(self, location: str):
        """The pending id when `location` is this server's consent page, else None."""
        prefix = self.base + "/consent?req="
        if not location.startswith(prefix):
            return None
        req = (parse_qs(urlsplit(location).query).get("req") or [None])[0]
        self.keep("pending_id", req)
        return req

    def consent_post(self, req, *, ip, secret, action="approve", site="same-origin", client_ip_header=None,
                     headers=None) -> Resp:
        form = {"req": req, "username": "owner", "secret": secret}
        if action is not None:
            form["action"] = action
        sent = {"Sec-Fetch-Site": site} if site else {}
        sent.update(headers or {})
        return self.http("POST", "/consent", ip=ip, form=form, headers=sent, client_ip_header=client_ip_header)

    def wrong_secret(self) -> str:
        value = "wrong-" + secrets.token_urlsafe(24)
        self.keep("wrong_secret", value)
        return value

    def start(self, client: dict, redirect_uri: str, ip: str, scope=SCOPE) -> Pending:
        verifier, challenge = self.pkce()
        state = self.new_state()
        r = self.authorize(client["client_id"], redirect_uri, challenge, state, ip=ip, scope=scope)
        loc = r.header("location")
        req = self.consent_req(loc)
        if r.status != 302 or not req:
            raise Broken(f"authorize gave HTTP {r.status} ({r.error}), not the consent page")
        return Pending(req, state, verifier, redirect_uri, loc)

    @staticmethod
    def query(location: str) -> dict:
        return {k: v[0] for k, v in parse_qs(urlsplit(location).query, keep_blank_values=True).items()}

    def approve(self, p: Pending, ip: str) -> str:
        r = self.consent_post(p.req, ip=ip, secret=self.owner_secret)
        loc = r.header("location")
        q = self.query(loc)
        self.keep("code", q.get("code"))
        if r.status not in (302, 303) or not loc.startswith(p.redirect_uri + "?") or not q.get("code") \
                or q.get("state") != p.state:
            raise Broken(f"consent approve gave HTTP {r.status}")
        return q["code"]

    def token(self, data: dict, ip: str) -> Resp:
        r = self.http("POST", "/token", ip=ip, form=data)
        j = r.obj()
        self.keep("access_token", j.get("access_token"))
        self.keep("refresh_token", j.get("refresh_token"))
        return r

    def exchange(self, client: dict, p: Pending, code: str, ip: str, *, verifier=None, client_secret=DEFAULT) -> Resp:
        data = {"grant_type": "authorization_code", "code": code, "redirect_uri": p.redirect_uri,
                "client_id": client["client_id"], "code_verifier": verifier or p.verifier, "resource": self.mcp_url}
        secret = client.get("client_secret") if client_secret is DEFAULT else client_secret
        if secret:
            data["client_secret"] = secret
        return self.token(data, ip)

    def refresh(self, client: dict, refresh_token: str, ip: str, scope=None) -> Resp:
        data = {"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": client["client_id"]}
        if client.get("client_secret"):
            data["client_secret"] = client["client_secret"]
        if scope:
            data["scope"] = scope
        return self.token(data, ip)

    def sign_in(self, ip: str, client=None, redirect_uri=CLAUDE_CALLBACK, scope=SCOPE) -> dict:
        """authorize -> consent -> code -> token for the S5 client; the token JSON."""
        client = client or self.need(self.client, "the S5 client")
        p = self.start(client, redirect_uri, ip, scope)
        code = self.approve(p, ip)
        r = self.exchange(client, p, code, ip)
        if r.status != 200 or not r.obj().get("access_token"):
            raise Broken(f"token exchange gave HTTP {r.status} ({r.error})")
        return r.obj()

    def fresh_code(self, ip: str):
        client = self.need(self.client, "the S5 client")
        p = self.start(client, CLAUDE_CALLBACK, ip)
        return client, p, self.approve(p, ip)

    @staticmethod
    def need(value, what: str):
        if not value:
            raise NotRun(f"needs {what}")
        return value

    def mcp_status(self, token: str, ip: str) -> int:
        return self.initialize_ok(token, ip).status

    # --- the checks: each returns (True|False|None, detail); None prints INFO -----------------

    def s1(self):
        r = self.mcp(None, "initialize", INIT_PARAMS, ip=self.ip())
        www = r.header("www-authenticate")
        want = f'resource_metadata="{self.base}/.well-known/oauth-protected-resource/mcp"'
        return r.status == 401 and want in www, f"HTTP {r.status}; WWW-Authenticate has resource_metadata: {yn(want in www)}"

    def s2(self):
        r = self.http("GET", "/.well-known/oauth-protected-resource/mcp", ip=self.ip())
        j = r.obj()
        res_ok = j.get("resource") == self.mcp_url
        as_ok = j.get("authorization_servers") == [self.base]
        if r.status == 200:
            self.prm = j
        return (r.status == 200 and res_ok and as_ok,
                f"HTTP {r.status}; resource is <base>/mcp: {yn(res_ok)}; authorization_servers is [<base>]: {yn(as_ok)}")

    def s3(self):
        r = self.http("GET", "/.well-known/oauth-protected-resource", ip=self.ip())
        same = self.prm is not None and r.obj() == self.prm
        return r.status == 200 and same, f"HTTP {r.status}; same JSON as S2: {yn(same)}"

    def s4(self):
        r = self.http("GET", "/.well-known/oauth-authorization-server", ip=self.ip())
        j = r.obj()
        servers = (self.prm or {}).get("authorization_servers") or [None]
        endpoints = {k: v for k, v in j.items() if k.endswith("_endpoint")}
        problems = [name for name, ok in (
            ("issuer", j.get("issuer") == self.base and j.get("issuer") == servers[0]),
            ("authorization_endpoint", j.get("authorization_endpoint") == self.base + "/authorize"),
            ("token_endpoint", j.get("token_endpoint") == self.base + "/token"),
            ("registration_endpoint", j.get("registration_endpoint") == self.base + "/register"),
            ("endpoints under base", bool(endpoints) and all(
                isinstance(v, str) and v.startswith(self.base + "/") for v in endpoints.values())),
            ("code_challenge_methods_supported", j.get("code_challenge_methods_supported") == ["S256"]),
            ("scopes_supported", j.get("scopes_supported") == [SCOPE]),
            ("no client_id_metadata_document_supported", "client_id_metadata_document_supported" not in j),
            ("no authorization_response_iss_parameter_supported",
             "authorization_response_iss_parameter_supported" not in j),
            ("token_endpoint_auth_methods_supported", j.get("token_endpoint_auth_methods_supported") == AUTH_METHODS),
        ) if not ok]
        detail = f"HTTP {r.status}; " + ("all 10 items as expected" if not problems else "wrong: " + ", ".join(problems))
        return r.status == 200 and not problems, detail

    def s5(self):
        ip = self.ip()
        body = self.dcr_body([CLAUDE_CALLBACK], name="Claude", scope="claudeai offline_access",
                             grant_types=GRANTS + [JWT_BEARER])
        r, j = self.register(body, ip)
        grants_ok = j.get("grant_types") == GRANTS
        scope_ok = j.get("scope") == SCOPE
        secret_ok = bool(j.get("client_secret"))
        ok = r.status == 201 and grants_ok and scope_ok and secret_ok
        if r.status == 201 and j.get("client_id") and secret_ok:
            self.client = j
        detail = (f"HTTP {r.status} ({r.error}); grant_types narrowed to the two: {yn(grants_ok)}; "
                  f"scope is offline_access: {yn(scope_ok)}; client_secret set: {yn(secret_ok)}")
        if self.client is None:
            # Keep going with a plain registration, so the later checks still run.
            r2, j2 = self.register(self.dcr_body([CLAUDE_CALLBACK], name="Claude"), ip)
            if r2.status == 201 and j2.get("client_id") and j2.get("client_secret"):
                self.client = j2
                detail += "; a plain client (no jwt-bearer, no scope) was registered for the later checks"
        return ok, detail

    def s5b(self):
        r, j = self.register(self.dcr_body([CLAUDE_CALLBACK], method="client_secret_basic"), self.ip())
        method_ok = j.get("token_endpoint_auth_method") == "client_secret_post"
        secret_ok = bool(j.get("client_secret"))
        return (r.status == 201 and method_ok and secret_ok,
                f"HTTP {r.status} ({r.error}); auth method rewritten to client_secret_post: {yn(method_ok)}; "
                f"client_secret set: {yn(secret_ok)}")

    def s6(self):
        r, j = self.register(self.dcr_body([LOOPBACK_REGISTERED], method="none", name="test-signin loopback"),
                             self.ip())
        no_key = "client_secret" not in j
        if r.status == 201 and j.get("client_id"):
            self.loop_client = j
        return r.status == 201 and no_key, f"HTTP {r.status} ({r.error}); no client_secret key: {yn(no_key)}"

    def s7(self):
        ip = self.ip()
        cases = (("https://evil.example/cb", "foreign https"), ("http://example.com/cb", "non-loopback http"),
                 ("javascript:alert(1)", "javascript"), ("cursor://x/cb", "custom scheme"),
                 ("http://localhost:1/callback#frag", "loopback with fragment"))
        bad = []
        for uri, label in cases:
            r, _ = self.register(self.dcr_body([uri]), ip)
            if r.status != 400 or r.error != "invalid_redirect_uri":
                bad.append(f"{label}: HTTP {r.status} ({r.error})")
        n = len(cases) - len(bad)
        return not bad, f"{n}/{len(cases)} refused with 400 invalid_redirect_uri" + ("; " + "; ".join(bad) if bad else "")

    def s8(self):
        body = self.dcr_body(["https://evil.example/cb"], client_name="x" * 70000)
        r, _ = self.register(body, self.ip())
        return r.status == 413, f"HTTP {r.status} for a {len(json.dumps(body)) // 1024} KiB body"

    def s9(self):
        ip = self.ip()
        r1, j1 = self.register(self.dcr_body([CLAUDE_CALLBACK], grant_types=["authorization_code", "client_credentials"]),
                               ip)
        r2, j2 = self.register(self.dcr_body([CLAUDE_CALLBACK], grant_types=["authorization_code"]), ip)
        a = r1.status == 201 and j1.get("grant_types") == GRANTS
        b = r2.status == 201 and j2.get("grant_types") == GRANTS
        return a and b, (f"with client_credentials: HTTP {r1.status} ({r1.error}), narrowed to the two: {yn(a)}; "
                         f"authorization_code alone: HTTP {r2.status} ({r2.error}), both grants: {yn(b)}")

    def s10(self):
        client = self.need(self.client, "the S5 client")
        verifier, challenge = self.pkce()
        state = self.new_state()
        r = self.authorize(client["client_id"], CLAUDE_CALLBACK, challenge, state, ip=self.ip())
        loc = r.header("location")
        req = self.consent_req(loc)
        nostore = "no-store" in r.header("cache-control").lower()
        if r.status == 302 and req:
            self.main = Pending(req, state, verifier, CLAUDE_CALLBACK, loc)
        return (r.status == 302 and bool(req) and nostore,
                f"HTTP {r.status} ({r.error}); to <base>/consent?req=: {yn(req)}; Cache-Control no-store: {yn(nostore)}")

    def consent_page_checks(self, r: Resp, phrases):
        """Problems with a consent page answer (6.3.5): headers, CSP, no script, the given phrases."""
        problems = []
        if "no-store" not in r.header("cache-control").lower():
            problems.append("Cache-Control")
        problems += [name for name, want in CONSENT_HEADERS.items() if r.header(name).strip().lower() != want]
        csp = {d.strip() for d in r.header("content-security-policy").split(";") if d.strip()}
        if csp != CONSENT_CSP:
            extra = sorted(d.split()[0] for d in csp - CONSENT_CSP)
            missing = sorted(d.split()[0] for d in CONSENT_CSP - csp)
            problems.append("CSP" + (f" extra {','.join(extra)}" if extra else "")
                            + (f" missing {','.join(missing)}" if missing else ""))
        if r.has("cross-origin-opener-policy"):
            problems.append("has COOP")
        if r.has("set-cookie"):
            problems.append("sets a cookie")
        if "<script" in r.text.lower():
            problems.append("has <script")
        text = page_text(r.text)
        problems += [f"text '{p}'" for p in phrases if p not in text]
        return problems

    def s11(self):
        p = self.need(self.main, "S10's consent link")
        r = self.http("GET", p.consent_url, ip=self.ip())
        problems = self.consent_page_checks(
            r, (self.host, "(not verified)", "claude.ai", "If someone sent you this link"))
        return (r.status == 200 and not problems,
                f"HTTP {r.status}; " + ("headers, CSP (no form-action) and page text as expected" if not problems
                                        else "wrong: " + "; ".join(problems)))

    def s12(self):
        ip = self.ip()
        p = self.start(self.need(self.client, "the S5 client"), CLAUDE_CALLBACK, ip)
        statuses, last = [], None
        for _ in range(3):
            last = self.consent_post(p.req, ip=ip, secret=self.wrong_secret())
            statuses.append(last.status)
        loc = last.header("location")
        q = self.query(loc)
        denied = loc.startswith(CLAUDE_CALLBACK + "?") and q.get("error") == "access_denied"
        state_ok = q.get("state") == p.state and "code" not in q
        after = self.http("GET", p.consent_url, ip=ip)
        ok = statuses == [403, 403, 303] and denied and state_ok and after.status == 400
        return ok, (f"wrong secret x3: HTTP {', '.join(map(str, statuses))}; third redirects with "
                    f"error=access_denied: {yn(denied)}, state matches: {yn(state_ok)}; GET afterwards HTTP {after.status}")

    def s13(self):
        p = self.need(self.main, "S10's consent link")
        ip = self.ip()
        r = self.consent_post(p.req, ip=ip, secret=self.owner_secret, site="cross-site")
        again = self.http("GET", p.consent_url, ip=ip)
        return (r.status == 403 and again.status == 200,
                f"cross-site POST HTTP {r.status}; request still usable (GET HTTP {again.status})")

    def s14(self):
        p = self.need(self.main, "S10's consent link")
        r = self.consent_post(p.req, ip=self.ip(), secret=self.owner_secret)
        loc = r.header("location")
        q = self.query(loc)
        self.keep("code", q.get("code"))
        to_cb = loc.startswith(CLAUDE_CALLBACK + "?")
        state_ok = q.get("state") == p.state
        nostore = "no-store" in r.header("cache-control").lower()
        if to_cb and q.get("code"):
            self.main_code = q["code"]
        ok = r.status == 303 and to_cb and bool(q.get("code")) and state_ok and "iss" not in q and nostore
        return ok, (f"HTTP {r.status}; to the claude.ai callback: {yn(to_cb)}; code: {yn(q.get('code'))}; "
                    f"state matches: {yn(state_ok)}; iss: {yn('iss' in q)}; no-store: {yn(nostore)}")

    def s15(self):
        p = self.need(self.main, "S10's consent link")
        r = self.consent_post(p.req, ip=self.ip(), secret=self.owner_secret)
        return r.status == 400, f"re-POST of the used request: HTTP {r.status}"

    def s16(self):
        ip = self.ip()
        p = self.start(self.need(self.client, "the S5 client"), CLAUDE_CALLBACK, ip)
        r = self.consent_post(p.req, ip=ip, secret="", action="deny")
        loc = r.header("location")
        q = self.query(loc)
        denied = loc.startswith(CLAUDE_CALLBACK + "?") and q.get("error") == "access_denied" and "code" not in q
        state_ok = q.get("state") == p.state
        return (r.status == 303 and denied and state_ok,
                f"HTTP {r.status}; error=access_denied: {yn(denied)}; state matches: {yn(state_ok)}")

    def s17(self):
        r = self.http("GET", "/consent?req=unknown-" + secrets.token_hex(8), ip=self.ip())
        return r.status == 400, f"HTTP {r.status}"

    def s18(self):
        client = self.need(self.client, "the S5 client")
        _, challenge = self.pkce()
        state = self.new_state()
        r = self.authorize(client["client_id"], CLAUDE_CALLBACK, challenge, state, ip=self.ip(),
                           resource="https://evil.example/mcp")
        loc = r.header("location")
        q = self.query(loc)
        ok = (r.status == 302 and loc.startswith(CLAUDE_CALLBACK + "?") and q.get("error") == "invalid_target"
              and q.get("state") == state)
        return ok, (f"HTTP {r.status}; error={error_code(q.get('error'))}; "
                    f"state matches: {yn(q.get('state') == state)}")

    def s19(self):
        client = self.need(self.client, "the S5 client")
        ip = self.ip()
        _, challenge = self.pkce()
        cases = (("plain", dict(method="plain")), ("missing", dict(omit_challenge=True)), ("not 43 characters", {}))
        bad = []
        for label, kw in cases:
            state = self.new_state()
            ch = "short-challenge" if label == "not 43 characters" else challenge
            r = self.authorize(client["client_id"], CLAUDE_CALLBACK, ch, state, ip=ip, **kw)
            loc = r.header("location")
            q = self.query(loc)
            if not (r.status == 302 and loc.startswith(CLAUDE_CALLBACK + "?") and q.get("error") == "invalid_request"
                    and "/consent?req=" not in loc):
                bad.append(f"{label}: HTTP {r.status}, to consent: {yn('/consent?req=' in loc)}")
        return not bad, (f"{len(cases) - len(bad)}/{len(cases)} redirected with error=invalid_request, no consent link"
                         + ("; " + "; ".join(bad) if bad else ""))

    def s20(self):
        client = self.need(self.client, "the S5 client")
        _, challenge = self.pkce()
        r = self.authorize(client["client_id"], "https://evil.example/cb", challenge, self.new_state(), ip=self.ip())
        return (r.status == 400 and not r.has("location"),
                f"HTTP {r.status} ({r.error}); Location: {yn(r.has('location'))}")

    def s21(self):
        client = self.need(self.loop_client, "the S6 loopback client")
        ip = self.ip()
        p = self.start(client, LOOPBACK_USED, ip)
        page = self.http("GET", p.consent_url, ip=ip)
        problems = self.consent_page_checks(page, ("a program on this computer", "localhost:54321"))
        r = self.consent_post(p.req, ip=ip, secret=self.owner_secret)
        loc = r.header("location")
        q = self.query(loc)
        self.keep("code", q.get("code"))
        back = r.status == 303 and loc.startswith(LOOPBACK_USED + "?") and bool(q.get("code")) \
            and q.get("state") == p.state
        t = None
        if q.get("code"):  # Claude Code's next step (contract 5.3): the code for tokens, no client secret
            t = self.exchange(client, p, q["code"], ip)
        tok_ok = t is not None and t.status == 200 and bool(t.obj().get("access_token"))
        ok = page.status == 200 and not problems and back and tok_ok
        return ok, (f"registered :1, used :54321: consent page HTTP {page.status}"
                    + ("" if not problems else " (wrong: " + "; ".join(problems) + ")")
                    + f"; approve HTTP {r.status}, back to that callback with code and state: {yn(back)}"
                    + f"; token exchange without a client secret HTTP {t.status if t else '-'}")

    def s22(self):
        client = self.need(self.loop_client, "the S6 loopback client")
        ip = self.ip()
        _, challenge = self.pkce()
        out = []
        for label, uri in (("other path", "http://localhost:54321/other"), ("127.0.0.1 for localhost",
                                                                           "http://127.0.0.1:1/callback")):
            r = self.authorize(client["client_id"], uri, challenge, self.new_state(), ip=ip)
            out.append((label, r.status, r.has("location")))
        ok = all(s == 400 and not has_loc for _, s, has_loc in out)
        return ok, "; ".join(f"{label}: HTTP {s}, Location: {yn(h)}" for label, s, h in out)

    def s23(self):
        client = self.need(self.client, "the S5 client")
        p = self.need(self.main, "S10's sign-in")
        code = self.need(self.main_code, "S14's code")
        r = self.exchange(client, p, code, self.ip())
        j = r.obj()
        nostore = "no-store" in r.header("cache-control").lower()
        ok = (r.status == 200 and j.get("token_type") == "Bearer" and j.get("expires_in") == 3600
              and bool(j.get("refresh_token")) and j.get("scope") == SCOPE and nostore and bool(j.get("access_token")))
        if r.status == 200 and j.get("access_token") and j.get("refresh_token"):
            self.main_tokens = j
        return ok, (f"HTTP {r.status} ({r.error}); token_type Bearer: {yn(j.get('token_type') == 'Bearer')}; "
                    f"expires_in 3600: {yn(j.get('expires_in') == 3600)}; refresh_token: {yn(j.get('refresh_token'))}; "
                    f"scope offline_access: {yn(j.get('scope') == SCOPE)}; no-store: {yn(nostore)}")

    def s24(self):
        ip = self.ip()
        client, p, code = self.fresh_code(ip)
        first = self.exchange(client, p, code, ip)
        at = first.obj().get("access_token")
        second = self.exchange(client, p, code, ip)
        after = self.mcp_status(at, ip) if at else None
        ok = first.status == 200 and second.status == 400 and second.error == "invalid_grant" and after == 401
        return ok, (f"first exchange HTTP {first.status}; replay HTTP {second.status} ({second.error}); "
                    f"that sign-in's access token on /mcp afterwards HTTP {after if after is not None else '-'}")

    def s25(self):
        ip = self.ip()
        client, p, code = self.fresh_code(ip)
        wrong, _ = self.pkce()
        r = self.exchange(client, p, code, ip, verifier=wrong)
        return r.status == 400 and r.error == "invalid_grant", f"HTTP {r.status} ({r.error})"

    def s26(self):
        ip = self.ip()
        client, p, code = self.fresh_code(ip)
        wrong = "wrong-" + secrets.token_hex(32)
        self.keep("wrong_secret", wrong)
        r = self.exchange(client, p, code, ip, client_secret=wrong)
        return r.status == 401 and r.error == "invalid_client", f"HTTP {r.status} ({r.error})"

    def s27(self):
        tokens = self.need(self.main_tokens, "S23's access token")
        init, note, tl = self.mcp_session(tokens["access_token"], self.ip())
        tools = (tl.rpc().get("result") or {}).get("tools") or []
        names = {t.get("name") for t in tools if isinstance(t, dict)}
        count_ok = len(tools) == self.args.expect_tools
        known = self.expect_names is None or names <= self.expect_names
        ok = self.rpc_ok(init) and note.status in (200, 202) and tl.status == 200 and count_ok and known
        return ok, (f"initialize HTTP {init.status}; initialized HTTP {note.status}; tools/list HTTP {tl.status}, "
                    f"{len(tools)} tools (expected {self.args.expect_tools}); every name in {self.tools_label}: "
                    + ("not checked (file missing)" if self.expect_names is None else yn(known)))

    def s28(self):
        client = self.need(self.client, "the S5 client")
        tokens = self.need(self.main_tokens, "S23's tokens")
        ip = self.ip()
        r = self.refresh(client, tokens["refresh_token"], ip)
        j = r.obj()
        new_pair = (bool(j.get("access_token")) and bool(j.get("refresh_token"))
                    and j.get("access_token") != tokens["access_token"]
                    and j.get("refresh_token") != tokens["refresh_token"])
        works = self.mcp_status(j["access_token"], ip) if j.get("access_token") else None
        if r.status == 200 and new_pair:
            self.rotated = (tokens["refresh_token"], j)
        return (r.status == 200 and new_pair and works == 200,
                f"HTTP {r.status} ({r.error}); new access and refresh tokens: {yn(new_pair)}; "
                f"new access token on /mcp HTTP {works if works is not None else '-'}")

    def s29(self):
        client = self.need(self.client, "the S5 client")
        old_rt, new = self.need(self.rotated, "S28's rotation")
        ip = self.ip()
        replays = [self.refresh(client, old_rt, ip) for _ in range(2)]
        same = [r.status == 200 and r.obj().get("access_token") == new["access_token"]
                and r.obj().get("refresh_token") == new["refresh_token"] for r in replays]
        return all(same), ("replaying the previous refresh token twice: "
                           + "; ".join(f"HTTP {r.status} ({r.error}), same pair as S28: {yn(s)}"
                                       for r, s in zip(replays, same)))

    def s30(self):
        client = self.need(self.client, "the S5 client")
        old_rt, new = self.need(self.rotated, "S28's rotation")
        ip = self.ip()
        step = self.refresh(client, new["refresh_token"], ip)
        newest = step.obj()
        replay = self.refresh(client, old_rt, ip)
        at = self.mcp_status(newest["access_token"], ip) if newest.get("access_token") else None
        rt = self.refresh(client, newest["refresh_token"], ip) if newest.get("refresh_token") else None
        ok = (step.status == 200 and replay.status == 400 and replay.error == "invalid_grant" and at == 401
              and rt is not None and rt.status == 400)
        return ok, (f"RT2 to RT3 HTTP {step.status}; then RT1 replay HTTP {replay.status} ({replay.error}); "
                    f"grant revoked: newest access on /mcp HTTP {at if at is not None else '-'}, "
                    f"newest refresh HTTP {rt.status if rt else '-'}")

    def s31(self):
        client = self.need(self.client, "the S5 client")
        ip = self.ip()
        tokens = self.sign_in(ip)
        r = self.refresh(client, tokens["refresh_token"], ip, scope="other")
        self.live_token = tokens["access_token"]
        return r.status == 400 and r.error == "invalid_scope", f"refresh asking for scope=other: HTTP {r.status} ({r.error})"

    def s32(self):
        client = self.need(self.client, "the S5 client")
        ip = self.ip()
        if self.args.live:  # revoke the grant this run made (S23/S28), rather than make another
            tokens = self.need(self.rotated, "S28's tokens")[1]
        else:
            tokens = self.sign_in(ip)
        data = {"token": tokens["refresh_token"], "client_id": client["client_id"],
                "client_secret": client["client_secret"]}
        rv = self.http("POST", "/revoke", ip=ip, form=data)
        at = self.mcp_status(tokens["access_token"], ip)
        rt = self.refresh(client, tokens["refresh_token"], ip)
        return (rv.status == 200 and at == 401 and rt.status == 400,
                f"revoke HTTP {rv.status}; its access token on /mcp HTTP {at}; refresh afterwards HTTP {rt.status}")

    def s33(self):
        if not self.static_bearer:
            return None, "skipped: MCP_BEARER_TOKEN is not set (optional)"
        r = self.initialize_ok(self.static_bearer, self.ip())
        return self.rpc_ok(r), f"initialize with the static bearer in the header: HTTP {r.status}"

    def s34(self):
        wrong = secrets.token_urlsafe(32)
        self.keep("wrong_secret", wrong)
        r = self.initialize_ok(wrong, self.ip())
        rm = "resource_metadata=" in r.header("www-authenticate")
        return r.status == 401 and rm, f"wrong bearer: HTTP {r.status}; resource_metadata: {yn(rm)}"

    def s35(self):
        token = self.need(self.live_token, "S31's access token")
        ip = self.ip()
        control = self.initialize_ok(token, ip)
        r = self.http("POST", "/mcp?" + urlencode({"access_token": token}), ip=ip,
                      headers={"Accept": "application/json, text/event-stream"},
                      json_body={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": INIT_PARAMS})
        return (r.status == 401 and control.status == 200,
                f"token in ?access_token= without the header: HTTP {r.status} (same token in the header: HTTP {control.status})")

    def s47(self):
        # Behind Railway, the last X-Forwarded-For hop is the edge, the same for
        # every caller; it must never decide the rate-limit bucket.
        hop = {"X-Forwarded-For": EDGE_HOP}
        callers = [self.consent_post("junk", ip=self.ip(), secret="x", headers=hop).status
                   for _ in range(CONSENT_LIMIT + 1)]
        ip = self.ip()
        one = [self.consent_post("junk", ip=ip, secret="x", headers=hop).status for _ in range(CONSENT_LIMIT + 1)]
        ok = 429 not in callers and 429 not in one[:-1] and one[-1] == 429
        return ok, (f"{CONSENT_LIMIT + 1} callers, each one POST /consent with X-Forwarded-For {EDGE_HOP}: "
                    f"HTTP {tally(callers)}; one caller {CONSENT_LIMIT + 1} times: HTTP {tally(one[:-1])}, "
                    f"then {one[-1]}")

    def s48(self):
        # Methods no client needs; before the fix they reached the SDK's handlers
        # past the rate limits (HEAD /authorize reads the form like POST).
        client = self.need(self.client, "the S5 client")
        ip = self.ip()
        _, challenge = self.pkce()
        form = {"response_type": "code", "client_id": client["client_id"], "redirect_uri": CLAUDE_CALLBACK,
                "code_challenge": challenge, "code_challenge_method": "S256", "state": self.new_state(),
                "scope": SCOPE, "resource": self.mcp_url}
        tries = [("HEAD", "/authorize", {"form": form}, "GET, POST"),
                 ("OPTIONS", "/register", {"json_body": self.dcr_body([CLAUDE_CALLBACK], name="test-signin S48")},
                  "POST"),
                 ("OPTIONS", "/token", {"form": {"grant_type": "refresh_token", "refresh_token": "junk",
                                                 "client_id": client["client_id"]}}, "POST"),
                 ("OPTIONS", "/revoke", {"form": {"token": "junk", "client_id": client["client_id"]}}, "POST")]
        seen, ok = [], True
        for method, path, body, allow in tries:
            r = self.http(method, path, ip=ip, **body)
            right = r.status == 405 and r.header("allow") == allow and not r.obj().get("client_id")
            ok = ok and right
            seen.append(f"{method} {path}: HTTP {r.status}, Allow {'as expected' if r.header('allow') == allow else 'wrong'}")
        pre = self.http("OPTIONS", "/register", ip=ip,
                        headers={"Origin": "https://app.example.com", "Access-Control-Request-Method": "POST"})
        want = 405 if self.args.via_caddy else 200
        registered = bool(pre.obj().get("client_id"))
        ok = ok and pre.status == want and not registered
        seen.append(f"CORS preflight OPTIONS /register: HTTP {pre.status} (expected {want}"
                    f"{', Caddy drops Origin' if self.args.via_caddy else ''}), client registered: {yn(registered)}")
        return ok, "; ".join(seen)

    def s40(self):
        r = self.http("GET", "/" + secrets.token_hex(32) + "/mcp", ip=self.ip())
        want = 410 if self.args.via_caddy else 404
        return r.status == want, f"GET /<64 hex>/mcp: HTTP {r.status} (expected {want} {'through Caddy' if self.args.via_caddy else 'direct'})"

    def consent_padding(self, size: int, ip: str) -> Resp:
        form = {"req": "x", "username": "owner", "secret": "x", "action": "approve", "pad": "a" * size}
        return self.http("POST", "/consent", ip=ip, form=form, headers={"Sec-Fetch-Site": "same-origin"})

    def s41(self):
        ip = self.ip()
        small = self.consent_padding(10 * 1024, ip)
        if not self.args.via_caddy:
            return small.status == 413, f"10 KB POST /consent direct: HTTP {small.status} (from Python)"
        big = self.consent_padding(20 * 1024, ip)
        return (big.status == 413 and small.status == 413,
                f"20 KB POST /consent: HTTP {big.status} (Caddy's cap); 10 KB: HTTP {small.status} (Python's cap)")

    def s42(self):
        ip = self.ip()
        if self.static_bearer:
            token, which = self.static_bearer, "static bearer"
        else:
            token, which = self.sign_in(ip)["access_token"], "fresh sign-in"
        r = self.initialize_ok(token, ip, path="/mcp/")
        if not self.args.via_caddy:
            return None, f"Caddy only; direct POST /mcp/ gets HTTP {r.status} (Starlette's 307; Caddy rewrites it)"
        return r.status != 307 and self.rpc_ok(r), f"POST /mcp/ with a valid token ({which}): HTTP {r.status}"

    def s43(self):
        if not self.args.via_caddy:
            return None, "Caddy only"
        server = sorted({s for s, has_server, _ in self.edge if has_server})
        via = sorted({s for s, _, has_via in self.edge if has_via})
        return (not server and not via,
                f"{len(self.edge)} responses so far; with Server: {len([e for e in self.edge if e[1]])}"
                + (f" (HTTP {', '.join(map(str, server))})" if server else "")
                + f"; with Via: {len([e for e in self.edge if e[2]])}"
                + (f" (HTTP {', '.join(map(str, via))})" if via else ""))

    def s44(self):
        ip = self.ip()
        h = self.http("GET", "/healthz", ip=ip)
        rz = self.http("GET", "/readyz", ip=ip)
        return h.status == 200 and rz.status == 503, f"/healthz HTTP {h.status}; /readyz HTTP {rz.status} (no Garmin login)"

    def s45(self):
        ip = self.ip()
        token = self.live_token or self.sign_in(ip)["access_token"]
        t0 = time.monotonic()
        r = self.mcp(token, "tools/call", {"name": "get_activities", "arguments": {"limit": 1}}, ip=ip,
                     timeout=TOOL_CALL_BUDGET + 5)
        took = time.monotonic() - t0
        result = r.rpc().get("result") or {}
        content = result.get("content")
        blocks = content if isinstance(content, list) else []
        n_blocks = len(blocks)
        texts = [c.get("text") or "" for c in blocks if isinstance(c, dict)]
        text = "\n".join(texts).strip()
        is_error = result.get("isError") is True
        # Step 1.3's wrapper turns upstream's login text into isError with the fixed login message.
        fixed = text.startswith(LOGIN_PREFIX) and "garmin-mcp-auth" in text and "Error retrieving" not in text
        ok = r.status == 200 and took <= TOOL_CALL_BUDGET and is_error and n_blocks == 1 and fixed
        return ok, (f"get_activities limit=1: HTTP {r.status} in {took:.1f}s (budget {TOOL_CALL_BUDGET:.0f}s); "
                    f"isError: {yn(is_error)}; blocks: {n_blocks} (want 1); fixed login text: {yn(fixed)}")

    def s46(self):
        if not self.args.run_protocol:
            return None, "skipped (no --run-protocol)"
        client = self.need(self.client, "the S5 client")
        ip = self.ip()
        tokens = self.sign_in(ip)
        try:
            ok, detail = self.run_protocol(tokens["access_token"])
        finally:
            revoked = self.revoke_own(client, tokens, ip)  # always, pass or fail
        return ok and revoked, f"{detail}; its sign-in revoked: {yn(revoked)}"

    def protocol_command(self) -> list:
        """S46's test-protocol.py command line: no secret in it (the token goes in the environment)."""
        return ([self.args.protocol_python, PROTOCOL_SCRIPT, "--url", self.mcp_url,
                 "--expect-tools", str(self.args.expect_tools), "--tools-file", self.args.tools_file]
                + shlex.split(self.args.protocol_args or ""))

    def run_protocol(self, token: str):
        env = {k: v for k, v in os.environ.items() if k not in ("OWNER_SECRET", "MCP_BEARER_TOKEN")}
        env["MCP_BEARER_TOKEN"] = token  # the fresh OAuth access token: environment only, never argv
        try:
            proc = subprocess.run(self.protocol_command(), env=env, stdin=subprocess.DEVNULL, capture_output=True,
                                  text=True, timeout=600)
        except subprocess.TimeoutExpired:
            return False, "test-protocol.py did not finish in 600s"
        except OSError as e:
            return False, f"cannot start --protocol-python: {type(e).__name__}"
        lines = (proc.stdout + "\n" + proc.stderr).splitlines()
        for line in lines:
            if line.startswith("FAIL"):
                print(f"INFO S46 test-protocol.py: {self.scrub(line.strip())[:300]}", flush=True)
        summary = next((ln.strip() for ln in reversed(lines) if "checks passed" in ln),
                       next((ln.strip() for ln in reversed(lines) if ln.strip()), "no output"))
        return (proc.returncode == 0,
                f"test-protocol.py --url <base>/mcp with a fresh OAuth access token: exit {proc.returncode}; "
                f"{summary[:200]}")

    def revoke_own(self, client: dict, tokens: dict, ip: str) -> bool:
        """Revoke a sign-in this run made (as S32 does); True when its access token is refused afterwards."""
        try:
            data = {"token": tokens["refresh_token"], "client_id": client["client_id"]}
            if client.get("client_secret"):
                data["client_secret"] = client["client_secret"]
            rv = self.http("POST", "/revoke", ip=ip, form=data)
            return rv.status == 200 and self.mcp_status(tokens["access_token"], ip) == 401
        except (OSError, http.client.HTTPException, KeyError):
            return False

    def s36(self):
        ip, other = self.ip(), self.ip()
        statuses = [self.consent_post("junk-" + secrets.token_hex(4), ip=ip, secret="x").status
                    for _ in range(CONSENT_LIMIT)]
        last = self.consent_post("junk", ip=ip, secret="x")
        retry = last.header("retry-after").strip()
        retry_ok = retry.isdigit() and int(retry) >= 1
        nostore = "no-store" in last.header("cache-control").lower()
        o = self.consent_post("junk", ip=other, secret="x")
        ok = 429 not in statuses and last.status == 429 and retry_ok and nostore and o.status != 429
        return ok, (f"{CONSENT_LIMIT} POSTs: HTTP {tally(statuses)}; #{CONSENT_LIMIT + 1}: HTTP {last.status}, "
                    f"Retry-After: {yn(retry_ok)}, no-store: {yn(nostore)}; another IP: HTTP {o.status}")

    def s37(self):
        ip = self.ip()
        query = "/authorize?" + urlencode({"response_type": "code", "client_id": "unknown-client"})
        statuses = [self.http("GET", query, ip=ip).status for _ in range(AUTHORIZE_LIMIT)]
        last = self.http("GET", query, ip=ip)
        ok = 429 not in statuses and last.status == 429 and last.header("retry-after").strip().isdigit()
        return ok, f"{AUTHORIZE_LIMIT} GETs: HTTP {tally(statuses)}; #{AUTHORIZE_LIMIT + 1}: HTTP {last.status}"

    def s38(self):
        ip = self.ip()
        junk = self.dcr_body(["https://evil.example/cb"])  # refused (400): stores nothing, still counted
        statuses = [self.register(junk, ip)[0].status for _ in range(REGISTER_LIMIT)]
        last = self.register(junk, ip)[0]
        ordinary = 429 not in statuses and last.status == 429
        wide = [self.register(junk, ANTHROPIC_IP)[0].status for _ in range(WIDE_REGISTER)]
        wide_ok = 429 not in wide
        created, ips = [], 0
        while len(created) < BULK_CLIENTS:
            bulk_ip, ips = self.ip(), ips + 1
            for _ in range(min(BULK_PER_IP, BULK_CLIENTS - len(created))):
                body = self.dcr_body([CLAUDE_CALLBACK], name=f"test-signin bulk {len(created) + 1}")
                created.append(self.register(body, bulk_ip)[0].status)
        bulk_ok = all(s == 201 for s in created)
        return ordinary and wide_ok and bulk_ok, (
            f"one ordinary IP: {REGISTER_LIMIT} POSTs HTTP {tally(statuses)}, #{REGISTER_LIMIT + 1} HTTP {last.status}; "
            f"one IP in 160.79.104.0/21: {WIDE_REGISTER} POSTs HTTP {tally(wide)}; "
            f"{BULK_CLIENTS} registrations over {ips} fresh IPs (past the 200-client cap): HTTP {tally(created)}")

    def s39(self):
        if not self.args.via_caddy:
            return None, "Caddy only"
        ip = self.ip()
        statuses = []
        for _ in range(CONSENT_LIMIT + 1):
            spoof = self.ip()  # a different caller-sent X-Client-IP every time
            statuses.append(self.consent_post("junk", ip=ip, secret="x", client_ip_header=spoof).status)
        ok = 429 not in statuses[:-1] and statuses[-1] == 429
        return ok, (f"{CONSENT_LIMIT + 1} POSTs from one X-Real-IP, each with another X-Client-IP: "
                    f"HTTP {tally(statuses[:-1])}, then {statuses[-1]} (the bucket follows X-Real-IP: {yn(ok)})")

    # --- run --------------------------------------------------------------------------

    def run(self) -> int:
        a = self.args
        print(f"INFO setup base {self.base}; {'live smoke subset' if a.live else 'local, every check'}; "
              f"{'through Caddy' if a.via_caddy else 'direct'}; expecting {a.expect_tools} tools; "
              f"static bearer {'set' if self.static_bearer else 'not set'}", flush=True)
        for cid in ORDER:
            if a.live and cid not in live_subset(a):
                continue
            try:
                ok, detail = getattr(self, cid.lower())()
            except NotRun as e:
                ok, detail = False, f"not run: {e}"
            except Broken as e:
                ok, detail = False, str(e)
            except (OSError, http.client.HTTPException) as e:
                ok, detail = False, f"request failed: {type(e).__name__}: {e}"
            except Exception as e:  # noqa: BLE001 - one line per check, never a traceback
                ok, detail = False, f"{type(e).__name__}: {e}"
            self.say("INFO" if ok is None else "PASS" if ok else "FAIL", cid, detail)
        fails = [cid for cid, status in self.results if status == "FAIL"]
        count = Counter(status for _, status in self.results)
        print(f"\n{count['PASS']} PASS, {count['FAIL']} FAIL, {count['INFO']} INFO"
              + (f"; FAILED: {' '.join(fails)}" if fails else ""), flush=True)
        return 1 if fails else 0

    def report(self) -> dict:
        count = Counter(status for _, status in self.results)
        return {"script": "test-signin", "mode": "live" if self.args.live else "local",
                "counts": {"PASS": count["PASS"], "FAIL": count["FAIL"], "INFO": count["INFO"]},
                "results": [{"id": cid, "status": status, "detail": detail} for cid, status, detail in self.details]}

    def write_report(self, path: str) -> None:
        write_private_json(path, self.report())

    def write_secrets(self, path: str) -> None:
        data = dict(self.secrets)
        data["client_credentials"] = self.credentials
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.chmod(path, 0o600)
        kinds = {k: len(v) for k, v in data.items()}
        print(f"INFO secrets-out written (values per kind: {kinds})", flush=True)


def live_subset(args) -> set:
    """The checks --live runs: the smoke subset, plus S46 with --run-protocol."""
    return LIVE | {"S46"} if args.run_protocol else set(LIVE)


def write_private_json(path: str, data) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    os.chmod(path, 0o600)


def normalize_base(p: argparse.ArgumentParser, value: str) -> str:
    parts = urlsplit(value.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.query or parts.fragment \
            or parts.username or parts.password or parts.path not in ("", "/"):
        p.error("--base must be the server's origin, e.g. https://your-app.up.railway.app (no path)")
    return f"{parts.scheme}://{parts.netloc}"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base", required=True, help="the server's origin, as PUBLIC_URL (no path)")
    p.add_argument("--via-caddy", action="store_true", help="--base is the local Caddy edge (adds S39-S43)")
    p.add_argument("--live", action="store_true", help="a real deployment: the smoke subset only")
    p.add_argument("--expect-tools", type=int, default=None,
                   help="tools the server must list (default: the count of names in --tools-file)")
    p.add_argument("--tools-file", default=ENABLED_TOOLS_FILE,
                   help="tool names S27 expects (default: config/enabled-tools.txt)")
    p.add_argument("--secrets-out", help="write the secret values this run saw here (JSON, 0600)")
    p.add_argument("--timeout", type=float, default=30.0, help="seconds per request (default 30)")
    p.add_argument("--run-protocol", action="store_true", help="S46: run scripts/test-protocol.py with a fresh token")
    p.add_argument("--protocol-python", default=sys.executable,
                   help="python with mcp 2.3.0 for --run-protocol (default: this python)")
    p.add_argument("--protocol-args", default="",
                   help='extra arguments for scripts/test-protocol.py in S46, as one shell-quoted string, '
                        'e.g. "--expect-version 1.0.0 --no-preview"')
    p.add_argument("--report", help="also write each check's ID, status and detail as JSON to FILE (mode 0600)")
    return p


def parse_args(argv=None):
    p = build_parser()
    args = p.parse_args(argv)
    args.base = normalize_base(p, args.base)
    if args.live and args.secrets_out:
        p.error("--secrets-out is refused with --live: it would write a real deployment's secrets to disk")
    args.tools_file = os.path.abspath(args.tools_file)
    if args.expect_tools is None:
        names = tool_names(args.tools_file)
        if names is None:
            p.error(f"cannot read {args.tools_file}; pass --expect-tools")
        args.expect_tools = len(names)
    try:
        shlex.split(args.protocol_args or "")
    except ValueError as e:
        p.error(f"--protocol-args is not a shell-quoted string: {e}")
    return p, args


def main() -> int:
    p, args = parse_args()

    owner_secret = os.environ.get("OWNER_SECRET") or ""
    if not owner_secret:
        try:
            owner_secret = getpass.getpass("OWNER_SECRET (not shown): ")
        except (EOFError, KeyboardInterrupt):
            owner_secret = ""
    if not owner_secret:
        p.error("set OWNER_SECRET in the environment, or type it at the prompt")
    static_bearer = os.environ.get("MCP_BEARER_TOKEN") or None

    check = SignInCheck(args, owner_secret, static_bearer)
    try:
        return check.run()
    finally:
        if args.secrets_out:
            check.write_secrets(args.secrets_out)
        if args.report:
            check.write_report(args.report)


if __name__ == "__main__":
    sys.exit(main())
