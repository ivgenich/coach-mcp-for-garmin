"""Put a failed tool call in one of six groups and write the short, fixed text the model sees.

The groups are login, rate_limited, bad_input, no_data, unavailable and internal (step 1.3 contract, sections 4.2
and 5.4-5.8, with amendments M2, M6 and M7). A failure arrives either as text (upstream answers "Error ...: ..."
instead of raising) or as an exception that reached the tool-call override. classify_text() and
classify_exception() pick the group with the ordered table of section 5.6; client_text() gives the fixed text for
the client, source_phrase() the short phrase for a status report's sources, and redact() the one-line form of an
original text for the server log.

Caller values echoed in error texts (an id, a date, a tool name) never pick the group: they are blanked out of
the copy the table reads, and texts this repository or upstream wrote for bad arguments are recognised by how they
start (M7). An exception only gets the full table when the failure started in Garmin, the network, a timeout or
this repository's own known failures (KnownFailure); any other exception is a bug in the server and is internal
(M2), whatever its message says, unless it carries the Garmin login text.

Standard library only (plus text_hygiene from this folder); imports on Python 3.10. Nothing here raises on any
input, except client_text() on a programming error: an unknown group, or INTERNAL without a ref from new_ref().
No logging, no I/O.
"""

import itertools
import json
import re
import secrets
import threading

import text_hygiene

LOGIN, RATE_LIMITED, BAD_INPUT, NO_DATA, UNAVAILABLE, INTERNAL = (
    "login", "rate_limited", "bad_input", "no_data", "unavailable", "internal")
GROUPS = (LOGIN, RATE_LIMITED, BAD_INPUT, NO_DATA, UNAVAILABLE, INTERNAL)
IS_ERROR = {LOGIN: True, RATE_LIMITED: True, BAD_INPUT: True, NO_DATA: False, UNAVAILABLE: True, INTERNAL: True}
GROUP_PHRASE = {LOGIN: "Garmin login missing or expired", RATE_LIMITED: "Garmin rate limit",
                BAD_INPUT: "Invalid input", NO_DATA: "No data:", UNAVAILABLE: "Garmin Connect did not answer",
                INTERNAL: "Internal server error"}
# Fixer (EV-SEC-2): the head may hold colons and runs up to 200 characters, matched lazily (contract 4.2 had
# [^:\n]{1,80}), so a caller value before the colon cannot hide a failure.
FAILURE_RE = re.compile(r"^Error(?: [^\n]{1,200}?)?: ")
PLAIN_FAILURE_PREFIXES = ("Invalid date format:", "Date range too large", "end_date must be on or after start_date",
                          "Provide both start_date and end_date", "Unexpected response format",
                          "fitparse library is not installed")
# Upstream failure texts that write the caller's value before their colon, known by how they start whatever that
# value holds (a long id, a colon, a line break): the FIT tools (UP/activity_analysis.py:1835, :1946).
ECHO_FAILURE_PREFIXES = ("Error downloading FIT data for activity ",)
UPSTREAM_HINTS = ("Garmin Connect may be unreachable; check your network connection or try again later",)

# Bad-input details (section 5.5).
DETAIL_UNKNOWN_TOOL = "there is no tool with that name; check the tool list"
DATE_DETAIL = "dates must be real calendar dates written YYYY-MM-DD"
RANGE_MAX_DETAIL = "the date range is too long: at most {n} days"
RANGE_DETAIL = "the date range is too long"
ORDER_DETAIL = "end_date must be on or after start_date"
BOTH_DATES_DETAIL = "give both start_date and end_date, or neither"
TEXT_FORMAT_DETAIL = 'text_format must be empty or "markdown"'
LIMIT_DETAIL = "limit is too large: at most {n}"
NUMBER_DETAIL = "an id, limit or count must be a whole number"
RANGE_VALUE_DETAIL = "an argument is outside its allowed values; check the tool description"
GARMIN_400_DETAIL = "Garmin rejected the request parameters; check them against the tool description"
GENERIC_DETAIL = "check the arguments against the tool description"

DETAIL_MAX = 200  # a longer detail is cut to 197 characters and "..."

