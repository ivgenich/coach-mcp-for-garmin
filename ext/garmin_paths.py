"""The Garmin request path guard and the argument checks before Garmin (security fix, 25 Nov 2026).

launch.py installs the guard on garminconnect's Client in full mode (patch()) and runs the argument checks in
call_tool(); worker_garmin.py installs the same guard before its first full-mode client. Standard library and
tool_errors only, so the rules are testable without upstream; garminconnect is imported only by the hook
check and the install.

Argument checks: upstream tools that put a caller value into a Garmin path with too little checking:
get_workout_by_id takes any id holding "-" for a UUID and builds "workout-service/fbt-adaptive/<id>";
get_vo2max_trend checks its dates with date.fromisoformat() only, which on Python 3.12 also accepts
"20261101/x", "20261101?x" or "20261101#x", and puts the raw strings in its "<metrics url>/<start>/<end>"
path. call_tool() runs these checks first, before the call guard and before Garmin; a failing one is a plain
bad-input answer. The path guard (check_garmin_path) stays the backstop for every other path. One row per
tool: {argument: (check, bad-input detail)}; a missing argument is left to the SDK's own validation.

Path guard: garminconnect's Client._run_request builds f"{connectapi}/{path.lstrip('/')}" and requests
(urllib3) then removes dot segments, so a caller value that reaches a path
("x-/../../../userprofile-service/...", from upstream's get_workout_by_id) reads ANOTHER Garmin API with the
owner's token. Every Garmin API call of garminconnect (connectapi, download, request, post, put, delete, and
Garmin's wrappers around them) goes through Client._run_request, so install_path_guard() wraps it at class
level: upstream re-creates clients on login, and each new instance gets the guard. The rules mirror
key_mode.KeyModeClient.connectapi (_PATH_BAD): no control characters or whitespace but a plain space, no
"..", "%", "#", "\\", "//", no "." segment. Unlike key mode, a space and non-ASCII LETTERS pass:
garminconnect puts the owner's display name in some paths (usersummary/daily/<name>, prs/<name>), and a name
such as "My Name" or "Ívar" must keep working; requests percent-encodes them, and neither can change the
path's structure. Any other non-ASCII character (a full-width dot or slash, a lookalike) is refused. Query
parameters passed as params= are not part of the path and stay untouched.

Method allowlist (check_garmin_request, in the same hook and in launch's tracing hook): GET, the GraphQL read
query, and nothing else unless allow_plan_writes() admitted the plan's workout writes (PLAN_WRITES=on).
"""

from __future__ import annotations

import datetime
import inspect
import json
import re
import unicodedata

import tool_errors

