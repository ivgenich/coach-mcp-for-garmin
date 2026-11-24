"""Run upstream garmin-mcp, written for mcp 1.x's FastMCP, on mcp 2.x.

mcp 2.x renamed `mcp.server.fastmcp.FastMCP` to `mcp.server.mcpserver.MCPServer`
and left `mcp.server.fastmcp` as a module that only raises
ModuleNotFoundError. It also moved host, port, stateless_http and the other
HTTP settings from the constructor to `run()`. Upstream (garmin_mcp, pinned in
the Dockerfile) still does

    from mcp.server.fastmcp import FastMCP
    FastMCP("Garmin Connect v1.0", host=..., port=..., stateless_http=...)
    app.tool(..., structured_output=False, annotations=...)
    app.resource("workout://..."); app.custom_route("/healthz", ...)
    app.run(transport=transport)

install() puts this module in sys.modules["mcp.server.fastmcp"], so that code
gets the `FastMCP` below: an MCPServer subclass whose constructor takes the
1.x keyword arguments and whose run() hands the HTTP ones to MCPServer.run().
tool(), resource(), custom_route(), list_tools() and the rest are MCPServer's
own, with the same signatures and semantics as 1.x for what upstream uses.

Parity with FastMCP 1.x that matters here:
- json_response defaults to False (SSE-capable responses), as in 1.x. In the
  2026-07-28 era the SDK answers with plain JSON when a handler finishes within
  its ping window and only switches to SSE (with keepalive pings) for a slow
  one, which is what a slow Garmin call behind Caddy needs.
- host is forwarded, so binding 127.0.0.1 turns on the same DNS-rebinding
  protection as 1.x (Host must be 127.0.0.1:*/localhost:*/[::1]:*, no foreign
  Origin). Caddy already rewrites Host to 127.0.0.1:8000 and drops Origin.
- serverInfo.version defaults to the installed mcp version, as 1.x did
  (2.x would report an empty string), for a caller that sets no version.
- set_server_overrides() sets constructor arguments that win over the caller's.
  launch.py uses it before upstream's main() builds the server, so serverInfo
  carries this project's name instead of upstream's "Garmin Connect v1.0" and
  this project's version (version.VERSION, not the mcp version), and the
  built-in sign-in reaches the server (auth=..., auth_server_provider=...).
- A tool that raises keeps its message: 1.x answered "Error executing tool
  <name>: <message>", 2.x sends only "Error executing tool <name>" for any
  exception but ToolError. tool() wraps each function so other exceptions are
  re-raised as ToolError(message), e.g. "Garmin login failed. Run
  'garmin-mcp-auth' ...". functools.wraps keeps name, docstring and signature,
  so input schemas do not change. (On the server launch.py builds, its
  call_tool() override then answers every failure with a fixed text and
  logs it once; the adapter logs the exception at DEBUG only.)

Two additions that 1.x did not have:
- set_asgi_middleware() wraps every HTTP app the adapter builds (streamable
  HTTP and SSE) in ASGI middleware, outside the SDK's own authentication
  middleware, so it sees every request first: /mcp, the OAuth endpoints and
  the custom routes alike. launch.py puts the sign-in rate limit there.
- The tool loop. Upstream's async tools block: each Garmin call joins a worker
  thread for up to GARMIN_MCP_CALL_TIMEOUT (90 s by default). On the server's
  event loop that would also stall the sign-in endpoints (/token must answer
  in seconds) and every other request. So every async tool that takes no
  Context parameter runs on one shared background event loop, in a daemon
  thread named "mcp2-compat-tool-loop"; the server's loop only awaits the
  result. One loop keeps upstream's Garmin calls one at a time, as before.
  Sync tools already run in worker threads (the SDK does that) and tools that
  take a Context stay on the server's loop. Contextvars are copied to the tool
  loop with each call, but a tool there must not use the request context
  (progress, logging, sampling): a tool that needs it takes a Context
  parameter and so stays on the server's loop. An async tool marked with
  server_loop() also stays there: one that never blocks its loop (it awaits
  each Garmin call in a worker thread, as status_common.fetch_all does) and
  times those calls with its loop's clock. On the tool loop, time another
  tool spent blocking that loop would count against its timeouts.

install() raises ShimError, before anything is patched, if the installed mcp
is not 2.x or MCPServer lacks what this adapter calls, so the Docker build
(launch.patch()) fails instead of the live server.

launch.py calls install() itself. Other entry points import garmin_mcp too,
above all upstream's garmin-mcp-auth script (the one-time Garmin login over
railway ssh), so the Dockerfile also writes a .pth file into site-packages that
runs register_finder() at interpreter start. That only adds an import hook;
mcp is imported, and install() runs, the first time anything imports
mcp.server.fastmcp. This module imports nothing from mcp, and not asyncio, at
import time, so that start stays cheap and works on any Python 3.10+.
"""

