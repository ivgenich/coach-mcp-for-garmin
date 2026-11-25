"""launch.py's log setup: one plain line per record, INFO on stdout and warnings and errors on stderr.

_configure_logging() in launch.py is configure_logging() here; _split_uvicorn_logs() is
split_uvicorn_logs(). Standard library and log_privacy at import time; the SDK and uvicorn are imported
where used, so the standard-library test leg runs without them.
"""

from __future__ import annotations

import logging
import os

import log_privacy

LOG_FORMAT = "%(levelname)s %(name)s: %(message)s"


class _LineHandler(log_privacy.LevelStreamHandler):
    """One plain line per record, INFO on stdout and warnings and errors on
    stderr (Railway marks every stderr line as an error), all on stderr with
    the stdio transport, whose stdout carries the protocol. The streams are
    whatever sys.stdout and sys.stderr are at the time, so a test's redirect
    sees the lines."""

    coach_line_handler = True

    def __init__(self, split: bool = False):
        super().__init__(LOG_FORMAT, split=split)


def configure_logging():
    """INFO on the root logger, one plain line per record (_LineHandler), set
    up now so the sign-in's startup lines are not dropped.

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
        transport = (os.environ.get("GARMIN_MCP_TRANSPORT") or "stdio").strip().lower()
        root.addHandler(_LineHandler(split=transport != "stdio"))
    root.setLevel(logging.INFO)
    # garminconnect logs a failed call with the display name in the path and Garmin's answer: hidden; and
    # every registered secret redacted on the root handlers.
    log_privacy.install()
    try:
        from mcp.server.mcpserver.utilities.logging import configure_logging as sdk_configure_logging
    except ImportError:  # the standard-library test leg; the image has the SDK
        return
    sdk_configure_logging("INFO")


def uvicorn_line_handler(split=True):
    """The handler uvicorn's dictConfig builds for its "default" handler: INFO on stdout, warnings and errors
    on stderr (log_privacy.LevelStreamHandler; uvicorn's own formatter replaces the format given here)."""
    return log_privacy.LevelStreamHandler(LOG_FORMAT, split=split)


def split_uvicorn_logs(transport):
    """With a network transport, uvicorn's own lines ("INFO:     Started server process ...") go to stdout and
    only its warnings and errors to stderr, as _LineHandler splits ours: uvicorn's LOGGING_CONFIG sends its
    "default" handler to stderr, and Railway marks every stderr line as an error. Never on the stdio
    transport, whose stdout carries the protocol.

    The SDK builds uvicorn.Config(app, host, port, log_level) without log_config (mcp 2.3.0,
    MCPServer.run_streamable_http_async and run_sse_async), so Config uses its default argument, which is the
    LOGGING_CONFIG dict object itself: it is changed in place, before the server starts. True when changed."""
    if (transport or "stdio").strip().lower() == "stdio":
        return False
    try:
        from uvicorn import config as uvicorn_config
    except ImportError:  # the standard-library test leg; the image has uvicorn
        return False
    handlers = uvicorn_config.LOGGING_CONFIG.get("handlers", {})
    default = handlers.get("default")
    if not isinstance(default, dict):
        return False
    formatter = default.get("formatter", "default")
    handlers["default"] = {"()": uvicorn_line_handler, "split": True, "formatter": formatter}
    return True
