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
    python launch.py --check-config   check the tool policy, GARMIN_AUTH and the
                                      key-mode settings, REPORT_TZ,
                                      EDGE_CLIENT_IP_HEADER and, with the
                                      sign-in on, its settings and AUTH_DB;
                                      print one line per problem, exit 1 on any
                                      (entrypoint.sh, before Caddy starts)

GARMIN_AUTH (required, no default) picks how the server reads Garmin: full
(upstream's garminconnect login, every tool) or key (a Garmin Personal API
key, ext/key_mode.py). In key mode the tools are narrowed to
key_mode.KEY_MODE_TOOLS, the status tools skip the sources the Personal API
does not serve, the server sends server_instructions.INSTRUCTIONS_KEY, and
key_mode.install() replaces upstream's login with the key client. main()
takes the key (and GARMIN_PERSONAL_API_BASE_URL) out of os.environ and
registers the key with tool_errors' redaction before anything logs.

Upstream is written for mcp 1.x; the image runs mcp 2.x for the 2026-07-28
protocol. patch() and main() first call mcp2_compat.install(), which serves
upstream's `from mcp.server.fastmcp import FastMCP` with an adapter over mcp
2.x's MCPServer, so it must run before anything imports garmin_mcp.

The built-in sign-in (single-owner OAuth 2.1) is on whenever
GARMIN_MCP_TRANSPORT is not stdio, which the image always sets. main() reads
its settings (auth_config), opens its database and builds the provider
(owner_auth) before upstream's main() builds the server, and hands them to
that server through mcp2_compat's constructor overrides. The sign-in rate
limit (auth_http) and owner_auth's registration filter wrap the HTTP app
through mcp2_compat's middleware hook, and the courses hook below adds the
consent page and the root protected-resource metadata. owner_auth needs the
mcp SDK, so it is imported only when the sign-in is on.

