"""The real MCP server in process, over the fake Garmin, with a frozen clock and a fixed internal-error ref.

start_server() builds the server exactly as the image does: launch.main() with this repo's code and upstream
garmin-mcp (the tool policy, the read-only gate, the tool-error wrapper, the description note, text hygiene, the
mode's server instructions), only MCPServer.run captured and the Garmin login answered by the fake (full mode:
upstream's background login returns FakeGarmin; key mode: the real key_mode.install() with a fake authenticator
and HTTP layer). Tools are listed and called with the official mcp client in memory (the 2026-07-28 era), so the
model sees exactly what a client of the server sees. No network, no port, no product change.

Patched for the life of the server, in the harness only:
- status_common.now_local / today_local: the frozen clock (the status tools' "today", the bad-input date hint);
- status_common._utc_stamp / weekly_status._utc_stamp: the frozen clock in UTC (sources' fetched_at);
- tool_errors.new_ref: FIXED_REF, so an internal error always reads "(ref 0badc0de)";
- with history other than "off": the history tools over an in-memory fixture (evals/harness/history.py).
One server per process at a time (upstream's client is a module global).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import dataclasses
import datetime as dt
import io
import logging
import os
import secrets
import sys
import tempfile
import threading
import time
from unittest import mock
from zoneinfo import ZoneInfo

from . import fake_garmin
from . import history as history_fixture
from .state import empty_state

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
EXT = os.path.join(REPO, "ext")

FIXED_REF = "0badc0de"
CALL_TIMEOUT_S = 60.0
LOGIN_WAIT_S = 30.0
MODES = ("full", "key")
# The modules upstream's main() configures with the Garmin client, in its order.
UPSTREAM_MODULES = (
    "activity_management", "health_wellness", "user_profile", "devices",
    "gear_management", "weight_management", "challenges", "training", "workouts",
    "data_management", "womens_health", "nutrition", "workout_builders", "courses",
    "activity_analysis", "calendar_events",
)
# Settings a server run may see; any other value the eval process has is removed for the run.
_CLEARED_ENV = ("GARMIN_ENABLED_TOOLS", "GARMIN_DISABLED_TOOLS", "GARMIN_PERSONAL_API_KEY",
                "GARMIN_PERSONAL_API_BASE_URL", "GARMIN_IS_CN", "GARMIN_KEY_TOKEN_CACHE", "EDGE_CLIENT_IP_HEADER",
                "PUBLIC_URL", "RAILWAY_PUBLIC_DOMAIN", "OWNER_SECRET", "MCP_BEARER_TOKEN", "AUTH_DB",
                "ALLOWED_REDIRECT_URIS", "GARMIN_EMAIL", "GARMIN_PASSWORD")
_QUIET_LOGGERS = ("mcp", "garmin_mcp", "garminconnect", "garmin_personal_api", "httpx", "anyio")

_ONE_SERVER = threading.Lock()


@dataclasses.dataclass(frozen=True)
class ToolOutcome:
    """One tool call as the model sees it: the result text and the MCP isError flag."""

    text: str
    is_error: bool


def ext_modules():
    """Import ext/ as launch.py does (its folder on sys.path, mcp2_compat's finder first) and return the
    modules the harness uses."""
    if EXT not in sys.path:
        sys.path.insert(0, EXT)
    import mcp2_compat

    mcp2_compat.register_finder()
    import key_mode
    import launch
    import server_instructions
    import status_common
    import tool_errors
    import weekly_status

    return dataclasses.make_dataclass("Ext", ["launch", "key_mode", "status_common", "tool_errors",
                                              "weekly_status", "server_instructions", "mcp2_compat"])(
        launch, key_mode, status_common, tool_errors, weekly_status, server_instructions, mcp2_compat)


def frozen_now(now, tz):
    """`now` ("2026-03-18T06:30", local time in `tz`; an offset is converted) as an aware datetime in `tz`."""
    zone = ZoneInfo(tz)
    moment = dt.datetime.fromisoformat(now)
    return moment.replace(tzinfo=zone) if moment.tzinfo is None else moment.astimezone(zone)


def clock_line(now, tz):
    """The one line every variant's system prompt starts with, e.g. "Current date and time: Wednesday,
    18 March 2026, 06:30 (Europe/Athens, UTC+02:00)." (claude.ai gives the model the same facts)."""
    moment = frozen_now(now, tz) if isinstance(now, str) else now.astimezone(ZoneInfo(tz))
    offset = moment.strftime("%z")
    offset = f"UTC{offset[:3]}:{offset[3:]}" if offset else "UTC"
    return (f"Current date and time: {moment.strftime('%A')}, {moment.day} {moment.strftime('%B %Y')}, "
            f"{moment.strftime('%H:%M')} ({tz}, {offset}).")


class _Loop:
    """An asyncio loop on its own thread, so call() works whether or not the caller runs a loop."""

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, name="eval-mcp-client", daemon=True)
        self.thread.start()

    def run(self, coro, timeout):
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        try:
            return future.result(timeout)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise TimeoutError(f"tool call did not finish within {timeout:g} s") from None

    def close(self):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)
        if not self.thread.is_alive():
            self.loop.close()


