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
  (2.x would report an empty string).
- set_server_overrides() sets constructor arguments that win over the caller's.
  launch.py uses it before upstream's main() builds the server, so serverInfo
  carries this project's name instead of upstream's "Garmin Connect v1.0".
- A tool that raises keeps its message: 1.x answered "Error executing tool
  <name>: <message>", 2.x sends only "Error executing tool <name>" for any
  exception but ToolError. tool() wraps each function so other exceptions are
  re-raised as ToolError(message), e.g. "Garmin login failed. Run
  'garmin-mcp-auth' ...". functools.wraps keeps name, docstring and signature,
  so input schemas do not change.

install() raises ShimError, before anything is patched, if the installed mcp
is not 2.x or MCPServer lacks what this adapter calls, so the Docker build
(launch.patch()) fails instead of the live server.

launch.py calls install() itself. Other entry points import garmin_mcp too,
above all upstream's garmin-mcp-auth script (the one-time Garmin login over
railway ssh), so the Dockerfile also writes a .pth file into site-packages that
runs register_finder() at interpreter start. That only adds an import hook;
mcp is imported, and install() runs, the first time anything imports
mcp.server.fastmcp.
"""

from __future__ import annotations

import sys
import types

_TARGET = "mcp.server.fastmcp"
_MARKER = "__garmin_mcp2_compat__"

# MCPServer methods upstream and this repo call; run_streamable_http_async
# must accept every keyword FastMCP.run() forwards to it.
_REQUIRED_METHODS = ("tool", "resource", "custom_route", "run", "list_tools", "run_streamable_http_async")
_HTTP_KWARGS = ("host", "port", "streamable_http_path", "json_response", "stateless_http", "transport_security")
_SSE_KWARGS = ("host", "port", "sse_path", "message_path", "transport_security")

# Constructor arguments that win over the caller's; see set_server_overrides().
_server_overrides = {}


class ShimError(RuntimeError):
    """The installed mcp SDK is not one this adapter supports."""


def set_server_overrides(**overrides):
    """Make every FastMCP built from now on take these constructor arguments
    over the caller's own, e.g. name=... (launch.py: this project's name, not
    upstream's). Replaces earlier overrides; no arguments clears them. A
    keyword the constructor does not take raises TypeError when a server is
    built, as it would from the caller."""
    _server_overrides.clear()
    _server_overrides.update(overrides)


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
    for method, needed in (("run_streamable_http_async", _HTTP_KWARGS), ("run_sse_async", _SSE_KWARGS)):
        params = inspect.signature(getattr(MCPServer, method)).parameters
        absent = [k for k in needed if k not in params]
        if absent:
            raise ShimError(f"mcp2_compat: mcp {version} MCPServer.{method}() takes no {', '.join(absent)}")
    tool_params = inspect.signature(MCPServer.tool).parameters
    absent = [k for k in ("name", "annotations", "structured_output") if k not in tool_params]
    if absent:
        raise ShimError(f"mcp2_compat: mcp {version} MCPServer.tool() takes no {', '.join(absent)}")
    return version, MCPServer, Context


def _keep_error_text(fn, passthrough, ToolError):
    """fn, with any exception not in `passthrough` re-raised as ToolError(str(exc))."""
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
        # Full traceback to the server log; the client gets the message (or the
        # exception type when the message is empty, e.g. a bare TimeoutError()).
        log.error("Tool %r failed", name, exc_info=exc)
        return ToolError(str(exc) or type(exc).__name__)

    if is_async_callable(fn):

        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            try:
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

    return wrapper


def _build(version, MCPServer):
    import functools

    from mcp.server.mcpserver.exceptions import ResourceError, ToolError
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
                register(_keep_error_text(fn, passthrough, ToolError))
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