Every tools/call goes through ReadOnlyFastMCP.call_tool() (see
read_only_server_class()). A failure, raised or answered as "Error ...: ..."
text, becomes one fixed text per group (tool_errors: Garmin login, rate limit,
invalid input, no data, Garmin unavailable, internal error with a ref), with
isError true for every group but "no data", and one redacted line on the
garmin_ext.tools log; the original text never reaches the client. A
successful upstream answer has its free text cleaned in a worker thread
(text_hygiene.clean_tool_answer); this repo's 3 tools clean their own read
points. main() also sends server_instructions.INSTRUCTIONS at the handshake,
add_tool() ends the descriptions of the tools whose answers carry names and
notes with server_instructions.FREE_TEXT_NOTE, and gate() fails the build if
the SDK stops sending tools/call through MCPServer.call_tool().
"""

from __future__ import annotations

import datetime
import inspect
import logging
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import auth_config  # noqa: E402
import auth_http  # noqa: E402
import auth_store  # noqa: E402
import course_profile  # noqa: E402
import mcp2_compat  # noqa: E402
import daily_status  # noqa: E402
import key_mode  # noqa: E402
import server_instructions  # noqa: E402
import status_common  # noqa: E402
import text_hygiene  # noqa: E402
import tool_errors  # noqa: E402
import version  # noqa: E402
import weekly_status  # noqa: E402

log = logging.getLogger("garmin_ext.launch")
# One line per failed tool call (call_tool() below); the adapter's own line
# for a raised exception (mcp2_compat) is DEBUG on the same logger.
tool_log = logging.getLogger("garmin_ext.tools")

# The sign-in main() built (owner_auth.OwnerAuth), or None when it is off; the
# courses hook in patch() reads it when upstream's main() registers the tools.
_AUTH = None
# True when main() runs in key mode (GARMIN_AUTH=key): _failure() then gives
# the key-mode texts of the login and no-data groups (tool_errors.client_text).
_KEY_MODE = False

# Effect annotation, handed out by upstream's annotations_for() at
# registration. All of this repo's tools are read-only; a tool missing from
# this set would register unannotated (and the read-only policy below would
# refuse to expose it).
EXTRA_READ_ONLY_TOOLS = frozenset({"get_course_profile", "get_daily_status", "get_weekly_status"})

# serverInfo.name for clients. Upstream's main() names its server
# "Garmin Connect v1.0", Garmin's product name; main() overrides it.
SERVER_NAME = "coach-mcp-for-garmin"
# serverInfo.version is this project's version (ext/version.py, one line), not
# upstream's or the mcp SDK's; the same number heads the CHANGELOG and tags the
# release.
SERVER_VERSION = version.VERSION


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
# A GARMIN_ENABLED_TOOLS entry that is no known tool goes into the error line
# only when it has the shape of a tool name (lower-case words joined by '_',
# each starting with a letter, like get_vo2max_trend), once whitespace and
# invisible characters are left out of the check; anything else may be a
# secret pasted into the wrong variable and is named without its value.
TOOL_NAME_SHAPE = re.compile(r"[a-z][a-z0-9]*(?:_[a-z][a-z0-9]*)+")
TOOL_NAME_SHAPE_MAX = 200
ERR_ENTRY_NOT_SHOWN = (f"GARMIN_ENABLED_TOOLS: an entry is not a tool name (it is not shown); "
                       f"the variable can only narrow the read-only list in {FILE_LABEL}")


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


def _tool_shaped(name):
    """True when `name`, without its whitespace and invisible characters, has
    the shape of a tool name (TOOL_NAME_SHAPE), so an error line may show it."""
    visible = "".join(c for c in name if c.isprintable() and not c.isspace())
    return len(visible) <= TOOL_NAME_SHAPE_MAX and TOOL_NAME_SHAPE.fullmatch(visible) is not None


def resolve_tools(environ, baked, read_only, write=frozenset(), available=None):
    """(allowed tool names, error lines) for this environment.

    Pure and standard library only, so the rules are testable without upstream:
    `baked` is the file's names, `read_only` every read-only tool name, `write`
    the known write tools (only used to word the errors). `available`, when
    given (key mode: key_mode.KEY_MODE_TOOLS), narrows the result further, and
    GARMIN_ENABLED_TOOLS naming a tool outside it is an error. Any error means
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
    usable = allowed if available is None else allowed & set(available)

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
        # Tool names are lower case: an unknown entry typed with an upper-case
        # letter is shown nowhere, as a mixed-case secret lower-cased would
        # otherwise pass for a tool-shaped name.
        had_upper = {n.strip().lower() for n in raw.split(",") if n.strip() != n.strip().lower()}
        if not requested:
            errors.append("GARMIN_ENABLED_TOOLS names no tools; unset it to expose the whole list")
        for name in sorted(requested - usable):
            if name in allowed:  # a tool of the baked list that key mode does not have
                errors.append(key_mode.ERR_TOOL_NOT_IN_KEY_MODE.format(name=_shown(name)))
                continue
            if name not in read_only and name not in write and (name in had_upper or not _tool_shaped(name)):
                if ERR_ENTRY_NOT_SHOWN not in errors:
                    errors.append(ERR_ENTRY_NOT_SHOWN)
                continue
            errors.append(
                f"GARMIN_ENABLED_TOOLS: {_shown(name)} is {_why_not(name, read_only, write)}; "
                f"the variable can only narrow the read-only list in {FILE_LABEL}"
            )
        usable &= requested

    return (frozenset() if errors else frozenset(usable)), errors


def allowed_tools(environ):
    """The tool names this server may register, or ConfigError. Needs upstream.

    In key mode (GARMIN_AUTH=key) only key_mode.KEY_MODE_TOOLS; an unset or
    invalid GARMIN_AUTH uses the full list (key_mode.check() reports it).
    """
    mcp2_compat.install()
    from garmin_mcp import tool_annotations

    write = tool_annotations.ADDITIVE_TOOLS | tool_annotations.DESTRUCTIVE_TOOLS
    # A name upstream lists as a write tool is never read-only here, not even
    # one of this repo's (or after patch() merged them into READ_ONLY_TOOLS).
    read_only = (tool_annotations.READ_ONLY_TOOLS | EXTRA_READ_ONLY_TOOLS) - write
    available = key_mode.KEY_MODE_TOOLS if _is_key_mode(environ) else None
    allowed, errors = resolve_tools(environ, read_enabled_tools_file(), read_only, write, available)
    if errors:
        raise ConfigError(errors)
    return allowed