# pydantic error type -> the words after the field name (section 5.5).
_PROBLEM_WORDS = {
    "missing": "is required",
    "int_parsing": "must be a whole number", "int_type": "must be a whole number",
    "int_from_float": "must be a whole number",
    "float_parsing": "must be a number", "float_type": "must be a number",
    "bool_parsing": "must be true or false", "bool_type": "must be true or false",
    "string_type": "must be text",
    "date_parsing": "must be a date written YYYY-MM-DD",
    "date_from_datetime_parsing": "must be a date written YYYY-MM-DD",
}
_OTHER_PROBLEM = "has an invalid value"

# Client texts (section 5.4): ASCII, at most 300 characters.
_CLIENT_TEXTS = {
    LOGIN: ("Error: Garmin login missing or expired. This server cannot read Garmin data until its owner signs in to "
            "Garmin again: run garmin-mcp-auth on the server, then restart it. Retrying will not help until then."),
    RATE_LIMITED: ("Error: Garmin rate limit. Garmin is refusing requests from this server for now. Stop calling "
                   "Garmin tools for at least 15 minutes, even if the user asks you to try again: each call can extend "
                   "the block. Tell the user to try again in 15 minutes or later."),
    BAD_INPUT: "Error: Invalid input: {detail}. Fix the arguments and call the tool again.",
    NO_DATA: ("No data: Garmin has nothing for this request. Check the id or the date (ids come from get_activities, "
              "get_scheduled_workouts or get_courses); repeating the same call will not help."),
    UNAVAILABLE: ("Error: Garmin Connect did not answer (a timeout, a network problem or an error on Garmin's side). "
                  "Retry this call at most once; if it fails again, stop calling Garmin tools and tell the user. "
                  "Try again in a few minutes."),
    INTERNAL: ("Error: Internal server error (ref {ref}). Retrying once later may help. If it fails again, tell the "
               "user; the server owner can find the details in the server log under this ref."),
}
# Key mode (GARMIN_AUTH=key, KEY D12 and 4.8) changes two texts only, after their first words: the login remedy is
# a new Personal API key, and the no-data hint names no tool key mode lacks. The other four groups are shared.
_KEY_MODE_TEXTS = {
    LOGIN: ("Error: Garmin login missing or expired. This server cannot read Garmin data until its owner makes a new "
            "Personal API key in Garmin Connect and sets it on the server. Retrying will not help until then."),
    NO_DATA: ("No data: Garmin has nothing for this request. Check the id or the date (ids come from get_activities "
              "or get_activities_by_date); repeating the same call will not help."),
}

# A date Garmin has no sleep for (live test B3: the night had not synced yet). Upstream's get_sleep_summary then
# answers an empty JSON object, or one with only nulls, which tells the model nothing; launch answers this instead.
NO_SLEEP_TEXT = ("No data: Garmin has no sleep recorded for this date. If the night just ended, the watch may not have "
                 "synced yet: sync it in the Garmin Connect app and ask again later. Repeating the call right away "
                 "will not help.")
NO_SLEEP_LOG = "no data from Garmin (empty sleep summary)"
_SLEEP_SUMMARY_MAX = 4000  # upstream's summary is about 350 bytes


def is_empty_sleep_summary(text) -> bool:
    """True when `text` is a get_sleep_summary answer with no value in it: a JSON object that is empty or holds
    only nulls. False for anything else (other JSON, a "No ... found" text, a non-str, a long text). Never raises."""
    if not isinstance(text, str) or len(text) > _SLEEP_SUMMARY_MAX:
        return False
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        return False
    return isinstance(data, dict) and all(value is None for value in data.values())


# Source phrases (section 5.7).
_SOURCE_PHRASES = {LOGIN: "Garmin login missing or expired", RATE_LIMITED: "Garmin rate limit",
                   BAD_INPUT: "invalid input", NO_DATA: "no data from Garmin",
                   UNAVAILABLE: "Garmin Connect did not answer"}
_INTERNAL_PHRASE = "internal error"

_REF_RE = re.compile(r"[0-9a-f]{8}")
_TODAY_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_I = re.IGNORECASE


class KnownFailure(Exception):
    """Base of the failures whose text this repository wrote and the table can read (status_common's
    BadArgument, NoData and SystemicFailure subclass it). classify_exception() gives them the full table."""


