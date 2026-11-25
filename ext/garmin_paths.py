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
"""

from __future__ import annotations

import datetime
import inspect
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


ARGUMENT_CHECKS = {
    "get_workout_by_id": {"workout_id": (_workout_id_ok, tool_errors.GENERIC_DETAIL)},
    "get_vo2max_trend": {"start_date": (_iso_date_ok, tool_errors.DATE_DETAIL),
                         "end_date": (_iso_date_ok, tool_errors.DATE_DETAIL)},
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


def path_guarded_run_request(original):
    """garminconnect Client._run_request with check_garmin_path() first; params= and the rest pass as given."""

    def _run_request(self, method, path, *args, **kwargs):
        check_garmin_path(path)
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
