"""Who may write to the Garmin calendar, and the user's yes/no before a write.

Two guards for plan_commit and plan_cancel (Stage 2) and plan_apply_changes (Stage 4), on top of the plan token:

- caller_may_write(): the request's OAuth access token carries the plan:write
  scope. owner_auth grants it only when the owner ticks the calendar box on the
  consent page, which it shows only with PLAN_WRITES=on. The static
  MCP_BEARER_TOKEN never has it, nor does a grant approved without the box, nor
  a request with no auth context (stdio, tests).
- confirm(): when the client declared form elicitation, ask the user with a
  summary this module builds from numbers, ISO dates and role enums only (never
  Garmin text, workout names or descriptions) and one required choice between
  two fixed labels fitting the change (choices(): "Don't add" / "Add them" and
  the like, the refusal listed first). Only action "accept" with exactly the
  approving label is "accepted"; decline, cancel, the other label, any other
  content (a boolean included: spike S0.5 saw a ticked checkbox come back false,
  so a boolean never carries consent), an error or no answer is "declined". A
  client without form elicitation gets "unsupported" and the caller keeps the
  token path (confirmed_by "token").

How the question travels depends on the request's protocol:

- 2026-07-28 and later, the input-required round. A server-to-client request
  cannot be sent during a tool call there (the SDK's
  ServerSession.can_send_request is False; production also serves stateless
  HTTP). Round 1: confirm() returns an mcp_types.InputRequiredResult whose
  input_requests hold the form under the key FIELD and whose request_state is a
  state this module minted; the tool returns it as is, having written nothing
  and called Garmin not at all (its caller has only read the token row: the
  token is not claimed). The client shows the form, then calls the same tool
  again with the same arguments, the answer in Context.input_responses and the
  state echoed in Context.request_state. Round 2: confirm() reads the answer,
  only when that state is valid for this very call.
- Older protocols, on a connection that can carry a server-to-client request:
  session.elicit_form() during the call, waiting at most CONFIRM_TIMEOUT_S. A
  connection that cannot (stateless HTTP) gets "unsupported".

How round 2 is bound to round 1, and why a client cannot skip round 1:

- The state is "pc1.<payload>.<mac>". The payload holds a digest of the call's
  binding (the action, plan_id, token and, for plan_apply_changes, the change
  ids, as binding() digests them: the token itself is never in the state), a
  digest of the exact message the user is shown, and the time it was minted.
  The mac is HMAC-SHA256 under a key this process draws at import (_STATE_KEY),
  never logged nor sent. A state is good for STATE_TTL_S (600 s, as the SDK's
  own request-state TTL).
- An answer counts only when the echoed state verifies, has not expired, and
  carries the binding of the current call and the digest of the message the
  current call would show (so the ops did not change in between). Anything
  else, a first call that brings input_responses but no state included (a
  client or a model forging an answer), is round 1 again: the input_responses
  are ignored, the form is asked anew, nothing is written. So an answer counts
  only for a form this process sent for this action, plan, token and summary.
  A state minted for another token, plan or action (an argument changed between
  the rounds) is likewise round 1 for the new arguments.
- On top of that the SDK seals request_state at the wire: MCPServer installs
  mcp.server.request_state.RequestStateBoundary by default (no
  request_state_security given: RequestStateSecurity.ephemeral(), a process
  key, audience the server name). The client sees only AES-GCM ciphertext bound
  to the tool name, a digest of the arguments, the signed-in principal and the
  audience; a state echoed to another tool, with other arguments or by another
  principal is refused before the tool runs (JSON-RPC -32602 "Invalid or expired
  requestState"). This module's own mac does not rely on that boundary: it
  holds on a server without one, and in the unit tests.
- The app runs one process. A restart between the two rounds makes the state
  unknown (the SDK refuses it, or this module asks again): nothing is written
  either way and the user calls again. A multi-process deployment would need
  RequestStateSecurity(keys=...) for the SDK and a shared key here.

Standard library only at import time: mcp is imported lazily, so the
standard-library test leg can import this module.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import re
import secrets
import time
from collections import Counter
from typing import Any, Dict, List, Literal, Optional, Tuple, Union

WRITE_SCOPE = "plan:write"

# How long confirm() waits for the user's answer. A person reads the summary and
# presses a button: three minutes is generous for that and still well inside the
# HTTP idle limits of the edge (Caddy, Railway). No answer in time = declined.
CONFIRM_TIMEOUT_S = 180.0

STATIC_CLIENT_ID = "static-bearer"   # owner_auth.STATIC_CLIENT_ID (not imported: that module needs mcp)

FIELD = "confirm"
MAX_DATES_SHOWN = 14

# The first protocol revision whose tools/call carries elicitation in an InputRequiredResult (pinned, as the
# SDK's mcp.server.mcpserver.resolve._INPUT_REQUIRED_VERSION is).
INPUT_REQUIRED_VERSION = "2026-07-28"
# How long a round-1 state stays good (the SDK's RequestStateSecurity default TTL).
STATE_TTL_S = 600.0
STATE_SKEW_S = 60.0
_STATE_PREFIX = "pc1."
_STATE_MAX = 1024
_STATE_KEY = secrets.token_bytes(32)   # this process only; never logged, never sent

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


def choices(*, action: str, count: int, removes: int = 0) -> Tuple[str, str]:
    """(refuse, approve): the two fixed labels of the form's one field, fitting the change."""
    if action == "cancel":
        return ("Don't remove", "Remove them")
    if action == "apply" and not count and not removes:
        return ("Don't change it", "Change the plan")
    if count and removes:
        return ("Don't change anything", "Make these changes")
    if removes:
        return ("Don't remove", "Remove them")
    return ("Don't add", "Add them")


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
    approve = choices(action=action, count=count, removes=removes)[1]
    lines.append(f'Choose "{approve}" only if you asked for this change in this conversation.')
    return "\n".join(lines)


