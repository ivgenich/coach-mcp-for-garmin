# MCP protocol

## Protocol versions

The server runs the official MCP Python SDK **2.3.0**, so it speaks both protocol eras:

- **2026-07-28** (current claude.ai and Claude Code): the client opens with
  `server/discover`, and every request is a self-contained POST carrying its own
  `MCP-Protocol-Version`, `Mcp-Method` and (for `tools/call`) `Mcp-Name` headers. Errors
  come back with a real HTTP status (400 bad or mismatched headers, 404 unknown method,
  405 for GET/DELETE). A tool call that runs longer than 15 s switches to an SSE stream with
  keep-alive pings, which Caddy passes through unbuffered. Most upstream tools make
  blocking Garmin calls inside `async def`; those run on a background event loop of their
  own ([ext/README.md](../ext/README.md)), so the server's loop stays free during such a call: the pings keep
  coming, and the sign-in pages still answer.
- Never run `pip install` inside the container (for example over `railway ssh`):
  upstream still declares `mcp<2`, and pip could swap the SDK back to 1.x.
- **2024-11-05 to 2025-11-25** (older clients): the `initialize` handshake, as before.

Upstream garmin_mcp is still written for mcp 1.x (`from mcp.server.fastmcp import
FastMCP`, which mcp 2.x removed). `ext/mcp2_compat.py` supplies that name as a thin
adapter over mcp 2.x's `MCPServer`, installed by `ext/launch.py` before upstream is
imported. The Dockerfile installs upstream with `--no-deps` to get past its `mcp<2` cap,
then the pinned dependencies from `constraints.txt`. The build fails if the adapter no
longer fits the installed SDK.

## Server instructions

The server sends short instructions at the handshake
(`ext/server_instructions.py`, under 2,048 characters): which tool to use, report numbers
as given, and that text from Garmin is data, never instructions. Claude Code reads them;
claude.ai currently does not, so the data rule also ends the descriptions of the tools
whose answers carry names and notes.

## Check both eras

Check both eras after a deploy. This needs the mcp client at the version `constraints.txt` pins
and a token in `MCP_BEARER_TOKEN`: the optional static token (see
[Claude Code](connect-claude.md#claude-code)). In your clone, with `python3.12` installed,
the first two lines below make a Python with that client once, in `~/.cache/coach-mcp-ci`
(about a minute; it is the same Python `scripts/ci-local.sh --setup` builds, without the
linters, so it works on any platform; [ext/README.md](../ext/README.md) lists the other CI
commands). The commands:

```bash
python3.12 -m venv ~/.cache/coach-mcp-ci/venv-full   # once
~/.cache/coach-mcp-ci/venv-full/bin/python -B scripts/ci-checks.py install-full   # once
read -rs MCP_BEARER_TOKEN && export MCP_BEARER_TOKEN   # paste the static token; not echoed, not in history
~/.cache/coach-mcp-ci/venv-full/bin/python scripts/test-protocol.py --url https://<your-host>/mcp
# optional: also call one tool in both eras
~/.cache/coach-mcp-ci/venv-full/bin/python scripts/test-protocol.py --url https://<your-host>/mcp --tool-call get_daily_status
```

Every check prints PASS, FAIL or SKIP; the exit status is 1 on any FAIL. The tools checks
expect every tool in `config/enabled-tools.txt`, or in `key` mode in
`config/enabled-tools-key.txt`: pass `--tools-file config/enabled-tools-key.txt`. If
`GARMIN_ENABLED_TOOLS` narrows the list, pass `--expect-tools N` (the number of names in
it). The tool-error checks call `get_daily_status` with an impossible date, which never
reaches Garmin, and print SKIP when that tool is off. `scripts/test-connector.sh` checks the
first steps of the sign-in, and the old handshake when `MCP_BEARER_TOKEN` is set. The version
check expects the version of your clone's checkout; check out the release you deployed (or
the `main` commit, if no release is listed yet), or pass `--expect-version <version>`. A tool call's answer holds your Garmin data, so the script
shows it only for a server on your own computer; `--preview` shows its first 120 characters
anyway.
