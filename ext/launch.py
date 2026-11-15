"""Start upstream garmin-mcp with this repo's extra tools, read-only only.

entrypoint.sh runs this instead of the garmin-mcp script. Upstream's main()
calls courses.register_tools(app) by attribute at startup, so wrapping that
attribute adds our tools to the same app: the tool filter and the effect
annotations apply to them as to upstream's own tools. The Dockerfile calls
patch() and gate() at build time, so an upstream bump that breaks a hook,
removes a helper the status tools import, or reclassifies a listed tool as a
write tool fails the build instead of the running server.

Read-only is enforced here, not by configuration (see "Read-only tool policy"
below): the server registers only read-only tools named in
config/enabled-tools.txt, and no environment variable can add a write tool.

    python launch.py                  run the server (entrypoint.sh's loop)
    python launch.py --check-config   check the tool policy and REPORT_TZ, print
                                      one line per problem, exit 1 on any
                                      (entrypoint.sh, before Caddy starts)

Upstream is written for mcp 1.x; the image runs mcp 2.x for the 2026-07-28
protocol. patch() and main() first call mcp2_compat.install(), which serves
upstream's `from mcp.server.fastmcp import FastMCP` with an adapter over mcp
2.x's MCPServer, so it must run before anything imports garmin_mcp.
"""

from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import course_profile  # noqa: E402
import mcp2_compat  # noqa: E402
import daily_status  # noqa: E402
import status_common  # noqa: E402
import weekly_status  # noqa: E402

# Effect annotation, handed out by upstream's annotations_for() at
# registration. All of this repo's tools are read-only; a tool missing from
# this set would register unannotated (and the read-only policy below would
# refuse to expose it).
EXTRA_READ_ONLY_TOOLS = frozenset({"get_course_profile", "get_daily_status", "get_weekly_status"})

# serverInfo.name for clients. Upstream's main() names its server
# "Garmin Connect v1.0", Garmin's product name; main() overrides it.
SERVER_NAME = "coach-mcp-for-garmin"


# --- Read-only tool policy ---------------------------------------------------
# One source of truth for the tools this server may register:
#
#     ((upstream tool_annotations.READ_ONLY_TOOLS | EXTRA_READ_ONLY_TOOLS)
#         - upstream's write tools) & config/enabled-tools.txt
#
# The file is baked into the image: the Dockerfile copies it to
# /opt/config/enabled-tools.txt, which is ../config/ from
# /opt/garmin-ext/launch.py, the same layout as this repo. Every name in it
# must be a read-only tool, or the build and the start fail.
# GARMIN_ENABLED_TOOLS may only NARROW that list: a name in it that is a write
# tool, an unknown name, or a read-only tool the file leaves out stops the
# server at startup. GARMIN_DISABLED_TOOLS is refused (it used to be the way
# to hide tools; ignoring it silently would expose tools someone meant to hide).
# Two gates apply the result: upstream's own allowlist filter (main() sets
# GARMIN_ENABLED_TOOLS to exactly the allowed set) and gate(), which makes
# upstream's server class refuse any other tool even if upstream's filter
# changes.
ENABLED_TOOLS_FILE = os.path.normpath(os.path.join(HERE, os.pardir, "config", "enabled-tools.txt"))
FILE_LABEL = "config/enabled-tools.txt"


class ConfigError(Exception):
    """Startup configuration problems; .errors holds one line per problem."""

    def __init__(self, errors):
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


def parse_tool_list(text):
    """Tool names from the enabled-tools.txt format: one per line; text after
    '#', whitespace and blank lines are ignored."""
    names = []
    for line in text.splitlines():
        name = "".join(line.split("#", 1)[0].split())
        if name:
            names.append(name)
    return names


def read_enabled_tools_file(path=ENABLED_TOOLS_FILE):
    """The baked tool list, or ConfigError if the file is missing."""
    try:
        with open(path, encoding="utf-8") as f:
            return parse_tool_list(f.read())
    except OSError as exc:
        raise ConfigError([f"cannot read {FILE_LABEL} at {path}: {exc.strerror or exc}"]) from exc


