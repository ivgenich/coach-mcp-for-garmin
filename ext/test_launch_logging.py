"""Tests for launch.py: the one-line log records and uvicorn's log split (launch_logging.py, re-exported by
launch.py).

    cd ext && python -m unittest test_launch_logging

Shared helpers: test_launch_support.py.
"""

from __future__ import annotations

import contextlib
import importlib
import importlib.metadata
import importlib.util
import io
import logging
import os
import types
import unittest
from unittest import mock

import tool_errors  # noqa: E402
import feature_config  # noqa: E402

from test_launch_support import (fake_db_url, fake_modules, keep_uvicorn_log_config, load_launch, Stubs)


class OneLineLogRecords(unittest.TestCase):
    """Live test R1 (17 Nov 2026): on Railway the SDK's rich handler wrapped
    each record at 80 columns, so live-check.py found no sign-in line. Every
    record must stay one line, also after the SDK's own logging setup."""

    def setUp(self):
        self.launch = load_launch(self, Stubs().modules)
        root = logging.getLogger()
        saved = (list(root.handlers), root.level)

        def restore():
            root.handlers[:] = saved[0]
            root.setLevel(saved[1])

        self.addCleanup(restore)
        root.handlers[:] = []

    def emit(self, setup_twice=False):
        err = io.StringIO()
        long_host = "example-mcp-production-" + "x" * 40 + ".up.railway.app"
        with contextlib.redirect_stderr(err), mock.patch.dict(os.environ, {"COLUMNS": "80"}):
            self.launch._configure_logging()
            if setup_twice:
                self.launch._configure_logging()
            try:  # what MCPServer() does when upstream's main() builds it
                from mcp.server.mcpserver.utilities.logging import configure_logging
            except ImportError:
                pass
            else:
                configure_logging("INFO")
            logging.getLogger("garmin_ext.auth").info("sign-in: on, issuer https://%s, static bearer on", long_host)
        return err.getvalue().splitlines(), long_host

    def test_a_long_record_is_one_plain_line(self):
        lines, host = self.emit()
        self.assertEqual(lines, [f"INFO garmin_ext.auth: sign-in: on, issuer https://{host}, static bearer on"])
        self.assertEqual([type(h).__name__ for h in logging.getLogger().handlers], ["_LineHandler"])
        self.assertEqual(logging.getLogger().level, logging.INFO)

    def test_setting_up_twice_adds_no_second_handler(self):
        lines, _ = self.emit(setup_twice=True)
        self.assertEqual(len(lines), 1)

    def test_a_rich_handler_already_there_is_replaced(self):
        class RichHandler(logging.Handler):  # stands in for rich.logging.RichHandler
            def emit(self, record):
                raise AssertionError("the rich handler wrote a record")

        logging.getLogger().addHandler(RichHandler())
        lines, _ = self.emit()
        self.assertEqual(len(lines), 1)
        self.assertEqual([type(h).__name__ for h in logging.getLogger().handlers], ["_LineHandler"])

    def test_garminconnect_records_and_registered_secrets_never_reach_the_output(self):
        """garminconnect logs a failed call with the display name in the path and Garmin's
        answer, with a traceback; after _configure_logging() each such record is one fixed line, and a
        registered secret in any record reads <redacted>."""
        names = ("garminconnect", "garminconnect.client", "garth")
        saved = {n: (list(logging.getLogger(n).handlers), logging.getLogger(n).propagate) for n in names}

        def restore():
            for n, (handlers, propagate) in saved.items():
                logging.getLogger(n).handlers[:] = handlers
                logging.getLogger(n).propagate = propagate

        self.addCleanup(restore)
        secret = "canary-app-registered-secret"
        tool_errors.register_secret(secret)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.launch._configure_logging()
            path = "/usersummary-service/usersummary/daily/canary-display-name"
            try:
                raise ConnectionError('API Error 500 - {"note": "canary garmin body"}')
            except ConnectionError as exc:
                logging.getLogger("garminconnect").exception("API call failed for path '%s': %s", path, exc)
            logging.getLogger("garminconnect.client").warning("%s failed", path)
            logging.getLogger("garmin_ext.auth").warning("a line with %s", secret)
        out = err.getvalue()
        for leak in ("canary-display-name", "canary garmin body", "usersummary", "Traceback", secret):
            self.assertNotIn(leak, out)
        self.assertEqual(out.count("garmin library: one "), 2, out)
        self.assertIn("WARNING garmin_ext.auth: a line with <redacted>", out)

    def split_run(self, transport):
        """(stdout lines, stderr lines) of one INFO and one WARNING record with that transport."""
        secret = "canary-split-registered-secret"
        tool_errors.register_secret(secret)
        out, err = io.StringIO(), io.StringIO()
        env = {"GARMIN_MCP_TRANSPORT": transport} if transport else {}
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), mock.patch.dict(os.environ, env):
            if not transport:
                os.environ.pop("GARMIN_MCP_TRANSPORT", None)
            self.launch._configure_logging()
            logging.getLogger("garmin_ext.auth").info("sign-in: on %s", secret)
            logging.getLogger("garmin_ext.auth").warning("rate limited")
        return out.getvalue().splitlines(), err.getvalue().splitlines()

    def test_on_the_network_info_goes_to_stdout_and_warnings_to_stderr(self):
        # Railway marks every stderr line as an error.
        out, err = self.split_run("streamable-http")
        self.assertEqual(out, ["INFO garmin_ext.auth: sign-in: on <redacted>"])
        self.assertEqual(err, ["WARNING garmin_ext.auth: rate limited"])

    def test_on_stdio_every_line_stays_on_stderr(self):
        # stdout carries the MCP protocol there.
        for transport in ("stdio", None):
            out, err = self.split_run(transport)
            self.assertEqual(out, [], transport)
            self.assertEqual(err, ["INFO garmin_ext.auth: sign-in: on <redacted>",
                                   "WARNING garmin_ext.auth: rate limited"], transport)

    def test_a_role_run_sets_up_its_logging_first(self):
        order = []
        module = types.SimpleNamespace(configure_logging=lambda: order.append("logging"),
                                       main=lambda: order.append("main") or 0)
        fake_modules(self, worker=module)
        self.assertEqual(self.launch._run_role(feature_config.load({"HISTORY_SEARCH": "on", "ROLE": "worker",
                                                                     "DATABASE_URL": fake_db_url()})), 0)
        self.assertEqual(order, ["logging", "main"])