# --- the table (section 5.6, with M2's "mfa required" in L1) -------------------------------------------

_L1 = re.compile(r"garmin login|garmin-mcp-auth|garmin authentication failed|authentication failed"
                 r"|authenticationerror|not authenticated|unauthori[sz]ed|refresh token|token exchange failed"
                 r"|api error 401(?!\d)|\(401\)|mfa required", _I)

_TABLE = (
    (_L1, LOGIN, None),
    (re.compile(r"rate limit|toomanyrequests|too many requests|api error 429(?!\d)|\(429\)|returned 429(?!\d)"
                r"|429 in json", _I), RATE_LIMITED, None),
    (re.compile(r"api error 404(?!\d)|\(404\)", _I), NO_DATA, None),
    (re.compile(r"api error (?:403|5\d\d)(?!\d)|\((?:403|5\d\d)\)", _I), UNAVAILABLE, None),
    (re.compile(r"api error 400(?!\d)|\(400\)", _I), BAD_INPUT, GARMIN_400_DETAIL),
    (re.compile(r"\bunknown tool:", _I), BAD_INPUT, DETAIL_UNKNOWN_TOOL),
    (re.compile(r"date range too large \(\d+ days?\)\. maximum is (\d{1,4}) days", _I), BAD_INPUT, RANGE_MAX_DETAIL),
    (re.compile(r"cannot be more than one year before", _I), BAD_INPUT, RANGE_DETAIL),
    (re.compile(r"must be on or after start_date|start ?date cannot be after end ?date", _I), BAD_INPUT,
     ORDER_DETAIL),
    (re.compile(r"provide both start_date and end_date|must either provide all parameters or no parameters", _I),
     BAD_INPUT, BOTH_DATES_DETAIL),
    (re.compile(r"yyyy-mm-dd|invalid date format|invalid (?:start_?date|end_?date|cdate|date)\b"
                r"|day is out of range for month|month must be in 1\.\.12", _I), BAD_INPUT, DATE_DETAIL),
    (re.compile(r"text_format must be", _I), BAD_INPUT, TEXT_FORMAT_DETAIL),
    (re.compile(r"limit cannot exceed (\d{1,6})", _I), BAD_INPUT, LIMIT_DETAIL),
    (re.compile(r"invalid literal for int\(\)|must be an integer|must be a positive integer|must be a number", _I),
     BAD_INPUT, NUMBER_DETAIL),
    (re.compile(r"must be positive|must be non-negative|must be between|must be one of|must be an int in", _I),
     BAD_INPUT, RANGE_VALUE_DETAIL),
    (re.compile(r"\binvalid input\b", _I), BAD_INPUT, GENERIC_DETAIL),
    (re.compile(r"no data received from server|not found or missing geopoints|fewer than two points with elevation"
                r"|empty fit download|no data from garmin|\bno data:", _I), NO_DATA, None),
    (re.compile(r"connection error|did not return within|did not answer|timed out|timeout|max retries exceeded"
                r"|failed to establish a new connection|name resolution|nodename nor servname"
                r"|name or service not known|connection refused|connection reset|connection aborted"
                r"|remote end closed|network is unreachable|service unavailable|bad gateway|gateway time-?out", _I),
     UNAVAILABLE, None),
)

# M7: texts this repository or upstream writes for a bad argument, recognised by how they start (re.match on the
# lstripped text), before row L1. The last two rows (B1's addition) read this module's own bad-input text back,
# so a client text classifies to its own group and detail.
_ANCHORED = (
    (re.compile(r"(?:error: )?date must be", _I), DATE_DETAIL),
    (re.compile(r"invalid date format:", _I), DATE_DETAIL),
    (re.compile(r"date range too large \(\d+ days?\)\. maximum is (\d{1,4}) days", _I), RANGE_MAX_DETAIL),
    (re.compile(r"date range too large", _I), RANGE_DETAIL),
    (re.compile(r"(?:error: )?text_format must be", _I), TEXT_FORMAT_DETAIL),
    (re.compile(r"(?:error: )?end_date must be on or after start_date", _I), ORDER_DETAIL),
    (re.compile(r"(?:error: )?provide both start_date and end_date", _I), BOTH_DATES_DETAIL),
    (re.compile(r"(?:error: )?invalid input: ([ -~]{1,200}?)\. fix the arguments and call the tool again\.", _I),
     None),  # the detail is the captured text
    (re.compile(r"(?:error: )?invalid input:", _I), GENERIC_DETAIL),
)