# --- Argument checks before Garmin ----------------------------------------------------------------------------
_WORKOUT_ID_RE = re.compile(r"[1-9][0-9]{0,18}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                            re.ASCII | re.IGNORECASE)
_ISO_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", re.ASCII)
_ID_MAX = 10 ** 19 - 1  # the most digits a string id may have (19), so both forms take the same ids


def _workout_id_ok(value):
    """A positive int (not bool, at most 19 digits), or a str: such a number without a sign or leading zero,
    or a canonical UUID (8-4-4-4-12 hex)."""
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return 0 < value <= _ID_MAX
    return isinstance(value, str) and _WORKOUT_ID_RE.fullmatch(value) is not None


def _iso_date_ok(value):
    """A str written YYYY-MM-DD in ASCII digits that is a real calendar date."""
    if not isinstance(value, str) or _ISO_DATE_RE.fullmatch(value) is None:
        return False
    try:
        datetime.date.fromisoformat(value)
    except ValueError:
        return False
    return True


_PLAN_ID_RE = re.compile(r"[0-9a-f]{32}", re.ASCII)


def _plan_id_ok(value):
    """Empty (the latest plan), or 32 lower-case hex characters (plan_store's plan ids)."""
    return isinstance(value, str) and (value == "" or _PLAN_ID_RE.fullmatch(value) is not None)


_COURSE_ID_RE = re.compile(r"[1-9][0-9]{0,14}", re.ASCII)


def _course_id_ok(value):
    """Empty or "0" (no course), or a Garmin course id: a positive int (not bool) or its digits as a str (at most
    15, no sign or leading zero); plan_preview puts it into the course read's path."""
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return 0 <= value < 10 ** 15
    return isinstance(value, str) and (value in ("", "0") or _COURSE_ID_RE.fullmatch(value) is not None)
_WORKOUT_KEY_RE = re.compile(r"w[0-9]{2}-[a-z0-9]{1,8}", re.ASCII)
# plan_store.PAIN_LOCATIONS (test_plan_tracking checks they agree); "" means none.
PAIN_LOCATIONS = frozenset({"", "none", "foot", "ankle", "achilles", "calf", "shin", "knee", "hamstring", "quad",
                            "hip", "glute", "lower_back", "other"})


def _workout_key_ok(value):
    """A plan session key such as w03-long (plan_store.WORKOUT_KEY_RE)."""
    return isinstance(value, str) and _WORKOUT_KEY_RE.fullmatch(value) is not None


def _small_int_ok(value, low, high):
    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high


def _pain_location_ok(value):
    return isinstance(value, str) and value in PAIN_LOCATIONS
_TOKEN_RE = re.compile(r"(ct|at|xt)1\.[A-Za-z0-9_-]{32}", re.ASCII)


def _token_ok(value, prefix):
    """A plan token (plan_store: ct1. commit, at1. apply, xt1. cancel) that starts with `prefix`."""
    return isinstance(value, str) and _TOKEN_RE.fullmatch(value) is not None and value.startswith(prefix + "1.")


def _commit_token_ok(value):
    return _token_ok(value, "ct")


def _cancel_token_ok(value):
    """Empty (plan_cancel then previews and issues a token) or a cancel token."""
    return value == "" or _token_ok(value, "xt")


def _apply_token_ok(value):
    return _token_ok(value, "at")


_CHANGE_ID_RE = re.compile(r"c[0-9a-f]{16}", re.ASCII)   # plan_store.CHANGE_ID_RE
MAX_CHANGE_IDS = 20                                      # plan_writes.MAX_CHANGE_IDS


def _change_ids_ok(value, empty=False):
    """Change ids from plan_review_week (c and 16 hex), 1 to MAX_CHANGE_IDS separated by commas; empty only
    where the argument is optional (plan_preview's accept_changes)."""
    if not isinstance(value, str):
        return False
    ids = [p.strip() for p in value.replace(";", ",").split(",") if p.strip()]
    if not ids:
        return empty and not value.strip()
    return len(ids) <= MAX_CHANGE_IDS and all(_CHANGE_ID_RE.fullmatch(i) for i in ids)


def _optional_date_ok(value):
    return value == "" or _iso_date_ok(value)


# plan_preview's overrides (plan_engine.OVERRIDE_KINDS and plan_tools.parse_overrides; test_deep_config_paths
# checks the kinds agree). Keys are checked by their shape only: plan_tools checks the session-key format.
OVERRIDE_KINDS = frozenset({"move", "swap", "rest", "easy", "cap_week_km", "long_run_day", "cap_km", "cap_long"})
_OVERRIDE_FIELDS = frozenset({"kind", "key", "date", "date_b", "week_start", "km", "weekday"})
_OVERRIDE_KEY_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}", re.ASCII)
_WEEKDAY_NAMES = frozenset({"mon", "tue", "wed", "thu", "fri", "sat", "sun"})
MAX_OVERRIDES = 20                 # plan_tools.MAX_OVERRIDES
MAX_OVERRIDES_CHARS = 8000


def _override_ok(o):
    if not isinstance(o, dict) or set(o) - _OVERRIDE_FIELDS or o.get("kind") not in OVERRIDE_KINDS:
        return False
    key = o.get("key")
    if key not in (None, "") and not (isinstance(key, str) and _OVERRIDE_KEY_RE.fullmatch(key)):
        return False
    for name in ("date", "date_b", "week_start"):
        if o.get(name) not in (None, "") and not _iso_date_ok(o[name]):
            return False
    km = o.get("km")
    if km is not None and (isinstance(km, bool) or not isinstance(km, (int, float)) or not 0 <= km <= 300):
        return False
    weekday = o.get("weekday")
    if weekday not in (None, "") and not (isinstance(weekday, str) and weekday.strip().lower()[:3] in _WEEKDAY_NAMES
                                          and weekday.isascii()):
        return False
    if o["kind"] == "cap_long" and (km is None or not (o.get("date") or key)):
        return False
    return True


def _overrides_ok(value):
    """Blank (no edits), or a JSON list of at most MAX_OVERRIDES edits, each a known kind with only the known
    fields: ISO dates, km a number from 0 to 300, a weekday name, a key of lower-case letters, digits and
    dashes; cap_long needs km and a date or key."""
    if not isinstance(value, str) or len(value) > MAX_OVERRIDES_CHARS:
        return False
    if not value.strip():
        return True
    try:
        raw = json.loads(value)
    except (ValueError, RecursionError):   # deep nesting: refused, not an error
        return False
    return isinstance(raw, list) and len(raw) <= MAX_OVERRIDES and all(_override_ok(o) for o in raw)