def read_only_server_class(base, allowed):
    """A subclass of upstream's server class that registers only `allowed`,
    answers every failed tool call with a fixed text, and cleans upstream's
    answers.

    MCPServer.tool() registers through add_tool(), so gating add_tool covers
    both. A refused tool is logged and left out (never served), whatever
    upstream's own filter decided. add_tool() also ends the description of
    each tool in server_instructions.FREE_TEXT_TOOLS with FREE_TEXT_NOTE.

    MCPServer sends every tools/call through call_tool(name, arguments,
    context) (gate() checks that at build time). The override below:
    - a raised exception (the SDK's ToolError around the tool's own, an
      unknown tool, invalid arguments) -> tool_errors.classify_exception();
    - a returned "Error ...: ..." text, or upstream's plain failure texts ->
      tool_errors.classify_text();
    - either way, the group's fixed text from tool_errors.client_text(), with
      isError true for every group but no_data, and one log line (_failure());
    - get_sleep_summary's answer with no value in it (an empty JSON object
      or only nulls: the night has not synced yet) -> the no_data answer
      tool_errors.NO_SLEEP_TEXT;
    - a successful answer of an upstream tool -> its JSON free text cleaned by
      text_hygiene.clean_tool_answer() in a worker thread (a 16 MB FIT answer
      takes a fraction of a second); this repo's 3 tools, which clean their
      own read points and whose markdown keeps its bidi isolates, unchanged;
    - an MCPError (a protocol error) passes through, and an
      InputRequiredResult is returned as is.
    A bug in the override itself is answered as an internal error, never as
    its own text.
    """
    allowed = frozenset(allowed)

    class ReadOnlyFastMCP(base):
        def add_tool(self, fn, name=None, *args, **kwargs):
            tool_name = name or getattr(fn, "__name__", None)
            if tool_name not in allowed:
                print(f"launch: refused to register tool {tool_name!r}: not in the read-only allowlist",
                      file=sys.stderr)
                return None
            if not args and tool_name in server_instructions.FREE_TEXT_TOOLS:
                # The SDK passes keywords, and uses the raw docstring when no
                # description is given, so every other description stays as is.
                # The docstring loses its indentation (cleandoc): that keeps every
                # noted description, note included, under the 2,048 characters
                # Claude Code kept before v2.1.296 (the longest is 1,867).
                doc = kwargs.get("description") or getattr(fn, "__doc__", None) or ""
                kwargs["description"] = (inspect.cleandoc(doc).rstrip() + "\n\n"
                                         + server_instructions.FREE_TEXT_NOTE)
            return super().add_tool(fn, name, *args, **kwargs)

        async def call_tool(self, name, arguments, context=None):
            import anyio.to_thread
            from mcp.shared.exceptions import MCPError
            from mcp_types import CallToolResult, InputRequiredResult, TextContent

            try:
                try:
                    result = await super().call_tool(name, arguments, context)
                except MCPError:
                    raise
                except Exception as exc:  # ToolError, UnexpectedToolError, anything else
                    group, detail, _root, text = tool_errors.classify_exception(exc)
                    return _failure(name, group, detail, text, exc_info=exc)
                if isinstance(result, InputRequiredResult) or not isinstance(result, CallToolResult):
                    return result
                first = result.content[0] if result.content else None
                text = first.text if getattr(first, "type", None) == "text" else None
                if text is None:
                    return result
                if result.is_error or tool_errors.is_failure_text(text):
                    group, detail = tool_errors.classify_text(text)
                    return _failure(name, group, detail, text, original=result)
                if name in EXTRA_READ_ONLY_TOOLS:
                    return result
                if name == "get_sleep_summary" and tool_errors.is_empty_sleep_summary(text):
                    # A night that has not synced yet: an empty object says nothing, so say "no data".
                    return _failure(name, tool_errors.NO_DATA, None, tool_errors.NO_SLEEP_LOG, original=result,
                                    message=tool_errors.NO_SLEEP_TEXT)
                cleaned = await anyio.to_thread.run_sync(text_hygiene.clean_tool_answer, text)
                if cleaned == text:
                    return result
                update = {"content": [TextContent(type="text", text=cleaned), *result.content[1:]]}
                if isinstance(result.structured_content, dict) and result.structured_content.get("result") == text:
                    update["structured_content"] = {**result.structured_content, "result": cleaned}
                return result.model_copy(update=update)
            except MCPError:
                raise
            except Exception as bug:  # a bug in this method: never leak, never raise
                return _failure(name, tool_errors.INTERNAL, None,
                                f"tool-error wrapper failed: {type(bug).__name__}: {bug}", exc_info=bug)

    return ReadOnlyFastMCP