_HINTS_RE = re.compile("|".join(re.escape(h) for h in UPSTREAM_HINTS), _I)
# Caller values echoed in error texts. A quoted value (a Python repr) after one of these lead-ins becomes '?':
# ", got 'x'" (status_common, weekly_status, daily_status), int() and date parsing messages, and upstream's
# "Invalid <field> 'x'" forms. After ", got: " (garminconnect) and "Unknown tool: " (the SDK) the value is not
# quoted and may hold line breaks, so the rest of the text becomes "?" (every such text puts its own words before
# the echo, and each link of an exception chain is prepared on its own). Only the copy the table reads changes.
_QUOTED_ECHO_RE = re.compile(
    r"(, got |invalid literal for int\(\) with base \d+: |isoformat string: |time data "
    r"|could not convert string to float: |\binvalid [a-z_]+ )"
    r"(?:'(?:[^'\\]|\\.?)*(?:'|\Z)|\"(?:[^\"\\]|\\.?)*(?:\"|\Z))", _I)
_TAIL_ECHO_RE = re.compile(r"(, got: |\bunknown tool: ).*", _I | re.S)
_UNKNOWN_TOOL_START_RE = re.compile(r"unknown tool:", _I)
# The FIT tools' first step is int(activity_id): a numeric id comes back as digits, which pick no group, and any
# other id fails there, with int()'s own words at the end of the text. Such a text is bad input, whatever the
# caller's id before the colon says (M7).
_INT_FAILURE_WORDS = ("invalid literal for int() with base ", "for integer string conversion")

_TEXT_MAX = 4000  # classify_text() reads at most this much
_LINK_TEXT_MAX = 2000  # each exception message in a chain text is cut to this
_CHAIN_MAX = 6
# garmin_personal_api is Garmin's official Personal API client (key mode, KEY D11): an exception of it that slips
# past key_mode's mapping is still read by the table.
_GARMIN_MODULES = ("garminconnect", "garth", "requests", "urllib3", "httpx", "httpcore", "curl_cffi",
                   "garmin_personal_api")


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _safe_str(value):
    if isinstance(value, str):
        return value
    try:
        text = str(value)
    except Exception:
        text = None
    if isinstance(text, str):
        return text
    return "<unprintable " + _type_name(value) + ">"


def _type_name(value):
    try:
        name = type(value).__name__
    except Exception:
        name = None
    return name if isinstance(name, str) else "object"


def _attr(obj, name, default=None):
    try:
        return getattr(obj, name, default)
    except Exception:
        return default


def _prepare(text):
    """The copy of `text` the table reads: upstream's network hint removed, caller echoes blanked."""
    text = _HINTS_RE.sub("", text)
    text = _QUOTED_ECHO_RE.sub(r"\1'?'", text)
    return _TAIL_ECHO_RE.sub(r"\1?", text)


def _with_number(detail, m):
    if "{n}" not in detail:
        return detail
    try:
        return detail.format(n=str(int(m.group(1))))  # int() also reads non-ASCII digits; str() writes ASCII
    except (ValueError, TypeError, IndexError):
        return detail.split(":")[0]  # "the date range is too long", "limit is too large"


def _anchored(prepared):
    head = prepared.lstrip()
    for rx, detail in _ANCHORED:
        m = rx.match(head)
        if m:
            if detail is None:
                return BAD_INPUT, _clean_detail(m.group(1))
            return BAD_INPUT, _with_number(detail, m)
    return None


def _table(prepared):
    for rx, group, detail in _TABLE:
        m = rx.search(prepared)
        if m:
            return group, (_with_number(detail, m) if detail is not None else None)
    return INTERNAL, None