from __future__ import annotations

import sys
import threading
import types

_TARGET = "mcp.server.fastmcp"
_MARKER = "__garmin_mcp2_compat__"

# MCPServer methods upstream and this repo call; run_streamable_http_async
# must accept every keyword FastMCP.run() forwards to it, and it and
# run_sse_async must build their app through streamable_http_app() and
# sse_app(), which the adapter overrides to add the ASGI middleware.
_REQUIRED_METHODS = (
    "tool", "resource", "custom_route", "run", "list_tools", "run_streamable_http_async",
    "streamable_http_app", "sse_app",
)
_HTTP_KWARGS = ("host", "port", "streamable_http_path", "json_response", "stateless_http", "transport_security")
_SSE_KWARGS = ("host", "port", "sse_path", "message_path", "transport_security")
# MCPServer() keywords that launch.py passes through set_server_overrides().
_INIT_KWARGS = ("name", "instructions", "version", "auth", "auth_server_provider")

# Constructor arguments that win over the caller's; see set_server_overrides().
_server_overrides = {}
# ASGI middleware around every HTTP app; see set_asgi_middleware().
_asgi_middleware = []

# The tool loop (see the module docstring and _ToolLoop).
TOOL_LOOP_THREAD_NAME = "mcp2-compat-tool-loop"
# Set on every wrapper tool() registers: True when the tool runs on the tool
# loop. Tests use it to prove that upstream's real tools take that path.
TOOL_LOOP_MARKER = "__mcp2_compat_tool_loop__"
# Set by server_loop() on a tool function: the tool stays on the server's loop.
SERVER_LOOP_MARKER = "__mcp2_compat_server_loop__"
# Guards _tool_loop and each _ToolLoop's pending set and stopped flag.
_tool_loop_lock = threading.Lock()
_tool_loop = None


class ShimError(RuntimeError):
    """The installed mcp SDK is not one this adapter supports."""


def set_server_overrides(**overrides):
    """Make every FastMCP built from now on take these constructor arguments
    over the caller's own, e.g. name=... (launch.py: this project's name, not
    upstream's), instructions=..., or auth=AuthSettings(...) together with
    auth_server_provider=... Replaces earlier overrides; no arguments clears
    them. A keyword the constructor does not take raises TypeError when a
    server is built, as it would from the caller."""
    _server_overrides.clear()
    _server_overrides.update(overrides)


def server_loop(fn):
    """Mark the async tool `fn` to run on the server's event loop, not the
    shared tool loop; returns fn itself (use it under @app.tool()).

    Only for a tool that never blocks the loop it runs on: every slow step
    (each Garmin call) is awaited in a worker thread. Such a tool times its
    calls with its loop's clock (asyncio.wait_for, a report deadline). On the
    tool loop, upstream's async tools block the loop's thread for each Garmin
    call, so a slow upstream tool called in parallel would count against
    those timers although Garmin answered in time. The input schema does not
    change. A no-op for sync tools and under mcp 1.x."""
    setattr(fn, SERVER_LOOP_MARKER, True)
    return fn


