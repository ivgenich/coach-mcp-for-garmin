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

The Garmin call guard (garmin_guard) is on in every install: main() builds
one, and call_tool() asks it before each Garmin tool call, so a Garmin rate
limit stops live retries for 15 minutes in code, not only in the text the
model sees; a refused call is answered with the rate-limit text and Garmin is
not called. It also serialises full mode's token refresh (refresh_lock).

The optional features (feature_config; both off by default, and then nothing
below runs):
- TRACING=on: main() calls telemetry.setup() and call_tool() wraps each call
  in a "tool" span and records its outcome (telemetry.py, docs/tracing.md);
- HISTORY_SEARCH=on: the history tools (history_tools.register(), the names
  in HISTORY_TOOLS) join the read-only list, the guard keeps its state in
  Postgres (garmin_guard_pg) so the worker sees it, and the app enqueues the
  catch-up jobs at startup (docs/history-search.md);
- HISTORY_FORGET=yes: main() deletes the stored history (history_store's
  forget_all()) before anything else uses the database;
- ROLE=worker or enqueue: main() runs worker.py's or enqueue.py's main()
  instead of the server (entrypoint.sh starts those directly;
  docs/history-worker.md).
The modules of the features are imported only when a feature is on, so the
server starts without their packages.

Parts of this file live in modules of their own, and launch.py keeps their
names (tests, docs and the Dockerfile's build check use them):
- garmin_paths.py: the argument checks before Garmin and the Garmin request
  path guard (the history worker installs the same guard);
- tool_answers.py: the description notes and the answer additions for
  upstream's tools;
- feature_startup.py: the optional features' startup pieces (the call guard,
  the history forget, the catch-up and token threads, ROLE dispatch);
- launch_logging.py: the one-line log handler and uvicorn's log split.
The module state (_GUARD, _TELEMETRY, _KEY_MODE, _FEATURES, _AUTH) stays
here, and so does every function that reads it.
"""

from __future__ import annotations

import contextlib
import contextvars
import datetime
import inspect
import logging
import os
import re
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import auth_config  # noqa: E402
import auth_http  # noqa: E402
import auth_store  # noqa: E402
import course_profile  # noqa: E402
import feature_config  # noqa: E402
import feature_startup  # noqa: E402
import garmin_guard  # noqa: E402
import garmin_paths  # noqa: E402
import mcp2_compat  # noqa: E402
import plan_engine  # noqa: E402
import daily_status  # noqa: E402
import key_mode  # noqa: E402
import launch_logging  # noqa: E402
import log_privacy  # noqa: E402
import server_instructions  # noqa: E402
import status_common  # noqa: E402
import tool_answers  # noqa: E402
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
# The history-search tools (history_tools.py): read-only, they read only
# Postgres and never call Garmin. They join the allowed list only with
# HISTORY_SEARCH=on (allowed_tools()), outside config/enabled-tools.txt, and
# GARMIN_ENABLED_TOOLS may narrow them away like any other tool.
HISTORY_TOOLS = frozenset({"find_similar_weeks", "search_history"})
# The training-plan read tools (plan_tools.py): they join the allowed list only with PLAN_TOOLS=on (which needs
# HISTORY_SEARCH=on, GARMIN_AUTH=full and ROLE=app), outside config/enabled-tools.txt, like HISTORY_TOOLS.
# plan_assess and plan_preview call Garmin (the call guard counts them); plan_get reads only Postgres.
PLAN_READ_TOOLS = frozenset({"plan_assess", "plan_get", "plan_preview"})
# The plan tracking write tool (PLAN_TRACKING=on, which needs PLAN_TOOLS=on): log_feedback writes only the app's
# own database (RPE and pain), never Garmin. It joins the allowed list only from the switch (allowed_tools()),
# never from config/enabled-tools.txt or GARMIN_ENABLED_TOOLS, and patch() then annotates it additive,
# idempotent and local-only (never read-only).
PLAN_TRACKING_TOOLS = frozenset({"log_feedback"})
# The weekly review (Stage 4, PLAN_TRACKING=on): plan_review_week reads Garmin and the plan database and stores
# its proposals in the app's own database, nothing the user sees in Garmin or in the plan (the same bookkeeping
# as get_daily_status refreshing the links), so it is a read tool: hinted read-only, in OWN_READ_TOOLS. It joins
# the allowed list only from the switch (full mode, ROLE=app), like the plan read tools.
PLAN_REVIEW_TOOLS = frozenset({"plan_review_week"})
# Tools that never call Garmin: the call guard skips them.
NO_GARMIN_TOOLS = HISTORY_TOOLS | frozenset({"plan_get"}) | PLAN_TRACKING_TOOLS
# This repo's read-only tools: patch() merges exactly these into upstream's READ_ONLY_TOOLS.
OWN_READ_TOOLS = EXTRA_READ_ONLY_TOOLS | HISTORY_TOOLS | PLAN_READ_TOOLS | PLAN_REVIEW_TOOLS
# The plan write tools. Never merged into READ_ONLY_TOOLS, and never named in config/enabled-tools.txt or
# GARMIN_ENABLED_TOOLS (both stay errors). PLAN_WRITE_TOOLS join the allowed list only with PLAN_WRITES=on and
# PLAN_TRACKING_TOOLS only with PLAN_TRACKING=on (allowed_tools()); patch() then merges them into upstream's
# effect sets (WRITE_EFFECTS; log_feedback additive, idempotent and local-only) so their hints are right.
# plan_apply_changes (Stage 4) joins only with PLAN_WRITES=on and PLAN_TRACKING=on (its token comes from
# plan_review_week): destructive (it replaces the plan's own workouts) and idempotent (a done token replays).
OWN_WRITE_TOOLS = frozenset({"log_feedback", "plan_apply_changes", "plan_cancel", "plan_commit"})
PLAN_APPLY_TOOLS = frozenset({"plan_apply_changes"})
PLAN_WRITE_TOOLS = frozenset({"plan_cancel", "plan_commit"})
# The tools that may change Garmin (the write failure text, the --check-config count).
GARMIN_WRITE_TOOLS = PLAN_WRITE_TOOLS | PLAN_APPLY_TOOLS
# name -> (upstream effect set, idempotent): plan_commit adds workouts, plan_cancel deletes them; both are safe to
# repeat with the same token (a finished token replays its answer).
WRITE_EFFECTS = {"plan_commit": ("ADDITIVE_TOOLS", True), "plan_cancel": ("DESTRUCTIVE_TOOLS", True),
                 "plan_apply_changes": ("DESTRUCTIVE_TOOLS", True)}
# This repo's tools: they clean their own read points, so call_tool() leaves
# their answers as they are.
OWN_TOOLS = OWN_READ_TOOLS | OWN_WRITE_TOOLS

# The feature settings main() read (feature_config.FeatureConfig); both off
# until main() runs.
_FEATURES = feature_config.FeatureConfig()
# The Garmin call guard main() builds; None until then, so a server built by a
# test without main() calls Garmin as before.
_GUARD = None
# The telemetry module, when main() set tracing up; None otherwise.
_TELEMETRY = None
# The outcome group of the tool call running in this context: _failure() sets
# it, call_tool() reads it for the guard and the telemetry.
_CALL_GROUP = contextvars.ContextVar("coach_call_group", default=None)

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
    With HISTORY_SEARCH=on (ROLE=app) the HISTORY_TOOLS join the list in
    both modes; with PLAN_TOOLS=on too the PLAN_READ_TOOLS join it (full mode
    only), and with PLAN_TRACKING=on the PLAN_REVIEW_TOOLS. Of OWN_WRITE_TOOLS only PLAN_WRITE_TOOLS
    (PLAN_WRITES=on), PLAN_TRACKING_TOOLS (PLAN_TRACKING=on) and PLAN_APPLY_TOOLS (both) join it, from their
    switch only, never from a tool list.
    """
    mcp2_compat.install()
    from garmin_mcp import tool_annotations

    # Upstream's own write tools (a snapshot taken before patch() merges the plan write tools) and this repo's.
    write = _upstream_write_tools() | OWN_WRITE_TOOLS
    # A name upstream lists as a write tool is never read-only here, not even
    # one of this repo's (or after patch() merged them into READ_ONLY_TOOLS).
    read_only = (tool_annotations.READ_ONLY_TOOLS | EXTRA_READ_ONLY_TOOLS) - write
    available = key_mode.KEY_MODE_TOOLS if _is_key_mode(environ) else None
    baked = read_enabled_tools_file()
    features = feature_config.load(environ)
    if features.history and features.role == "app":
        baked = list(baked) + sorted(HISTORY_TOOLS)
        read_only = read_only | (HISTORY_TOOLS - write)
        if available is not None:
            available = frozenset(available) | HISTORY_TOOLS
    if features.plan_tools and features.history and features.role == "app" and available is None:
        baked = list(baked) + sorted(PLAN_READ_TOOLS)
        read_only = read_only | (PLAN_READ_TOOLS - write)
        if getattr(features, "plan_tracking", False):
            baked = list(baked) + sorted(PLAN_REVIEW_TOOLS)
            read_only = read_only | (PLAN_REVIEW_TOOLS - write)
    allowed, errors = resolve_tools(environ, baked, read_only, write, available)
    if errors:
        raise ConfigError(errors)
    if features.plan_writes and features.plan_tools and features.history and features.role == "app" \
            and available is None:
        # Only from the switch: never from the file or GARMIN_ENABLED_TOOLS, and never an upstream write tool.
        allowed = allowed | (PLAN_WRITE_TOOLS - _upstream_write_tools())
        if getattr(features, "plan_tracking", False):
            allowed = allowed | (PLAN_APPLY_TOOLS - _upstream_write_tools())
    return allowed | (plan_tracking_tools(features, key_mode=available is not None) - _upstream_write_tools())