def is_failure_text(text) -> bool:
    """True for an answer text that reports a failure: it starts "Error: " or "Error <words>: " (FAILURE_RE, on
    the first 210 characters after leading space), or with one of PLAIN_FAILURE_PREFIXES (upstream answers
    those without "Error") or ECHO_FAILURE_PREFIXES. False for a non-str, empty text, JSON, "No ... found" and
    "Errors: none"."""
    if not isinstance(text, str):
        return False
    head = text.lstrip()
    return (FAILURE_RE.match(head[:210]) is not None
            or head.startswith(PLAIN_FAILURE_PREFIXES) or head.startswith(ECHO_FAILURE_PREFIXES))


def _int_echo_failure(text):
    """True for a FIT tool's text whose caller id failed int() (read in full: the words come last)."""
    return text.lstrip().startswith(ECHO_FAILURE_PREFIXES) and any(word in text for word in _INT_FAILURE_WORDS)


_LONG_ID_RE = re.compile(r"[0-9]{100}")  # no Garmin id is near this long (they have about 11 digits)


def _long_id_echo(text):
    """True for a FIT tool's text whose caller id starts with 100 or more digits. int() accepts such an id (up to
    4,300 digits, and any length before Python 3.10.7), so the id reaches Garmin and Garmin's words land after it:
    past classify_text()'s window, where no group could be read."""
    head = text.lstrip()
    return any(head.startswith(prefix) and _LONG_ID_RE.match(head, len(prefix)) is not None
               for prefix in ECHO_FAILURE_PREFIXES)


def classify_text(text) -> tuple:
    """(group, detail or None) for a failure text. The detail is set for bad_input only. A FIT tool's text whose
    caller id failed int(), or is a number of 100 or more digits, is (BAD_INPUT, NUMBER_DETAIL) whatever the id
    and the words after it say. Otherwise reads the first 4,000
    characters, removes UPSTREAM_HINTS, blanks caller echoes, tries the anchored bad-input rows (M7), then the
    table of section 5.6 top to bottom; no match -> (INTERNAL, None). A non-str -> (INTERNAL, None)."""
    if not isinstance(text, str):
        return INTERNAL, None
    if _int_echo_failure(text) or _long_id_echo(text):
        return BAD_INPUT, NUMBER_DETAIL
    prepared = _prepare(text[:_TEXT_MAX])
    return _anchored(prepared) or _table(prepared)


# --- exceptions --------------------------------------------------------------------------------------

def _chain(exc):
    """exc, then each __cause__ (else __context__ when not suppressed); at most 6 links, stops on a repeat."""
    chain = []
    seen = set()
    link = exc
    while isinstance(link, BaseException) and id(link) not in seen and len(chain) < _CHAIN_MAX:
        seen.add(id(link))
        chain.append(link)
        nxt = _attr(link, "__cause__")
        if nxt is None and not _attr(link, "__suppress_context__", False):
            nxt = _attr(link, "__context__")
        link = nxt
    return chain


def _mro(exc):
    mro = _attr(type(exc), "__mro__", ())
    return mro if isinstance(mro, tuple) else ()


def _named(exc, name):
    return any(_attr(cls, "__name__") == name for cls in _mro(exc))


def _is_wrapper(exc):
    """A ToolError (any class of that name in the MRO, so the SDK's UnexpectedToolError too) raised from another
    exception: the adapter's ToolError(str(exc)) and the SDK's "Error executing tool ..." wrappers."""
    return _named(exc, "ToolError") and _attr(exc, "__cause__") is not None


def _eligible(exc):
    """True when the table may read the failure: a KnownFailure, an OSError (ConnectionError, the builtin
    TimeoutError), any class named TimeoutError, a class from garminconnect, garth, requests, urllib3, httpx,
    httpcore or curl_cffi, or a ToolError with no cause."""
    if isinstance(exc, (KnownFailure, OSError)):
        return True
    for cls in _mro(exc):
        if _attr(cls, "__name__") == "TimeoutError":
            return True
        module = _attr(cls, "__module__")
        if isinstance(module, str) and module.startswith(_GARMIN_MODULES):
            return True
    return _named(exc, "ToolError") and _attr(exc, "__cause__") is None


def _validation_errors(exc):
    errors = _attr(exc, "errors")
    try:
        return errors(include_url=False, include_input=False)
    except TypeError:
        try:
            return errors()
        except Exception:
            return []
    except Exception:
        return []


def _message(exc):
    return _safe_str(exc)[:_LINK_TEXT_MAX]