@unittest.skipUnless(importlib.util.find_spec("uvicorn") is not None, "needs uvicorn")
class UvicornLogSplit(unittest.TestCase):
    """uvicorn's own INFO lines on stdout with a network transport, its warnings on stderr; never on stdio."""

    def setUp(self):
        from uvicorn import config as uvicorn_config

        self.launch = load_launch(self, Stubs().modules)
        self.config = uvicorn_config.LOGGING_CONFIG
        keep_uvicorn_log_config(self)
        loggers = [logging.getLogger(n) for n in ("uvicorn", "uvicorn.error", "uvicorn.access")]
        state = [(lg, list(lg.handlers), lg.level, lg.propagate, lg.disabled) for lg in loggers]

        def restore():
            for lg, handlers, level, propagate, disabled in state:
                lg.handlers[:] = handlers
                lg.setLevel(level)
                lg.propagate, lg.disabled = propagate, disabled

        self.addCleanup(restore)

    def lines(self):
        """(stdout, stderr) of one INFO and one WARNING line on uvicorn.error after uvicorn's own dictConfig."""
        import uvicorn

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            # Config() runs uvicorn's dictConfig; its plain StreamHandler binds sys.stderr then.
            uvicorn.Config(app=None, log_level="info")
            logging.getLogger("uvicorn.error").info("Started server process [1]")
            logging.getLogger("uvicorn.error").warning("something to look at")
        return out.getvalue(), err.getvalue()

    def test_network_transport_splits(self):
        for transport in ("streamable-http", " SSE "):
            with self.subTest(transport=transport):
                self.assertTrue(self.launch._split_uvicorn_logs(transport))
        out, err = self.lines()
        self.assertIn("Started server process [1]", out)
        self.assertNotIn("something to look at", out)
        self.assertIn("something to look at", err)
        self.assertNotIn("Started server process", err)

    def test_stdio_does_not(self):
        for transport in ("stdio", None, " STDIO "):
            with self.subTest(transport=transport):
                self.assertFalse(self.launch._split_uvicorn_logs(transport))
        self.assertEqual(self.config["handlers"]["default"]["stream"], "ext://sys.stderr")
        out, err = self.lines()
        self.assertEqual(out, "")
        self.assertIn("Started server process [1]", err)


if __name__ == "__main__":
    unittest.main()