def plan_tracking_tools(features, *, key_mode=False):
    """The tracking write tool PLAN_TRACKING=on turns on (PLAN_TRACKING_TOOLS: log_feedback, which writes only
    the app's database), full mode and ROLE=app only."""
    if key_mode or not (getattr(features, "plan_tracking", False) and features.plan_tools and features.history
                        and features.role == "app"):
        return frozenset()
    return PLAN_TRACKING_TOOLS & OWN_WRITE_TOOLS


_UPSTREAM_WRITE = {}


def _upstream_write_tools():
    """Upstream's ADDITIVE_TOOLS | DESTRUCTIVE_TOOLS as they were before this process merged anything into
    them (taken once, on the first call; patch() calls it before its merge)."""
    if "tools" not in _UPSTREAM_WRITE:
        mcp2_compat.install()
        from garmin_mcp import tool_annotations

        _UPSTREAM_WRITE["tools"] = frozenset(tool_annotations.ADDITIVE_TOOLS | tool_annotations.DESTRUCTIVE_TOOLS)
    return _UPSTREAM_WRITE["tools"]


def _merge_write_effects(tool_annotations, tracking=False):
    """With PLAN_WRITES=on: plan_commit into ADDITIVE_TOOLS, plan_cancel (and with PLAN_TRACKING=on
    plan_apply_changes) into DESTRUCTIVE_TOOLS, all into IDEMPOTENT_TOOLS, so upstream's annotations_for() hints
    them right (readOnlyHint false)."""
    for name, (effect, idempotent) in WRITE_EFFECTS.items():
        if name in PLAN_APPLY_TOOLS and not tracking:
            continue
        setattr(tool_annotations, effect, getattr(tool_annotations, effect) | {name})
        if idempotent:
            tool_annotations.IDEMPOTENT_TOOLS = tool_annotations.IDEMPOTENT_TOOLS | {name}


