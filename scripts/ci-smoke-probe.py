"""Checks a running server from inside its own network (scripts/ci-smoke.sh
uses it for the key-mode run, which has no network, and for the all-switches-on
runs, which share only a private network with their Postgres, so nothing
outside can reach them). Standard library only. Run it with the container's
Python:

    docker exec -i NAME python - --expect-ready 200 \
        --expect-names get_activities,get_stats,... --expect-version 1.0.0 \
        --call get_stats --call-args '{"date": "2026-01-15"}' \
        --expect-error "Garmin Connect did not answer" < scripts/ci-smoke-probe.py

It reads MCP_BEARER_TOKEN from the environment and never prints it. One line
per check (PASS or FAIL); exit status 1 on any FAIL.

Every listed tool must be annotated readOnlyHint=true. With --write-tools
NAMES (a server with PLAN_WRITES=on or PLAN_TRACKING=on) those names must be
listed as well (add them to --expect-names) with readOnlyHint=false,
idempotentHint=true, the destructiveHint of their kind and the openWorldHint
of their reach (log_feedback, plan_activate false: only the app's database; the
Garmin writers true); every other tool stays readOnlyHint=true. A write tool
is never called, except the one --refused-call names (below). Without these
flags the run is unchanged.

The all-switches-on runs add (each optional):
  --instructions FILE  the handshake's instructions must equal FILE's text
  --refused-call NAME --refused-args JSON --expect-refusal TEXT
                       call one write tool with the static bearer, which never
                       carries the plan:write scope: the answer must hold TEXT
                       (the scope refusal). The arguments name no plan the
                       server has, so nothing could be written even past it.
  --oauth yes|no       sign in as Claude does (register, authorize, the consent
                       page with OWNER_SECRET from the environment, token): the
                       consent page must offer the calendar box (yes) or not
                       (no); with yes the box is ticked and the grant must
                       carry plan:write, with no it must not. tools/list with
                       the OAuth token must list the same tools with the same
                       hints, and with --refused-call the same call must get
                       past the scope check and be refused for its token
                       instead (--oauth-refusal TEXT). OWNER_SECRET, codes and
                       tokens are never printed.
"""

import argparse
import base64
import hashlib
import json
import os
import secrets
import sys
import urllib.error
import urllib.parse
import urllib.request

INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "ci-smoke-probe", "version": "1"}}}
ACCEPT = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
failed = []
# The own write tools (launch.OWN_WRITE_TOOLS) and the destructiveHint each carries; all are idempotent.
WRITE_TOOL_DESTRUCTIVE = {"plan_commit": False, "log_feedback": False, "plan_activate": False, "plan_cancel": True,
                          "plan_apply_changes": True}
# The openWorldHint each carries: log_feedback and plan_activate are in upstream's LOCAL_ONLY_TOOLS
# (launch.patch()); the others write Garmin.
WRITE_TOOL_OPEN_WORLD = {"plan_commit": True, "log_feedback": False, "plan_activate": False, "plan_cancel": True,
                         "plan_apply_changes": True}
# The consent page's calendar box (owner_auth._form_page), offered only with PLAN_WRITES=on.
WRITE_BOX_TEXT = "Also allow this app to change my Garmin calendar"
WRITE_FIELD = "plan_write"        # owner_auth.WRITE_FIELD
WRITE_SCOPE = "plan:write"        # plan_confirm.WRITE_SCOPE
CALLBACK = "https://claude.ai/api/mcp/auth_callback"   # auth_config.CLAUDE_CALLBACKS[0]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def record(name, ok, detail):
    print(f"{'PASS' if ok else 'FAIL'}  probe/{name}: {detail}", flush=True)
    if not ok:
        failed.append(name)


def _send(req):
    """(status, headers, text) without raising on 3xx/4xx/5xx; redirects are never followed."""
    try:
        with _OPENER.open(req, timeout=60) as r:
            return r.status, r.headers, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read().decode("utf-8", "replace")