def classify_exception(exc) -> tuple:
    """(group, detail or None, root, chain text) for an exception that reached the tool-call override.

    The chain is `exc`, then each __cause__, else __context__ when it is not suppressed (at most 6 links,
    stopping on a repeat); `root` is its last link; the chain text joins "<Type>: <message>" of every link with
    " <- " (each message cut to 2,000 characters). In order (amendments M2 and M7):
    1. a link that is a pydantic ValidationError (duck-typed) -> (BAD_INPUT, validation_detail(its errors)), and
       the chain text is "ValidationError: <that detail>": pydantic's message holds the rejected values, which are
       the caller's data and never go to the log (D16);
    2. `root` is a ToolError with no cause and no unsuppressed context whose text starts "Unknown tool:" ->
       (BAD_INPUT, DETAIL_UNKNOWN_TOOL);
    3. the origin (the first link that is not a ToolError wrapper with a cause) is table-eligible and its message
       starts like a bad-argument text (the anchored rows of M7) -> that bad_input detail;
    4. row L1 matches the chain text -> LOGIN, whatever the types;
    5. the origin is table-eligible (_eligible) -> the table over the chain text; otherwise INTERNAL.
    Echoes and UPSTREAM_HINTS are removed from the copy the rows read, link by link; the returned chain text is
    the original. Anything that is not an exception -> (INTERNAL, None, exc, "<Type>: <text>")."""
    chain = _chain(exc)
    if not chain:
        return INTERNAL, None, exc, f"{_type_name(exc)}: {_safe_str(exc)[:_LINK_TEXT_MAX]}"
    root = chain[-1]
    messages = [_message(link) for link in chain]
    names = [_type_name(link) for link in chain]
    text = " <- ".join(f"{name}: {msg}" for name, msg in zip(names, messages))

    for link in chain:
        if _type_name(link) == "ValidationError" and callable(_attr(link, "errors")):
            detail = validation_detail(_validation_errors(link))
            return BAD_INPUT, detail, root, f"ValidationError: {detail}"

    if (_named(root, "ToolError") and _attr(root, "__cause__") is None
            and (_attr(root, "__suppress_context__", False) or _attr(root, "__context__") is None)
            and _UNKNOWN_TOOL_START_RE.match(messages[-1].lstrip())):
        return BAD_INPUT, DETAIL_UNKNOWN_TOOL, root, text

    origin_index = next((i for i, link in enumerate(chain) if not _is_wrapper(link)), len(chain) - 1)
    origin = chain[origin_index]
    prepared = [_prepare(msg) for msg in messages]
    eligible = _eligible(origin)
    if eligible:
        hit = _anchored(prepared[origin_index])
        if hit:
            return hit[0], hit[1], root, text
    matching = " <- ".join(f"{name}: {msg}" for name, msg in zip(names, prepared))
    if _L1.search(matching):
        return LOGIN, None, root, text
    if eligible:
        group, detail = _table(matching)
        return group, detail, root, text
    return INTERNAL, None, root, text


_FIELD_JUNK_RE = re.compile(r"[^A-Za-z0-9_.\[\]]")


def _field(loc):
    if loc is None:
        parts = ()
    elif isinstance(loc, (list, tuple)):
        parts = loc
    else:
        parts = (loc,)
    name = ".".join(_safe_str(part) for part in itertools.islice(parts, 16))
    name = _FIELD_JUNK_RE.sub("", name)[:text_hygiene.ECHO_MAX]
    return name or "an argument"


def _first_errors(errors):
    if isinstance(errors, (list, tuple)):
        return errors[:3]
    if isinstance(errors, (str, bytes, dict)) or errors is None:
        return ()
    try:
        return list(itertools.islice(iter(errors), 3))
    except Exception:
        return ()


def _cut_detail(detail):
    return detail if len(detail) <= DETAIL_MAX else detail[:DETAIL_MAX - 3] + "..."