# --- Argument checks and the Garmin request path guard (garmin_paths.py) --------------------------------------
# Upstream tools that put a caller value into a Garmin path: call_tool() runs ARGUMENT_CHECKS
# (refused_argument()) before the call guard and before Garmin. In full mode patch() installs the path guard
# on garminconnect's Client._run_request (_install_path_guard()), the backstop for every other path; the
# history worker installs the same guard (worker_garmin). The names below are garmin_paths' under the names
# this module always had (tests, docs and the Dockerfile's build check use them).
ARGUMENT_CHECKS = garmin_paths.ARGUMENT_CHECKS
refused_argument = garmin_paths.refused_argument
PATH_REFUSED_TEXT = garmin_paths.PATH_REFUSED_TEXT
GarminPathRefused = garmin_paths.GarminPathRefused
garmin_path_ok = garmin_paths.garmin_path_ok
check_garmin_path = garmin_paths.check_garmin_path
_path_guarded_run_request = garmin_paths.path_guarded_run_request
_path_guard_installed = garmin_paths.path_guard_installed
_check_path_guard_hook = garmin_paths.check_path_guard_hook
_install_path_guard = garmin_paths.install_path_guard

# --- Description notes and answer additions for upstream tools (tool_answers.py) ------------------------------
# add_tool() ends the descriptions of the tools in TOOL_NOTES with their note; call_tool() cleans a successful
# upstream answer and adds the fields of ANSWER_ADDITIONS (_clean_texts()). The same objects as tool_answers',
# so mock.patch.dict() on either changes both.
TOOL_NOTES = tool_answers.TOOL_NOTES
ANSWER_ADDITIONS = tool_answers.ANSWER_ADDITIONS
add_to_answer = tool_answers.add_to_answer
_clean_texts = tool_answers.clean_texts


def read_only_server_class(base, allowed):
    """A subclass of upstream's server class that registers only `allowed`,
    answers every failed tool call with a fixed text, and cleans upstream's
    answers.

    MCPServer.tool() registers through add_tool(), so gating add_tool covers
    both. A refused tool is logged and left out (never served), whatever
    upstream's own filter decided. add_tool() also ends the description of
    each tool in TOOL_NOTES with its note, and of each tool in
    server_instructions.FREE_TEXT_TOOLS with FREE_TEXT_NOTE.

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
      takes a fraction of a second), then the fields of ANSWER_ADDITIONS added
      (structured "result" kept equal to the text); this repo's 3 tools, which clean their
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
            free_text = tool_name in server_instructions.FREE_TEXT_TOOLS
            if not args and (free_text or tool_name in TOOL_NOTES):
                # The SDK passes keywords, and uses the raw docstring when no
                # description is given, so every other description stays as is.
                # The docstring loses its indentation (cleandoc): that keeps every
                # noted description, notes included, under the 2,048 characters
                # Claude Code kept before v2.1.296. The tool's own note (TOOL_NOTES)
                # comes before the free-text note.
                doc = kwargs.get("description") or getattr(fn, "__doc__", None) or ""
                parts = [inspect.cleandoc(doc).rstrip()]
                if tool_name in TOOL_NOTES:
                    parts.append(TOOL_NOTES[tool_name])
                if free_text:
                    parts.append(server_instructions.FREE_TEXT_NOTE)
                kwargs["description"] = "\n\n".join(parts)
            return super().add_tool(fn, name, *args, **kwargs)

        async def call_tool(self, name, arguments, context=None):
            # A caller value that would reach a Garmin path unchecked: refused before the guard (it must not
            # count as a call, nor start a cooldown) and before Garmin. Never logs the value.
            try:
                refused = refused_argument(name, arguments) if name in allowed else None
            except Exception:  # noqa: BLE001 - a check that breaks refuses rather than passes
                refused = ("an argument", tool_errors.GENERIC_DETAIL)
            if refused is not None:
                result = _failure(name, tool_errors.BAD_INPUT, refused[1],
                                  f"argument {refused[0]} refused before Garmin")
                _record_tool_call(name, "error", tool_errors.BAD_INPUT, 0.0, result, None)
                return result
            # The guard (set up by main()) for a Garmin tool; the history tools and plan_get never call Garmin.
            guard = _GUARD if (name in allowed and name not in NO_GARMIN_TOOLS) else None
            if guard is not None:
                try:
                    guard.before_call(garmin_guard.TOOL)
                except garmin_guard.GuardBlocked as blocked:
                    _record_tool_call(name, "blocked", tool_errors.RATE_LIMITED, 0.0, None, guard)
                    return _failure(name, tool_errors.RATE_LIMITED, None, str(blocked))
                except Exception:  # noqa: BLE001 - the guard never breaks a call
                    guard = None
                else:
                    guard.live_call_started()
            token = _CALL_GROUP.set(None)
            # The rate-limit and login failures of single report sources (status_common.fetch_all), which a
            # report that still answers would otherwise hide from the guard.
            capture, sources = status_common.capture_outcomes()
            started = time.monotonic()
            result = None
            try:
                with _tool_span(name) as span:
                    result = await self._coach_call_tool(name, arguments, context)
                    _finish_span(span, _CALL_GROUP.get(), result)
                return result
            finally:
                group = _CALL_GROUP.get() if result is not None else tool_errors.INTERNAL
                status_common.end_capture(capture)
                if guard is not None:
                    guard.live_call_finished()
                    guard.after_outcome(garmin_guard.TOOL, _guard_outcome(group, sources))
                if _TELEMETRY is not None:
                    _record_tool_call(name, "ok" if group is None else "error", group,
                                      time.monotonic() - started, result, guard)
                _CALL_GROUP.reset(token)

        async def _coach_call_tool(self, name, arguments, context=None):
            import anyio.to_thread
            from mcp.shared.exceptions import MCPError
            from mcp_types import CallToolResult, InputRequiredResult, TextContent

            try:
                try:
                    result = await super().call_tool(name, arguments, context)
                except MCPError:
                    raise
                except Exception as exc:  # noqa: BLE001 - ToolError, UnexpectedToolError, anything else
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
                if name in OWN_TOOLS:
                    return result
                if name == "get_sleep_summary" and tool_errors.is_empty_sleep_summary(text):
                    # A night that has not synced yet: an empty object says nothing, so say "no data".
                    return _failure(name, tool_errors.NO_DATA, None, tool_errors.NO_SLEEP_LOG, original=result,
                                    message=tool_errors.NO_SLEEP_TEXT)
                # Every text block is cleaned, not only the first (one worker-thread hop for all of them).
                texts = [b.text if getattr(b, "type", None) == "text" and isinstance(getattr(b, "text", None), str)
                         else None for b in result.content]
                cleaned_texts = await anyio.to_thread.run_sync(_clean_texts, texts, name)
                if cleaned_texts == texts:
                    return result
                content = []
                for block, old, new in zip(result.content, texts, cleaned_texts):
                    if old is not None and new != old:
                        copy = getattr(block, "model_copy", None)
                        block = copy(update={"text": new}) if callable(copy) else TextContent(type="text", text=new)
                    content.append(block)
                cleaned = cleaned_texts[0]
                update = {"content": content}
                if isinstance(result.structured_content, dict) and result.structured_content.get("result") == text:
                    update["structured_content"] = {**result.structured_content, "result": cleaned}
                return result.model_copy(update=update)
            except MCPError:
                raise
            except Exception as bug:  # noqa: BLE001 - a bug in this method: never leak, never raise
                return _failure(name, tool_errors.INTERNAL, None,
                                f"tool-error wrapper failed: {type(bug).__name__}: {bug}", exc_info=bug)

    return ReadOnlyFastMCP


def _guard_outcome(group, source_groups):
    """The group the call guard hears for a tool call that ended in `group` (None: it answered) after its
    report sources met `source_groups`: the call's own group when it starts a cooldown, else a rate limit or
    login failure of any source (so the cooldown starts and a partial answer never ends a login cooldown),
    else the call's own group."""
    if group in garmin_guard.WORKER_PAUSE:
        return group
    return status_common.guard_group(source_groups) or group