def set_asgi_middleware(*middleware):
    """Wrap the HTTP app of every FastMCP built from now on in these ASGI
    middleware.

    Each entry is an ASGI middleware class, or a (class, options) pair where
    options is a dict of keyword arguments; the app is built as
    cls(app, **options). The first entry is the outermost. They run inside
    Starlette's error handler and outside the SDK's own middleware
    (authentication), so they see every request first. Replaces earlier
    entries; no arguments clears them. A bad entry raises TypeError and
    leaves the earlier list in place.
    """
    entries = []
    for item in middleware:
        if isinstance(item, tuple):
            if len(item) != 2:
                raise TypeError(f"mcp2_compat: a middleware entry must be a class or a (class, options) pair, got {item!r}")
            cls, options = item
        else:
            cls, options = item, {}
        if not isinstance(cls, type):
            raise TypeError(f"mcp2_compat: ASGI middleware must be a class, got {cls!r}")
        if not isinstance(options, dict) or not all(isinstance(key, str) for key in options):
            raise TypeError(f"mcp2_compat: options for {cls.__name__} must be a dict of keyword arguments, got {options!r}")
        entries.append((cls, dict(options)))
    _asgi_middleware[:] = entries


def _add_asgi_middleware(app):
    # Starlette.add_middleware() puts each new entry outermost, so adding them
    # in reverse leaves the first entry outermost. It refuses once the app has
    # served a request, which cannot have happened yet here.
    for cls, options in reversed(list(_asgi_middleware)):
        app.add_middleware(cls, **options)
    return app


def _check_sdk():
    import importlib.metadata
    import inspect

    try:
        version = importlib.metadata.version("mcp")
    except importlib.metadata.PackageNotFoundError as exc:
        raise ShimError("mcp2_compat: the 'mcp' package is not installed") from exc
    if version.split(".", 1)[0] != "2":
        raise ShimError(
            f"mcp2_compat: needs mcp 2.x, found mcp {version}. constraints.txt pins mcp==2.3.0; "
            "rebuild the image, or drop ext/mcp2_compat.py if going back to mcp 1.x."
        )
    try:
        from mcp.server.mcpserver import Context, MCPServer
    except ImportError as exc:
        raise ShimError(f"mcp2_compat: mcp {version} has no mcp.server.mcpserver.MCPServer ({exc})") from exc
    missing = [m for m in _REQUIRED_METHODS if not callable(getattr(MCPServer, m, None))]
    if missing:
        raise ShimError(f"mcp2_compat: mcp {version} MCPServer lacks {', '.join(missing)}")
    init_params = inspect.signature(MCPServer.__init__).parameters
    absent = [k for k in _INIT_KWARGS if k not in init_params]
    if absent:
        raise ShimError(
            f"mcp2_compat: mcp {version} MCPServer() takes no {', '.join(absent)}; "
            "launch.py passes these through set_server_overrides()"
        )
    for method, needed in (("run_streamable_http_async", _HTTP_KWARGS), ("run_sse_async", _SSE_KWARGS)):
        params = inspect.signature(getattr(MCPServer, method)).parameters
        absent = [k for k in needed if k not in params]
        if absent:
            raise ShimError(f"mcp2_compat: mcp {version} MCPServer.{method}() takes no {', '.join(absent)}")
    tool_params = inspect.signature(MCPServer.tool).parameters
    absent = [k for k in ("name", "annotations", "structured_output") if k not in tool_params]
    if absent:
        raise ShimError(f"mcp2_compat: mcp {version} MCPServer.tool() takes no {', '.join(absent)}")
    try:  # the SDK's own test for a Context parameter, which decides the tool loop
        from mcp.server.mcpserver.utilities.context_injection import find_context_parameter  # noqa: F401
    except ImportError as exc:
        raise ShimError(f"mcp2_compat: mcp {version} has no find_context_parameter ({exc})") from exc
    return version, MCPServer, Context


