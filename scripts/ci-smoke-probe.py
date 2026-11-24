"""Checks a running server from inside its own container (scripts/ci-smoke.sh
uses it for the key-mode run, which has no network, so nothing outside can
reach it). Standard library only. Run it with the container's Python:

    docker exec -i NAME python - --expect-ready 200 \
        --expect-names get_activities,get_stats,... --expect-version 1.0.0 \
        --call get_stats --call-args '{"date": "2026-01-15"}' \
        --expect-error "Garmin Connect did not answer" < scripts/ci-smoke-probe.py

It reads MCP_BEARER_TOKEN from the container's environment and never prints
it. One line per check (PASS or FAIL); exit status 1 on any FAIL.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "ci-smoke-probe", "version": "1"}}}
ACCEPT = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
failed = []


def record(name, ok, detail):
    print(f"{'PASS' if ok else 'FAIL'}  probe/{name}: {detail}", flush=True)
    if not ok:
        failed.append(name)


def http(method, url, body=None, headers=None):
    """(status, headers, text) without raising on 4xx/5xx."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, r.headers, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read().decode("utf-8", "replace")


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


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--base", default="http://127.0.0.1:8080")
    p.add_argument("--expect-ready", type=int, required=True, help="the /readyz status code")
    p.add_argument("--expect-names", required=True, help="the exact tool names, comma-separated")
    p.add_argument("--expect-version", help="serverInfo.version the server must report")
    p.add_argument("--expect-name", default="coach-mcp-for-garmin", help="serverInfo.name")
    p.add_argument("--call", required=True, help="a tool to call once")
    p.add_argument("--call-args", default="{}")
    p.add_argument("--expect-error", required=True, help="text the tool's isError answer must hold")
    a = p.parse_args()
    token = os.environ.get("MCP_BEARER_TOKEN", "")
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

    auth = dict(ACCEPT, Authorization=f"Bearer {token}")
    code, hdrs, text = http("POST", mcp, INIT, auth)
    result = message(text).get("result") or {}
    version = result.get("protocolVersion")
    record("initialize", code == 200 and bool(version), f"HTTP {code}, protocolVersion {version}")
    info = result.get("serverInfo") or {}
    ok = info.get("name") == a.expect_name and (a.expect_version is None or info.get("version") == a.expect_version)
    record("server-info", ok, f"serverInfo name {info.get('name')!r}, version {info.get('version')!r} "
           f"(want {a.expect_name!r}, {a.expect_version or 'any'!r})")
    follow = dict(auth, **{"Mcp-Protocol-Version": version or "2025-06-18"})
    if hdrs and hdrs.get("Mcp-Session-Id"):
        follow["Mcp-Session-Id"] = hdrs["Mcp-Session-Id"]
    http("POST", mcp, {"jsonrpc": "2.0", "method": "notifications/initialized"}, follow)

    code, _, text = http("POST", mcp, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}, follow)
    tools = (message(text).get("result") or {}).get("tools") or []
    names = {t.get("name") for t in tools}
    want = {n for n in a.expect_names.split(",") if n}
    read_only = all((t.get("annotations") or {}).get("readOnlyHint") is True for t in tools)
    diff = sorted(f"+{n}" for n in names - want) + sorted(f"-{n}" for n in want - names)
    record("tools", code == 200 and names == want and len(tools) == len(want) and read_only,
           f"{len(tools)} tools (want {len(want)}); all readOnlyHint: {'yes' if read_only else 'no'}"
           + (f"; differences: {' '.join(diff)}" if diff else ""))

    if a.call not in names:
        record("call", False, f"{a.call} is not listed")
    else:
        body = {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                "params": {"name": a.call, "arguments": json.loads(a.call_args)}}
        code, _, text = http("POST", mcp, body, follow)
        result = message(text).get("result") or {}
        out = " ".join(c.get("text", "") for c in result.get("content") or [] if c.get("type") == "text")
        ok = code == 200 and result.get("isError") is True and a.expect_error in out and "Traceback" not in out
        record("call", ok, f"{a.call}: isError {result.get('isError')}; {out[:120]!r}")

    print(f"probe: {'FAIL' if failed else 'PASS'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