# Log levels of _failure()'s line: these groups need the owner;
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

    _CALL_GROUP.set(group)
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
    if message is None and name in GARMIN_WRITE_TOOLS and group != tool_errors.BAD_INPUT:
        # The generic "retry once" is unsafe after a write: say that Garmin may have changed.
        message = tool_errors.write_client_text(group, ref=ref)
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


def _answer_bytes(result):
    """The UTF-8 size of a result's first text block; 0 when there is none."""
    try:
        first = result.content[0] if result is not None and result.content else None
        text = first.text if getattr(first, "type", None) == "text" else ""
        return len(text.encode("utf-8"))
    except Exception:  # noqa: BLE001
        return 0


def _mode_name():
    return "key" if _KEY_MODE else "full"


def _tool_span(name):
    """The "tool <name>" span around one tool call (telemetry.span; a name the client sent that is no
    served tool reads "other"), or a context that yields None when tracing is off."""
    if _TELEMETRY is None:
        return contextlib.nullcontext()
    try:
        return _TELEMETRY.span(f"tool {name}", tool=name, caller="tool", mode=_mode_name())
    except Exception:  # noqa: BLE001 - tracing never breaks a tool call
        return contextlib.nullcontext()


def _finish_span(span, group, result):
    """The outcome on the tool span: outcome, error group and the answer's size bucket. Never raises."""
    if span is None:
        return
    try:
        span.set(outcome="ok" if group is None else "error",
                 answer_bytes_bucket=_TELEMETRY.bytes_bucket(_answer_bytes(result)))
        if group is not None:
            span.set(error_group=group)
            if tool_errors.IS_ERROR.get(group, True):
                span.error(group)
    except Exception:  # noqa: BLE001
        pass


def _record_tool_call(name, outcome, group, seconds, result, guard):
    """The tool metrics (coach.tool.calls, .duration, .answer.size) and the cooldown gauge. Never raises."""
    if _TELEMETRY is None:
        return
    try:
        mode = _mode_name()
        attrs = {"tool": name, "outcome": outcome, "mode": mode}
        if group is not None:
            attrs["error_group"] = group
        _TELEMETRY.metric_count("coach.tool.calls", 1, **attrs)
        if outcome != "blocked":
            _TELEMETRY.metric_observe("coach.tool.duration", seconds, tool=name, mode=mode)
            _TELEMETRY.metric_observe("coach.tool.answer.size", _answer_bytes(result), tool=name)
        if guard is not None:
            until, cooldown_group = guard.cooldown()
            active = until is not None and until > time.time()
            gauge = {"error_group": cooldown_group} if active and cooldown_group in tool_errors.GROUPS else {}
            _TELEMETRY.metric_gauge("coach.garmin.cooldown.active", 1 if active else 0, **gauge)
    except Exception:  # noqa: BLE001
        pass


# The first path segments of the Garmin Connect API that garminconnect and garmin_mcp call (collected from
# their URL constants); telemetry.GARMIN_ENDPOINT_FAMILIES is the same list (a test keeps them equal). Any
# other segment, such as a display name in a "/latest/<name>" path, is reported as "other".
GARMIN_ENDPOINT_FAMILIES = frozenset((
    "activity-service", "activitylist-service", "adhocchallenge-service", "badge-service",
    "badgechallenge-service", "biometric-service", "bloodpressure-service", "calendar-service", "course-service",
    "device-service", "download-service", "fitnessage-service", "fitnessstats-service", "gcs-golfcommunity",
    "gear-service", "goal-service", "graphql-gateway", "hrv-service", "lifestylelogging-service",
    "metrics-service", "mobile-gateway", "nutrition-service", "periodichealth-service", "personalrecord-service",
    "sleep-service", "trainingplan-service", "upload-service", "userprofile-service", "userstats-service",
    "usersummary-service", "web-gateway", "weight-service", "wellness-service", "workout-service",
))
_SOURCE_RE = re.compile(r"^[a-z][a-z0-9_]{0,40}$")


