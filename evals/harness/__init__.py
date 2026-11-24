"""Eval harness: the real MCP server in process over a fake Garmin, a frozen clock and a fixed error ref.

    from evals.harness import load_state, start_server

    state = load_state("daily_2026_03_18", ["daily_yellow"], {})
    with start_server("full", state, now="2026-03-18T06:30", tz="Europe/Athens") as srv:
        srv.tools          # [{"name", "description", "input_schema"}], exactly as MCP list_tools gives them
        srv.instructions   # the server's instructions for the mode
        srv.call("get_daily_status", {})   # ToolOutcome(text, is_error)
        srv.calls          # the Garmin client calls made: [{"tool", "method", "args", "kwargs"}]

See evals/harness/README.md for the state format, the Garmin methods each tool calls and the faults.
"""

from .server import FIXED_REF, Server, ToolOutcome, clock_line, start_server
from .state import FAULT_GROUPS, StateError, load_state

__all__ = ["FAULT_GROUPS", "FIXED_REF", "Server", "StateError", "ToolOutcome", "clock_line", "load_state",
           "start_server"]