def _schema(labels: Tuple[str, str]) -> Dict[str, Any]:
    """One required string enum and no default: the user picks a label. Never a boolean (a ticked checkbox
    comes back false, spike S0.5). The refusal is listed first, so a client that preselects the first entry
    preselects no."""
    return {
        "type": "object",
        "properties": {
            FIELD: {"type": "string", "title": "Your answer", "enum": list(labels),
                    "description": f'Choose "{labels[1]}" to change your Garmin Connect calendar as described.'},
        },
        "required": [FIELD],
    }


def _session_of(ctx: Any) -> Any:
    try:
        session = getattr(ctx, "session", None)
        if session is None:
            request_context = getattr(ctx, "request_context", None)
            session = getattr(request_context, "session", None)
    except Exception:  # noqa: BLE001 - a context outside a request has no session
        return None
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


def _protocol(ctx: Any) -> Optional[str]:
    """The request's negotiated protocol version (Context.protocol_version); None when unknown."""
    for owner in (ctx, getattr(ctx, "request_context", None)):
        try:
            value = getattr(owner, "protocol_version", None)
        except Exception:  # noqa: BLE001 - a context outside a request: unknown
            value = None
        if isinstance(value, str) and _DATE.fullmatch(value):
            return value
    return None


def _input_required(ctx: Any) -> bool:
    """True when this request asks through the input-required round (protocol 2026-07-28 or later)."""
    version = _protocol(ctx)
    return version is not None and version >= INPUT_REQUIRED_VERSION   # YYYY-MM-DD compares as text