# Log levels of _failure()'s line (contract 5.8): these groups need the owner;
# bad input and no data are the caller's business.
_WARNING_GROUPS = frozenset({tool_errors.LOGIN, tool_errors.RATE_LIMITED, tool_errors.UNAVAILABLE})


def _failure(name, group, detail, text, *, exc_info=None, original=None, message=None):
    """The CallToolResult for a failed tool call, after its one log line.

    `group` and `detail` come from tool_errors; `text` is the original failure
    text, which only the log sees (redacted, one line); `exc_info` is the
    exception call_tool() caught: logged with its whole chain, for internal
    errors only, under a fresh ref that the client text carries too.
    `original` is the result the tool returned, if it returned one: a no_data
    answer keeps its structured "result" form. `message` replaces the group's
    fixed client text (one of tool_errors' own constants, never caller text).
    Arguments are never logged,
    and the tool name (caller text for an unknown tool) is redacted too.
    """
    from mcp_types import CallToolResult, TextContent

    ref = tool_errors.new_ref() if group == tool_errors.INTERNAL else None
    tool = tool_errors.redact(name, limit=64)
    line = tool_errors.redact(text)
    try:
        if ref is not None:
            tool_log.error("tool error: ref=%s tool=%s group=internal: %s", ref, tool, line, exc_info=exc_info)
        else:
            level = logging.WARNING if group in _WARNING_GROUPS else logging.INFO
            tool_log.log(level, "tool error: tool=%s group=%s: %s", tool, group, line)
    except Exception:  # noqa: BLE001 - the answer matters more than its log line
        pass
    if message is None:
        message = tool_errors.client_text(group, ref=ref, detail=detail, today=_today_iso(), key_mode=_KEY_MODE)
    structured = None
    if group == tool_errors.NO_DATA:
        kept = getattr(original, "structured_content", None)
        if isinstance(kept, dict) and "result" in kept:
            structured = {"result": message}
    return CallToolResult(content=[TextContent(type="text", text=message)], structured_content=structured,
                          is_error=tool_errors.IS_ERROR[group])


def _today_iso():
    """Today in REPORT_TZ as YYYY-MM-DD (the bad-input date detail gives it);
    the UTC date when REPORT_TZ is unset or unknown."""
    try:
        return status_common.today_local(status_common.zone()).isoformat()
    except Exception:  # noqa: BLE001 - a failure answer must not fail
        return datetime.datetime.now(datetime.timezone.utc).date().isoformat()


_HOOK_ERROR = ("launch: mcp's MCPServer no longer sends tools/call through call_tool(name, arguments, context); "
               "the tool-error wrapper in read_only_server_class() needs a new hook")


def _check_call_tool_hook():
    """RuntimeError unless the installed SDK still sends every tools/call
    through MCPServer.call_tool(self, name, arguments, context), the method
    ReadOnlyFastMCP overrides. If a later SDK moved that hook, the override
    would silently stop running and raw errors would reach clients again;
    gate() runs this at build time, so the build fails instead."""
    import inspect

    from mcp.server.mcpserver import MCPServer

    try:
        hook = MCPServer.__dict__["call_tool"]
        params = list(inspect.signature(hook).parameters)[:4]
        source = inspect.getsource(MCPServer._handle_call_tool)
    except (KeyError, AttributeError, OSError, TypeError, ValueError) as exc:
        raise RuntimeError(_HOOK_ERROR) from exc
    if params != ["self", "name", "arguments", "context"] or "self.call_tool(" not in source:
        raise RuntimeError(_HOOK_ERROR)