def _endpoint_family(path):
    """The first segment of a Garmin API path when it is a known service ("wellness-service"), else
    "other". Never the rest: later segments can hold the account's display name or ids."""
    try:
        head = str(path).lstrip("/").split("/", 1)[0].split("?", 1)[0]
    except Exception:  # noqa: BLE001
        return "other"
    return head if head in GARMIN_ENDPOINT_FAMILIES else "other"


class _BoundThreading:
    """Stands in for the `threading` module inside upstream garmin_mcp: Thread targets run in the
    caller's trace context (telemetry.bind_context), so a Garmin call made on _GarminProxy's timeout
    thread is a child of the tool (or source) span. Everything else is the real module."""

    def __init__(self, real):
        self._real = real

    def __getattr__(self, name):
        return getattr(self._real, name)

    def Thread(self, *args, **kwargs):  # noqa: N802 - the threading.Thread name
        target = kwargs.get("target")
        if target is not None and _TELEMETRY is not None:
            try:
                kwargs["target"] = _TELEMETRY.bind_context(target)
            except Exception:  # noqa: BLE001
                pass
        return self._real.Thread(*args, **kwargs)


def _bind_garmin_threads():
    """Install _BoundThreading in upstream garmin_mcp (idempotent). False when upstream is missing."""
    try:
        import garmin_mcp
    except Exception:  # noqa: BLE001
        return False
    if not isinstance(getattr(garmin_mcp, "threading", None), _BoundThreading):
        garmin_mcp.threading = _BoundThreading(threading)
    return True


def _record_garmin_request(started, group):
    tel = _TELEMETRY
    if tel is None:
        return
    try:
        mode = _mode_name()
        attrs = {"caller": "tool", "outcome": "ok" if group is None else "error", "mode": mode}
        if group is not None:
            attrs["error_group"] = group
        tel.metric_count("coach.garmin.requests", 1, **attrs)
        tel.metric_observe("coach.garmin.request.duration", time.monotonic() - started, caller="tool", mode=mode)
    except Exception:  # noqa: BLE001
        pass


def _traced_run_request(original):
    """garminconnect Client._run_request wrapped: a CLIENT span "garmin GET <family>" (GET only; the
    caller, the family and the status code) and the coach.garmin.requests / .request.duration metrics for
    every request. Exceptions pass through unchanged; only their group is recorded."""

    def _run_request(self, method, path, *args, **kwargs):
        # The tracing hook is installed after the path guard (patch()), so it is the outer wrapper: a refused
        # path is refused here too, before it counts as a Garmin request in the metrics; so is a method the
        # allowlist does not admit (garmin_paths.check_garmin_request).
        garmin_paths.check_garmin_request(method, path, kwargs)
        tel = _TELEMETRY
        if tel is None:
            return original(self, method, path, *args, **kwargs)
        family = _endpoint_family(path)
        started = time.monotonic()
        is_get = str(method).upper() == "GET"
        try:
            span_cm = (tel.span(f"garmin GET {family}", {"http.request.method": "GET"}, kind="client",
                                caller="tool", endpoint_family=family)
                       if is_get else contextlib.nullcontext())
        except Exception:  # noqa: BLE001
            span_cm = contextlib.nullcontext()
        with span_cm as span:
            try:
                resp = original(self, method, path, *args, **kwargs)
            except Exception as exc:
                group = _exception_group(exc)
                _record_garmin_request(started, group)
                _set_garmin_span(span, getattr(getattr(exc, "response", None), "status_code", None), group)
                raise
            _record_garmin_request(started, None)
            _set_garmin_span(span, getattr(resp, "status_code", None), None)
            return resp

    _run_request._coach_traced = True
    _run_request.__wrapped__ = original
    return _run_request


def _exception_group(exc):
    try:
        return tool_errors.classify_exception(exc)[0]
    except Exception:  # noqa: BLE001
        return tool_errors.INTERNAL


def _set_garmin_span(span, status, group):
    if span is None:
        return
    try:
        if isinstance(status, int) and 100 <= status <= 599:
            span.set(status_code=status)
        span.set(outcome="ok" if group is None else "error")
        if group is not None:
            span.set(error_group=group)
            if tool_errors.IS_ERROR.get(group, True):
                span.error(group)
    except Exception:  # noqa: BLE001
        pass


def _trace_garmin_requests():
    """Wrap garminconnect's Client._run_request (full mode; idempotent). False when it is missing."""
    try:
        from garminconnect import client as gc_client
    except Exception:  # noqa: BLE001
        return False
    cls = getattr(gc_client, "Client", None)
    original = getattr(cls, "_run_request", None) if cls is not None else None
    if original is None:
        return False
    if not getattr(original, "_coach_traced", False):
        cls._run_request = _traced_run_request(original)
    return True


def _source_outcome(result):
    """coach.status.sources' outcome for one fetch_all result: ok, skipped, timeout or missing."""
    if not isinstance(result, dict):
        return "missing"
    if result.get("ok"):
        return "ok"
    error = str(result.get("error") or "")
    if error.startswith("skipped"):
        return "skipped"
    if error.startswith("timeout"):
        return "timeout"
    return "missing"