def _why_not(name, read_only, write):
    """Why GARMIN_ENABLED_TOOLS may not name `name`, for the error line."""
    if name in write:
        return "a write tool"
    return f"not in {FILE_LABEL}" if name in read_only else "an unknown tool"


def _shown(name):
    """`name` for an error line: as is, or repr() when it holds a newline or an
    invisible character, so each problem stays on one readable line."""
    return name if name.isprintable() else repr(name)


def resolve_tools(environ, baked, read_only, write=frozenset()):
    """(allowed tool names, error lines) for this environment.

    Pure and standard library only, so the rules are testable without upstream:
    `baked` is the file's names, `read_only` every read-only tool name, `write`
    the known write tools (only used to word the errors). Any error means
    nothing is allowed: the caller must not start.
    """
    errors = []
    baked = set(baked)
    if not baked:
        errors.append(f"{FILE_LABEL} lists no tools")
    for name in sorted(baked - set(read_only)):
        kind = "a write tool" if name in write else "not a known read-only tool"
        errors.append(f"{FILE_LABEL}: {_shown(name)} is {kind}; this server only exposes read-only tools")
    allowed = baked & set(read_only)

    if (environ.get("GARMIN_DISABLED_TOOLS") or "").strip():
        errors.append(
            "GARMIN_DISABLED_TOOLS is no longer supported; delete it, and to hide tools set "
            f"GARMIN_ENABLED_TOOLS to the subset of {FILE_LABEL} to keep"
        )

    # Parsed as upstream parses it (comma-separated, case-insensitive);
    # unset or blank means the whole baked list.
    raw = environ.get("GARMIN_ENABLED_TOOLS") or ""
    if raw.strip():
        requested = {n.strip().lower() for n in raw.split(",") if n.strip()}
        if not requested:
            errors.append("GARMIN_ENABLED_TOOLS names no tools; unset it to expose the whole list")
        for name in sorted(requested - allowed):
            errors.append(
                f"GARMIN_ENABLED_TOOLS: {_shown(name)} is {_why_not(name, read_only, write)}; "
                f"the variable can only narrow the read-only list in {FILE_LABEL}"
            )
        allowed &= requested

    return (frozenset() if errors else frozenset(allowed)), errors


def allowed_tools(environ):
    """The tool names this server may register, or ConfigError. Needs upstream."""
    mcp2_compat.install()
    from garmin_mcp import tool_annotations

    write = tool_annotations.ADDITIVE_TOOLS | tool_annotations.DESTRUCTIVE_TOOLS
    # A name upstream lists as a write tool is never read-only here, not even
    # one of this repo's (or after patch() merged them into READ_ONLY_TOOLS).
    read_only = (tool_annotations.READ_ONLY_TOOLS | EXTRA_READ_ONLY_TOOLS) - write
    allowed, errors = resolve_tools(environ, read_enabled_tools_file(), read_only, write)
    if errors:
        raise ConfigError(errors)
    return allowed


def read_only_server_class(base, allowed):
    """A subclass of upstream's server class that registers only `allowed`.

    MCPServer.tool() registers through add_tool(), so gating add_tool covers
    both. A refused tool is logged and left out (never served), whatever
    upstream's own filter decided.
    """
    allowed = frozenset(allowed)

    class ReadOnlyFastMCP(base):
        def add_tool(self, fn, name=None, *args, **kwargs):
            tool_name = name or getattr(fn, "__name__", None)
            if tool_name not in allowed:
                print(f"launch: refused to register tool {tool_name!r}: not in the read-only allowlist",
                      file=sys.stderr)
                return None
            return super().add_tool(fn, name, *args, **kwargs)

    return ReadOnlyFastMCP