ARGUMENT_CHECKS = {
    "get_workout_by_id": {"workout_id": (_workout_id_ok, tool_errors.GENERIC_DETAIL)},
    "get_vo2max_trend": {"start_date": (_iso_date_ok, tool_errors.DATE_DETAIL),
                         "end_date": (_iso_date_ok, tool_errors.DATE_DETAIL)},
    # The plan tools put one caller value into a Garmin path, plan_preview's course_id (the course read); it and
    # their other ids and dates are refused here, before the call guard counts a call.
    "plan_assess": {"race_date": (_iso_date_ok, tool_errors.DATE_DETAIL)},
    "plan_preview": {"race_date": (_iso_date_ok, tool_errors.DATE_DETAIL),
                     "plan_id": (_plan_id_ok, tool_errors.GENERIC_DETAIL),
                     "course_id": (_course_id_ok, tool_errors.GENERIC_DETAIL),
                     "overrides": (_overrides_ok, tool_errors.GENERIC_DETAIL),
                     "accept_changes": (lambda v: _change_ids_ok(v, empty=True), tool_errors.GENERIC_DETAIL)},
    "plan_get": {"plan_id": (_plan_id_ok, tool_errors.GENERIC_DETAIL)},
    # log_feedback (PLAN_TRACKING=on) writes only the app's database; its values are checked here first too.
    "log_feedback": {"workout_key": (_workout_key_ok, tool_errors.RANGE_VALUE_DETAIL),
                     "rpe": (lambda v: _small_int_ok(v, 1, 10), tool_errors.RANGE_VALUE_DETAIL),
                     "pain": (lambda v: _small_int_ok(v, 0, 10), tool_errors.RANGE_VALUE_DETAIL),
                     "pain_location": (_pain_location_ok, tool_errors.RANGE_VALUE_DETAIL),
                     "sharp": (lambda v: isinstance(v, bool), tool_errors.RANGE_VALUE_DETAIL),
                     "plan_id": (_plan_id_ok, tool_errors.GENERIC_DETAIL)},
    "plan_commit": {"plan_id": (_plan_id_ok, tool_errors.GENERIC_DETAIL),
                    "commit_token": (_commit_token_ok, tool_errors.GENERIC_DETAIL)},
    "plan_cancel": {"plan_id": (_plan_id_ok, tool_errors.GENERIC_DETAIL),
                    "cancel_token": (_cancel_token_ok, tool_errors.GENERIC_DETAIL)},
    # Stage 4: the weekly review (PLAN_TRACKING=on) and its Garmin write (PLAN_WRITES=on too).
    "plan_review_week": {"plan_id": (_plan_id_ok, tool_errors.GENERIC_DETAIL),
                         "week_start": (_optional_date_ok, tool_errors.DATE_DETAIL)},
    "plan_apply_changes": {"plan_id": (_plan_id_ok, tool_errors.GENERIC_DETAIL),
                           "apply_token": (_apply_token_ok, tool_errors.GENERIC_DETAIL),
                           "change_ids": (_change_ids_ok, tool_errors.GENERIC_DETAIL)},
}


def refused_argument(name, arguments):
    """(argument name, bad-input detail) for the first argument of tool `name` that fails its check in
    ARGUMENT_CHECKS, or None. Arguments that are absent, and tools without a row, are not checked here."""
    checks = ARGUMENT_CHECKS.get(name)
    if not checks or not isinstance(arguments, dict):
        return None
    for argument, (check, detail) in checks.items():
        if argument in arguments and not check(arguments[argument]):
            return argument, detail
    return None


# --- Garmin request path guard ----------------------------------------------------------------------------------
_PATH_REFUSED_PARTS = ("..", "%", "#", "\\", "//")
# garminconnect itself writes a few query strings into its paths (get_lactate_threshold, the daily events,
# the gear calls: garminconnect/__init__.py at 0.3.2). Only these names, with plain values, may follow a "?";
# a caller's "?" anywhere else (a date "20261101?x") is refused like any other bad path.
_PATH_QUERY_NAMES = frozenset({"sport", "aggregation", "aggregationStrategy", "calendarDate", "userProfilePk",
                               "start", "limit"})
_PATH_QUERY_VALUE_RE = re.compile(r"[A-Za-z0-9_.:-]{1,64}", re.ASCII)
PATH_REFUSED_TEXT = "Invalid input: a Garmin request path was refused (it is not a plain API path)"