def _traced_source(name, fn):
    """fn run inside a "source <name>" span (in fetch_all's worker thread, which carries the tool span)."""

    def run(*args, **kwargs):
        tel = _TELEMETRY
        if tel is None:
            return fn(*args, **kwargs)
        try:
            span_cm = tel.span(f"source {name}", source=name)
        except Exception:  # noqa: BLE001
            span_cm = contextlib.nullcontext()
        with span_cm as span:
            try:
                value = fn(*args, **kwargs)
            except Exception as exc:
                if span is not None:
                    with contextlib.suppress(Exception):
                        group = _exception_group(exc)
                        span.set(outcome="missing", error_group=group)
                        if tool_errors.IS_ERROR.get(group, True):
                            span.error(group)
                raise
            if span is not None:
                with contextlib.suppress(Exception):
                    span.set(outcome="ok")
            return value

    return run


def _traced_fetch_all(original):
    """status_common.fetch_all wrapped: each call runs in a "source <name>" span, and every source,
    skipped ones too, counts once in coach.status.sources by outcome. Results are unchanged."""

    async def fetch_all(calls, *args, **kwargs):
        if _TELEMETRY is None:
            return await original(calls, *args, **kwargs)
        names = {name: (name if isinstance(name, str) and _SOURCE_RE.fullmatch(name) else "other")
                 for name in calls}
        wrapped = {name: (_traced_source(names[name], spec[0]), spec[1], spec[2]) for name, spec in calls.items()}
        results = await original(wrapped, *args, **kwargs)
        tel = _TELEMETRY
        if tel is not None:
            for name, result in results.items():
                with contextlib.suppress(Exception):
                    tel.metric_count("coach.status.sources", 1, source=names.get(name, "other"),
                                     outcome=_source_outcome(result))
        return results

    fetch_all._coach_traced = True
    fetch_all.__wrapped__ = original
    return fetch_all


def _trace_status_sources():
    """Wrap status_common.fetch_all (idempotent); daily_status and weekly_status look it up by attribute."""
    if not getattr(status_common.fetch_all, "_coach_traced", False):
        status_common.fetch_all = _traced_fetch_all(status_common.fetch_all)


