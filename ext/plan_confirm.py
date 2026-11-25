"""Who may write to the Garmin calendar, and the user's yes/no before a write.

Two guards for plan_commit and plan_cancel (Stage 2) and plan_apply_changes (Stage 4), on top of the plan token:

- caller_may_write(): the request's OAuth access token carries the plan:write
  scope. owner_auth grants it only when the owner ticks the calendar box on the
  consent page, which it shows only with PLAN_WRITES=on. The static
  MCP_BEARER_TOKEN never has it, nor does a grant approved without the box, nor
  a request with no auth context (stdio, tests).
- confirm(): when the client declared the elicitation capability, ask the user
  through MCP elicitation (form mode) with a summary this module builds from
  numbers, ISO dates and role enums only (never Garmin text, workout names or
  descriptions) and one yes/no field. Only an explicit yes is "accepted";
  decline, cancel, an error or no answer within CONFIRM_TIMEOUT_S is
  "declined"; a client without the capability, or a connection that cannot
  carry a server-to-client request (stateless HTTP, the 2026 protocol), gets
  "unsupported" and the caller keeps the token path.

Standard library only at import time: mcp is imported lazily, so the
standard-library test leg can import this module.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import Counter
from typing import Any, Dict, List, Literal, Optional

WRITE_SCOPE = "plan:write"

# How long confirm() waits for the user's answer. A person reads the summary and
# presses a button: three minutes is generous for that and still well inside the
# HTTP idle limits of the edge (Caddy, Railway). No answer in time = declined.
CONFIRM_TIMEOUT_S = 180.0

STATIC_CLIENT_ID = "static-bearer"   # owner_auth.STATIC_CLIENT_ID (not imported: that module needs mcp)

FIELD = "confirm"
MAX_DATES_SHOWN = 14

ConfirmResult = Literal["accepted", "declined", "unsupported"]

_ADD = "Add {count} training-plan workout{s} to your Garmin Connect calendar?"
_REMOVE = "Remove {count} training-plan workout{s} that this server added from your Garmin Connect calendar?"
_BOTH = ("Add {count} training-plan workout{s} to your Garmin Connect calendar and remove {removes} that this "
         "server added earlier?")
_ACTIONS = ("commit", "cancel", "apply")
_APPLY_ONLY = "Change the training plan as approved (no workout on your Garmin Connect calendar changes)?"
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_ROLE = re.compile(r"[a-z][a-z0-9_]{0,31}")

log = logging.getLogger("garmin_ext.plans")


def caller_may_write() -> bool:
    """True only when the current request's access token carries WRITE_SCOPE."""
    try:
        from mcp.server.auth.middleware.auth_context import get_access_token
    except ImportError:
        return False
    token = get_access_token()
    if token is None or getattr(token, "client_id", None) == STATIC_CLIENT_ID:
        return False
    scopes = getattr(token, "scopes", None) or []
    return WRITE_SCOPE in scopes


def _check(count: Any, dates: List[str], roles: List[str]) -> None:
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise ValueError("count must be a non-negative int")
    for value in dates:
        if not isinstance(value, str) or not _DATE.fullmatch(value):
            raise ValueError("dates must be YYYY-MM-DD")
    for value in roles:
        if not isinstance(value, str) or not _ROLE.fullmatch(value):
            raise ValueError("roles must be role enums")


def _detail(label: str, dates: List[str], roles: List[str]) -> List[str]:
    lines = []
    shown = sorted(set(dates))
    if shown:
        text = ", ".join(shown[:MAX_DATES_SHOWN])
        if len(shown) > MAX_DATES_SHOWN:
            text += f" and {len(shown) - MAX_DATES_SHOWN} more"
        lines.append(f"{label}Dates: {text}" if not label else f"{label} dates: {text}")
    if roles:
        counts = Counter(roles)
        sessions = ", ".join(f"{role.replace('_', ' ')} ({n})" for role, n in sorted(counts.items()))
        lines.append(f"Sessions: {sessions}" if not label else f"{label} sessions: {sessions}")
    return lines