class _ToolLoop:
    """One asyncio event loop, run forever by a daemon thread.

    A loop that stops (loop.stop(), or an exception that escaped
    run_forever()) is not restarted: its thread ends, any call still waiting
    on it fails with RuntimeError instead of hanging, and the next call
    starts a new _ToolLoop (_submit_to_tool_loop()).
    """

    def __init__(self):
        import asyncio

        self.loop = asyncio.new_event_loop()
        self.pending = set()  # the futures callers await, not finished yet
        self.stopped = False
        ready = threading.Event()
        self.thread = threading.Thread(target=self._serve, args=(ready,), name=TOOL_LOOP_THREAD_NAME, daemon=True)
        self.thread.start()
        if not ready.wait(10):
            raise RuntimeError("mcp2_compat: the tool loop thread did not start")

    def usable(self):
        return not self.stopped and self.thread.is_alive() and self.loop.is_running()

    def forget(self, future):
        with _tool_loop_lock:
            self.pending.discard(future)

    def _serve(self, ready):
        import asyncio
        from concurrent.futures import InvalidStateError

        asyncio.set_event_loop(self.loop)
        self.loop.call_soon(ready.set)
        try:
            self.loop.run_forever()
        finally:
            with _tool_loop_lock:
                self.stopped = True
                stranded, self.pending = self.pending, set()
            for future in stranded:
                if not future.done():
                    try:
                        future.set_exception(RuntimeError("the tool loop stopped before this call finished; try again"))
                    except InvalidStateError:  # finished or cancelled meanwhile
                        pass
            self.loop.close()


def _submit_to_tool_loop(coro, result):
    """Schedule `coro` on the tool loop; the run_coroutine_threadsafe future
    of its task. `result` is the concurrent.futures.Future the caller awaits:
    it goes into the loop's pending set, so a call the loop strands when it
    stops fails instead of hanging (F9).

    Starts the loop on first use, and replaces it first when its thread died
    or it stopped (F9), all under the lock.
    """
    import asyncio

    global _tool_loop
    with _tool_loop_lock:
        tool_loop = _tool_loop
        if tool_loop is None or not tool_loop.usable():
            tool_loop = _tool_loop = _ToolLoop()
        task_future = asyncio.run_coroutine_threadsafe(coro, tool_loop.loop)
        tool_loop.pending.add(result)
    # Outside the lock: an already finished future runs the callback at once.
    result.add_done_callback(tool_loop.forget)
    return task_future


def _publish(result, value=None, exception=None):
    """Finish `result` with a value or an exception; a no-op once it is done
    (the caller cancelled it, or F9 failed it because the loop stopped)."""
    from concurrent.futures import InvalidStateError

    try:
        if exception is not None:
            result.set_exception(exception)
        else:
            result.set_result(value)
    except InvalidStateError:
        pass


def _copy_outcome(task_future, result):
    """Give `result` the task's outcome when _guarded() did not publish one:
    the task was cancelled before it started, or the tool raised
    CancelledError itself."""
    if result.done():
        return
    if task_future.cancelled():
        result.cancel()
    elif task_future.exception() is not None:
        _publish(result, exception=task_future.exception())
    else:
        _publish(result, value=task_future.result())


async def _guarded(awaitable, result):
    """Await `awaitable` on the tool loop and publish its outcome to `result`
    from right here. Left to the task's done callback, which the loop runs
    only after the callbacks already queued, an answer would wait for the
    next call's blocking step when two calls arrive together (V2-2).

    A SystemExit, KeyboardInterrupt or other BaseException that is not an
    Exception would escape run_forever() and stop the loop: it is published
    as RuntimeError instead (F9). Nothing is raised again after publishing;
    the caller's thread raises it, and raising the same exception here too
    would change its traceback from two threads. CancelledError (cancelling
    the caller cancels the tool) and GeneratorExit (closing the coroutine)
    pass through unchanged, and _copy_outcome() hands them on."""
    import asyncio

    try:
        value = await awaitable
    except (asyncio.CancelledError, GeneratorExit):
        raise
    except Exception as exc:
        _publish(result, exception=exc)
    except BaseException as exc:
        text = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
        error = RuntimeError(text)
        error.__cause__ = exc
        _publish(result, exception=error)
    else:
        _publish(result, value=value)


