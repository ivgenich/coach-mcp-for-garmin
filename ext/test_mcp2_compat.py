"""Tests for mcp2_compat: upstream's FastMCP calls work on mcp 2.x MCPServer.

    python -m unittest discover -s ext -p "test_mcp2_compat*.py" -v

Skipped without mcp 2.x (the py3.10/3.12 venvs may carry mcp 1.x or none);
the version-check and middleware-entry tests run everywhere because they mock
the SDK version or need no SDK. The _ToolFilter tests and the real-main()
tool loop test also need upstream garmin_mcp.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import gc
import importlib.metadata
import importlib.util
import inspect
import io
import json
import logging
import os
import sys
import threading
import time
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import mcp2_compat  # noqa: E402

TARGET = "mcp.server.fastmcp"


def _mcp_major():
    try:
        return importlib.metadata.version("mcp").split(".", 1)[0]
    except importlib.metadata.PackageNotFoundError:
        return None


HAVE_MCP2 = _mcp_major() == "2"
HAVE_UPSTREAM = importlib.util.find_spec("garmin_mcp") is not None


class ShimState(unittest.TestCase):
    """Restores sys.modules["mcp.server.fastmcp"] and mcp.server.fastmcp after each test."""

    def setUp(self):
        self._saved = sys.modules.get(TARGET, None)
        self._had = TARGET in sys.modules
        parent = sys.modules.get("mcp.server")
        self._parent_attr = getattr(parent, "fastmcp", None) if parent else None
        self._parent_had = parent is not None and hasattr(parent, "fastmcp")
        self.addCleanup(self._restore)

    def _restore(self):
        if self._had:
            sys.modules[TARGET] = self._saved
        else:
            sys.modules.pop(TARGET, None)
        parent = sys.modules.get("mcp.server")
        if parent is not None:
            if self._parent_had:
                parent.fastmcp = self._parent_attr
            elif hasattr(parent, "fastmcp"):
                delattr(parent, "fastmcp")

    def fresh_install(self):
        sys.modules.pop(TARGET, None)
        return mcp2_compat.install()


class VersionCheck(ShimState):
    """install() refuses anything but mcp 2.x before patching anything."""

    def test_rejects_mcp_1(self):
        sentinel = object()
        sys.modules[TARGET] = sentinel  # stands in for whatever was there
        with mock.patch("importlib.metadata.version", return_value="1.28.1"):
            with self.assertRaises(mcp2_compat.ShimError) as cm:
                mcp2_compat.install()
        self.assertIn("1.28.1", str(cm.exception))
        self.assertIs(sys.modules[TARGET], sentinel, "sys.modules changed although install() failed")

    def test_rejects_mcp_3(self):
        with mock.patch("importlib.metadata.version", return_value="3.0.0"):
            with self.assertRaises(mcp2_compat.ShimError):
                mcp2_compat.install()

    def test_rejects_missing_mcp(self):
        err = importlib.metadata.PackageNotFoundError("mcp")
        with mock.patch("importlib.metadata.version", side_effect=err):
            with self.assertRaises(mcp2_compat.ShimError):
                mcp2_compat.install()

    def test_shim_error_is_runtime_error(self):
        # launch.patch() at docker build relies on any exception failing the build.
        self.assertTrue(issubclass(mcp2_compat.ShimError, RuntimeError))


@unittest.skipUnless(HAVE_MCP2, "needs mcp 2.x")
class Install(ShimState):
    def test_maps_sys_modules_and_import(self):
        module = self.fresh_install()
        self.assertIs(sys.modules[TARGET], module)
        import mcp.server

        self.assertIs(mcp.server.fastmcp, module)
        from mcp.server.fastmcp import Context, FastMCP  # noqa: F401  (the upstream import line)
        from mcp.server.mcpserver import MCPServer

        self.assertIs(FastMCP, module.FastMCP)
        self.assertTrue(issubclass(FastMCP, MCPServer))

    def test_idempotent(self):
        first = self.fresh_install()
        second = mcp2_compat.install()
        self.assertIs(first, second)
        self.assertIs(first.FastMCP, second.FastMCP)
        self.assertIs(sys.modules[TARGET], first)

    def test_replaces_the_raising_stub(self):
        # mcp 2.x ships mcp/server/fastmcp.py that only raises on import.
        sys.modules.pop(TARGET, None)
        with mock.patch.object(sys, "meta_path", [f for f in sys.meta_path if type(f).__name__ != "_Finder"]):
            with self.assertRaises(ImportError):
                importlib.import_module(TARGET)
        module = mcp2_compat.install()
        self.assertIs(importlib.import_module(TARGET), module)


@unittest.skipUnless(HAVE_MCP2 and hasattr(mcp2_compat, "register_finder"), "needs mcp 2.x and register_finder")
class Finder(ShimState):
    """register_finder() (the image's .pth hook) installs the shim on first import."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(sys, "meta_path", [f for f in sys.meta_path if type(f).__name__ != "_Finder"])
        patcher.start()
        self.addCleanup(patcher.stop)
        sys.modules.pop(TARGET, None)

    def finders(self):
        return [f for f in sys.meta_path if type(f).__name__ == "_Finder"]

    def test_idempotent(self):
        mcp2_compat.register_finder()
        mcp2_compat.register_finder()
        self.assertEqual(len(self.finders()), 1)
        self.assertIs(sys.meta_path[0], self.finders()[0])

    def test_first_import_installs_the_shim(self):
        mcp2_compat.register_finder()
        self.assertNotIn(TARGET, sys.modules)  # lazy: nothing installed yet
        from mcp.server.fastmcp import FastMCP
        from mcp.server.mcpserver import MCPServer

        self.assertTrue(issubclass(FastMCP, MCPServer))
        self.assertIs(sys.modules[TARGET], mcp2_compat.install())

    def test_other_imports_untouched(self):
        mcp2_compat.register_finder()
        self.assertIsNone(self.finders()[0].find_spec("mcp.server.mcpserver"))

    def test_server_version_defaults_to_mcp_version(self):
        FastMCP = self.fresh_install().FastMCP
        app = FastMCP("Garmin Connect v1.0")
        self.assertEqual(app._lowlevel_server.version, importlib.metadata.version("mcp"))
        self.assertEqual(FastMCP("x", version="9.9")._lowlevel_server.version, "9.9")


@unittest.skipUnless(HAVE_MCP2, "needs mcp 2.x")
class RunForwarding(ShimState):
    """The 1.x constructor's HTTP settings reach MCPServer.run()."""

    def setUp(self):
        super().setUp()
        from mcp.server.mcpserver import MCPServer

        self.FastMCP = self.fresh_install().FastMCP
        patcher = mock.patch.object(MCPServer, "run", autospec=True)
        self.run = patcher.start()
        self.addCleanup(patcher.stop)

    def test_streamable_http_kwargs(self):
        # upstream: FastMCP(name, host=..., port=..., stateless_http=...); app.run(transport=...)
        app = self.FastMCP("Garmin Connect v1.0", host="127.0.0.1", port=8102, stateless_http=True)
        app.run(transport="streamable-http")
        self.run.assert_called_once()
        args, kwargs = self.run.call_args
        self.assertIs(args[0], app)
        self.assertEqual(args[1], "streamable-http")
        self.assertEqual(kwargs["host"], "127.0.0.1")
        self.assertEqual(kwargs["port"], 8102)
        self.assertIs(kwargs["stateless_http"], True)
        # 1.x defaults kept
        self.assertIs(kwargs["json_response"], False)
        self.assertEqual(kwargs["streamable_http_path"], "/mcp")
        self.assertIsNone(kwargs["transport_security"])

    def test_defaults_match_fastmcp_1x(self):
        self.FastMCP("x").run(transport="streamable-http")
        kwargs = self.run.call_args.kwargs
        self.assertEqual((kwargs["host"], kwargs["port"]), ("127.0.0.1", 8000))
        self.assertIs(kwargs["stateless_http"], False)

    def test_stdio_gets_no_http_kwargs(self):
        self.FastMCP("x", host="0.0.0.0", port=1).run(transport="stdio")
        args, kwargs = self.run.call_args
        self.assertEqual(args[1], "stdio")
        self.assertEqual(kwargs, {})

    def test_sse_kwargs(self):
        self.FastMCP("x", host="h", port=2, sse_path="/s").run(transport="sse")
        args, kwargs = self.run.call_args
        self.assertEqual(args[1], "sse")
        self.assertEqual((kwargs["host"], kwargs["port"], kwargs["sse_path"]), ("h", 2, "/s"))
        self.assertNotIn("stateless_http", kwargs)

    def test_mount_path_rejected(self):
        with self.assertRaises(TypeError):
            self.FastMCP("x").run(transport="sse", mount_path="/m")
        self.run.assert_not_called()

    def test_unknown_constructor_kwarg_raises(self):
        with self.assertRaises(TypeError):
            self.FastMCP("x", no_such_setting=1)


def _call(app, mode, tool, arguments):
    """List tools and call one through the official client, in process."""
    from mcp.client import Client

    async def go():
        async with Client(app, mode=mode) as client:
            listed = await client.list_tools()
            result = await client.call_tool(tool, arguments)
            return listed, result

    return asyncio.run(go())


@unittest.skipUnless(HAVE_MCP2, "needs mcp 2.x")
class JsonStringTool(ShimState):
    """A tool returning json.dumps(...) yields one text block, no structuredContent."""

    PAYLOAD = {"steps": 12345, "date": "2026-11-13"}

    def check(self, app, name):
        for mode in ("legacy", "auto"):
            with self.subTest(mode=mode):
                listed, result = _call(app, mode, name, {"date": "2026-11-13"})
                tool = next(t for t in listed.tools if t.name == name)
                self.assertIsNone(tool.output_schema)
                self.assertFalse(result.is_error)
                self.assertIsNone(result.structured_content)
                self.assertEqual([c.type for c in result.content], ["text"])
                self.assertEqual(json.loads(result.content[0].text), self.PAYLOAD)
                return tool

    def test_explicit_structured_output_false(self):
        app = self.fresh_install().FastMCP("x")
        payload = self.PAYLOAD

        @app.tool(structured_output=False)
        def get_steps(date: str) -> str:
            return json.dumps(payload)

        self.check(app, "get_steps")

    @unittest.skipUnless(HAVE_UPSTREAM, "needs upstream garmin_mcp")
    def test_tool_filter_defaults_structured_output_false(self):
        # A real read-only upstream tool name, so annotations_for() knows it.
        # upstream's _ToolFilter sets structured_output=False and annotations;
        # the tool here passes neither, as upstream's modules do.
        self.fresh_install()
        import garmin_mcp

        FastMCP = sys.modules[TARGET].FastMCP
        app = FastMCP("x")
        filtered = garmin_mcp._ToolFilter(app, {"get_steps_data"}, set())
        payload = self.PAYLOAD

        @filtered.tool()
        def get_steps_data(date: str) -> str:
            return json.dumps(payload)

        @filtered.tool()
        def delete_activity(activity_id: int) -> str:  # not allowlisted
            return "{}"

        tool = self.check(app, "get_steps_data")
        self.assertEqual([t.name for t in asyncio.run(app.list_tools())], ["get_steps_data"])
        self.assertIsNotNone(tool.annotations)
        self.assertIs(tool.annotations.read_only_hint, True)

    @unittest.skipUnless(HAVE_UPSTREAM, "needs upstream garmin_mcp")
    def test_tool_filter_respects_explicit_structured_output(self):
        self.fresh_install()
        import garmin_mcp

        app = sys.modules[TARGET].FastMCP("x")
        filtered = garmin_mcp._ToolFilter(app, set(), set())

        @filtered.tool(structured_output=True)
        def get_steps(date: str) -> dict[str, int]:
            return {"steps": 1}

        listed, result = _call(app, "legacy", "get_steps", {"date": "2026-11-13"})
        self.assertIsNotNone(next(t for t in listed.tools if t.name == "get_steps").output_schema)
        self.assertEqual(result.structured_content, {"steps": 1})


@unittest.skipUnless(HAVE_MCP2, "needs mcp 2.x")
class ServerOverrides(ShimState):
    """set_server_overrides(): launch.py's constructor arguments win over the
    caller's (upstream's main() names its server "Garmin Connect v1.0")."""

    def setUp(self):
        super().setUp()
        self.addCleanup(mcp2_compat.set_server_overrides)
        self.FastMCP = self.fresh_install().FastMCP

    def test_name_wins_and_everything_else_stays(self):
        mcp2_compat.set_server_overrides(name="coach-mcp-for-garmin")
        # upstream's call: the name by position, the HTTP settings by keyword
        app = self.FastMCP("Garmin Connect v1.0", host="127.0.0.1", port=8102, stateless_http=True)
        self.assertEqual(app.name, "coach-mcp-for-garmin")
        self.assertEqual(app.version, importlib.metadata.version("mcp"))
        self.assertEqual((app._compat_http["port"], app._compat_http["stateless_http"]), (8102, True))
        self.assertEqual(self.FastMCP(name="x").name, "coach-mcp-for-garmin")

    def test_without_overrides_the_callers_arguments_apply(self):
        self.assertEqual(self.FastMCP("Garmin Connect v1.0").name, "Garmin Connect v1.0")
        mcp2_compat.set_server_overrides(name="a")
        mcp2_compat.set_server_overrides()  # clears
        self.assertEqual(self.FastMCP("b").name, "b")

    def test_any_constructor_argument_can_be_overridden(self):
        mcp2_compat.set_server_overrides(instructions="Read-only.", port=9, version="1.2.3")
        app = self.FastMCP("x", port=1)
        self.assertEqual((app.name, app.instructions, app._compat_http["port"], app.version),
                         ("x", "Read-only.", 9, "1.2.3"))

    def test_unknown_override_raises_when_a_server_is_built(self):
        mcp2_compat.set_server_overrides(no_such_setting=1)
        with self.assertRaises(TypeError):
            self.FastMCP("x")

    def test_clients_see_the_name_in_both_eras(self):
        mcp2_compat.set_server_overrides(name="coach-mcp-for-garmin")
        app = self.FastMCP("Garmin Connect v1.0")
        from mcp.client import Client

        async def server_info(mode):
            async with Client(app, mode=mode) as client:
                return client.server_info

        want = ("coach-mcp-for-garmin", importlib.metadata.version("mcp"))
        for mode in ("legacy", "auto"):
            with self.subTest(mode=mode):
                info = asyncio.run(server_info(mode))
                self.assertEqual((info.name, info.version), want)
        # launch.py overrides the version too (KEY contract D17): this project's, in both eras.
        mcp2_compat.set_server_overrides(name="coach-mcp-for-garmin", version="1.0.0")
        app = self.FastMCP("Garmin Connect v1.0")
        for mode in ("legacy", "auto"):
            with self.subTest(mode=mode, version="override"):
                info = asyncio.run(server_info(mode))
                self.assertEqual((info.name, info.version), ("coach-mcp-for-garmin", "1.0.0"))

    def test_instructions_reach_the_initialize_result(self):
        mcp2_compat.set_server_overrides(name="coach-mcp-for-garmin", instructions="Read-only. Ask before guessing.")
        app = self.FastMCP("Garmin Connect v1.0")
        from mcp.client import Client

        async def instructions(mode):
            async with Client(app, mode=mode) as client:
                return client.instructions

        for mode in ("legacy", "auto"):
            with self.subTest(mode=mode):
                self.assertEqual(asyncio.run(instructions(mode)), "Read-only. Ask before guessing.")

    def test_auth_and_provider_reach_the_server(self):
        # launch.py's sign-in: set_server_overrides(auth=..., auth_server_provider=...)
        from mcp.server.auth.provider import ProviderTokenVerifier

        settings, provider = auth_settings(), StubProvider()
        mcp2_compat.set_server_overrides(name="coach-mcp-for-garmin", auth=settings, auth_server_provider=provider)
        app = self.FastMCP("Garmin Connect v1.0", host="127.0.0.1", port=8102, stateless_http=True)
        self.assertEqual(app.settings.auth, settings)
        self.assertIs(app._auth_server_provider, provider)
        self.assertIsInstance(app._token_verifier, ProviderTokenVerifier)  # it gets a token verifier
        self.assertIs(app._token_verifier.provider, provider)
        self.assertEqual(app._compat_http["port"], 8102)  # the caller's HTTP settings stay
        self.assertLessEqual(SDK_AUTH_PATHS, route_paths(app.streamable_http_app()))


# --- ASGI middleware hook -----------------------------------------------------
SEEN = []  # what the recording middleware below saw, outermost first


class First:
    """ASGI middleware that records itself (a bare-class entry)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            SEEN.append("first")
        await self.app(scope, receive, send)


class Second:
    """ASGI middleware that records its option (a (class, options) entry)."""

    def __init__(self, app, tag):
        self.app, self.tag = app, tag

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            SEEN.append(self.tag)
        await self.app(scope, receive, send)


class StubProvider:
    """Stands in for an OAuth provider: MCPServer only wraps it at build time."""


def auth_settings():
    from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions

    return AuthSettings(
        issuer_url="https://coach.example.com",
        resource_server_url="https://coach.example.com/mcp",
        validate_token_resource=True,
        client_registration_options=ClientRegistrationOptions(enabled=True),
        revocation_options=RevocationOptions(enabled=True),
    )


SDK_AUTH_PATHS = {"/mcp", "/authorize", "/token", "/register", "/revoke",
                  "/.well-known/oauth-authorization-server", "/.well-known/oauth-protected-resource/mcp"}


def route_paths(app):
    return {getattr(route, "path", None) for route in app.routes}


def asgi_get(app, path):
    """GET `path` from an ASGI app in process: (status, body)."""

    async def go():
        sent = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            sent.append(message)

        scope = {
            "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"}, "http_version": "1.1",
            "method": "GET", "scheme": "http", "path": path, "raw_path": path.encode(), "root_path": "",
            "query_string": b"", "headers": [(b"host", b"127.0.0.1:8000")],
            "client": ("127.0.0.1", 50000), "server": ("127.0.0.1", 8000),
        }
        await app(scope, receive, send)
        return sent

    sent = asyncio.run(go())
    start = next(m for m in sent if m["type"] == "http.response.start")
    return start["status"], b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")


class MiddlewareEntries(unittest.TestCase):
    """set_asgi_middleware() checks its entries; no SDK needed."""

    def setUp(self):
        self.addCleanup(mcp2_compat.set_asgi_middleware)

    def test_classes_and_pairs_replace_the_list_and_no_arguments_clear_it(self):
        mcp2_compat.set_asgi_middleware(First, (Second, {"tag": "x"}))
        self.assertEqual(mcp2_compat._asgi_middleware, [(First, {}), (Second, {"tag": "x"})])
        mcp2_compat.set_asgi_middleware((Second, {"tag": "y"}))
        self.assertEqual(mcp2_compat._asgi_middleware, [(Second, {"tag": "y"})])
        mcp2_compat.set_asgi_middleware()
        self.assertEqual(mcp2_compat._asgi_middleware, [])

    def test_options_are_copied(self):
        options = {"tag": "x"}
        mcp2_compat.set_asgi_middleware((Second, options))
        options["tag"] = "changed later"
        self.assertEqual(mcp2_compat._asgi_middleware, [(Second, {"tag": "x"})])

    def test_bad_entries_raise_type_error_and_keep_the_list(self):
        mcp2_compat.set_asgi_middleware(First)
        bad = (First(None), "First", None, (First,), (First, {}, {}), (First, "tag=x"), (First, {1: "x"}),
               (First, None), ("First", {}), [First, {}])
        for entry in bad:
            with self.subTest(entry=entry):
                with self.assertRaises(TypeError):
                    mcp2_compat.set_asgi_middleware((Second, {"tag": "ok"}), entry)
                self.assertEqual(mcp2_compat._asgi_middleware, [(First, {})])


@unittest.skipUnless(HAVE_MCP2, "needs mcp 2.x")
class AsgiMiddleware(ShimState):
    """The middleware wraps both HTTP apps, first entry outermost, outside the
    SDK's own authentication middleware, on the path run() takes."""

    def setUp(self):
        super().setUp()
        self.addCleanup(mcp2_compat.set_asgi_middleware)
        self.addCleanup(mcp2_compat.set_server_overrides)
        self.FastMCP = self.fresh_install().FastMCP
        SEEN.clear()

    def app_with_route(self):
        from starlette.responses import PlainTextResponse

        app = self.FastMCP("x")

        @app.custom_route("/ping", methods=["GET"])
        async def ping(request):
            SEEN.append("route")
            return PlainTextResponse("pong")

        return app

    def test_first_entry_is_outermost_on_both_apps(self):
        mcp2_compat.set_asgi_middleware(First, (Second, {"tag": "second"}))
        server = self.app_with_route()
        for build in (server.streamable_http_app, server.sse_app):
            with self.subTest(app=build.__name__):
                SEEN.clear()
                app = build()
                self.assertEqual([m.cls for m in app.user_middleware], [First, Second])
                self.assertEqual(asgi_get(app, "/ping"), (200, b"pong"))
                self.assertEqual(SEEN, ["first", "second", "route"])

    def test_outside_the_sdk_authentication_middleware(self):
        from mcp.server.auth.middleware.auth_context import AuthContextMiddleware
        from starlette.middleware.authentication import AuthenticationMiddleware

        mcp2_compat.set_server_overrides(auth=auth_settings(), auth_server_provider=StubProvider())
        mcp2_compat.set_asgi_middleware(First, (Second, {"tag": "second"}))
        app = self.FastMCP("x").streamable_http_app()
        self.assertEqual([m.cls for m in app.user_middleware],
                         [First, Second, AuthenticationMiddleware, AuthContextMiddleware])
        self.assertLessEqual(SDK_AUTH_PATHS, route_paths(app))

    def test_replaced_and_cleared(self):
        server = self.app_with_route()
        mcp2_compat.set_asgi_middleware(First)
        mcp2_compat.set_asgi_middleware((Second, {"tag": "only"}))
        self.assertEqual([m.cls for m in server.streamable_http_app().user_middleware], [Second])
        mcp2_compat.set_asgi_middleware()
        app = server.streamable_http_app()
        self.assertEqual(app.user_middleware, [])
        self.assertEqual(asgi_get(app, "/ping"), (200, b"pong"))
        self.assertEqual(SEEN, ["route"])

    def test_run_serves_the_wrapped_app(self):
        # MCPServer.run() -> run_*_async() -> self.streamable_http_app()/sse_app()
        # -> uvicorn: the app uvicorn gets carries the middleware.
        served = []

        class FakeConfig:
            def __init__(self, app, **kwargs):
                self.app, self.kwargs = app, kwargs

        class FakeServer:
            def __init__(self, config):
                self.config = config

            async def serve(self):
                served.append(self.config)

        mcp2_compat.set_asgi_middleware(First, (Second, {"tag": "second"}))
        with mock.patch("uvicorn.Config", FakeConfig), mock.patch("uvicorn.Server", FakeServer):
            self.FastMCP("x", host="127.0.0.1", port=8331).run(transport="streamable-http")
            self.FastMCP("x", host="127.0.0.1", port=8331).run(transport="sse")
        self.assertEqual(len(served), 2)
        for config in served:
            self.assertEqual([m.cls for m in config.app.user_middleware], [First, Second])
            self.assertEqual((config.kwargs["host"], config.kwargs["port"]), ("127.0.0.1", 8331))


@unittest.skipUnless(HAVE_MCP2, "needs mcp 2.x")
class SdkCheck(ShimState):
    """install() fails with ShimError, before patching anything, when MCPServer
    no longer has what the adapter or launch.py rely on."""

    def test_missing_app_methods(self):
        from mcp.server.mcpserver import MCPServer

        for method in ("streamable_http_app", "sse_app"):
            with self.subTest(method=method), mock.patch.object(MCPServer, method, None):
                with self.assertRaisesRegex(mcp2_compat.ShimError, f"lacks {method}"):
                    self.fresh_install()

    def test_missing_constructor_keywords(self):
        from mcp.server.mcpserver import MCPServer

        real = inspect.signature(MCPServer.__init__)
        self.assertEqual(mcp2_compat._INIT_KWARGS, ("name", "instructions", "version", "auth", "auth_server_provider"))
        for name in mcp2_compat._INIT_KWARGS:
            def init(self, *args, **kwargs):  # pragma: no cover - never called
                pass

            init.__signature__ = real.replace(parameters=[p for p in real.parameters.values() if p.name != name])
            with self.subTest(name=name), mock.patch.object(MCPServer, "__init__", init):
                with self.assertRaisesRegex(mcp2_compat.ShimError, rf"MCPServer\(\) takes no {name}\b"):
                    self.fresh_install()

    def test_missing_context_parameter_helper(self):
        stub = types.ModuleType("mcp.server.mcpserver.utilities.context_injection")
        with mock.patch.dict(sys.modules, {stub.__name__: stub}):
            with self.assertRaisesRegex(mcp2_compat.ShimError, "find_context_parameter"):
                self.fresh_install()

    def test_the_installed_sdk_passes(self):
        from mcp.server.mcpserver import MCPServer

        self.assertTrue(issubclass(self.fresh_install().FastMCP, MCPServer))


# --- The tool loop ------------------------------------------------------------
LOGIN_FAILED = "Garmin login failed. Run 'garmin-mcp-auth' to authenticate, then restart the server."


def _client_run(app, body):
    """asyncio.run(body(client)) with an in-process Client connected to app."""
    from mcp.client import Client

    async def go():
        async with Client(app) as client:
            return await body(client)

    return asyncio.run(go())


@unittest.skipUnless(HAVE_MCP2, "needs mcp 2.x")
class ToolLoop(ShimState):
    """D12: an async tool that takes no Context runs on one shared background
    event loop (thread mcp2-compat-tool-loop), so a tool that blocks, as
    upstream's do for each Garmin call, does not stall the server's loop."""

    def setUp(self):
        super().setUp()
        self.app = self.fresh_install().FastMCP("x")
        logging.disable(logging.ERROR)  # the SDK and the adapter log every failed call
        self.addCleanup(logging.disable, logging.NOTSET)

    def call(self, name, arguments=None):
        return _client_run(self.app, lambda client: client.call_tool(name, arguments or {}))

    def wrapper(self, name):
        return self.app._tool_manager.get_tool(name).fn

    def test_server_loop_keeps_running_while_a_tool_blocks(self):
        @self.app.tool()
        async def blocks() -> str:
            time.sleep(1.5)  # like upstream's join() on a Garmin call
            return threading.current_thread().name

        async def body(client):
            ticks, stop = [], asyncio.Event()

            async def heartbeat():
                while not stop.is_set():
                    ticks.append(time.monotonic())
                    await asyncio.sleep(0.01)

            beat = asyncio.ensure_future(heartbeat())
            result = await client.call_tool("blocks", {})
            stop.set()
            await beat
            return result, ticks

        result, ticks = _client_run(self.app, body)
        self.assertFalse(result.is_error, result)
        self.assertEqual(result.content[0].text, mcp2_compat.TOOL_LOOP_THREAD_NAME)
        self.assertGreaterEqual(ticks[-1] - ticks[0], 1.4)  # it ticked through the whole block
        self.assertLessEqual(max(b - a for a, b in zip(ticks, ticks[1:])), 0.1)

    def test_blocking_tools_run_one_after_the_other(self):
        spans = {"blocks": [], "waits": []}

        @self.app.tool()
        async def blocks() -> str:
            start = time.monotonic()
            time.sleep(0.3)
            spans["blocks"].append((start, time.monotonic()))
            return "done"

        @self.app.tool()
        async def waits() -> str:
            start = time.monotonic()
            await asyncio.sleep(0.3)
            spans["waits"].append((start, time.monotonic()))
            return "done"

        async def body(client):
            return await asyncio.gather(*(client.call_tool(name, {}) for name in ("blocks", "blocks", "waits", "waits")))

        results = _client_run(self.app, body)
        self.assertEqual([r.is_error for r in results], [False] * 4)
        (a_start, a_end), (b_start, b_end) = sorted(spans["blocks"])
        self.assertGreaterEqual(b_start, a_end - 0.01, "two blocking tools overlapped")
        # The server hands calls over concurrently: two tools that await overlap.
        (c_start, c_end), (d_start, d_end) = sorted(spans["waits"])
        self.assertLess(d_start, c_end, "the calls were not handed over concurrently")

    def test_an_answer_does_not_wait_for_the_next_calls_block(self):
        # V2-2: two calls that reach the tool loop together. Each answer leaves
        # as soon as its call ends, not after the other call's blocking step.
        @self.app.tool()
        async def quick() -> str:
            return "here"

        @self.app.tool()
        async def blocks() -> str:
            time.sleep(0.4)
            return "done"

        asyncio.run(self.wrapper("quick")())  # the tool loop is up and idle
        wrapper = self.wrapper("blocks")

        async def body():
            # Busy the tool loop for a moment, so both calls are handed over
            # before either starts, as when two clients call at the same time.
            mcp2_compat._tool_loop.loop.call_soon_threadsafe(time.sleep, 0.2)
            t0 = time.monotonic()

            async def one():
                self.assertEqual(await wrapper(), "done")
                return time.monotonic() - t0

            return sorted(await asyncio.gather(one(), one()))

        first, second = asyncio.run(body())
        self.assertGreaterEqual(second - first, 0.25, f"answers at {first:.2f}s and {second:.2f}s")
        self.assertGreaterEqual(second, 0.95)  # still one blocking call at a time

    def test_a_tool_that_awaits_returns(self):
        @self.app.tool()
        async def naps(seconds: float) -> str:
            await asyncio.sleep(seconds)
            return "slept on " + threading.current_thread().name

        result = self.call("naps", {"seconds": 0.05})
        self.assertFalse(result.is_error, result)
        self.assertEqual(result.content[0].text, "slept on " + mcp2_compat.TOOL_LOOP_THREAD_NAME)
        self.assertIs(getattr(self.wrapper("naps"), mcp2_compat.TOOL_LOOP_MARKER), True)

    def test_error_text_is_unchanged(self):
        from mcp.server.mcpserver.exceptions import ToolError

        @self.app.tool()
        async def login_fails(date: str = "") -> str:
            raise RuntimeError(LOGIN_FAILED)

        @self.app.tool()
        async def refuses(date: str = "") -> str:
            raise ToolError("bad date")

        @self.app.tool()
        async def stalls(date: str = "") -> str:
            raise TimeoutError()

        for name, text in (("login_fails", LOGIN_FAILED), ("refuses", "bad date"), ("stalls", "TimeoutError")):
            with self.subTest(name=name):
                result = self.call(name)
                self.assertTrue(result.is_error)
                self.assertEqual(result.content[0].text, f"Error executing tool {name}: {text}")
                self.assertIs(getattr(self.wrapper(name), mcp2_compat.TOOL_LOOP_MARKER), True)

    def test_a_tool_with_a_context_parameter_stays_on_the_server_loop(self):
        from mcp.server.mcpserver import Context

        async def with_context(ctx: Context) -> str:
            return threading.current_thread().name

        # This module uses `from __future__ import annotations`; give the SDK
        # the class itself, as a module that imports Context at the top would.
        with_context.__annotations__ = {"ctx": Context, "return": str}
        self.app.tool()(with_context)
        result = self.call("with_context")
        self.assertFalse(result.is_error, result)
        self.assertEqual(result.content[0].text, threading.current_thread().name)  # asyncio.run's thread
        self.assertIs(getattr(self.wrapper("with_context"), mcp2_compat.TOOL_LOOP_MARKER), False)

    def test_a_server_loop_tool_stays_on_the_server_loop(self):
        # R6: an async tool marked by server_loop() runs on the server's loop, so a tool blocking the tool loop
        # does not stop its clock; the input schema is the same as without the mark.
        async def own(date: str = "") -> str:
            await asyncio.sleep(0.01)
            return threading.current_thread().name

        async def plain_twin(date: str = "") -> str:
            return ""

        self.assertIs(mcp2_compat.server_loop(own), own)
        self.app.tool()(own)
        self.app.tool()(plain_twin)

        @self.app.tool()
        async def blocks() -> str:
            time.sleep(0.6)
            return "done"

        async def body(client):
            blocked = asyncio.ensure_future(client.call_tool("blocks", {}))
            await asyncio.sleep(0.1)  # the tool loop is blocked now
            start = time.monotonic()
            result = await client.call_tool("own", {})
            return result, time.monotonic() - start, await blocked

        result, took, blocked = _client_run(self.app, body)
        self.assertFalse(result.is_error, result)
        self.assertFalse(blocked.is_error, blocked)
        self.assertEqual(result.content[0].text, threading.current_thread().name)  # asyncio.run's thread
        self.assertLess(took, 0.4, "the tool waited for the blocked tool loop")
        self.assertIs(getattr(self.wrapper("own"), mcp2_compat.TOOL_LOOP_MARKER), False)
        self.assertIs(getattr(self.wrapper("plain_twin"), mcp2_compat.TOOL_LOOP_MARKER), True)
        tools = {tool.name: tool for tool in self.app._tool_manager.list_tools()}
        own_schema, twin_schema = (dict(tools[name].parameters) for name in ("own", "plain_twin"))
        self.assertEqual((own_schema.pop("title"), twin_schema.pop("title")), ("ownArguments", "plain_twinArguments"))
        self.assertEqual(own_schema, twin_schema)

    def test_sync_tools_are_unchanged(self):
        @self.app.tool()
        def plain() -> str:
            return threading.current_thread().name

        @self.app.tool()
        def plain_fails() -> str:
            raise RuntimeError(LOGIN_FAILED)

        result = self.call("plain")
        self.assertFalse(result.is_error, result)
        # The SDK runs it in a worker thread, as before: neither loop.
        self.assertNotIn(result.content[0].text, (mcp2_compat.TOOL_LOOP_THREAD_NAME, threading.current_thread().name))
        self.assertIs(getattr(self.wrapper("plain"), mcp2_compat.TOOL_LOOP_MARKER), False)
        self.assertEqual(self.call("plain_fails").content[0].text, f"Error executing tool plain_fails: {LOGIN_FAILED}")

    def test_registration_still_returns_the_original_function(self):
        async def get_steps(date: str = "") -> str:
            return "{}"

        self.assertIs(self.app.tool()(get_steps), get_steps)
        self.assertIsNot(self.wrapper("get_steps"), get_steps)
        self.assertFalse(hasattr(get_steps, mcp2_compat.TOOL_LOOP_MARKER))

    def test_system_exit_and_keyboard_interrupt_become_errors_and_the_loop_survives(self):
        threads = []

        @self.app.tool()
        async def exits(code: int) -> str:
            raise SystemExit(code)

        @self.app.tool()
        async def interrupted() -> str:
            raise KeyboardInterrupt

        @self.app.tool()
        async def where() -> str:
            threads.append(threading.current_thread())
            return "here"

        async def body(client):
            return [await client.call_tool(name, args) for name, args in
                    (("where", {}), ("exits", {"code": 3}), ("interrupted", {}), ("where", {}))]

        before, exited, interrupted_, after = _client_run(self.app, body)
        self.assertTrue(exited.is_error)
        self.assertEqual(exited.content[0].text, "Error executing tool exits: SystemExit: 3")
        self.assertTrue(interrupted_.is_error)
        self.assertEqual(interrupted_.content[0].text, "Error executing tool interrupted: KeyboardInterrupt")
        self.assertFalse(after.is_error, after)
        self.assertIs(threads[0], threads[1], "the tool loop was replaced: the exception escaped it")
        self.assertTrue(threads[0].is_alive())

    def test_a_stopped_loop_is_replaced(self):
        threads = []

        @self.app.tool()
        async def where() -> str:
            threads.append(threading.current_thread())
            return "here"

        self.assertFalse(self.call("where").is_error)
        old = mcp2_compat._tool_loop
        old.loop.call_soon_threadsafe(old.loop.stop)
        old.thread.join(5)
        self.assertFalse(old.thread.is_alive())
        self.assertTrue(old.loop.is_closed())
        result = self.call("where")
        self.assertFalse(result.is_error, result)
        new = mcp2_compat._tool_loop
        self.assertIsNot(new, old)
        self.assertEqual((threads[0], threads[1]), (old.thread, new.thread))
        self.assertEqual(new.thread.name, mcp2_compat.TOOL_LOOP_THREAD_NAME)
        self.assertTrue(new.thread.daemon)

    def test_a_call_in_flight_when_the_loop_stops_fails_instead_of_hanging(self):
        started = threading.Event()

        @self.app.tool()
        async def waits_long() -> str:
            started.set()
            await asyncio.sleep(30)
            return "late"

        @self.app.tool()
        async def quick() -> str:
            return "here"

        async def body(client):
            call = asyncio.ensure_future(client.call_tool("waits_long", {}))
            self.assertTrue(await asyncio.get_running_loop().run_in_executor(None, started.wait, 5))
            old = mcp2_compat._tool_loop
            old.loop.call_soon_threadsafe(old.loop.stop)
            return await asyncio.wait_for(call, 10)

        result = _client_run(self.app, body)
        self.assertTrue(result.is_error)
        self.assertIn("the tool loop stopped before this call finished", result.content[0].text)
        gc.collect()  # the abandoned task logs "destroyed but pending"; logging is still off
        self.assertEqual(self.call("quick").content[0].text, "here")  # a new loop serves the next call

    def test_cancelling_the_caller_cancels_the_tool(self):
        started, cancelled = threading.Event(), threading.Event()

        @self.app.tool()
        async def slow() -> str:
            started.set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cancelled.set()
                raise
            return "late"

        wrapper = self.wrapper("slow")

        async def body():
            task = asyncio.ensure_future(wrapper())
            self.assertTrue(await asyncio.get_running_loop().run_in_executor(None, started.wait, 5))
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        asyncio.run(body())
        self.assertTrue(cancelled.wait(5), "the tool kept running on the tool loop")

    def test_a_tool_that_raises_cancelled_error_cancels_the_caller(self):
        # The outcome _guarded() leaves to the task's future still reaches the caller.
        @self.app.tool()
        async def gives_up() -> str:
            raise asyncio.CancelledError()

        wrapper = self.wrapper("gives_up")

        async def body():
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(wrapper(), 5)

        asyncio.run(body())

    def test_contextvars_are_copied_to_the_tool_loop(self):
        # F14: a copy of the caller's context, as run_coroutine_threadsafe makes.
        marker = contextvars.ContextVar("marker", default="unset")

        @self.app.tool()
        async def reads() -> str:
            seen = marker.get()
            marker.set("changed on the tool loop")
            return seen

        wrapper = self.wrapper("reads")

        async def body():
            marker.set("set by the caller")
            seen = await wrapper()
            return seen, marker.get()

        self.assertEqual(asyncio.run(body()), ("set by the caller", "set by the caller"))


class LogRecords(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)


@contextlib.contextmanager
def captured(name, level=logging.DEBUG):
    """The records logged under `name` at `level` and up, kept away from the
    root logger's handlers (the SDK puts its own there)."""
    logger = logging.getLogger(name)
    handler, saved = LogRecords(), (logger.level, logger.propagate)
    logger.setLevel(level)
    logger.propagate = False
    logger.addHandler(handler)
    try:
        yield handler.records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(saved[0])
        logger.propagate = saved[1]


@unittest.skipUnless(HAVE_MCP2, "needs mcp 2.x")
class FailedToolLog(ShimState):
    """Step 1.3: the adapter logs a failed tool at DEBUG only, with its
    exception; on the server launch.py builds, its call_tool() override logs
    each failed call once. The plain adapter's client text is unchanged
    (ToolLoop.test_error_text_is_unchanged)."""

    def setUp(self):
        super().setUp()
        self.app = self.fresh_install().FastMCP("x")
        # The SDK logs each failed call; not with logging.disable(), which would
        # drop the DEBUG records this test reads.
        sdk = captured("mcp", logging.CRITICAL + 1)
        sdk.__enter__()
        self.addCleanup(sdk.__exit__, None, None, None)

    def test_a_failed_tool_is_logged_at_debug_with_its_exception(self):
        @self.app.tool()
        async def login_fails(date: str = "") -> str:
            raise RuntimeError(LOGIN_FAILED)

        @self.app.tool()
        def sync_fails(date: str = "") -> str:
            raise RuntimeError(LOGIN_FAILED)

        for name in ("login_fails", "sync_fails"):
            with self.subTest(tool=name), captured("garmin_ext") as records:
                result = _client_run(self.app, lambda client: client.call_tool(name, {}))
                self.assertTrue(result.is_error)
                self.assertEqual([block.text for block in result.content],
                                 [f"Error executing tool {name}: {LOGIN_FAILED}"])
                (record,) = records
                self.assertEqual((record.name, record.levelno, record.getMessage()),
                                 ("garmin_ext.tools", logging.DEBUG, f"Tool {name!r} failed"))
                self.assertIsInstance(record.exc_info[1], RuntimeError)
                self.assertEqual(str(record.exc_info[1]), LOGIN_FAILED)


UPSTREAM_MODULES = (
    "activity_management", "health_wellness", "user_profile", "devices",
    "gear_management", "weight_management", "challenges", "training", "workouts",
    "data_management", "womens_health", "nutrition", "workout_builders", "courses",
    "activity_analysis", "calendar_events",
)


@unittest.skipUnless(HAVE_MCP2 and HAVE_UPSTREAM, "needs mcp 2.x and upstream garmin_mcp")
class UpstreamToolsOnTheToolLoop(ShimState):
    """F7: on the real launch.main() path (stdio, no Garmin login, run()
    stubbed), upstream's async tools are wrapped for the tool loop, and a call
    of get_activities runs there."""

    def serve(self):
        import garmin_mcp
        import launch
        from garmin_mcp import courses, tool_annotations
        from mcp.server.mcpserver import MCPServer

        saved = (courses.register_tools, tool_annotations.READ_ONLY_TOOLS, tool_annotations.ADDITIVE_TOOLS,
                 garmin_mcp.FastMCP)

        def restore():
            (courses.register_tools, tool_annotations.READ_ONLY_TOOLS, tool_annotations.ADDITIVE_TOOLS,
             garmin_mcp.FastMCP) = saved

        self.addCleanup(restore)
        for name in UPSTREAM_MODULES:  # main() configures each module's client
            module = getattr(garmin_mcp, name)
            self.addCleanup(setattr, module, "garmin_client", getattr(module, "garmin_client", None))
        self.addCleanup(setattr, sys, "stdout", sys.stdout)  # upstream's main() wraps it
        self.addCleanup(mcp2_compat.set_server_overrides)
        self.addCleanup(mcp2_compat.set_asgi_middleware)
        self.addCleanup(setattr, launch, "_AUTH", None)
        served = []
        env = {"GARMIN_MCP_TRANSPORT": "stdio", "GARMIN_MCP_STATELESS_HTTP": "false", "REPORT_TZ": "UTC",
               "GARMIN_AUTH": "full"}
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.dict(os.environ, env))
            for key in ("GARMIN_ENABLED_TOOLS", "GARMIN_DISABLED_TOOLS", "GARMIN_PERSONAL_API_KEY",
                        "GARMIN_PERSONAL_API_BASE_URL"):
                os.environ.pop(key, None)
            stack.enter_context(mock.patch.object(garmin_mcp._PendingGarminClient, "start", lambda self, login: self))
            stack.enter_context(mock.patch.object(MCPServer, "run", lambda self, *a, **k: served.append(self)))
            stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
            launch.main([])
        (server,) = served
        return server

    def test_get_activities_runs_on_the_tool_loop(self):
        from garmin_mcp import activity_management

        server = self.serve()
        tool = server._tool_manager.get_tool("get_activities")
        self.assertIsNotNone(tool)
        self.assertIs(getattr(tool.fn, mcp2_compat.TOOL_LOOP_MARKER, None), True)

        threads = []

        class Garmin:  # stands in for upstream's logged-in client
            def get_activities(self, start, limit):
                threads.append(threading.current_thread().name)
                return []

        activity_management.garmin_client = Garmin()  # restored by serve()'s cleanup
        _, result = _call(server, "legacy", "get_activities", {"limit": 1})
        self.assertFalse(result.is_error, result)
        self.assertEqual(result.content[0].text, "No activities found at index 0")
        self.assertEqual(threads, [mcp2_compat.TOOL_LOOP_THREAD_NAME])

    def test_a_failing_upstream_tool_answers_the_fixed_login_text(self):
        # Step 1.3: on this server launch.py's call_tool() override answers a
        # failed call with its group's fixed text and logs it once.
        import tool_errors
        from garmin_mcp import activity_management

        server = self.serve()
        threads = []

        class Garmin:  # raises as upstream's client does before the login is done
            def get_activities(self, start, limit):
                threads.append(threading.current_thread().name)
                raise RuntimeError(LOGIN_FAILED)

        activity_management.garmin_client = Garmin()  # restored by serve()'s cleanup
        with captured("mcp", logging.CRITICAL + 1), captured("garmin_ext") as records:
            _, result = _call(server, "legacy", "get_activities", {"limit": 1})
        self.assertTrue(result.is_error)
        self.assertEqual([block.text for block in result.content], [tool_errors.client_text(tool_errors.LOGIN)])
        self.assertEqual(threads, [mcp2_compat.TOOL_LOOP_THREAD_NAME])
        lines = [(r.levelno, r.getMessage()) for r in records if r.levelno >= logging.INFO]
        self.assertEqual(len(lines), 1, lines)
        self.assertEqual(lines[0][0], logging.WARNING)
        self.assertTrue(lines[0][1].startswith("tool error: tool=get_activities group=login: "), lines[0][1])

    def test_every_async_tool_served_takes_the_tool_loop(self):
        # But the two status reports (R6): they await every Garmin call in a worker thread and time it on their
        # own loop, so they stay on the server's loop, marked by server_loop().
        tools = self.serve()._tool_manager.list_tools()
        self.assertGreater(len(tools), 20)
        own_loop = {"get_daily_status", "get_weekly_status"}
        self.assertLessEqual(own_loop, {tool.name for tool in tools})
        for tool in tools:
            with self.subTest(tool=tool.name):
                self.assertIsNone(tool.context_kwarg)
                on_tool_loop = tool.is_async and tool.name not in own_loop
                self.assertIs(getattr(tool.fn, mcp2_compat.TOOL_LOOP_MARKER, None), on_tool_loop)
                self.assertIs(getattr(tool.fn, mcp2_compat.SERVER_LOOP_MARKER, False), tool.name in own_loop)


if __name__ == "__main__":
    unittest.main()
