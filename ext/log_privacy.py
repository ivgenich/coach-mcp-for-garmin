"""Log privacy for the app, the history worker and the enqueue job: install() once logging has its handlers.

- The Garmin client libraries' own records (loggers "garminconnect" and "garth", with their children) never
  reach the output as written: garminconnect logs a failed call as "API call failed for path
  '/usersummary-service/usersummary/daily/<display name>': ... {<Garmin's answer>}" with a traceback. Each such
  record of WARNING or above becomes one fixed line on the logger "garmin_ext.garmin_library" (its level, the
  library's logger name and the exception class name only); records below WARNING are dropped.
- LevelStreamHandler: one line per record, INFO and below on stdout and WARNING and above on stderr, so a log
  viewer that marks stderr as errors (Railway) shows only real warnings and errors that way.
- Every handler on the root logger gets a filter that replaces each value registered with
  tool_errors.register_secret() with "<redacted>" in the message and the traceback text.

Standard library only (plus tool_errors from this folder); imports on Python 3.10. Idempotent; never raises.
"""

from __future__ import annotations

import logging
import sys
import traceback

import tool_errors

LIBRARY_LOGGERS = ("garminconnect", "garth")
OUT_LOGGER = "garmin_ext.garmin_library"
HIDDEN_LINE = "garmin library: one %s line of %s was hidden (it may quote a Garmin path or answer)%s"


class LevelStreamHandler(logging.StreamHandler):
    """One line per record: below WARNING on stdout, WARNING and above on stderr (split=False: all on stderr).

    The streams are looked up when a record is written (as logging.lastResort does), so a test's
    redirect_stdout/redirect_stderr sees the lines. Each line is flushed, so the two streams keep their order
    as closely as the reader allows. An MCP server on the stdio transport must pass split=False: there stdout
    carries the protocol.
    """

    def __init__(self, fmt: str, split: bool = True):
        logging.Handler.__init__(self)
        self.setFormatter(logging.Formatter(fmt))
        self.split = split

    @property
    def stream(self):
        return sys.stderr

    def emit(self, record):
        try:
            out = sys.stdout if self.split and record.levelno < logging.WARNING else sys.stderr
            out.write(self.format(record) + self.terminator)
            out.flush()
        except RecursionError:
            raise
        except Exception:  # noqa: BLE001 - as logging.StreamHandler: report and go on
            self.handleError(record)


def _exc_name(record) -> str:
    exc = record.exc_info[1] if record.exc_info and len(record.exc_info) > 1 else None
    return f"; exception {type(exc).__name__}" if exc is not None else ""


class _LibraryHandler(logging.Handler):
    """The only handler of a library logger (which no longer propagates): one fixed line per record of WARNING
    and up, on OUT_LOGGER; nothing of the record's message, arguments or traceback."""

    coach_library_handler = True

    def __init__(self):
        super().__init__(logging.WARNING)
        self._out = logging.getLogger(OUT_LOGGER)

    def handle(self, record):
        if record.levelno < self.level:
            return False
        try:
            name = record.name if isinstance(record.name, str) and record.name.isascii() else "a library"
            self._out.log(record.levelno, HIDDEN_LINE, record.levelname, name[:64], _exc_name(record))
        except Exception:  # noqa: BLE001 - logging must never fail the caller
            pass
        return True

    def emit(self, record):  # handle() does the work
        pass


class SecretFilter(logging.Filter):
    """A handler filter: every registered secret (tool_errors.register_secret) becomes "<redacted>" in the
    record's message and traceback text before the handler formats it. Never drops a record."""

    coach_secret_filter = True

    def filter(self, record):
        try:
            if not tool_errors._SECRETS:
                return True
            msg = record.getMessage()
            hidden = tool_errors._hide_secrets(msg)
            if hidden != msg:
                record.msg, record.args = hidden, ()
            if record.exc_info and not record.exc_text:
                record.exc_text = "".join(traceback.format_exception(*record.exc_info)).rstrip("\n")
            if record.exc_text:
                record.exc_text = tool_errors._hide_secrets(record.exc_text)
            if record.stack_info:
                record.stack_info = tool_errors._hide_secrets(record.stack_info)
        except Exception:  # noqa: BLE001 - a record that cannot be read is passed on unchanged
            pass
        return True


_SECRET_FILTER = SecretFilter()


def quiet_libraries() -> None:
    """The library loggers stop propagating and get _LibraryHandler as their only handler (idempotent)."""
    for name in LIBRARY_LOGGERS:
        logger = logging.getLogger(name)
        logger.propagate = False
        if not any(getattr(h, "coach_library_handler", False) for h in logger.handlers):
            logger.handlers[:] = [_LibraryHandler()]


def guard_root_handlers() -> None:
    """SecretFilter on every handler the root logger has now (idempotent)."""
    for handler in logging.getLogger().handlers:
        if not any(getattr(f, "coach_secret_filter", False) for f in handler.filters):
            handler.addFilter(_SECRET_FILTER)


def install() -> None:
    """Both of the above. Call it after the root logger has its handlers (basicConfig, launch's line handler)."""
    try:
        quiet_libraries()
        guard_root_handlers()
    except Exception:  # noqa: BLE001 - never stops a start
        pass