def validation_detail(errors) -> str:
    """The bad-input detail for pydantic validation errors (a list of dicts from ValidationError.errors()): for at
    most the first 3, "<field> <problem>" joined with "; ". The field is the error's loc joined with "." and
    kept to [A-Za-z0-9_.[]] and 64 characters ("an argument" when empty); the problem comes from the error's type
    (section 5.5). Never reads "input" or "ctx". No usable error -> GENERIC_DETAIL. Longer than 200 characters ->
    cut to 197 and "...". Any input is accepted."""
    parts = []
    for err in _first_errors(errors):
        if not isinstance(err, dict):
            continue
        try:
            kind = err.get("type")
            problem = _PROBLEM_WORDS.get(kind, _OTHER_PROBLEM) if isinstance(kind, str) else _OTHER_PROBLEM
            parts.append(f"{_field(err.get('loc'))} {problem}")
        except Exception:  # an odd mapping or loc: skip this error
            continue
    if not parts:
        return GENERIC_DETAIL
    return _cut_detail("; ".join(parts))


_SPACE_RUN_RE = re.compile(r"\s+")


def _clean_detail(detail):
    """A detail made safe for the client text: a str of printable ASCII, single spaces, at most 200 characters;
    GENERIC_DETAIL when nothing is left."""
    if not isinstance(detail, str):
        return GENERIC_DETAIL
    detail = "".join(ch for ch in detail if " " <= ch <= "~")
    detail = _SPACE_RUN_RE.sub(" ", detail).strip()
    return _cut_detail(detail) if detail else GENERIC_DETAIL


def client_text(group, *, ref=None, detail=None, today=None, key_mode=False) -> str:
    """The fixed text of section 5.4 for `group`: ASCII, at most 300 characters.

    BAD_INPUT takes `detail` (one of the *_DETAIL constants, validation_detail(), or the detail classify_text()
    returned; None -> GENERIC_DETAIL; made printable ASCII and cut to 200 characters). With DATE_DETAIL and
    `today` a "YYYY-MM-DD" str, the detail gains ", for example <today> (today)". INTERNAL needs `ref`, 8
    lowercase hex characters from new_ref(). With `key_mode` true (GARMIN_AUTH=key), LOGIN and NO_DATA give
    their key-mode texts (_KEY_MODE_TEXTS, same first words); the other groups do not change. Raises ValueError
    for an unknown group or INTERNAL without a valid ref: both are programming errors."""
    if key_mode and isinstance(group, str) and group in _KEY_MODE_TEXTS:
        return _KEY_MODE_TEXTS[group]
    if not isinstance(group, str) or group not in _CLIENT_TEXTS:
        raise ValueError(f"unknown error group: {_safe_str(group)[:40]!r}")
    if group == INTERNAL:
        if not (isinstance(ref, str) and _REF_RE.fullmatch(ref)):
            raise ValueError("client_text(INTERNAL) needs ref: 8 lowercase hex characters from new_ref()")
        return _CLIENT_TEXTS[INTERNAL].format(ref=ref)
    if group == BAD_INPUT:
        detail = GENERIC_DETAIL if detail is None else _clean_detail(detail)
        if detail == DATE_DETAIL and isinstance(today, str) and _TODAY_RE.fullmatch(today):
            detail = f"{DATE_DETAIL}, for example {today} (today)"
        return _CLIENT_TEXTS[BAD_INPUT].format(detail=detail)
    return _CLIENT_TEXTS[group]


def source_phrase(group, ref=None) -> str:
    """The phrase of section 5.7 for a status report's sources[*].error. INTERNAL gives "internal error (ref
    <ref>)" with a valid ref and "internal error" without one; an unknown group gives "internal error" too.
    Never raises."""
    if isinstance(group, str):
        if group in _SOURCE_PHRASES:
            return _SOURCE_PHRASES[group]
        if group == INTERNAL and isinstance(ref, str) and _REF_RE.fullmatch(ref):
            return f"{_INTERNAL_PHRASE} (ref {ref})"
    return _INTERNAL_PHRASE


def new_ref() -> str:
    """A fresh ref for an internal error: 8 lowercase hex characters (secrets.token_hex(4))."""
    return secrets.token_hex(4)