def _install_tracing_hooks():
    """With tracing on: Garmin calls on upstream's timeout threads keep the trace context, full mode's
    Garmin requests get CLIENT spans and metrics, and status sources get spans and a counter. Each hook
    is a no-op while _TELEMETRY is None. Never raises."""
    for hook in (_bind_garmin_threads, _trace_status_sources) + (() if _KEY_MODE else (_trace_garmin_requests,)):
        try:
            hook()
        except Exception as exc:  # noqa: BLE001
            log.warning("tracing: a hook could not be installed (%s)", type(exc).__name__)


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
    secret value.

    Then the optional features (feature_config.problems(), and whether the
    image has a turned-on feature's packages). With ROLE=worker only
    GARMIN_AUTH and the key, REPORT_TZ and the features are checked (the
    worker serves no tools and no sign-in), with ROLE=enqueue only REPORT_TZ
    and the features; the allowed tools are then empty."""
    features = feature_config.load(environ)
    if features.role != "app":
        errors = key_mode.check(environ) if features.role == "worker" else []
        errors += status_common.check_report_tz(environ)
        errors += feature_config.problems(environ)
        errors += _package_errors(features)
        return frozenset(), errors
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
    errors += feature_config.problems(environ)
    errors += _package_errors(features)
    return allowed, errors


_has_module = feature_startup.has_module


def _package_errors(features):
    """[] or the lines for a turned-on feature whose packages this Python lacks (the image was built
    without them)."""
    errors = []
    if features.tracing and not _has_module("opentelemetry.sdk"):
        errors.append(feature_config.ERR_T7)
    if (features.history or features.history_forget) and not all(
            _has_module(m) for m in feature_config.HISTORY_PACKAGES):
        errors.append(feature_config.ERR_H11)
    return errors


def _check_config_cli(environ):
    try:
        allowed, errors = check_config(environ)
    except Exception as exc:  # noqa: BLE001 - one clear line, not a traceback
        errors = [f"the check could not run: {type(exc).__name__}: {exc}"]
    for line in errors:
        print(f"launch: config error: {line}", file=sys.stderr)
    if errors:
        return 1
    features = feature_config.load(environ)
    extra = features.summary()
    if features.role != "app":
        print(f"launch: config OK ({extra})", file=sys.stderr)
        return 0
    extra = f"; {extra}" if extra else ""
    own_writes = allowed & PLAN_TRACKING_TOOLS
    if own_writes:
        # PLAN_TRACKING=on: log_feedback writes the app's own database only; it is not a read-only tool.
        allowed = allowed - own_writes
        extra = f"; {', '.join(sorted(own_writes))} writes only the app's database{extra}"
    if _is_key_mode(environ):
        print(f"launch: config OK ({len(allowed)} read-only tools, Garmin Personal API key{extra})", file=sys.stderr)
    elif allowed & GARMIN_WRITE_TOOLS:
        writes = len(allowed & GARMIN_WRITE_TOOLS)
        print(f"launch: config OK ({len(allowed) - writes} read-only tools and {writes} plan write tools{extra})",
              file=sys.stderr)
    else:
        print(f"launch: config OK ({len(allowed)} read-only tools{extra})", file=sys.stderr)
    return 0


def patch(garmin_auth="full"):
    """Hook this repo's tools into upstream's tool registration.

    garmin_auth "key" registers the status tools in key mode (they skip the
    sources Garmin's Personal API does not serve); "full" (the default, and
    the Dockerfile's build check) registers them as they are and installs the
    Garmin request path guard on garminconnect's Client (_install_path_guard()).

    Raises, before changing anything in upstream, if a status module's
    selfcheck fails (an upstream helper it imports is gone) or a hook moved
    (the path guard's included, _check_path_guard_hook()),
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
    plan_engine.selfcheck()
    # The Garmin path guard's hook (garminconnect's Client._run_request), checked in both modes so the
    # Dockerfile's patch() fails the build when a garminconnect bump moves it.
    _check_path_guard_hook()
    # The SDK keeps the first tool registered under a name: an upstream tool
    # named like one of this repo's, registered before it, would be served in
    # its place, labelled read-only by the READ_ONLY_TOOLS merge below.
    taken = OWN_TOOLS & tool_annotations.ALL_ANNOTATED_TOOLS
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
        if _history_tools_wanted(_FEATURES):
            _register_history_tools(app, _FEATURES)
        if _plan_tools_wanted(_FEATURES):
            _register_plan_tools(app, _FEATURES, get_client)
        if _AUTH is not None:
            # The consent page and the root protected-resource metadata. app
            # is upstream's _ToolFilter, which passes custom_route() through.
            import owner_auth

            owner_auth.register_routes(app, _AUTH)
        return app

    # annotations_for() reads this module global at registration time.
    # Only the read tools: a write tool must never be labelled read-only.
    tool_annotations.READ_ONLY_TOOLS = tool_annotations.READ_ONLY_TOOLS | OWN_READ_TOOLS
    _upstream_write_tools()   # the snapshot, before anything below changes upstream's write sets
    if getattr(_FEATURES, "plan_writes", False) and not keyed:
        _merge_write_effects(tool_annotations, tracking=bool(plan_tracking_tools(_FEATURES, key_mode=keyed)))
    # PLAN_TRACKING=on: log_feedback is additive, idempotent and local-only (it writes only the app's database).
    # With the switches off the upstream sets stay untouched.
    tracking = plan_tracking_tools(_FEATURES, key_mode=keyed)
    if tracking:
        tool_annotations.ADDITIVE_TOOLS = tool_annotations.ADDITIVE_TOOLS | tracking
        tool_annotations.IDEMPOTENT_TOOLS = tool_annotations.IDEMPOTENT_TOOLS | tracking
        tool_annotations.LOCAL_ONLY_TOOLS = tool_annotations.LOCAL_ONLY_TOOLS | tracking
    courses.register_tools = register_tools
    if not keyed:
        # Full mode: no Garmin request leaves with a path a caller bent (key mode's client refuses its own).
        _install_path_guard()


# --- The optional features' startup (feature_startup.py) -----------------------------------------------------
# The pieces that read no state of this module are feature_startup's own functions under their old names here.
# The three that need this module's state (the call guard and telemetry, the refresh hook, _has_module) are
# small functions here that look that state up at call time, and _setup_features() calls them by name, so a
# test that sets launch._GUARD or patches launch._start_catch_up sees the same behaviour as before the split.
_optional = feature_startup.optional
_history_tools_wanted = feature_startup.history_tools_wanted
_register_history_tools = feature_startup.register_history_tools
_plan_tools_wanted = feature_startup.plan_tools_wanted
_register_plan_tools = feature_startup.register_plan_tools
_build_guard = feature_startup.build_guard
_forget_history = feature_startup.forget_history
_removed_text = feature_startup.removed_text
_run_role = feature_startup.run_role
_app_garmin_client = feature_startup.app_garmin_client
TOKEN_CLEANUP_HISTORY_OFF = feature_startup.TOKEN_CLEANUP_HISTORY_OFF
TOKEN_CLEANUP_KEY_MODE = feature_startup.TOKEN_CLEANUP_KEY_MODE
TOKEN_CLEANUP_NO_DRIVER = feature_startup.TOKEN_CLEANUP_NO_DRIVER


def _start_catch_up(features):
    """feature_startup.start_catch_up() with this process's call guard and telemetry module (the history
    startup thread: the migrations, then the catch-up jobs)."""
    return feature_startup.start_catch_up(features, _GUARD, _TELEMETRY)


def _setup_features(features, environ, allowed=frozenset()):
    """Startup of the optional features in the app (main()), in this order: their notes logged, the secrets
    registered for redaction, tracing set up, the secrets taken out of `environ`, the call guard built (with
    the refresh lock and the atomic token-file write in full mode), the history forgotten when asked, the catch-up started."""
    global _TELEMETRY, _GUARD

    for value in feature_config.secret_values(features):
        tool_errors.register_secret(value)
    if features.any_on or features.notes or features.history_forget:
        _configure_logging()
    for note in features.notes:
        log.info("%s", note)
    _TELEMETRY = None
    if features.tracing:
        module = _optional("telemetry")
        if module is not None:
            try:
                module.setup(features)
                # The served tool names may appear in spans; any other name reads "other".
                # The history tools too when history search is on, so their spans and coach.history.searches
                # read their names, not "other".
                module.register_tools(sorted(set(allowed) | (HISTORY_TOOLS if features.history else set())
                                             | (PLAN_READ_TOOLS if features.plan_tools else set())
                                             | plan_tracking_tools(features)
                                             | (PLAN_REVIEW_TOOLS if plan_tracking_tools(features) else set())))
                _TELEMETRY = module
                _install_tracing_hooks()
            except Exception as exc:  # noqa: BLE001
                log.error("tracing: setup failed (%s); tracing stays off", type(exc).__name__)
    feature_config.take_secrets(environ)
    _GUARD = _build_guard(features, environ)
    if not _KEY_MODE:
        garmin_guard.install_refresh_lock(_GUARD.refresh_lock)
        # garminconnect rewrites the token file after each refresh: atomically and owner-only from here on.
        garmin_guard.install_atomic_token_dump()
    forgot_ok = True
    if features.history_forget and features.database_url_set:
        forgot_ok = _forget_history(features)
    if features.history and features.role == "app":
        if not forgot_ok or features.history_forget:
            log.warning("history search: the history tools are off while HISTORY_FORGET is set")
        else:
            log.info("history search: on")
            if not _KEY_MODE:
                _start_token_publisher(features, environ)
            elif features.database_url_set:
                # Key mode needs no stored login: delete one a full-mode setup left behind.
                _start_token_cleanup(features, reason=TOKEN_CLEANUP_KEY_MODE)
            _start_catch_up(features)
    elif features.role == "app" and features.database_url_set:
        _start_token_cleanup(features)


def _refresh_app_token():
    """TokenPublisher's refresh hook: under the guard's refresh lock, refresh the app's Garmin login when its
    token expires within garminconnect's margin, so garminconnect rewrites garmin_tokens.json even while no
    tool call comes in. Calls the unwrapped _refresh_session (install_refresh_lock's wrapper takes the same
    lock, which is not reentrant). Never raises; a failure is one WARNING line with the class name."""
    guard = _GUARD
    client = _app_garmin_client()
    if guard is None or client is None:
        return
    method = type(client)._refresh_session
    refresh = getattr(method, "__wrapped__", method)
    try:
        with guard.refresh_lock:
            if getattr(client, "is_authenticated", True) and client._token_expires_soon():
                refresh(client)
    except Exception as exc:  # noqa: BLE001
        log.warning("history token publish: the token refresh failed (%s)", type(exc).__name__)


def _start_token_publisher(features, environ):
    """feature_startup.start_token_publisher() with _refresh_app_token as the publisher's refresh hook (full
    mode with history search on: the app's Garmin token file, sealed, to Postgres for the worker)."""
    return feature_startup.start_token_publisher(features, environ, _refresh_app_token)


def _start_token_cleanup(features, reason=TOKEN_CLEANUP_HISTORY_OFF):
    """feature_startup.start_token_cleanup() with this module's _has_module (the sealed Garmin token a
    full-mode history setup left in Postgres, deleted in a daemon thread)."""
    return feature_startup.start_token_cleanup(features, reason, has_module=_has_module)


# --- Logging (launch_logging.py) ------------------------------------------------------------------------------
LOG_FORMAT = launch_logging.LOG_FORMAT
_LineHandler = launch_logging._LineHandler
_configure_logging = launch_logging.configure_logging
_uvicorn_line_handler = launch_logging.uvicorn_line_handler
_split_uvicorn_logs = launch_logging.split_uvicorn_logs


# The sign-in's secret variables (auth_config reads them): their values are hidden in every log line.
AUTH_SECRET_ENVS = ("OWNER_SECRET", "MCP_BEARER_TOKEN")


def _register_auth_secrets(environ):
    """Register the values of AUTH_SECRET_ENVS with tool_errors.register_secret() (unset or short values are
    ignored there), so log_privacy and redact() replace them with "<redacted>". Never raises."""
    for name in AUTH_SECRET_ENVS:
        tool_errors.register_secret(environ.get(name))


def _exit_config_errors(errors):
    for line in errors:
        print(f"launch: config error: {line}", file=sys.stderr)
    sys.exit(1)


def main(argv=None):
    global _AUTH, _KEY_MODE, _FEATURES

    args = sys.argv[1:] if argv is None else list(argv)
    if args == ["--check-config"]:
        sys.exit(_check_config_cli(os.environ))
    if args[:1] == ["--plan-cleanup"]:
        # The owner's cleanup of the plan's own workouts (plan_writes.cleanup_cli); works with PLAN_WRITES off.
        import plan_writes

        log_privacy.quiet_libraries()
        mcp2_compat.install()
        sys.exit(plan_writes.cleanup_cli(os.environ, args[1:]))
    if args:
        sys.exit("usage: launch.py [--check-config | --plan-cleanup [--dry-run] [--orphans]]")

    # The optional features, again (entrypoint.sh ran --check-config first).
    errors = feature_config.problems(os.environ)
    if errors:
        _exit_config_errors(errors)
    _FEATURES = feature_config.load(os.environ)
    if _FEATURES.role != "app":
        sys.exit(_run_role(_FEATURES))

    # The sign-in secrets join the log redaction before anything can log them (auth_config reads them later).
    _register_auth_secrets(os.environ)
    # Before any Garmin call: garminconnect's own records (a failed call quotes the display name in its path
    # and Garmin's answer) become one fixed line, whatever handler the SDK adds later.
    log_privacy.quiet_libraries()
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
        # The key leaves os.environ (with GARMIN_PERSONAL_API_BASE_URL)
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

    # The call guard (every install) and the optional features: tracing, the
    # history forget and catch-up. The history tools register later, when
    # upstream's main() calls the courses hook (patch()).
    _setup_features(_FEATURES, os.environ, allowed)

    # Before upstream's main() builds the server: its name (SERVER_NAME), its
    # version (SERVER_VERSION), the instructions it sends at the handshake
    # (the mode's) and, with the sign-in on, the sign-in itself.
    _AUTH = None
    instructions = server_instructions.INSTRUCTIONS_KEY if _KEY_MODE else server_instructions.INSTRUCTIONS
    # PLAN_TRACKING=on: log_feedback writes the app's database, so "No tool changes any data." is false too.
    tracking = not _KEY_MODE and bool(plan_tracking_tools(_FEATURES))
    if tracking:
        instructions = getattr(server_instructions, "INSTRUCTIONS_TRACKING", instructions)
    if _FEATURES.plan_writes and not _KEY_MODE and allowed & PLAN_WRITE_TOOLS:
        # PLAN_WRITES=on: "No tool changes any data." is false now. The plan's workout writes leave this process
        # only through garmin_paths' method allowlist, admitted here.
        name = "INSTRUCTIONS_PLAN_TRACKING" if tracking else "INSTRUCTIONS_PLAN"
        instructions = getattr(server_instructions, name, instructions)
        garmin_paths.allow_plan_writes(set(garmin_paths.WRITE_PATHS))
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
    # uvicorn's INFO lines on stdout with a network transport (Railway shows stderr red); upstream's main()
    # starts uvicorn through the SDK after this point.
    _split_uvicorn_logs(os.environ.get("GARMIN_MCP_TRANSPORT"))
    from garmin_mcp import main as upstream_main

    upstream_main()


if __name__ == "__main__":
    main()