def gate(allowed):
    """Second gate: upstream's main() builds its app from garmin_mcp.FastMCP;
    swap in a subclass that refuses every tool outside `allowed` and wraps
    every tool call (read_only_server_class()). Raises if a hook moved, so an
    upstream or SDK bump fails the build (Dockerfile)."""
    mcp2_compat.install()
    _check_call_tool_hook()
    import garmin_mcp

    base = getattr(garmin_mcp, "FastMCP", None)
    if not (isinstance(base, type) and callable(getattr(base, "add_tool", None))):
        raise RuntimeError("launch: garmin_mcp.FastMCP is gone or has no add_tool(); "
                           "the read-only registration gate needs a new hook")
    garmin_mcp.FastMCP = read_only_server_class(base, allowed)


def _is_key_mode(environ):
    """True when GARMIN_AUTH is key (spaces and case ignored); False for full, unset or invalid."""
    return key_mode.auth_mode(environ) == "key"


def check_config(environ):
    """(allowed tools, error lines), in this order: the tool policy (narrowed
    to the key-mode tools with GARMIN_AUTH=key), GARMIN_AUTH and the key-mode
    settings (key_mode.check()), REPORT_TZ (required), EDGE_CLIENT_IP_HEADER
    and, when the sign-in is on (any transport but stdio), its settings and
    then, only if they are valid, whether AUTH_DB opens. No line holds a
    secret value."""
    allowed, errors = frozenset(), []
    try:
        allowed = allowed_tools(environ)
    except ConfigError as exc:
        errors += exc.errors
    errors += key_mode.check(environ)
    errors += status_common.check_report_tz(environ)
    errors += auth_config.edge_header_errors(environ)
    if auth_config.auth_required(environ):
        config, auth_errors = auth_config.config_from_environ(environ)
        errors += auth_errors
        if config is not None and not auth_errors:
            # Creates the file (0600) and its tables as the server would.
            errors += auth_store.check_writable(config.db_path)
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
    if _is_key_mode(environ):
        print(f"launch: config OK ({len(allowed)} read-only tools, Garmin Personal API key)", file=sys.stderr)
    else:
        print(f"launch: config OK ({len(allowed)} read-only tools)", file=sys.stderr)
    return 0


def patch(garmin_auth="full"):
    """Hook this repo's tools into upstream's tool registration.

    garmin_auth "key" registers the status tools in key mode (they skip the
    sources Garmin's Personal API does not serve); "full" (the default, and
    the Dockerfile's build check) changes nothing.

    Raises, before changing anything in upstream, if a status module's
    selfcheck fails (an upstream helper it imports is gone) or a hook moved,
    if upstream now has a tool named like one of this repo's, or if
    mcp2_compat cannot adapt the installed mcp SDK.
    """
    if garmin_auth not in ("full", "key"):
        raise ValueError(f"launch.patch: garmin_auth must be 'full' or 'key', got {garmin_auth!r}")
    keyed = garmin_auth == "key"
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
        daily_status.register_tools(app, get_client, key_mode=keyed)
        weekly_status.register_tools(app, get_client, key_mode=keyed)
        if _AUTH is not None:
            # The consent page and the root protected-resource metadata. app
            # is upstream's _ToolFilter, which passes custom_route() through.
            import owner_auth

            owner_auth.register_routes(app, _AUTH)
        return app

    # annotations_for() reads this module global at registration time.
    tool_annotations.READ_ONLY_TOOLS = tool_annotations.READ_ONLY_TOOLS | EXTRA_READ_ONLY_TOOLS
    courses.register_tools = register_tools


LOG_FORMAT = "%(levelname)s %(name)s: %(message)s"


class _LineHandler(logging.StreamHandler):
    """One plain line per record, written to whatever sys.stderr is at the
    time (as logging.lastResort does), so a test's redirect_stderr sees it."""

    coach_line_handler = True

    def __init__(self):
        logging.Handler.__init__(self)
        self.setFormatter(logging.Formatter(LOG_FORMAT))

    @property
    def stream(self):
        return sys.stderr