def gate(allowed):
    """Second gate: upstream's main() builds its app from garmin_mcp.FastMCP;
    swap in a subclass that refuses every tool outside `allowed`. Raises if
    the hook moved, so an upstream bump fails the build (Dockerfile)."""
    mcp2_compat.install()
    import garmin_mcp

    base = getattr(garmin_mcp, "FastMCP", None)
    if not (isinstance(base, type) and callable(getattr(base, "add_tool", None))):
        raise RuntimeError("launch: garmin_mcp.FastMCP is gone or has no add_tool(); "
                           "the read-only registration gate needs a new hook")
    garmin_mcp.FastMCP = read_only_server_class(base, allowed)


def check_config(environ):
    """(allowed tools, error lines): the tool policy and REPORT_TZ (required)."""
    allowed, errors = frozenset(), []
    try:
        allowed = allowed_tools(environ)
    except ConfigError as exc:
        errors += exc.errors
    errors += status_common.check_report_tz(environ)
    return allowed, errors


def _check_config_cli(environ):
    try:
        allowed, errors = check_config(environ)
    except Exception as exc:  # noqa: BLE001 - one clear line, not a traceback
        errors = [f"the check could not run: {type(exc).__name__}: {exc}"]
    for line in errors:
        print(f"launch: config error: {line}", file=sys.stderr)
    if errors:
        return 1
    print(f"launch: config OK ({len(allowed)} read-only tools)", file=sys.stderr)
    return 0


def patch():
    """Hook this repo's tools into upstream's tool registration.

    Raises, before changing anything in upstream, if a status module's
    selfcheck fails (an upstream helper it imports is gone) or a hook moved,
    if upstream now has a tool named like one of this repo's, or if
    mcp2_compat cannot adapt the installed mcp SDK.
    """
    mcp2_compat.install()
    from garmin_mcp import courses, tool_annotations

    daily_status.selfcheck()
    weekly_status.selfcheck()
    # The SDK keeps the first tool registered under a name: an upstream tool
    # named like one of this repo's, registered before it, would be served in
    # its place, labelled read-only by the READ_ONLY_TOOLS merge below.
    taken = EXTRA_READ_ONLY_TOOLS & tool_annotations.ALL_ANNOTATED_TOOLS
    if taken:
        raise RuntimeError(f"launch: upstream now has a tool named {', '.join(sorted(taken))}; "
                           "rename this repo's tool")

    activity_names = {pk: name for name, pk in courses._ACTIVITY_TYPE_IDS.items()}
    upstream_register = courses.register_tools

    def get_client():
        # Looked up at call time: main() configures courses.garmin_client
        # (upstream's _GarminProxy, the only logged-in client) at startup.
        return courses.garmin_client

    def register_tools(app):
        app = upstream_register(app)
        app = course_profile.register_tools(app, get_client, activity_names)
        daily_status.register_tools(app, get_client)
        weekly_status.register_tools(app, get_client)
        return app

    # annotations_for() reads this module global at registration time.
    tool_annotations.READ_ONLY_TOOLS = tool_annotations.READ_ONLY_TOOLS | EXTRA_READ_ONLY_TOOLS
    courses.register_tools = register_tools


def main(argv=None):
    args = sys.argv[1:] if argv is None else list(argv)
    if args == ["--check-config"]:
        sys.exit(_check_config_cli(os.environ))
    if args:
        sys.exit("usage: launch.py [--check-config]")

    mcp2_compat.install()
    patch()
    try:
        allowed = allowed_tools(os.environ)
    except ConfigError as exc:
        for line in exc.errors:
            print(f"launch: config error: {line}", file=sys.stderr)
        sys.exit(1)
    # First gate: upstream's allowlist filter registers exactly these.
    os.environ["GARMIN_ENABLED_TOOLS"] = ",".join(sorted(allowed))
    # Second gate: the server class itself refuses anything else.
    gate(allowed)
    # Before upstream's main() builds the server (see SERVER_NAME).
    mcp2_compat.set_server_overrides(name=SERVER_NAME)
    from garmin_mcp import main as upstream_main

    upstream_main()


if __name__ == "__main__":
    main()