async def _on_tool_loop(awaitable):
    """Run `awaitable` (a tool's coroutine) on the tool loop and await its
    result from the caller's loop. Cancelling the caller cancels it there."""
    import asyncio
    from concurrent.futures import Future

    result = Future()  # never set running, so the caller can always cancel it
    coro = _guarded(awaitable, result)
    try:
        task_future = _submit_to_tool_loop(coro, result)
    except BaseException:
        coro.close()  # never started; close both so neither warns "never awaited"
        if asyncio.iscoroutine(awaitable):
            awaitable.close()
        raise

    def cancel_the_task(done):
        if done.cancelled():
            task_future.cancel()  # the task is cancelled on the tool loop

    result.add_done_callback(cancel_the_task)
    task_future.add_done_callback(lambda task: _copy_outcome(task, result))
    return await asyncio.wrap_future(result)


def _keep_error_text(fn, passthrough, ToolError, find_context_parameter=None):
    """fn, with any exception not in `passthrough` re-raised as ToolError(str(exc)).

    With `find_context_parameter` (the SDK's), an async fn that takes no
    Context parameter and is not marked by server_loop() also runs on the
    tool loop. The wrapper's TOOL_LOOP_MARKER attribute says which path it
    takes.
    """
    import functools
    import logging

    try:  # the SDK's own test, which also sees async __call__ objects and partials
        from mcp.shared._callable_inspection import is_async_callable
    except ImportError:  # pragma: no cover - private helper moved
        import inspect

        is_async_callable = inspect.iscoroutinefunction

    log = logging.getLogger("garmin_ext.tools")
    name = getattr(fn, "__name__", repr(fn))

    def convert(exc):
        # The client gets the message (or the exception type when the message
        # is empty, e.g. a bare TimeoutError()). DEBUG only: launch.py's
        # call_tool() override logs each failed call once, with its group and,
        # for an internal error, the traceback; an ERROR here as well would be
        # a second record for the same failure.
        log.debug("Tool %r failed", name, exc_info=exc)
        return ToolError(str(exc) or type(exc).__name__)

    on_tool_loop = False
    if is_async_callable(fn):
        on_tool_loop = (find_context_parameter is not None and find_context_parameter(fn) is None
                        and not getattr(fn, SERVER_LOOP_MARKER, False))

        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            try:
                if on_tool_loop:
                    return await _on_tool_loop(fn(*args, **kwargs))
                return await fn(*args, **kwargs)
            except passthrough:
                raise
            except Exception as exc:
                raise convert(exc) from exc

    else:

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except passthrough:
                raise
            except Exception as exc:
                raise convert(exc) from exc

    setattr(wrapper, TOOL_LOOP_MARKER, on_tool_loop)
    return wrapper


