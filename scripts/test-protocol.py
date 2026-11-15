#!/usr/bin/env python3
"""Check that coach-mcp-for-garmin speaks both MCP protocol eras.

Run with a Python that has mcp==2.3.0 installed (the official SDK is the
reference client):

    MCP_BEARER_TOKEN=... python scripts/test-protocol.py --url https://HOST/mcp
    python scripts/test-protocol.py --url http://127.0.0.1:8102/mcp \
        --host-header your-app.up.railway.app

Checks (each prints PASS/FAIL with a reason; exit status 1 on any FAIL):
  modern      the official client in mode="auto" negotiates 2026-07-28 through
              server/discover (not a fallback to initialize)
  legacy      the official client in mode="legacy" completes the initialize
              handshake at a 2025 version
              (both lines also print the server's name and version, serverInfo)
  old-client  a raw client that opens with initialize (2025-06-18) and no
              discover probe, the way claude.ai used to, gets the tool list
  tools       tools/list in both eras: same names, expected count (default:
              the number of tools in config/enabled-tools.txt), every tool
              annotated readOnlyHint=true
  resources   resources/list in both eras lists the workout:// resources
  raw-http    POST server/discover at 2026-07-28 answers 200 with a
              DiscoverResult; an unsupported version gets JSON-RPC -32022
  tool-call   (with --tool-call) the tool answers with non-error text in both eras

MCP_BEARER_TOKEN (optional for a local server) is sent as a bearer token and is
never printed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any

try:
    import httpx2
    from mcp.client import Client
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared._httpx_utils import create_mcp_http_client
    from mcp_types.version import HANDSHAKE_PROTOCOL_VERSIONS, LATEST_MODERN_VERSION
except ImportError as e:  # pragma: no cover - environment problem
    sys.exit(f"test-protocol: needs mcp==2.3.0 (pip install mcp==2.3.0): {e}")

MODERN = "2026-07-28"
OLD_CLIENT_VERSION = "2025-06-18"
UNSUPPORTED_VERSION = "2099-01-01"
UNSUPPORTED_PROTOCOL_VERSION_CODE = -32022
PV_META = "io.modelcontextprotocol/protocolVersion"
CI_META = "io.modelcontextprotocol/clientInfo"
CC_META = "io.modelcontextprotocol/clientCapabilities"
CLIENT_INFO = {"name": "coach-mcp-for-garmin-test-protocol", "version": "1"}
# The read-only tool list baked into the image; the server exposes exactly
# these unless GARMIN_ENABLED_TOOLS narrows them (then pass --expect-tools).
ENABLED_TOOLS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "config", "enabled-tools.txt")


def enabled_tool_count(path: str = ENABLED_TOOLS_FILE) -> int | None:
    """Distinct names in enabled-tools.txt (text after '#', whitespace and blank
    lines ignored, as ext/launch.py reads it); None if the file is missing."""
    try:
        with open(path, encoding="utf-8") as f:
            names = {"".join(line.split("#", 1)[0].split()) for line in f}
    except OSError:
        return None
    names.discard("")
    return len(names)


# upstream workout_templates.py registers these as static resources.
DEFAULT_RESOURCES = (
    "workout://templates/simple-run",
    "workout://templates/interval-running",
    "workout://templates/tempo-run",
    "workout://templates/strength-circuit",
    "workout://reference/structure",
)

results: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, reason: str) -> None:
    results.append((name, ok, reason))
    print(f"{'PASS' if ok else 'FAIL'}  {name}: {reason}", flush=True)


def redact(text: str, token: str | None) -> str:
    return text.replace(token, "<token>") if token else text


def http_headers(token: str | None, host: str | None) -> dict[str, str]:
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if host:
        headers["Host"] = host
    return headers


def short(e: BaseException, token: str | None) -> str:
    # ExceptionGroups from anyio task groups hide the cause; unwrap one level.
    while isinstance(e, BaseExceptionGroup) and len(e.exceptions) == 1:
        e = e.exceptions[0]
    return redact(f"{type(e).__name__}: {e}", token)[:300]


def server_label(info: Any) -> str:
    """serverInfo as "serverInfo <name> <version>" for the handshake lines."""
    if info is None:
        return "no serverInfo"
    return f"serverInfo {info.name} {info.version}"


# --------------------------------------------------------------------------
# Official client sessions


async def collect(mode: str, args: argparse.Namespace, token: str | None) -> dict[str, Any]:
    """Connect in `mode` and gather what the checks need."""
    out: dict[str, Any] = {}
    http = create_mcp_http_client(headers=http_headers(token, args.host_header))
    transport = streamable_http_client(args.url, http_client=http)
    async with http:
        async with Client(transport, mode=mode, read_timeout_seconds=args.timeout, cache=None) as client:
            session = client.session
            out["version"] = client.protocol_version
            out["server_info"] = client.server_info
            out["via_discover"] = session.discover_result is not None
            out["via_initialize"] = session.initialize_result is not None
            if session.discover_result is not None:
                out["supported"] = list(session.discover_result.supported_versions)

            tools, cursor = [], None
            while True:
                page = await client.list_tools(cursor=cursor)
                tools.extend(page.tools)
                cursor = page.next_cursor
                if not cursor:
                    break
            out["tools"] = tools

            resources, cursor = [], None
            try:
                while True:
                    page = await client.list_resources(cursor=cursor)
                    resources.extend(page.resources)
                    cursor = page.next_cursor
                    if not cursor:
                        break
                out["resources"] = [str(r.uri) for r in resources]
            except Exception as e:  # noqa: BLE001 - reported by the check
                out["resources_error"] = short(e, token)

            if args.tool_call:
                try:
                    res = await client.call_tool(args.tool_call, args.tool_args)
                    texts = [c.text for c in res.content if getattr(c, "type", None) == "text"]
                    out["call"] = {"is_error": bool(res.is_error), "text": "\n".join(texts)}
                except Exception as e:  # noqa: BLE001
                    out["call"] = {"is_error": True, "text": short(e, token)}
    return out


def check_eras(modern: dict | None, legacy: dict | None, args: argparse.Namespace) -> None:
    eras = {k: v for k, v in (("modern", modern), ("legacy", legacy)) if v is not None}

    for era, data in eras.items():
        tools = data["tools"]
        names = sorted(t.name for t in tools)
        not_ro = sorted(
            t.name for t in tools if not (t.annotations is not None and t.annotations.read_only_hint is True)
        )
        ok = len(names) == args.expect_tools and not not_ro
        reason = f"{len(names)} tools (expected {args.expect_tools})"
        if not_ro:
            reason += f"; without readOnlyHint=true: {', '.join(not_ro)}"
        else:
            reason += ", all readOnlyHint=true"
        record(f"tools/{era}", ok, reason)

    if len(eras) == 2:
        m = {t.name for t in modern["tools"]}
        lg = {t.name for t in legacy["tools"]}
        diff = sorted(m ^ lg)
        record("tools/same-set", not diff, "identical name sets" if not diff else f"differ: {', '.join(diff)}")

    want = set(args.expect_resources)
    for era, data in eras.items():
        if "resources_error" in data:
            record(f"resources/{era}", False, f"resources/list failed: {data['resources_error']}")
            continue
        got = set(data["resources"])
        missing = sorted(want - got)
        templates = sorted(u for u in got if u.startswith("workout://templates/"))
        record(
            f"resources/{era}",
            not missing,
            f"{len(got)} resources, {len(templates)} workout://templates/*"
            + (f"; missing: {', '.join(missing)}" if missing else ""),
        )
    if len(eras) == 2 and "resources" in modern and "resources" in legacy:
        diff = sorted(set(modern["resources"]) ^ set(legacy["resources"]))
        record("resources/same-set", not diff, "identical URI sets" if not diff else f"differ: {', '.join(diff)}")

    if args.tool_call:
        for era, data in eras.items():
            call = data["call"]
            text = call["text"].strip()
            # Upstream reports Garmin failures as plain text ("Error retrieving
            # ...: Garmin login failed ...") without isError, so check both.
            text_error = text.lower().startswith("error")
            ok = not call["is_error"] and bool(text) and not text_error
            state = "isError" if call["is_error"] else "error text" if text_error else "ok"
            preview = text.replace("\n", " ")[:120]
            record(f"tool-call/{era}", ok, f"{args.tool_call}: {state}, {len(text)} chars: {preview!r}")


# --------------------------------------------------------------------------
# Raw HTTP


def parse_body(resp: httpx2.Response) -> Any:
    """JSON body, or the last JSON-RPC message of an SSE body."""
    ctype = resp.headers.get("content-type", "")
    if "text/event-stream" in ctype:
        last = None
        for line in resp.text.splitlines():
            if line.startswith("data:"):
                data = line[5:].strip()
                if data:
                    try:
                        msg = json.loads(data)
                    except ValueError:
                        continue
                    if isinstance(msg, dict) and ("result" in msg or "error" in msg):
                        last = msg
        return last
    try:
        return resp.json()
    except ValueError:
        return None


def modern_post(version: str, method: str, req_id: int) -> tuple[dict, dict]:
    body = {
        "jsonrpc": "2.0",
        "id": req_id,
        "method": method,
        "params": {"_meta": {PV_META: version, CI_META: CLIENT_INFO, CC_META: {}}},
    }
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "Mcp-Protocol-Version": version,
        "Mcp-Method": method,
    }
    return body, headers


async def raw_checks(args: argparse.Namespace, token: str | None) -> None:
    base = http_headers(token, args.host_header)
    async with httpx2.AsyncClient(timeout=args.timeout, headers=base) as http:
        # e1: discover at the modern version
        body, headers = modern_post(MODERN, "server/discover", 1)
        try:
            resp = await http.post(args.url, json=body, headers=headers)
            msg = parse_body(resp)
            result = (msg or {}).get("result") if isinstance(msg, dict) else None
            supported = (result or {}).get("supportedVersions") or []
            ok = resp.status_code == 200 and MODERN in supported
            reason = f"HTTP {resp.status_code}, supportedVersions={supported}"
            if not ok and isinstance(msg, dict) and "error" in msg:
                reason += f", error={msg['error']}"
            elif not ok and msg is None:
                reason += f", body={redact(resp.text, token)[:200]!r}"
            record("raw-http/discover-2026-07-28", ok, reason)
        except Exception as e:  # noqa: BLE001
            record("raw-http/discover-2026-07-28", False, short(e, token))

        # e2: an unsupported version must get a structured -32022, not a crash or a 200
        body, headers = modern_post(UNSUPPORTED_VERSION, "server/discover", 2)
        try:
            resp = await http.post(args.url, json=body, headers=headers)
            msg = parse_body(resp)
            err = msg.get("error") if isinstance(msg, dict) else None
            code = (err or {}).get("code")
            supported = ((err or {}).get("data") or {}).get("supported")
            ok = 400 <= resp.status_code < 500 and code == UNSUPPORTED_PROTOCOL_VERSION_CODE and MODERN in (supported or [])
            record(
                "raw-http/unsupported-version",
                ok,
                f"HTTP {resp.status_code}, code={code}, supported={supported}",
            )
        except Exception as e:  # noqa: BLE001
            record("raw-http/unsupported-version", False, short(e, token))

        # g: an old client that opens with initialize and never probes
        name = "old-client/initialize-no-probe"
        try:
            init = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": OLD_CLIENT_VERSION,
                    "capabilities": {},
                    "clientInfo": CLIENT_INFO,
                },
            }
            accept = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
            resp = await http.post(args.url, json=init, headers=accept)  # no Mcp-Protocol-Version header
            msg = parse_body(resp)
            result = msg.get("result") if isinstance(msg, dict) else None
            negotiated = (result or {}).get("protocolVersion")
            if resp.status_code != 200 or negotiated not in HANDSHAKE_PROTOCOL_VERSIONS:
                record(name, False, f"initialize: HTTP {resp.status_code}, protocolVersion={negotiated}, msg={msg}")
            else:
                follow = dict(accept, **{"Mcp-Protocol-Version": negotiated})
                sid = resp.headers.get("mcp-session-id")
                if sid:
                    follow["Mcp-Session-Id"] = sid
                note = await http.post(
                    args.url, json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=follow
                )
                resp = await http.post(
                    args.url, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}, headers=follow
                )
                msg = parse_body(resp)
                tools = ((msg or {}).get("result") or {}).get("tools") if isinstance(msg, dict) else None
                count = len(tools) if isinstance(tools, list) else None
                ok = note.status_code in (200, 202) and resp.status_code == 200 and count == args.expect_tools
                record(
                    name,
                    ok,
                    f"offered {OLD_CLIENT_VERSION}, got {negotiated}; initialized HTTP {note.status_code}; "
                    f"tools/list HTTP {resp.status_code}, {count} tools",
                )
        except Exception as e:  # noqa: BLE001
            record(name, False, short(e, token))


# --------------------------------------------------------------------------


async def run(args: argparse.Namespace) -> None:
    token = os.environ.get("MCP_BEARER_TOKEN") or None

    modern = legacy = None
    try:
        modern = await collect("auto", args, token)
        ok = modern["version"] == MODERN and modern["via_discover"] and not modern["via_initialize"]
        how = "server/discover" if modern["via_discover"] else "initialize fallback"
        record(
            "modern/auto-negotiates-2026-07-28",
            ok,
            f"negotiated {modern['version']} via {how}"
            + (f", server supports {modern['supported']}" if "supported" in modern else "")
            + f"; {server_label(modern['server_info'])}",
        )
    except Exception as e:  # noqa: BLE001
        record("modern/auto-negotiates-2026-07-28", False, short(e, token))

    try:
        legacy = await collect("legacy", args, token)
        ok = legacy["via_initialize"] and legacy["version"] in HANDSHAKE_PROTOCOL_VERSIONS
        record("legacy/initialize-handshake", ok,
               f"negotiated {legacy['version']} via initialize; {server_label(legacy['server_info'])}")
    except Exception as e:  # noqa: BLE001
        record("legacy/initialize-handshake", False, short(e, token))

    check_eras(modern, legacy, args)
    await raw_checks(args, token)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--url", required=True, help="MCP endpoint, e.g. http://127.0.0.1:8102/mcp")
    p.add_argument(
        "--expect-tools",
        type=int,
        default=None,
        help="number of tools the server must list (default: the count in config/enabled-tools.txt)",
    )
    p.add_argument(
        "--expect-resources",
        default=",".join(DEFAULT_RESOURCES),
        help="comma-separated resource URIs that must be listed in both eras",
    )
    p.add_argument("--tool-call", help="tool to call in both eras, e.g. get_daily_status")
    p.add_argument("--tool-args", default="{}", help='JSON arguments for --tool-call, e.g. \'{"limit": 1}\'')
    p.add_argument("--host-header", help="send this Host header (e.g. the Railway hostname to a local server)")
    p.add_argument("--timeout", type=float, default=60.0, help="seconds per request")
    args = p.parse_args()
    if args.expect_tools is None:
        args.expect_tools = enabled_tool_count()
        if args.expect_tools is None:
            p.error(f"cannot read {ENABLED_TOOLS_FILE}; pass --expect-tools")
    args.expect_resources = [u for u in (s.strip() for s in args.expect_resources.split(",")) if u]
    try:
        args.tool_args = json.loads(args.tool_args)
    except ValueError as e:
        p.error(f"--tool-args is not JSON: {e}")
    assert LATEST_MODERN_VERSION == MODERN, f"SDK's latest modern version is {LATEST_MODERN_VERSION}, update MODERN"

    asyncio.run(run(args))
    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed" + (f"; FAILED: {', '.join(failed)}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