def _digest(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(raw).hexdigest()[:32]


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _mac(payload: str) -> str:
    return _b64(hmac.new(_STATE_KEY, payload.encode(), hashlib.sha256).digest())


def _mint_state(key: str, message: str) -> str:
    claims = {"b": key, "m": _digest(message), "t": int(time.time())}
    payload = _b64(json.dumps(claims, separators=(",", ":")).encode())
    return f"{_STATE_PREFIX}{payload}.{_mac(payload)}"


def _state_matches(state: Any, key: str, message: str) -> bool:
    """True only for a state this process minted, unexpired, for this binding and this message."""
    if not isinstance(state, str) or not state.startswith(_STATE_PREFIX) or len(state) > _STATE_MAX:
        return False
    payload, _sep, mac = state[len(_STATE_PREFIX):].partition(".")
    if not payload or not hmac.compare_digest(mac.encode(), _mac(payload).encode()):
        return False
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        minted = float(claims["t"])
        same = (hmac.compare_digest(str(claims["b"]), key)
                and hmac.compare_digest(str(claims["m"]), _digest(message)))
    except (ValueError, KeyError, TypeError):
        return False
    now = time.time()
    return same and minted - STATE_SKEW_S <= now < minted + STATE_TTL_S


def _answer_of(responses: Any) -> Tuple[Any, Any]:
    """(action, content) of the response under FIELD; (None, None) when there is none."""
    if not isinstance(responses, dict):
        return None, None
    response = responses.get(FIELD)
    if isinstance(response, dict):
        return response.get("action"), response.get("content")
    return getattr(response, "action", None), getattr(response, "content", None)


def _is_yes(action: Any, content: Any, approve: str) -> bool:
    if action != "accept" or not isinstance(content, dict):
        return False
    value = content.get(FIELD)
    return isinstance(value, str) and value == approve


def _ask(message: str, labels: Tuple[str, str], state: str) -> Any:
    """Round 1: the InputRequiredResult the tool returns; None when the SDK lacks the 2026 types."""
    try:
        import mcp_types
    except ImportError:
        return None
    request = mcp_types.ElicitRequest(method="elicitation/create", params=mcp_types.ElicitRequestFormParams(
        mode="form", message=message, requested_schema=_schema(labels)))
    return mcp_types.InputRequiredResult(input_requests={FIELD: request}, request_state=state)


def binding(*parts: Any) -> str:
    """The binding of one call (action, plan_id, token, change ids), as confirm(bind=) takes it: a digest, so
    the token itself never goes into the state."""
    return _digest([str(p) for p in parts])


def is_input_required(value: Any) -> bool:
    """True for the InputRequiredResult confirm() returns in round 1."""
    return getattr(value, "result_type", None) == "input_required"


async def confirm(ctx: Any, *, action: str, count: int, dates: List[str], roles: List[str], removes: int = 0,
                  remove_dates: Optional[List[str]] = None, remove_roles: Optional[List[str]] = None,
                  bind: str = "") -> Union[ConfirmResult, Any]:
    """Ask the user before a Garmin write.

    "accepted" only for action accept with the approving label; "declined" for anything else, an error or (on
    older protocols) no answer within CONFIRM_TIMEOUT_S; "unsupported" when the client did not declare form
    elicitation, or the connection carries neither the input-required round nor a server-to-client request.
    On protocol 2026-07-28 and later, round 1 (no valid state for `bind` and this summary) returns the
    InputRequiredResult the tool returns as is (is_input_required()); round 2 reads the answer. `bind` is
    binding() of the call's action, plan_id and token (and change ids). Raises ValueError for arguments
    summary() refuses (a programming error, checked first)."""
    message = summary(action=action, count=count, dates=dates, roles=roles, removes=removes,
                      remove_dates=remove_dates, remove_roles=remove_roles)
    labels = choices(action=action, count=count, removes=removes)
    session = _session_of(ctx)
    if session is None or not _supports_form(session):
        return "unsupported"
    if _input_required(ctx):
        try:
            state = getattr(ctx, "request_state", None)
            responses = getattr(ctx, "input_responses", None)
        except Exception:  # noqa: BLE001 - a context that cannot tell: round 1
            state, responses = None, None
        key = _digest([action, bind])
        if not _state_matches(state, key, message):
            if responses or state is not None:
                log.info("plan confirm: an answer without a matching state, asked again")
            ask = _ask(message, labels, _mint_state(key, message))
            if ask is None:
                return "unsupported"
            log.info("plan confirm: asked through the input-required round")
            return ask
        if _is_yes(*_answer_of(responses), labels[1]):
            log.info("plan confirm: accepted")
            return "accepted"
        log.info("plan confirm: declined")
        return "declined"
    if not _can_ask(session):
        return "unsupported"
    try:
        result = await asyncio.wait_for(
            session.elicit_form(message=message, requested_schema=_schema(labels),
                                related_request_id=_request_id(ctx)),
            timeout=CONFIRM_TIMEOUT_S)
    except asyncio.TimeoutError:
        log.info("plan confirm: no answer within %d s, counted as declined", int(CONFIRM_TIMEOUT_S))
        return "declined"
    except Exception as exc:  # noqa: BLE001 - any failure to ask is a no; the type only, never a value
        if type(exc).__name__ == "NoBackChannelError":
            # The connection cannot carry a server-to-client request (stateless HTTP): the user was never
            # asked, so this is no answer at all, not a no.
            log.info("plan confirm: the connection cannot ask the user, the token confirms")
            return "unsupported"
        log.warning("plan confirm: elicitation failed (%s), counted as declined", type(exc).__name__)
        return "declined"
    if _is_yes(getattr(result, "action", None), getattr(result, "content", None), labels[1]):
        log.info("plan confirm: accepted")
        return "accepted"
    log.info("plan confirm: declined")
    return "declined"