def _build(version, MCPServer):
    import functools

    from mcp.server.mcpserver.exceptions import ResourceError, ToolError
    from mcp.server.mcpserver.utilities.context_injection import find_context_parameter
    from mcp.shared.exceptions import MCPError

    # ToolError/ResourceError already reach the client with their text; an
    # MCPError is a protocol error the SDK must see unchanged.
    passthrough = (ToolError, ResourceError, MCPError)

    class FastMCP(MCPServer):
        """mcp 1.x FastMCP's constructor and run() over mcp 2.x MCPServer."""

        @functools.wraps(MCPServer.tool)
        def tool(self, *args, **kwargs):
            register = super().tool(*args, **kwargs)

            def decorator(fn):
                register(_keep_error_text(fn, passthrough, ToolError, find_context_parameter))
                return fn  # as MCPServer.tool does: the caller keeps the original

            return decorator

        def __init__(self, name=None, instructions=None, **kwargs):
            # set_server_overrides() wins over the caller (upstream's main()).
            self._compat_init(**{"name": name, "instructions": instructions, **kwargs, **_server_overrides})

        def _compat_init(
            self,
            name=None,
            instructions=None,
            *,
            host="127.0.0.1",
            port=8000,
            streamable_http_path="/mcp",
            json_response=False,
            stateless_http=False,
            sse_path="/sse",
            message_path="/messages/",
            transport_security=None,
            **kwargs,
        ):
            kwargs.setdefault("version", version)
            # Anything else goes to MCPServer as is; a 1.x-only keyword raises
            # TypeError there instead of being silently dropped.
            super().__init__(name, instructions=instructions, **kwargs)
            self._compat_http = {
                "host": host,
                "port": port,
                "streamable_http_path": streamable_http_path,
                "json_response": json_response,
                "stateless_http": stateless_http,
                "transport_security": transport_security,
            }
            self._compat_sse = {
                "host": host,
                "port": port,
                "sse_path": sse_path,
                "message_path": message_path,
                "transport_security": transport_security,
            }

        # MCPServer.run_streamable_http_async() and run_sse_async() build the
        # app uvicorn serves through these two methods on self
        # (mcpserver/server.py:1096, :1131), so set_asgi_middleware() reaches it.
        @functools.wraps(MCPServer.streamable_http_app)
        def streamable_http_app(self, **kwargs):
            return _add_asgi_middleware(super().streamable_http_app(**kwargs))

        @functools.wraps(MCPServer.sse_app)
        def sse_app(self, **kwargs):
            return _add_asgi_middleware(super().sse_app(**kwargs))

        def run(self, transport="stdio", mount_path=None):
            if mount_path is not None:
                raise TypeError("mcp2_compat: run(mount_path=...) has no mcp 2.x equivalent")
            if transport == "streamable-http":
                return super().run("streamable-http", **self._compat_http)
            if transport == "sse":
                return super().run("sse", **self._compat_sse)
            return super().run(transport)

    FastMCP.__module__ = _TARGET
    FastMCP.__qualname__ = "FastMCP"
    return FastMCP


def install():
    """Make `from mcp.server.fastmcp import FastMCP` return the adapter.

    Idempotent; the SDK check runs on every call, so a later call still
    fails loudly on an unsupported mcp. Call before anything imports garmin_mcp.
    """
    version, MCPServer, Context = _check_sdk()
    current = sys.modules.get(_TARGET)
    if current is not None and getattr(current, _MARKER, False):
        return current
    import mcp.server

    module = types.ModuleType(_TARGET, __doc__)
    module.FastMCP = _build(version, MCPServer)
    module.Context = Context
    module.__all__ = ["FastMCP", "Context"]
    setattr(module, _MARKER, True)
    sys.modules[_TARGET] = module
    mcp.server.fastmcp = module
    return module


def _mcp_major():
    import importlib.metadata

    try:
        return importlib.metadata.version("mcp").split(".", 1)[0]
    except importlib.metadata.PackageNotFoundError:
        return None


class _Finder:
    """sys.meta_path hook: serves mcp.server.fastmcp from install()."""

    def find_spec(self, fullname, path=None, target=None):
        if fullname != _TARGET or _mcp_major() != "2":
            # mcp 1.x has its own mcp.server.fastmcp; leave it alone.
            return None
        import importlib.machinery

        module = install()
        return importlib.machinery.ModuleSpec(_TARGET, _Loader(module))


class _Loader:
    def __init__(self, module):
        self._module = module

    def create_module(self, spec):
        return self._module

    def exec_module(self, module):
        pass


def register_finder():
    """Install the adapter lazily, on the first import of mcp.server.fastmcp.

    Idempotent, and safe under mcp 1.x, where it changes nothing. Called from
    the .pth file the Dockerfile writes, and by the tests that import
    garmin_mcp, so they run without that file.
    """
    if not any(isinstance(f, _Finder) for f in sys.meta_path):
        sys.meta_path.insert(0, _Finder())