# Log redaction (section 5.8 as amended by M6), applied in this order. Fixer changes, each one tested:
# - pattern 2 (EVE-3): the key may end a longer name (oauth_token, di_refresh_token, accessToken), a backslash may
#   escape the quotes, and the value runs to a space, a quote or & , ; < > (a password may hold "!" or "@");
# - pattern 3 (EVE-1, EV-SEC-1): an address starts where a run of address characters starts, so a long run with
#   no "@" is read once, not once per position (the contract's form took 32 s for 100,000 characters).
# KEY D14: the first row hides a Personal API key in Garmin's "Authorization: ApiKey <key>" header form, and row 3's
# names gain api_?key (GARMIN_PERSONAL_API_KEY=..., "apiKey": "...").
_REDACT = (
    (re.compile(r"(?i)\bapikey\s+[^\s\"'&,;<>]+"), "ApiKey <redacted>"),
    (re.compile(r"(?i)\bbearer\s+[\w.~+/=-]+"), "Bearer <redacted>"),
    (re.compile(r"(?i)(access_token|refresh_token|id_token|client_secret|password|secret|token|api_?key)"
                r"(\\?[\"']?\s*[:=]\s*\\?[\"']?)([^\s\"'&,;<>]+)"), r"\1\2<redacted>"),
    (re.compile(r"(?<![\w.+-])[\w.+-]+@[\w-]+(?:\.[\w-]+)+"), "<email>"),
    (re.compile(r"[A-Za-z0-9_\-.~+=]{40,}"), "<redacted>"),
    (re.compile(r"(?i)rate limit:"), "rate limit -"),
)
_SPACES_RE = re.compile(" {2,}")
# No secret the patterns know holds one of these (pattern 2's value stops at them). When redact() cuts its input,
# everything after the last of them goes: it may be the start of a secret the patterns no longer recognise.
_CUT_STOPS = (" ", "\"", "'", "&", ",", ";", "<", ">")
# Exact secret values redact() always hides (register_secret()), longest first. A tuple swapped whole under a lock,
# so redact() reads it without one.
_SECRETS = ()
_SECRET_MIN = 8
_SECRETS_LOCK = threading.Lock()


def register_secret(value) -> None:
    """From now on redact() replaces every occurrence of `value` (an exact str) with "<redacted>", before its
    patterns run (KEY D14: launch registers the Personal API key, which no generic pattern may catch). A value
    that is not a str or is shorter than 8 characters is ignored (it would hide ordinary words). Never raises."""
    global _SECRETS
    if not isinstance(value, str) or len(value) < _SECRET_MIN:
        return
    with _SECRETS_LOCK:
        if value not in _SECRETS:
            _SECRETS = tuple(sorted(_SECRETS + (value,), key=len, reverse=True))


def _hide_secrets(line):
    for secret in _SECRETS:
        if secret in line:
            line = line.replace(secret, "<redacted>")
    return line


def redact(text, limit=2000) -> str:
    """One log line for an original failure text: str(text) cut to 4 x `limit` + 256 characters (the line is
    at most `limit` long, so a multi-megabyte argument costs no more than a short one), text_hygiene.
    strip_invisible() (controls, line breaks and bidi become spaces or vanish, so a zero-width character cannot
    split a token), runs of spaces collapsed, every registered secret (register_secret()) replaced with
    "<redacted>", the patterns of section 5.8 in order ("ApiKey <key>", bearer tokens, key=value secrets, email
    addresses, long tokens; "rate limit:" becomes "rate limit -"), runs of spaces collapsed again,
    trimmed, then cut to `limit` (2000 when it is not an int) with an ellipsis. When the input was cut, the text
    after the last space, quote or & , ; < > becomes the ellipsis (all of it when there is none), so no part of a
    secret the patterns no longer recognise reaches the line, even in a body with no spaces. URLs and Garmin API
    paths stay. Linear time. Never raises."""
    limit = limit if _is_int(limit) else 2000
    line = _safe_str(text)
    bound = 4 * max(limit, 0) + 256
    clipped = len(line) > bound
    line = text_hygiene.strip_invisible(line[:bound] if clipped else line)
    line = _SPACES_RE.sub(" ", line)
    line = _hide_secrets(line)
    if clipped:
        stop = max(line.rfind(ch) for ch in _CUT_STOPS)
        line = line[:stop + 1] + text_hygiene.ELLIPSIS
    for rx, repl in _REDACT:
        line = rx.sub(repl, line)
    line = _SPACES_RE.sub(" ", line).strip(" ")
    return text_hygiene.cap(line, limit)
