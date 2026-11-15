"""Tests for mcp2_compat: upstream's FastMCP calls work on mcp 2.x MCPServer.

    python -m unittest discover -s ext -p "test_mcp2_compat*.py" -v

Skipped without mcp 2.x (the py3.10/3.12 venvs may carry mcp 1.x or none);
the version-check tests run everywhere because they mock the SDK version.
The _ToolFilter tests also need upstream garmin_mcp.
"""

from __future__ import annotations

import asyncio
import importlib.metadata
import importlib.util
import json
import os
import sys
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


if __name__ == "__main__":
    unittest.main()