def _configure_logging():
    """INFO on the root logger, one plain line per record on stderr, set up
    now so the sign-in's startup lines are not dropped.

    Not the SDK's rich handler: with no terminal (Railway, Docker) it wraps
    each record at 80 columns, and Railway's log viewer and
    scripts/live-check.py then see one record as several entries (live test
    R1, 17 Nov 2026). The SDK's configure_logging(), called here and again
    when upstream's main() builds the server, finds a handler on the root
    logger and changes nothing (logging.basicConfig)."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        if type(handler).__name__ == "RichHandler":
            root.removeHandler(handler)
    if not any(getattr(h, "coach_line_handler", False) for h in root.handlers):
        root.addHandler(_LineHandler())
    root.setLevel(logging.INFO)
    try:
        from mcp.server.mcpserver.utilities.logging import configure_logging
    except ImportError:  # the standard-library test leg; the image has the SDK
        return
    configure_logging("INFO")


def _exit_config_errors(errors):
    for line in errors:
        print(f"launch: config error: {line}", file=sys.stderr)
    sys.exit(1)


def main(argv=None):
    global _AUTH, _KEY_MODE

    args = sys.argv[1:] if argv is None else list(argv)
    if args == ["--check-config"]:
        sys.exit(_check_config_cli(os.environ))
    if args:
        sys.exit("usage: launch.py [--check-config]")

    mcp2_compat.install()
    # GARMIN_AUTH and the key, again (entrypoint.sh ran --check-config first):
    # a server started by hand must not run without a mode.
    errors = key_mode.check(os.environ)
    if errors:
        _exit_config_errors(errors)
    mode = key_mode.auth_mode(os.environ)
    _KEY_MODE = mode == "key"
    patch(garmin_auth=mode)
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

    if _KEY_MODE:
        # The key leaves os.environ (with GARMIN_PERSONAL_API_BASE_URL, D3)
        # before anything else runs or logs, and redaction knows its value
        # (take_key() registers it; registering twice changes nothing).
        cache_file = key_mode.cache_path(os.environ)
        key = key_mode.take_key(os.environ)
        tool_errors.register_secret(key)
        # Logging first, or install()'s INFO line "garmin key mode: on, token
        # cache <path>" (4.11) is dropped: the root logger is still at WARNING.
        _configure_logging()
        # Replaces upstream's garminconnect login with the key client.
        key_mode.install(key, cache_file)
        del key
    else:
        # Full mode: an empty base-URL variable passed the check; drop it too.
        os.environ.pop(key_mode.BASE_URL_ENV, None)

    # Before upstream's main() builds the server: its name (SERVER_NAME), its
    # version (SERVER_VERSION), the instructions it sends at the handshake
    # (the mode's) and, with the sign-in on, the sign-in itself.
    _AUTH = None
    instructions = server_instructions.INSTRUCTIONS_KEY if _KEY_MODE else server_instructions.INSTRUCTIONS
    overrides = {"name": SERVER_NAME, "version": SERVER_VERSION, "instructions": instructions}
    middleware = ()
    if auth_config.auth_required(os.environ):
        config, errors = auth_config.config_from_environ(os.environ)
        if errors:
            for line in errors:
                print(f"launch: config error: {line}", file=sys.stderr)
            sys.exit(1)
        _configure_logging()
        import owner_auth

        _AUTH = owner_auth.build(config)
        auth_http.install_log_redaction()
        # uvicorn trusts proxy headers from 127.0.0.1 by default: it would put
        # the last X-Forwarded-For address (behind Railway, the edge's) in
        # place of the real peer, Caddy, and auth_http.client_ip() would then
        # ignore X-Client-IP and give every caller one rate-limit bucket. An
        # empty list trusts no proxy, so the peer stays the TCP peer and
        # uvicorn ignores X-Forwarded-For and X-Forwarded-Proto. The SDK reads
        # this when it builds uvicorn's config, after this point.
        os.environ["FORWARDED_ALLOW_IPS"] = ""
        overrides.update(auth=_AUTH.settings, auth_server_provider=_AUTH.provider)
        # Outermost first: the rate limit sees every request before anything else.
        middleware = (auth_http.SignInRateLimit, *owner_auth.asgi_middleware(_AUTH))
        log.info("sign-in: on, issuer %s, static bearer %s", config.public_url, "on" if config.static_bearer else "off")
    mcp2_compat.set_asgi_middleware(*middleware)
    mcp2_compat.set_server_overrides(**overrides)
    from garmin_mcp import main as upstream_main

    upstream_main()


if __name__ == "__main__":
    main()