def http(method, url, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    return _send(urllib.request.Request(url, data=data, method=method, headers=headers or {}))


def form(url, fields, headers=None):
    """POST an HTML form."""
    data = urllib.parse.urlencode(fields).encode("ascii")
    sent = dict(headers or {}, **{"Content-Type": "application/x-www-form-urlencoded"})
    return _send(urllib.request.Request(url, data=data, method="POST", headers=sent))


def message(text):
    """The JSON-RPC result or error in a JSON or SSE body."""
    for line in reversed(text.splitlines()):
        if line.startswith("data:"):
            text = line[5:].strip()
            break
    try:
        msg = json.loads(text)
    except ValueError:
        return {}
    return msg if isinstance(msg, dict) else {}


def write_hints(tool):
    """None when the tools/list entry carries the hints of its write kind, else what is wrong."""
    ann = tool.get("annotations") or {}
    want = WRITE_TOOL_DESTRUCTIVE.get(tool.get("name"))
    wrong = []
    if ann.get("readOnlyHint") is not False:
        wrong.append(f"readOnlyHint {ann.get('readOnlyHint')} (want false)")
    if want is not None and ann.get("destructiveHint") is not want:
        wrong.append(f"destructiveHint {ann.get('destructiveHint')} (want {str(want).lower()})")
    if ann.get("idempotentHint") is not True:
        wrong.append(f"idempotentHint {ann.get('idempotentHint')} (want true)")
    world = WRITE_TOOL_OPEN_WORLD.get(tool.get("name"))
    if world is not None and ann.get("openWorldHint") is not world:
        wrong.append(f"openWorldHint {ann.get('openWorldHint')} (want {str(world).lower()})")
    return "; ".join(wrong) or None


def split_names(text):
    return sorted({n.strip() for n in (text or "").split(",") if n.strip()})


def text_of(result):
    return " ".join(c.get("text", "") for c in result.get("content") or [] if c.get("type") == "text")


class Session:
    """One MCP session (initialize, initialized) with a bearer token."""

    def __init__(self, mcp, token):
        self.mcp = mcp
        auth = dict(ACCEPT, Authorization=f"Bearer {token}")
        self.code, hdrs, text = http("POST", mcp, INIT, auth)
        self.result = message(text).get("result") or {}
        version = self.result.get("protocolVersion")
        self.headers = dict(auth, **{"Mcp-Protocol-Version": version or "2025-06-18"})
        if hdrs and hdrs.get("Mcp-Session-Id"):
            self.headers["Mcp-Session-Id"] = hdrs["Mcp-Session-Id"]
        http("POST", mcp, {"jsonrpc": "2.0", "method": "notifications/initialized"}, self.headers)

    def rpc(self, req_id, method, params):
        code, _, text = http("POST", self.mcp, {"jsonrpc": "2.0", "id": req_id, "method": method,
                                                "params": params}, self.headers)
        return code, message(text).get("result") or {}

    def tools(self):
        code, result = self.rpc(2, "tools/list", {})
        return code, result.get("tools") or []

    def call(self, name, arguments):
        return self.rpc(3, "tools/call", {"name": name, "arguments": arguments})


def check_tools(label, code, tools, want, writes):
    """The tools check (exact names, read-only hints) and, with writes, the write-tools check; returns
    {name: annotations} for comparing two listings."""
    names = {t.get("name") for t in tools}
    read_only = all((t.get("annotations") or {}).get("readOnlyHint") is True for t in tools
                    if t.get("name") not in writes)
    diff = sorted(f"+{n}" for n in names - want) + sorted(f"-{n}" for n in want - names)
    kind = "all readOnlyHint" if not writes else "all but the write tools readOnlyHint"
    record(label, code == 200 and names == want and len(tools) == len(want) and read_only,
           f"{len(tools)} tools (want {len(want)}); {kind}: {'yes' if read_only else 'no'}"
           + (f"; differences: {' '.join(diff)}" if diff else ""))
    if writes:
        by_name = {t.get("name"): t for t in tools}
        missing = [n for n in writes if n not in by_name]
        wrong = [f"{n}: {w}" for n in writes if n in by_name for w in [write_hints(by_name[n])] if w]
        detail = "; ".join([f"{len(writes)} write tool(s): {', '.join(writes)}"]
                           + ([f"not listed: {', '.join(missing)}"] if missing else []) + wrong)
        record(label.replace("tools", "write-tools"), not missing and not wrong,
               detail if missing or wrong else detail + "; listed, readOnlyHint=false, hints of their kind")
    return {t.get("name"): t.get("annotations") or {} for t in tools}


def refused_call(label, session, name, args, want):
    code, result = session.call(name, args)
    out = text_of(result)
    ok = code == 200 and want in out and "Traceback" not in out
    record(label, ok, f"{name}: HTTP {code}; isError {result.get('isError')}; holds {want!r}: "
           f"{'yes' if want in out else 'no'}; {out[:100]!r}")


def oauth_token(base, owner_secret, tick):
    """Sign in as Claude does; (access token or None, granted scopes, consent page text). Records each step."""
    mcp = base + "/mcp"
    code, _, text = http("POST", base + "/register", {
        "client_name": "ci-smoke-probe", "redirect_uris": [CALLBACK], "grant_types": ["authorization_code",
                                                                                       "refresh_token"],
        "response_types": ["code"], "token_endpoint_auth_method": "client_secret_post"},
        {"Content-Type": "application/json"})
    client = message(text) if code in (200, 201) else {}
    if not client.get("client_id"):
        record("oauth/register", False, f"POST /register {code}")
        return None, "", ""
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    state = secrets.token_urlsafe(16)
    query = urllib.parse.urlencode({"response_type": "code", "client_id": client["client_id"],
                                    "redirect_uri": CALLBACK, "code_challenge": challenge,
                                    "code_challenge_method": "S256", "state": state, "scope": "offline_access",
                                    "resource": mcp})
    code, hdrs, _ = http("GET", f"{base}/authorize?{query}")
    location = (hdrs.get("Location") if hdrs else "") or ""
    req = (urllib.parse.parse_qs(urllib.parse.urlsplit(location).query).get("req") or [""])[0]
    if code != 302 or not req:
        record("oauth/authorize", False, f"GET /authorize {code}; consent page: {'yes' if req else 'no'}")
        return None, "", ""
    code, _, page = http("GET", f"{base}/consent?req={urllib.parse.quote(req)}")
    if code != 200:
        record("oauth/consent-page", False, f"GET /consent {code}")
        return None, "", ""
    fields = {"req": req, "username": "owner", "secret": owner_secret, "action": "approve"}
    if tick:
        fields[WRITE_FIELD] = "yes"
    code, hdrs, _ = form(base + "/consent", fields, {"Sec-Fetch-Site": "same-origin"})
    location = (hdrs.get("Location") if hdrs else "") or ""
    back = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(location).query).items()}
    if code not in (302, 303) or not back.get("code") or back.get("state") != state:
        record("oauth/consent", False, f"POST /consent {code}; code: {'yes' if back.get('code') else 'no'}")
        return None, "", page
    code, _, text = form(base + "/token", {
        "grant_type": "authorization_code", "code": back["code"], "redirect_uri": CALLBACK,
        "client_id": client["client_id"], "client_secret": client.get("client_secret") or "",
        "code_verifier": verifier, "resource": mcp})
    answer = message(text)
    if code != 200 or not answer.get("access_token"):
        record("oauth/token", False, f"POST /token {code}")
        return None, "", page
    return answer["access_token"], answer.get("scope") or "", page


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--base", default="http://127.0.0.1:8080")
    p.add_argument("--expect-ready", type=int, required=True, help="the /readyz status code")
    p.add_argument("--expect-names", required=True, help="the exact tool names, comma-separated")
    p.add_argument("--expect-version", help="serverInfo.version the server must report")
    p.add_argument("--expect-name", default="coach-mcp-for-garmin", help="serverInfo.name")
    p.add_argument("--call", required=True, help="a tool to call once")
    p.add_argument("--call-args", default="{}")
    p.add_argument("--expect-error", required=True, help="text the tool's isError answer must hold")
    p.add_argument("--write-tools", default="", metavar="NAMES",
                   help="comma-separated write tools that must be listed as writes (never called)")
    p.add_argument("--instructions", metavar="FILE", help="the exact server instructions")
    p.add_argument("--refused-call", metavar="NAME", help="a write tool the static bearer must be refused")
    p.add_argument("--refused-args", default="{}")
    p.add_argument("--expect-refusal", help="text --refused-call's answer must hold (static bearer)")
    p.add_argument("--oauth", choices=("yes", "no"), help="sign in too; yes: tick the calendar box")
    p.add_argument("--oauth-refusal", help="text --refused-call's answer must hold with the OAuth token")
    a = p.parse_args(argv)
    writes = split_names(a.write_tools)
    unknown = [n for n in writes if n not in WRITE_TOOL_DESTRUCTIVE]
    if unknown:
        p.error(f"--write-tools: not a write tool of this server: {', '.join(unknown)}")
    if a.call in writes:
        p.error("--call cannot name a write tool: the probe never writes")
    if a.refused_call and (a.refused_call not in writes or not a.expect_refusal):
        p.error("--refused-call names one of --write-tools and needs --expect-refusal")
    if a.oauth_refusal and not (a.oauth and a.refused_call):
        p.error("--oauth-refusal needs --oauth and --refused-call")
    instructions = None
    if a.instructions:
        with open(a.instructions, encoding="utf-8") as f:
            instructions = f.read()
    token = os.environ.get("MCP_BEARER_TOKEN", "")
    owner_secret = os.environ.get("OWNER_SECRET", "")
    mcp = a.base + "/mcp"

    code, _, _ = http("GET", a.base + "/healthz")
    record("healthz", code == 200, f"GET /healthz {code}")
    code, _, text = http("GET", a.base + "/readyz")
    record("readyz", code == a.expect_ready, f"GET /readyz {code} (want {a.expect_ready}) {text[:60]!r}")
    code, hdrs, _ = http("POST", mcp, INIT, ACCEPT)
    www = hdrs.get("WWW-Authenticate", "") if hdrs else ""
    record("no-token", code == 401 and "resource_metadata=" in www,
           f"POST /mcp without a token {code}; resource_metadata: {'yes' if 'resource_metadata=' in www else 'no'}")
    if not token:
        record("token", False, "MCP_BEARER_TOKEN is not set in the container")
        return 1
    if a.oauth and not owner_secret:
        record("oauth", False, "OWNER_SECRET is not set in the container")
        return 1

    s = Session(mcp, token)
    version = s.result.get("protocolVersion")
    record("initialize", s.code == 200 and bool(version), f"HTTP {s.code}, protocolVersion {version}")
    info = s.result.get("serverInfo") or {}
    ok = info.get("name") == a.expect_name and (a.expect_version is None or info.get("version") == a.expect_version)
    record("server-info", ok, f"serverInfo name {info.get('name')!r}, version {info.get('version')!r} "
           f"(want {a.expect_name!r}, {a.expect_version or 'any'!r})")
    if instructions is not None:
        got = s.result.get("instructions")
        same = got == instructions
        record("instructions", same, f"{len(got or '')} characters; equal to {a.instructions}: "
               f"{'yes' if same else 'no'}" + ("" if same else f"; starts {(got or '')[:80]!r}"))

    want = {n for n in a.expect_names.split(",") if n}
    code, tools = s.tools()
    listed = check_tools("tools", code, tools, want, writes)
    names = set(listed)

    if a.call not in names:
        record("call", False, f"{a.call} is not listed")
    else:
        code, result = s.call(a.call, json.loads(a.call_args))
        out = text_of(result)
        ok = code == 200 and result.get("isError") is True and a.expect_error in out and "Traceback" not in out
        record("call", ok, f"{a.call}: isError {result.get('isError')}; {out[:120]!r}")
    if a.refused_call:
        refused_call("static-bearer-write", s, a.refused_call, json.loads(a.refused_args), a.expect_refusal)

    if a.oauth:
        tick = a.oauth == "yes"
        access, scopes, page = oauth_token(a.base, owner_secret, tick)
        offered = WRITE_BOX_TEXT in page and f'name="{WRITE_FIELD}"' in page
        if page:
            record("oauth/consent-box", offered == tick,
                   f"the consent page offers {WRITE_BOX_TEXT!r}: {'yes' if offered else 'no'} "
                   f"(want {'yes' if tick else 'no'})")
        if access:
            granted = WRITE_SCOPE in scopes.split()
            record("oauth/grant", granted == tick,
                   f"token issued; scopes {scopes!r}; {WRITE_SCOPE}: {'yes' if granted else 'no'} "
                   f"(want {'yes' if tick else 'no'})")
            o = Session(mcp, access)
            record("oauth/initialize", o.code == 200, f"HTTP {o.code} with the OAuth token")
            if instructions is not None:
                record("oauth/instructions", o.result.get("instructions") == instructions,
                       "the same instructions with the OAuth token")
            code, tools = o.tools()
            got = check_tools("oauth/tools", code, tools, want, writes)
            record("oauth/same-hints", got == listed, "OAuth and static bearer list the same tools with the "
                   f"same annotations: {'yes' if got == listed else 'no'}")
            if a.refused_call and a.oauth_refusal:
                refused_call("oauth-write", o, a.refused_call, json.loads(a.refused_args), a.oauth_refusal)

    print(f"probe: {'FAIL' if failed else 'PASS'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