class _Records(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines = []

    def emit(self, record):
        try:
            self.lines.append(f"{record.levelname} {record.name}: {record.getMessage()}")
        except Exception:  # noqa: BLE001 - a log line must never fail a tool call
            pass


class Server:
    """A running eval server (start_server() yields it)."""

    def __init__(self, mcp_server, loop, log, records, mode, now, tz, timeout):
        self._server = mcp_server
        self._loop = loop
        self._log = log
        self._records = records
        self.mode = mode
        self.now = now
        self.tz = tz
        self.timeout = timeout
        self.tools, self.instructions = self._loop.run(self._discover(), timeout)

    async def _discover(self):
        from mcp.client import Client

        async with Client(self._server, mode="auto") as client:
            instructions = client.session.discover_result.instructions or ""
            listed = await client.list_tools()
        tools = [{"name": t.name, "description": t.description or "", "input_schema": t.input_schema}
                 for t in listed.tools]
        return tools, instructions

    async def _call(self, name, args):
        from mcp.client import Client
        from mcp.shared.exceptions import MCPError

        async with Client(self._server, mode="auto") as client:
            try:
                return await client.call_tool(name, args)
            except MCPError as exc:
                return exc

    def call(self, name, args):
        """Call tool `name` with `args` through the server, as the model's tool_use would: a ToolOutcome.
        Raises TimeoutError after `timeout` seconds (60 by default)."""
        from mcp.shared.exceptions import MCPError

        self._log.tool = name
        try:
            result = self._loop.run(self._call(name, dict(args or {})), self.timeout)
        finally:
            self._log.tool = None
        if isinstance(result, MCPError):
            return ToolOutcome(text=f"Error: {result}", is_error=True)
        parts = []
        for block in result.content or ():
            text = getattr(block, "text", None)
            parts.append(text if getattr(block, "type", None) == "text" and isinstance(text, str)
                         else f"[{getattr(block, 'type', 'unknown')} content]")
        return ToolOutcome(text="\n".join(parts), is_error=bool(result.is_error))

    @property
    def calls(self):
        """Every Garmin client call so far, in order: {"tool", "method", "args", "kwargs"} (JSON-safe)."""
        return list(self._log.calls)

    @property
    def missing(self):
        """Client names a tool asked for that the client does not have: {"tool", "method"}."""
        return list(self._log.missing)

    @property
    def log_lines(self):
        """The server's own log lines (garmin_ext.*), e.g. "WARNING garmin_ext.tools: tool error: ..."."""
        return list(self._records.lines)

    @property
    def clock_line(self):
        return clock_line(self.now, self.tz)


@contextlib.contextmanager
def start_server(mode, state, now, tz, *, timeout=CALL_TIMEOUT_S, history="off"):
    """Run the server in `mode` ("full" or "key") over the fake Garmin `state` (load_state()'s result, or None
    for no data), with the clock frozen at `now` ("2026-03-18T06:30", local time) in zone `tz` (also the
    server's REPORT_TZ). history: a fixture of evals/harness/history.py ("athlete", "partial", "two_weeks",
    "mismatch") adds find_similar_weeks and search_history over it; "off" (the default) leaves them out.
    Yields a Server: .tools, .instructions, .call(name, args), .calls."""
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, got {mode!r}")
    if history not in history_fixture.FIXTURES:
        raise ValueError(f"history must be one of {history_fixture.FIXTURES}, got {history!r}")
    moment = frozen_now(now, tz)
    if not _ONE_SERVER.acquire(blocking=False):
        raise RuntimeError("an eval server is already running in this process (one at a time)")
    try:
        with contextlib.ExitStack() as stack:
            yield _start(stack, mode, state if state is not None else empty_state(), moment, tz, timeout, history)
    finally:
        _ONE_SERVER.release()


def _start(stack, mode, state, moment, tz, timeout, history="off"):
    ext = ext_modules()
    import garmin_mcp
    from garmin_mcp import courses, tool_annotations
    from mcp.server.mcpserver import MCPServer

    keyed = mode == "key"
    env = {"GARMIN_AUTH": mode, "GARMIN_MCP_TRANSPORT": "stdio", "GARMIN_MCP_STATELESS_HTTP": "false",
           "REPORT_TZ": tz}
    stack.enter_context(mock.patch.dict(os.environ, env))
    for name in _CLEARED_ENV:
        os.environ.pop(name, None)
    if keyed:
        folder = stack.enter_context(tempfile.TemporaryDirectory(prefix="eval-key-"))
        os.environ["GARMIN_PERSONAL_API_KEY"] = "EVAL-ONLY-" + secrets.token_urlsafe(24)
        os.environ["GARMIN_KEY_TOKEN_CACHE"] = os.path.join(folder, "garmin-key", "token.json")

    records = _Records()
    _quiet_logging(stack, records)

    # The clock and the ref.
    utc_stamp = moment.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat()
    sc = ext.status_common
    stack.enter_context(mock.patch.object(sc, "now_local", lambda zone: moment.astimezone(zone)))
    stack.enter_context(mock.patch.object(sc, "today_local", lambda zone: moment.astimezone(zone).date()))
    stack.enter_context(mock.patch.object(sc, "_utc_stamp", lambda: utc_stamp))
    stack.enter_context(mock.patch.object(ext.weekly_status, "_utc_stamp", lambda: utc_stamp))
    stack.enter_context(mock.patch.object(ext.tool_errors, "new_ref", lambda: FIXED_REF))
    if history != "off":
        history_fixture.install(stack, ext.launch, history, moment.date(), keyed)

    # Upstream's globals and launch's, restored when the server stops.
    saved = (courses.register_tools, tool_annotations.READ_ONLY_TOOLS, tool_annotations.ADDITIVE_TOOLS,
             garmin_mcp.FastMCP, garmin_mcp.init_api)
    clients = {name: getattr(getattr(garmin_mcp, name), "garmin_client", None) for name in UPSTREAM_MODULES}

    def restore():
        (courses.register_tools, tool_annotations.READ_ONLY_TOOLS, tool_annotations.ADDITIVE_TOOLS,
         garmin_mcp.FastMCP, garmin_mcp.init_api) = saved
        for name, client in clients.items():
            getattr(garmin_mcp, name).garmin_client = client
        ext.launch._AUTH, ext.launch._KEY_MODE = None, False
        ext.mcp2_compat.set_server_overrides()
        ext.mcp2_compat.set_asgi_middleware()

    stack.callback(restore)

    log = fake_garmin.CallLog(state.get("faults"), keyed)
    served = []
    stdout, root = sys.stdout, logging.getLogger()
    root_state = (root.handlers[:], root.level)
    with contextlib.ExitStack() as boot:
        boot.enter_context(mock.patch.object(MCPServer, "run", lambda server, *a, **k: served.append(server)))
        boot.enter_context(contextlib.redirect_stderr(io.StringIO()))
        if keyed:
            boot.enter_context(mock.patch.object(ext.key_mode, "install", _key_install(ext.key_mode, state, log)))
        else:
            start = garmin_mcp._PendingGarminClient.start
            client = fake_garmin.RecordingClient(fake_garmin.FakeGarmin(state), log)
            boot.enter_context(mock.patch.object(garmin_mcp._PendingGarminClient, "start",
                                                 lambda pending, login: start(pending, lambda: client)))
        try:
            ext.launch.main([])
        finally:
            sys.stdout = stdout  # upstream's main() filters stdout to its own thread; no stdio transport here
            root.handlers[:] = root_state[0]  # key mode's main() adds a stderr handler and sets INFO
            root.setLevel(root_state[1])
        pending = garmin_mcp.activity_management.garmin_client._client
        deadline = time.monotonic() + LOGIN_WAIT_S
        while pending.login_state() == "pending" and time.monotonic() < deadline:
            time.sleep(0.01)
    if pending.login_state() != "ready" or len(served) != 1:
        raise RuntimeError(f"the eval server did not start (login {pending.login_state()}, "
                           f"{len(served)} server(s) built)")
    loop = _Loop()
    stack.callback(loop.close)
    return Server(served[0], loop, log, records, mode, moment, tz, timeout)


def _key_install(key_mode, state, log):
    """key_mode.install() with Garmin's client replaced by the fakes, and the client it logs in with wrapped
    in the RecordingClient."""
    real_install = key_mode.install

    def install(key, cache_file=None, **kwargs):
        import garmin_mcp

        tokens = real_install(key, cache_file, authenticator=fake_garmin.FakeAuthenticator(time.time),
                              http=fake_garmin.FakePersonalApiHttp(state, log.slot))
        key_login = garmin_mcp.init_api

        def init_api(email=None, password=None):
            return fake_garmin.RecordingClient(key_login(email, password), log)

        garmin_mcp.init_api = init_api
        return tokens

    return install


def _quiet_logging(stack, records):
    """The server's own lines (garmin_ext.*) go to `records` only; the libraries' are dropped."""
    loggers = [(logging.getLogger("garmin_ext"), records)]
    loggers += [(logging.getLogger(name), logging.NullHandler()) for name in _QUIET_LOGGERS]
    for logger, handler in loggers:
        saved = (logger.handlers[:], logger.propagate, logger.level)

        def restore(logger=logger, saved=saved):
            logger.handlers[:], logger.propagate = saved[0], saved[1]
            logger.setLevel(saved[2])

        stack.callback(restore)
        logger.handlers[:] = [handler]
        logger.propagate = False
        if logger.name == "garmin_ext":
            logger.setLevel(logging.INFO)