def summary(*, action: str, count: int, dates: List[str], roles: List[str], removes: int = 0,
            remove_dates: Optional[List[str]] = None, remove_roles: Optional[List[str]] = None) -> str:
    """The text the user sees. commit: `count` workouts to add on `dates` with `roles`, and `removes`
    workouts this server added earlier (replaced or dropped days) on `remove_dates` with `remove_roles`;
    apply (plan_apply_changes): as commit;
    cancel: `count` workouts to remove. Raises ValueError for an unknown action or a value that is not a
    count, an ISO date or a role enum: nothing free-form reaches the prompt."""
    if action not in _ACTIONS:
        raise ValueError("unknown confirm action")
    remove_dates, remove_roles = list(remove_dates or []), list(remove_roles or [])
    _check(count, dates, roles)
    _check(removes, remove_dates, remove_roles)
    if action == "cancel" and (removes or remove_dates or remove_roles):
        raise ValueError("cancel takes its workouts in count, dates and roles")
    s = "" if count == 1 else "s"
    if action == "cancel":
        lines = [_REMOVE.format(count=count, s=s)] + _detail("", dates, roles)
    elif action == "apply" and not count and not removes:
        lines = [_APPLY_ONLY]
    elif not removes:
        lines = [_ADD.format(count=count, s=s)] + _detail("", dates, roles)
    elif not count:
        lines = [_REMOVE.format(count=removes, s="" if removes == 1 else "s")] + _detail("", remove_dates,
                                                                                      remove_roles)
    else:
        lines = ([_BOTH.format(count=count, s=s, removes=removes)] + _detail("Add", dates, roles)
                 + _detail("Remove", remove_dates, remove_roles))
    lines.append("Answer yes only if you asked for this change in this conversation.")
    return "\n".join(lines)


def _schema() -> Dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            FIELD: {"type": "boolean", "title": "Yes, make this change",
                    "description": "Tick to change your Garmin Connect calendar as described.", "default": False},
        },
        "required": [FIELD],
    }


def _session_of(ctx: Any) -> Any:
    session = getattr(ctx, "session", None)
    if session is None:
        request_context = getattr(ctx, "request_context", None)
        session = getattr(request_context, "session", None)
    return session


def _supports_form(session: Any) -> bool:
    capabilities = getattr(session, "client_capabilities", None)
    if capabilities is None:
        params = getattr(session, "client_params", None)
        capabilities = getattr(params, "capabilities", None)
    elicitation = getattr(capabilities, "elicitation", None)
    if elicitation is None:
        return False
    # 2025-11-25: {"form": {}} and/or {"url": {}}; an empty {} (older clients) means form.
    form, url = getattr(elicitation, "form", None), getattr(elicitation, "url", None)
    return form is not None or url is None


def _can_ask(session: Any) -> bool:
    """False when the request's channel cannot deliver a server-to-client request (mcp 2.x
    ServerSession.can_send_request: stateless HTTP, the 2026 protocol); True when it can or the SDK does not
    say."""
    try:
        return getattr(session, "can_send_request", True) is not False
    except Exception:  # noqa: BLE001 - a session that cannot tell: try, confirm() handles the failure
        return True


def _request_id(ctx: Any) -> Optional[Any]:
    try:
        return getattr(ctx, "request_id", None)
    except Exception:  # noqa: BLE001 - a context outside a request: send it unrelated
        return None


async def confirm(ctx: Any, *, action: str, count: int, dates: List[str], roles: List[str], removes: int = 0,
                  remove_dates: Optional[List[str]] = None,
                  remove_roles: Optional[List[str]] = None) -> ConfirmResult:
    """Ask the user yes/no through MCP elicitation before a Garmin write.

    "accepted" only for an explicit yes; "declined" for no, cancel, an error or a timeout
    (CONFIRM_TIMEOUT_S); "unsupported" when the client did not declare form elicitation.
    Raises ValueError for arguments summary() refuses (a programming error, checked first)."""
    message = summary(action=action, count=count, dates=dates, roles=roles, removes=removes,
                      remove_dates=remove_dates, remove_roles=remove_roles)
    session = _session_of(ctx)
    if session is None or not _supports_form(session) or not _can_ask(session):
        return "unsupported"
    try:
        result = await asyncio.wait_for(
            session.elicit_form(message=message, requested_schema=_schema(), related_request_id=_request_id(ctx)),
            timeout=CONFIRM_TIMEOUT_S)
    except asyncio.TimeoutError:
        log.info("plan confirm: no answer within %d s, counted as declined", int(CONFIRM_TIMEOUT_S))
        return "declined"
    except Exception as exc:  # noqa: BLE001 - any failure to ask is a no; the type only, never a value
        if type(exc).__name__ == "NoBackChannelError":
            # The connection cannot carry a server-to-client request (stateless HTTP, the 2026 protocol): the
            # user was never asked, so this is no answer at all, not a no.
            log.info("plan confirm: the connection cannot ask the user, the token confirms")
            return "unsupported"
        log.warning("plan confirm: elicitation failed (%s), counted as declined", type(exc).__name__)
        return "declined"
    content = getattr(result, "content", None)
    if getattr(result, "action", None) == "accept" and isinstance(content, dict) and content.get(FIELD) is True:
        log.info("plan confirm: accepted")
        return "accepted"
    log.info("plan confirm: declined")
    return "declined"