class GarminPathRefused(tool_errors.KnownFailure, ValueError):
    """A Garmin API path the guard refused before any request left. Its fixed text starts "Invalid input:"
    (never the path, which is caller data), so tool_errors puts it in the bad-input group whether it arrives
    raised, wrapped by garminconnect ("Connection error: Invalid input: ...") or as an upstream tool's
    "Error ...: ..." answer: never a Garmin failure, never a guard cooldown."""

    def __init__(self):
        super().__init__(PATH_REFUSED_TEXT)


def _without_dot_segments(route):
    """`route` with "." and ".." segments resolved (RFC 3986 5.2.4, as urllib3 does before sending)."""
    out = []
    for segment in route.split("/"):
        if segment == ".":
            continue
        if segment == "..":
            if out:
                out.pop()
            continue
        out.append(segment)
    return "/".join(out)


def garmin_path_ok(path):
    """True when `path` is a plain Garmin API path the guard lets through (see the rules above)."""
    if not isinstance(path, str) or not path or not path.isprintable():
        return False
    # Non-ASCII: letters and combining accents only ("E" + U+0301 is how some systems write "É").
    if any((ch.isspace() and ch != " ") or (not ch.isascii() and not (ch.isalpha() or unicodedata.combining(ch)))
           for ch in path):
        return False
    if any(bad in path for bad in _PATH_REFUSED_PARTS):
        return False
    route, has_query, query = path.partition("?")
    if has_query:
        for pair in query.split("&"):
            name, has_value, value = pair.partition("=")
            if name not in _PATH_QUERY_NAMES or not has_value or not _PATH_QUERY_VALUE_RE.fullmatch(value):
                return False
    return _without_dot_segments(route) == route


def check_garmin_path(path):
    """Raise GarminPathRefused unless garmin_path_ok(path)."""
    if not garmin_path_ok(path):
        raise GarminPathRefused()


# --- Method allowlist (training-plan writes) -----------------------------------------------------------------
# Only reads leave: GET, and POST to the GraphQL gateway with a body whose query starts with "query" (the calendar
# read; no mutation channel). With PLAN_WRITES=on, launch.main() calls allow_plan_writes() and plan_writes may
# also create, schedule, unschedule and delete workouts on exactly these workout-service paths; --plan-cleanup
# allows only the two deletes. The history worker never allows a write.
GRAPHQL_PATH = "graphql-gateway/graphql"
_ID = r"[1-9][0-9]{0,18}"
WRITE_PATHS = {
    "create": ("POST", re.compile(r"workout-service/workout", re.ASCII)),
    "schedule": ("POST", re.compile(rf"workout-service/schedule/{_ID}", re.ASCII)),
    "unschedule": ("DELETE", re.compile(rf"workout-service/schedule/{_ID}", re.ASCII)),
    "delete": ("DELETE", re.compile(rf"workout-service/workout/{_ID}", re.ASCII)),
}
_ALLOWED_WRITES = {"kinds": frozenset()}
METHOD_REFUSED_TEXT = "a Garmin request was refused: this server sends only reads and the plan's own workout writes"


class GarminMethodRefused(RuntimeError):
    """A Garmin request whose method and path the allowlist does not admit (fixed text, never the path)."""

    def __init__(self):
        super().__init__(METHOD_REFUSED_TEXT)


def allow_plan_writes(kinds):
    """Admit the WRITE_PATHS of `kinds` (a subset of create, schedule, unschedule, delete) from now on in this
    process; an empty set admits none again. ValueError for an unknown kind."""
    kinds = frozenset(kinds)
    if kinds - set(WRITE_PATHS):
        raise ValueError("garmin_paths: unknown write kind")
    _ALLOWED_WRITES["kinds"] = kinds


def allowed_write_kinds():
    """The write kinds this process admits (empty unless PLAN_WRITES=on or --plan-cleanup)."""
    return _ALLOWED_WRITES["kinds"]


# A GraphQL body's strings ("..." with escapes, """block""") and comments (# to the line end), cut out before the
# keyword check so a quoted word neither hides nor fakes an operation. A comment ends at "\r", "\n" or "\r\n"
# (GraphQL's LineTerminator), and so does a plain string.
_GRAPHQL_NOISE = re.compile(r'"""(?:\\"""|[^"]|"(?!""))*"""|"(?:\\.|[^"\\\n\r])*"|#[^\n\r]*')
_GRAPHQL_WRITE_WORD = re.compile(r"\b(?:mutation|subscription)\b")
_GRAPHQL_KEYS = frozenset({"query", "variables", "operationName"})


def graphql_read_ok(body):
    """True for a GraphQL request body that can only read: a dict with a string query that starts with
    "query", no mutation or subscription keyword outside its strings and comments, no operationName (one
    anonymous query, as every call here sends it) and no other keys."""
    if not isinstance(body, dict) or set(body) - _GRAPHQL_KEYS or body.get("operationName") not in (None, ""):
        return False
    query = body.get("query")
    if not isinstance(query, str):
        return False
    bare = _GRAPHQL_NOISE.sub(" ", query)
    return bare.lstrip().startswith("query") and not _GRAPHQL_WRITE_WORD.search(bare)


# Request bodies requests sends other than `json` (it sends `data` or `files` instead of `json` when given both),
# so a non-GET request carrying one is refused: the only body checked, and so the only one sent, is `json`.
_UNCHECKED_BODIES = ("data", "files")


def garmin_method_ok(method, path, kwargs=None):
    """True for GET, a GraphQL read query (graphql_read_ok), or a write the allowed kinds admit (compared on
    path.lstrip("/")). A non-GET request carrying `data` or `files` is refused."""
    verb = str(method).upper()
    if verb == "GET":
        return True
    if any((kwargs or {}).get(name) is not None for name in _UNCHECKED_BODIES):
        return False
    route = path.lstrip("/") if isinstance(path, str) else ""
    if verb == "POST" and route == GRAPHQL_PATH:
        return graphql_read_ok((kwargs or {}).get("json"))
    for kind in _ALLOWED_WRITES["kinds"]:
        want, pattern = WRITE_PATHS[kind]
        if verb == want and pattern.fullmatch(route):
            return True
    return False


def check_garmin_request(method, path, kwargs=None):
    """check_garmin_path(path), then GarminMethodRefused unless garmin_method_ok()."""
    check_garmin_path(path)
    if not garmin_method_ok(method, path, kwargs):
        raise GarminMethodRefused()


def path_guarded_run_request(original):
    """garminconnect Client._run_request with check_garmin_request() first (the path, then the method
    allowlist); params= and the rest pass as given."""

    def _run_request(self, method, path, *args, **kwargs):
        check_garmin_request(method, path, kwargs)
        return original(self, method, path, *args, **kwargs)

    _run_request._coach_path_guard = True
    _run_request.__wrapped__ = original
    return _run_request


def path_guard_installed(fn):
    """True when `fn` or any function it wraps (__wrapped__, as the tracing hook wraps) is the path guard."""
    for _ in range(8):
        if fn is None:
            return False
        if getattr(fn, "_coach_path_guard", False):
            return True
        fn = getattr(fn, "__wrapped__", None)
    return False


PATH_HOOK_ERROR = ("launch: garminconnect no longer sends every API call through Client._run_request(method, "
                   "path, ...) building the URL from the path; the Garmin path guard needs a new hook")


def check_path_guard_hook():
    """RuntimeError unless the installed garminconnect still builds every API URL in
    Client._run_request(self, method, path, ...) from `path`, and its API methods (Client.connectapi,
    download, request, post, put, delete; Garmin.connectapi and download through them) still call it. A
    garminconnect bump that moved this would leave the guard installed on a method nothing calls; launch's
    patch() and worker_garmin.selfcheck() run this at build time (Dockerfile), so the build fails instead."""
    try:
        import garminconnect
        from garminconnect import client as gc_client

        cls = gc_client.Client
        hook = cls.__dict__["_run_request"]
        while getattr(hook, "__wrapped__", None) is not None:  # this repo's own wrappers
            hook = hook.__wrapped__
        params = list(inspect.signature(hook).parameters)[:3]
        source = inspect.getsource(hook)
        callers = [inspect.getsource(cls.__dict__[name])
                   for name in ("connectapi", "download", "request", "post", "put", "delete")]
        wrappers = {name: inspect.getsource(garminconnect.Garmin.__dict__[name]) for name in ("connectapi", "download")}
    except (ImportError, KeyError, AttributeError, OSError, TypeError, ValueError) as exc:
        raise RuntimeError(PATH_HOOK_ERROR) from exc
    if (params != ["self", "method", "path"] or "{path.lstrip('/')}" not in source
            or not all("self._run_request(" in text for text in callers)
            or not all(f"self.client.{name}(" in text for name, text in wrappers.items())):
        raise RuntimeError(PATH_HOOK_ERROR)


def install_path_guard():
    """Wrap garminconnect's Client._run_request with the path guard (idempotent, also under the tracing
    wrapper). Raises RuntimeError when the hook moved (check_path_guard_hook())."""
    check_path_guard_hook()
    from garminconnect import client as gc_client

    cls = gc_client.Client
    current = cls.__dict__["_run_request"]
    if not path_guard_installed(current):
        cls._run_request = path_guarded_run_request(current)
