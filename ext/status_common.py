"""Shared helpers for the status tools in ext/ (daily_status, weekly_status).

Standard library only, so the unit tests run without garmin_mcp installed.

The Garmin client upstream hands us (garmin_mcp's _GarminProxy) is synchronous
and bounds each call with its own 90 s timeout. fetch_all() runs many such
calls in worker threads, a few at a time, under a shorter per-call budget, and
turns every failure into a recorded source with ok=False instead of an
exception: one missing Garmin endpoint never sinks a whole report, and missing
data is reported as missing, never guessed.

A failed source carries a fixed phrase from tool_errors (for example "Garmin
rate limit" or "internal error (ref 1a2b3c4d)"), never the exception text: the
original text goes to the server log once, redacted, on the "garmin_ext.status"
logger. When the serial first call of a report fails in a way every other call
would share (login, rate limit, network, stall), the report raises
SystemicFailure instead of answering an all-missing status.
"""

from __future__ import annotations

import asyncio
import contextvars
import datetime as dt
import json
import logging
import os
import re
import time
import weakref
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Set, Tuple, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import tool_errors

log = logging.getLogger("garmin_ext.status")

# The report time zone is REQUIRED and has no default: a baked-in zone would be
# one person's location in a public build and would silently shift "today" for
# everyone else. launch.py --check-config refuses to start the server without a
# valid REPORT_TZ (check_report_tz below). If a tool still runs without one
# (launch.py started by hand, a test), zone() raises BadArgument with a
# one-line fix and the tool answers "Error: REPORT_TZ is not set: ..." instead
# of crashing or guessing a zone; launch.py's tool-error wrapper then gives the
# client the internal-error text with a ref and logs the original under it.
TZ_ENV = "REPORT_TZ"
TZ_EXAMPLE = "Europe/London"
_TZ_SHOWN_MAX = 64  # an echoed bad value is cut to this many characters
# Per-call budget inside a report. Upstream's proxy allows 90 s per call; a
# report would rather mark one stalled endpoint missing than wait that long.
DEFAULT_CALL_TIMEOUT_S = 30.0
# Concurrent Garmin calls per report: enough to finish in a few seconds, few
# enough not to look like a burst to Garmin's rate limiter.
DEFAULT_CONCURRENCY = 6
# Concurrent Garmin calls of all reports together, per event loop (the
# server's): two reports at once still send at most this many.
PROCESS_CONCURRENCY = 6
# Whole-report budget: calls still queued when it runs out are skipped, so a
# Garmin outage costs a report about this long, not calls x timeout.
DEFAULT_DEADLINE_S = 45.0

_ERROR_MAX = 200
_URL_RE = re.compile(r"https?://\S+")
# Garmin API paths ("/usersummary-service/usersummary/daily/<display name>"):
# an error body can echo the request path without its host.
_PATH_RE = re.compile(r"/[\w.-]+-service/[^\s'\"<>]*")
# Failures of the serial first call that mean every other call would fail too
# (login missing or expired, rate limit, network down, stall): skip the rest
# instead of sending Garmin a burst of doomed requests. Data-specific failures
# (an empty day, a 404) are NOT systemic: garminconnect raises
# GarminConnectConnectionError for those too ("No data received from server",
# "API client error (404)"), and upstream's proxy appends the same "may be
# unreachable; check your network connection" hint to every such error, so
# neither the class nor the hint can be the test. Network-level failures carry
# requests/urllib3 wording, matched here. HTTP statuses are matched only in
# their error forms ("API Error 429", "(429)"): a bare "429" can be part of an
# id or a trace id in a 404 body. fetch_sync() asks this only for failures the
# tool_errors table put in the "unavailable" group (login and rate limit are
# always systemic; no data, bad input and internal errors never are).
_SYSTEMIC_MARKERS = (
    "authentication", "garmin login", "garmin-mcp-auth", "rate limit",
    "toomanyrequests", "api error 429", "(429)", "returned 429", "api error 401",
    "(401)", "api error 403", "(403)", "not authenticated", "timeout", "timed out",
    "did not return within", "max retries exceeded",
    "failed to establish a new connection", "name resolution",
    "nodename nor servname", "name or service not known", "connection refused",
    "connection reset", "connection aborted", "remote end closed",
    "network is unreachable",
)
_UPSTREAM_HINTS = (
    "garmin connect may be unreachable; check your network connection or try again later",
)
# The source phrases (tool_errors.source_phrase) of the groups that end a report.
_SYSTEMIC_PHRASES = frozenset(
    tool_errors.source_phrase(group)
    for group in (tool_errors.LOGIN, tool_errors.RATE_LIMITED, tool_errors.UNAVAILABLE)
)
_TIMEOUT_PREFIX = "timeout after"  # fetch_all's own timeout: "timeout after 30s"
# Log level of a failed source by group; internal errors log at ERROR with the traceback.
_LOG_LEVELS = {
    tool_errors.LOGIN: logging.WARNING, tool_errors.RATE_LIMITED: logging.WARNING,
    tool_errors.UNAVAILABLE: logging.WARNING, tool_errors.BAD_INPUT: logging.INFO,
    tool_errors.NO_DATA: logging.DEBUG,
}


class BadArgument(tool_errors.KnownFailure, ValueError):
    """A bad tool argument, or an unset or unknown REPORT_TZ; the text says what to fix.

    The status tools answer it as "Error: <text>". Its text starts the way
    tool_errors' bad-input rows expect ("date must be YYYY-MM-DD, got ...",
    "text_format must be ..."), so the tool-error wrapper groups it as bad
    input; the REPORT_TZ texts group as internal (the server owner fixes them,
    not the caller).
    """


class NoData(tool_errors.KnownFailure, ValueError):
    """Garmin has nothing usable for the request (a course without elevation points); groups as no data."""


class SystemicFailure(tool_errors.KnownFailure):
    """The first Garmin call of a report failed in a way every other call
    would share (login, rate limit, network, stall); no other call was made.
    str() is the source phrase or fetch_all's 'timeout after Ns'."""

# Key mode (GARMIN_AUTH=key, ext/key_mode.py): Garmin's Personal API has no
# data for some sources. The status tools never call them: fetch_all(skip=...)
# records each as "skipped: " + NOT_WITH_KEY, and every key-mode report carries
# KEY_MODE_NOTE ("key_mode_note", and the last line of its markdown), because
# claude.ai drops the server instructions that would otherwise explain the gaps.
NOT_WITH_KEY = "not available with a Garmin Personal API key"
KEY_MODE_NOTE = ("This server reads Garmin through a Personal API key, so training readiness, training status and "
                 "load, planned workouts, devices and gear are not available and are listed as missing.")

# The report dates the tools accept: a year inside date.min and date.max (see report_date).
REPORT_DATE_MIN = dt.date(2, 1, 1)
REPORT_DATE_MAX = dt.date(9998, 12, 31)

WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")  # date.weekday() order
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


# --- time ---------------------------------------------------------------------

def _tz_unset_error() -> str:
    return (f"{TZ_ENV} is not set: set it to your IANA time zone name "
            f"(for example {TZ_EXAMPLE}) and restart the server")


def _load_zone(name: Any) -> ZoneInfo:
    """ZoneInfo(name), or BadArgument("unknown time zone ...") for anything else.

    ZoneInfo raises ZoneInfoNotFoundError (a KeyError), ValueError, TypeError
    or OSError depending on the input; callers get one type, BadArgument (a
    ValueError).
    """
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, TypeError, OSError):
        shown = repr(name)[:_TZ_SHOWN_MAX]
        raise BadArgument(f"unknown time zone {shown}: use an IANA name such as {TZ_EXAMPLE}") from None


def check_report_tz(environ: Mapping[str, str]) -> List[str]:
    """Startup check for REPORT_TZ; [] means OK, else one error line per problem.

    Called by launch.py --check-config (with os.environ) before the server
    starts. Errors: REPORT_TZ unset or blank, or not a known IANA zone.
    Never raises.
    """
    value = environ.get(TZ_ENV)
    name = value.strip() if isinstance(value, str) else ""
    if not name:
        return [_tz_unset_error()]
    try:
        _load_zone(name)
    except ValueError as exc:
        return [f"{TZ_ENV}: {exc}"]
    return []


def tz_name() -> str:
    """REPORT_TZ (surrounding spaces dropped). BadArgument (a ValueError) when unset or blank."""
    name = (os.environ.get(TZ_ENV) or "").strip()
    if not name:
        raise BadArgument(_tz_unset_error())
    return name


def zone(name: Optional[str] = None) -> ZoneInfo:
    """The zone `name`, else REPORT_TZ. BadArgument (a ValueError, clear text) when unset or unknown."""
    if name:
        return _load_zone(name)
    env_name = tz_name()  # raises when REPORT_TZ is unset
    try:
        return _load_zone(env_name)
    except ValueError as exc:
        raise BadArgument(f"{TZ_ENV}: {exc}") from None


def now_local(tz: ZoneInfo) -> dt.datetime:
    return dt.datetime.now(tz)


def today_local(tz: ZoneInfo) -> dt.date:
    return now_local(tz).date()


def report_date(day: dt.date, name: str, value: Any) -> dt.date:
    """`day` when it lies in REPORT_DATE_MIN..REPORT_DATE_MAX, else BadArgument naming the argument `name`.

    A report also reads the days around its date (the daily status the day
    before and after it and two more days of HRV, the weekly status 89 days
    of weigh-ins), and date arithmetic past year 1 or 9999 raises
    OverflowError, which would answer a bad argument as an internal error.
    The text names YYYY-MM-DD, so the tool-error wrapper gives the date
    detail.
    """
    if not REPORT_DATE_MIN <= day <= REPORT_DATE_MAX:
        raise BadArgument(f"{name} must be YYYY-MM-DD between {REPORT_DATE_MIN.isoformat()} and "
                          f"{REPORT_DATE_MAX.isoformat()}, got {value!r}")
    return day


def parse_date(value: Optional[str], tz: ZoneInfo) -> dt.date:
    """'' or None -> today in tz; 'YYYY-MM-DD' -> that date; else BadArgument (a ValueError).

    A date outside REPORT_DATE_MIN..REPORT_DATE_MAX is a BadArgument too (report_date).
    """
    if value is None or not str(value).strip():
        return today_local(tz)
    try:
        day = dt.date.fromisoformat(str(value).strip())
    except ValueError:
        raise BadArgument(f"date must be YYYY-MM-DD, got {value!r}") from None
    return report_date(day, "date", value)


def iso_now(tz: ZoneInfo) -> str:
    """Local wall-clock time with offset, seconds precision."""
    return now_local(tz).replace(microsecond=0).isoformat()


def en_date(d: dt.date) -> str:
    """10 Nov (day, short month name): no day/month order to misread."""
    return f"{d.day} {MONTHS[d.month - 1]}"


def en_weekday(d: dt.date, short: bool = False) -> str:
    """Tuesday, or Tue when short."""
    name = WEEKDAYS[d.weekday()]
    return name[:3] if short else name


# Hebrew, Arabic, Syriac, Thaana, NKo, Samaritan and their presentation forms
# (U+FEFF, the byte order mark, is not right-to-left).
_RTL_RE = re.compile("[\u0590-\u08FF\uFB1D-\uFDFF\uFE70-\uFEFC]")


def isolate(name: Any) -> Any:
    """A right-to-left name (Hebrew, Arabic) wrapped in FIRST STRONG ISOLATE ...
    POP DIRECTIONAL ISOLATE, so it keeps its own word order inside an English
    line: without them, a Hebrew name followed by "5x1000" shows the number on
    the wrong side. Other names come back unchanged. The marks are invisible."""
    if isinstance(name, str) and _RTL_RE.search(name):
        return f"\u2068{name}\u2069"
    return name


def hours_to_hmm(hours: Any) -> Optional[str]:
    """6.85 -> '6:51'. None for anything that is not a number."""
    h = num(hours)
    if h is None or h < 0:
        return None
    total_min = int(round(h * 60))
    return f"{total_min // 60}:{total_min % 60:02d}"


# --- report text (shared by daily_status and weekly_status) ---------------------

NO_VALUE = "no data"   # a null value in the report text
_MD_SPECIAL_RE = re.compile(r"([\\`*_\[\]<>#|~])")


def text_format(value: Any) -> str:
    """A report's text_format argument: '' (none) or 'markdown'; stripped and lower-cased, so ' Markdown '
    counts and blank or None means none. Anything else is BadArgument("text_format must be ...")."""
    fmt = value.strip().lower() if isinstance(value, str) else value
    if not fmt:
        return ""
    if fmt != "markdown":
        raise BadArgument(f"text_format must be '' or 'markdown', got {value!r}")
    return fmt


def local_now(zone: ZoneInfo, now: Optional[dt.datetime] = None) -> dt.datetime:
    """`now` in `zone` (a naive `now` is taken as wall-clock time there); the current time when None."""
    if now is None:
        return now_local(zone)
    return now.replace(tzinfo=zone) if now.tzinfo is None else now.astimezone(zone)


def word_count(text: str) -> int:
    """Words of a markdown report text; '**' counts as a space, so bold never joins two words."""
    return len(re.sub(r"\*\*", " ", text or "").split())


def md_escape(text: str) -> str:
    """Markdown special characters escaped with a backslash (~ too: '~~x~~' in a name is no strikethrough)."""
    return _MD_SPECIAL_RE.sub(r"\\\1", text)


def num_txt(value: Any) -> str:
    """A JSON number as report text: whole numbers without a decimal point, others to at most 2 decimals
    without trailing zeros (1.5, 1.23); NO_VALUE for anything that is not a number."""
    v = num(value)
    if v is None:
        return NO_VALUE
    if isinstance(v, int):
        return str(v)
    if float(v).is_integer():
        return str(int(v))
    text = f"{v:.2f}".rstrip("0").rstrip(".")
    return "0" if text == "-0" else text


# --- numbers and nested payloads ----------------------------------------------

def num(value: Any) -> Optional[float]:
    """A real number, else None (bools, strings, NaN and None are not numbers)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value != value:  # NaN
        return None
    return value


def rnd(value: Any, ndigits: int = 0) -> Optional[float]:
    """round() that passes None through and returns int for ndigits=0."""
    v = num(value)
    if v is None:
        return None
    r = round(v, ndigits)
    return int(r) if ndigits == 0 else r


def acwr_ratio(acute: Any, chronic: Any, garmin_ratio: Any) -> Optional[float]:
    """Acute:chronic workload ratio, 2 decimals, computed from the two loads.

    Garmin's own dailyAcuteChronicWorkloadRatio is cut (not rounded) to one
    decimal: acute 654 and chronic 948 come back as 0.6, not 0.69, which can
    hide a ratio just over a threshold (1.39 reads 1.3). Garmin's ratio is only
    the fallback when a load is missing or chronic is 0.
    """
    a, c = num(acute), num(chronic)
    if a is not None and c is not None and a >= 0 and c > 0:
        return round(a / c, 2)
    r = num(garmin_ratio)
    return round(r, 2) if r is not None and r >= 0 else None


def dig(obj: Any, *path: Any, default: Any = None) -> Any:
    """Safe nested lookup: dig(d, "hrvSummary", "baseline", "balancedLow").

    Integer keys index lists. Any miss, or a None on the way, returns default.
    """
    cur = obj
    for key in path:
        if isinstance(key, int) and not isinstance(key, bool):
            if isinstance(cur, (list, tuple)) and -len(cur) <= key < len(cur):
                cur = cur[key]
            else:
                return default
        elif isinstance(cur, dict) and key in cur:
            cur = cur[key]
        else:
            return default
        if cur is None:
            return default
    return cur


def key_paths(obj: Any, max_depth: int = 3, max_keys: int = 300) -> List[str]:
    """Key paths of a payload without its values ("dailySleepDTO.sleepTimeSeconds").

    For checking Garmin field names against a live response without copying
    any personal value. Lists contribute their first element as "[]".
    """
    out: List[str] = []

    def walk(node: Any, prefix: str, depth: int) -> None:
        if len(out) >= max_keys or depth > max_depth:
            return
        if isinstance(node, dict):
            for k in sorted(node, key=str):
                path = f"{prefix}.{k}" if prefix else str(k)
                out.append(path)
                if len(out) >= max_keys:
                    return
                walk(node[k], path, depth + 1)
        elif isinstance(node, list) and node:
            walk(node[0], f"{prefix}[]", depth + 1)

    walk(obj, "", 1)
    return out


def compact_json(obj: Any) -> str:
    """One-line JSON, Hebrew kept as text (tool results are read by people)."""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


# --- sports -------------------------------------------------------------------

def sport_group(type_key: Any) -> str:
    """Garmin activity/workout type key -> run | bike | swim | other.

    Covers running, treadmill_running, trail_running, virtual_run, cycling,
    road_biking, virtual_ride, indoor_cycling, lap_swimming,
    open_water_swimming and friends. Motor sports (motorcycling, ...) are
    other, not bike.
    """
    key = str(type_key or "").lower()
    if "swim" in key:
        return "swim"
    if "run" in key:
        return "run"
    if "motor" not in key and any(part in key for part in ("cycl", "bik", "ride")):
        return "bike"
    return "other"


def match_completions(scheduled: List[Dict[str, Any]], activities: List[Dict[str, Any]]) -> List[bool]:
    """Which scheduled workouts were done; shared by the daily and weekly reports.

    Garmin rarely links a finished activity to its scheduled workout
    (associatedActivityId stays null), so a workout also counts as done when an
    activity of the same sport group happened on the same local date. Each
    activity completes at most one workout; linked activities are never reused.

    scheduled:  [{"date": "YYYY-MM-DD", "sport": "run|bike|swim|other",
                  "activity_id": <associatedActivityId or None>}, ...]
    activities: [{"date": "YYYY-MM-DD" (local start date), "sport": ...,
                  "id": <activityId>}, ...] in start-time order.
    Returns one bool per scheduled workout, in order. Ids stay internal.
    """
    return [shift is not None for shift in match_completion_shifts(scheduled, activities, 0)]


def _signed_day_gap(activity_day: Any, planned_day: Any) -> Optional[int]:
    """activity_day - planned_day in whole days (negative = done early); None when either is not a date."""
    try:
        return (dt.date.fromisoformat(str(activity_day)) - dt.date.fromisoformat(str(planned_day))).days
    except ValueError:
        return 0 if activity_day is not None and activity_day == planned_day else None


def match_completion_shifts(
    scheduled: List[Dict[str, Any]],
    activities: List[Dict[str, Any]],
    max_shift_days: int = 0,
    *,
    max_early_days: Optional[int] = None,
    max_late_days: Optional[int] = None,
) -> List[Optional[int]]:
    """Per scheduled workout: how many days away it was done, or None if not done.

    Same inputs as match_completions. 0 = linked by Garmin, or an activity of
    the same sport group on the planned day; n = the nearest unused activity of
    that sport n days before or after (n <= max_shift_days). max_early_days /
    max_late_days, when given, replace max_shift_days for that direction: the
    weekly report passes 1 early / 2 late, so a session done days before the
    plan (or before the workout existed) never completes it; the daily report
    uses 0. Same-day matches are made first for every workout, then 1 day away,
    and so on, so a moved session never takes the activity of a session done on
    its own day. Then each workout still without one tries an augmenting path
    (an already matched workout moves to another activity in its own window), so
    the number of completed workouts is the largest possible: planned Tue + Wed,
    runs Mon + Tue gives Mon->Tue and Tue->Wed, not Tue->Tue alone. Each activity
    completes at most one workout.
    """
    early = max(0, int(max_shift_days if max_early_days is None else max_early_days))
    late = max(0, int(max_shift_days if max_late_days is None else max_late_days))
    linked_ids = {s.get("activity_id") for s in scheduled if s.get("activity_id") is not None}
    used = {i for i, act in enumerate(activities) if act.get("id") is not None and act.get("id") in linked_ids}
    shifts: List[Optional[int]] = [0 if s.get("activity_id") is not None else None for s in scheduled]

    def gap_of(n: int, i: int) -> Optional[int]:
        """Signed day gap when activity i may complete workout n (same sport, inside the window), else None."""
        act, item = activities[i], scheduled[n]
        if act.get("sport") is None or act.get("sport") != item.get("sport"):
            return None
        gap = _signed_day_gap(act.get("date"), item.get("date"))
        if gap is None or (-gap > early if gap < 0 else gap > late):
            return None
        return gap

    owner: Dict[int, int] = {}  # activity index -> workout index (matched here, not Garmin-linked)
    for shift in range(0, max(early, late) + 1):
        for n in range(len(scheduled)):
            if shifts[n] is not None:
                continue
            for i in range(len(activities)):
                if i in used or i in owner:
                    continue
                gap = gap_of(n, i)
                if gap is not None and abs(gap) == shift:
                    owner[i] = n
                    shifts[n] = shift
                    break

    def augment(n: int, seen: Set[int]) -> bool:
        candidates = [(abs(g), i) for i in range(len(activities)) if i not in used and (g := gap_of(n, i)) is not None]
        for _, i in sorted(candidates):
            if i in seen:
                continue
            seen.add(i)
            if i not in owner or augment(owner[i], seen):
                owner[i] = n
                return True
        return False

    for n in range(len(scheduled)):
        if shifts[n] is None and scheduled[n].get("activity_id") is None:
            augment(n, set())
    for i, n in owner.items():
        shifts[n] = abs(cast(int, gap_of(n, i)))  # owner holds only pairs whose gap is not None
    return shifts


# --- fetching -----------------------------------------------------------------

Call = Tuple[Callable[..., Any], Tuple[Any, ...], Dict[str, Any]]


def call(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Call:
    """Describe one client call for fetch_all: call(client.get_stats, "2026-11-10")."""
    return (fn, args, kwargs)


# Ids taken from one Garmin answer go into the path or query of the next Garmin request. Garmin's answer is
# not trusted input: a value like "x/../../userprofile-service" would steer the request to another endpoint,
# so each id must have its exact shape or the item is treated as having no id. fullmatch, not $: "$" also
# matches before a trailing newline.
_UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_UUID_HEX_RE = re.compile(r"[0-9a-fA-F]{32}")
_DIGITS_RE = re.compile(r"[0-9]{1,19}")


def path_uuid(value: Any, dashless: bool = False) -> Optional[str]:
    """value if it is a canonical 8-4-4-4-12 hex UUID (or, with dashless, 32 hex digits), else None.

    Never stripped or repaired: a value with any other character is not an id.
    """
    if not isinstance(value, str):
        return None
    if _UUID_RE.fullmatch(value) or (dashless and _UUID_HEX_RE.fullmatch(value)):
        return value
    return None


def path_int_id(value: Any) -> Optional[int]:
    """A positive integer id (an int, or a string of ASCII digits), else None. bool is not an id."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and _DIGITS_RE.fullmatch(value) and int(value) > 0:
        return int(value)
    return None


# garminconnect's activity search path (Garmin.garmin_connect_activities); used when the client has none.
ACTIVITIES_PATH = "/activitylist-service/activities/search/activities"


def client_method(client: Any, name: str) -> Callable[..., Any]:
    """client.<name>(...), looked up when the call runs, in fetch_all's worker thread.

    Upstream's proxy blocks at the lookup (up to 90 s) while its background
    login is pending and raises the stored login error there once the login
    failed, so the lookup must not happen on the event loop: one report would
    stall every other request of the server, one wait per method. A missing
    client or method is one failed source. A failed lookup raises its own
    exception, unwrapped: fetch_sync groups a failure by its type and text,
    so a wrapper would turn a rate limit or a network failure into an
    internal error.
    """
    def invoke(*args: Any, **kwargs: Any) -> Any:
        fn = getattr(client, name)
        if not callable(fn):
            raise AttributeError(f"Garmin client attribute {name} is not callable")
        return fn(*args, **kwargs)

    invoke.__name__ = name
    return invoke


def activities_call(client: Any) -> Callable[..., Any]:
    """connectapi(<the client's activity search path, else ACTIVITIES_PATH>, **kwargs); the path is read in
    the worker thread too."""
    def invoke(**kwargs: Any) -> Any:
        try:
            path = getattr(client, "garmin_connect_activities", None)
        except Exception:  # noqa: BLE001 - fall back to garminconnect's path
            path = None
        return client_method(client, "connectapi")(path if isinstance(path, str) and path else ACTIVITIES_PATH,
                                                   **kwargs)

    invoke.__name__ = "connectapi"
    return invoke


def clean_error(exc: BaseException) -> str:
    """Short error text without URLs or API paths: both carry the account's display name."""
    text = f"{type(exc).__name__}: {exc}".strip()
    text = _URL_RE.sub("<url>", text)
    return _PATH_RE.sub("<path>", text)[:_ERROR_MAX]


def _utc_stamp() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def fetch_sync(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Run one client call; never raises.

    -> {"ok": True, "data": <payload>, "fetched_at", "ms"} or
       {"ok": False, "error": <source phrase>, "systemic": <bool>, "group": <tool_errors group>,
        "_failure": (group, ref, original text, exception), "fetched_at", "ms"}.

    tool_errors.classify_exception() puts a failure in a group; "error" is
    that group's fixed source phrase ("no data from Garmin", "internal error
    (ref 1a2b3c4d)" with a new ref, ...), never exception text. "systemic"
    follows the group: login and rate limit always; Garmin unavailable when
    the full text names a network failure or a stall (is_systemic_error, on
    the whole chain text, not a cut copy); never no data, bad input or an
    internal error, so a bug inside a Garmin call never ends a report.
    "_failure" is for fetch_all's log line only; fetch_all removes it.
    """
    started = time.monotonic()
    result: Dict[str, Any] = {"fetched_at": _utc_stamp()}
    try:
        result["data"] = fn(*args, **kwargs)
        result["ok"] = True
    except Exception as exc:  # noqa: BLE001 - every failure becomes "missing"
        group, _detail, _root, full = tool_errors.classify_exception(exc)
        ref = tool_errors.new_ref() if group == tool_errors.INTERNAL else None
        result["ok"] = False
        result["error"] = tool_errors.source_phrase(group, ref)
        result["systemic"] = group in (tool_errors.LOGIN, tool_errors.RATE_LIMITED) or (
            group == tool_errors.UNAVAILABLE and is_systemic_error(full))
        result["group"] = group
        result["_failure"] = (group, ref, full, exc)
    result["ms"] = int((time.monotonic() - started) * 1000)
    return result


def _skipped(reason: str) -> Dict[str, Any]:
    return {"ok": False, "error": f"skipped: {reason}", "fetched_at": _utc_stamp(), "ms": 0}


def is_systemic_error(error: Any) -> bool:
    """True for errors that would hit every Garmin call alike (see _SYSTEMIC_MARKERS)."""
    text = str(error or "").lower()
    for hint in _UPSTREAM_HINTS:
        text = text.replace(hint, "")
    return any(marker in text for marker in _SYSTEMIC_MARKERS)


def systemic_failure(results: Dict[str, Dict[str, Any]], first: str) -> Optional[str]:
    """Why a report's serial first call ended the report, or None.

    The first result's error when that call failed systemically (fetch_all
    then skipped every other call) and the error is the login, rate-limit or
    Garmin-unavailable source phrase or fetch_all's "timeout after Ns". None
    when the call succeeded, failed for itself only (no data, bad input, an
    internal error), or `first` is not in `results`.
    """
    res = results.get(first) if isinstance(results, dict) else None
    if not isinstance(res, dict) or res.get("ok") or not res.get("systemic"):
        return None
    error = res.get("error")
    if isinstance(error, str) and (error in _SYSTEMIC_PHRASES or error.startswith(_TIMEOUT_PREFIX)):
        return error
    return None


def _log_failure(source: str, group: str, ref: Optional[str], text: Any, exc: Optional[BaseException]) -> None:
    """One server-log line for a failed source: its original text, redacted to one line.

    An internal error logs at ERROR with its ref and traceback; the other
    groups log at WARNING (login, rate limit, Garmin unavailable), INFO (bad
    input) or DEBUG (no data). `source` is the report's own name for the
    call, never caller text.
    """
    if group == tool_errors.INTERNAL:
        log.error("status source failed: ref=%s source=%s group=internal: %s", ref, source,
                  tool_errors.redact(text), exc_info=exc)
        return
    level = _LOG_LEVELS.get(group, logging.WARNING)
    if log.isEnabledFor(level):
        log.log(level, "status source failed: source=%s group=%s: %s", source, group, tool_errors.redact(text))


def unusable_answer(result: Dict[str, Any], source: str, why: str) -> Dict[str, Any]:
    """A fetch that worked but whose answer cannot be used, recorded as a failed source.

    For an error Garmin reports inside a normal answer (a GraphQL error
    arrives as HTTP 200 with "data": null): the source gets the Garmin
    unavailable phrase ("an error on Garmin's side") and one "status source
    failed:" log line with `why`, a fixed text of the caller's, never
    Garmin's. The other keys of `result` stay; it is not systemic.
    """
    _log_failure(source, tool_errors.UNAVAILABLE, None, why, None)
    return dict(result, ok=False, error=tool_errors.source_phrase(tool_errors.UNAVAILABLE))


# The guard outcome of a report (launch.py's call guard, garmin_guard): a rate limit or a login failure on ANY
# source must start the guard's cooldown, also when the report still answers with what the other sources gave.
# launch.py opens a capture around each tool call (capture_outcomes); fetch_all adds the group of every such
# failed source to it; launch.py hands guard_group() of the captured groups to the guard.
_COOLDOWN_GROUPS = (tool_errors.RATE_LIMITED, tool_errors.LOGIN)   # in order of precedence
_OUTCOMES: "contextvars.ContextVar[Optional[Set[str]]]" = contextvars.ContextVar("coach_status_outcomes", default=None)


def capture_outcomes() -> Tuple[contextvars.Token[Optional[Set[str]]], Set[str]]:
    """Start collecting the cooldown groups fetch_all meets in this context: (token for end_capture, the set
    it fills). The set is shared, so a fetch_all in a copied context (a task, a worker thread) still adds to it."""
    groups: Set[str] = set()
    return _OUTCOMES.set(groups), groups


def end_capture(token: contextvars.Token[Optional[Set[str]]]) -> None:
    _OUTCOMES.reset(token)


def _note_group(group: Optional[str]) -> None:
    if group in _COOLDOWN_GROUPS:
        sink = _OUTCOMES.get()
        if sink is not None:
            sink.add(group)


def guard_group(groups: Iterable[Optional[str]]) -> Optional[str]:
    """The group the call guard should hear for a report whose sources met `groups`: rate_limited before login,
    else None (nothing that starts a cooldown)."""
    seen = set(groups)
    for group in _COOLDOWN_GROUPS:
        if group in seen:
            return group
    return None


# One semaphore per event loop (an asyncio.Semaphore belongs to the loop it first waits on; tests run many
# loops), shared by every fetch_all on that loop.
_LOOP_SEMAPHORES: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore]" = (
    weakref.WeakKeyDictionary())


def _process_semaphore(loop: asyncio.AbstractEventLoop) -> asyncio.Semaphore:
    """The loop's shared semaphore of PROCESS_CONCURRENCY slots (made on first use; only the loop's thread
    calls this)."""
    sem = _LOOP_SEMAPHORES.get(loop)
    if sem is None:
        sem = _LOOP_SEMAPHORES[loop] = asyncio.Semaphore(max(1, int(PROCESS_CONCURRENCY)))
    return sem


async def fetch_all(
    calls: Dict[str, Call],
    *,
    first: Optional[str] = None,
    timeout_s: float = DEFAULT_CALL_TIMEOUT_S,
    concurrency: int = DEFAULT_CONCURRENCY,
    deadline_s: float = DEFAULT_DEADLINE_S,
    skip: Iterable[str] = frozenset(),
) -> Dict[str, Dict[str, Any]]:
    """Run named calls in worker threads; never raises (but see `skip`).

    `first` runs alone before the rest: garminconnect refreshes its OAuth token
    without a lock, so one serial call lets an expiring token refresh once
    instead of racing in parallel threads. If that call fails with a systemic
    error (auth, rate limit, network, stall), the others are skipped
    (systemic_failure() then names the reason).

    At most `concurrency` calls of this fetch_all run at once, and at most
    PROCESS_CONCURRENCY of all fetch_all calls on the same event loop together.

    A call that outlives timeout_s, or the time left of deadline_s, is recorded
    as failed; its thread is left to finish on its own. Calls that have not
    started when deadline_s runs out are recorded as skipped.

    A failed call keeps its tool_errors group in "group" (fetch_all's own
    timeouts: unavailable). A rate-limited or login failure is also added to
    the capture launch.py opened for the tool call (capture_outcomes), so the
    call guard starts its cooldown even though the report still answers.

    Each failed call writes one "status source failed:" line to the
    "garmin_ext.status" log (its own timeouts as group unavailable); skipped
    calls write none. No returned result holds fetch_sync's "_failure".

    Names in `skip` (key mode: sources Garmin's Personal API does not serve)
    are never called: each is recorded as "skipped: " + NOT_WITH_KEY, also
    when a systemic first failure skips the others. `first` in `skip` is a
    programming error: ValueError, before any call.
    """
    skip = frozenset(skip)
    if first is not None and first in skip:
        raise ValueError(f"fetch_all: first={first!r} is in skip")
    loop = asyncio.get_running_loop()
    t_end = loop.time() + max(0.0, float(deadline_s))
    sem = asyncio.Semaphore(max(1, int(concurrency)))
    shared = _process_semaphore(loop)

    async def one(name: str, spec: Call) -> Tuple[str, Dict[str, Any]]:
        fn, args, kwargs = spec
        async with sem, shared:
            budget = min(float(timeout_s), t_end - loop.time())
            if budget <= 0:
                return name, _skipped("report deadline reached")
            try:
                res = await asyncio.wait_for(
                    asyncio.to_thread(fetch_sync, fn, *args, **kwargs), budget
                )
            except asyncio.TimeoutError:
                res = {
                    "ok": False,
                    "error": f"timeout after {budget:.0f}s",
                    "fetched_at": _utc_stamp(),
                    "ms": int(budget * 1000),
                    "systemic": True,
                    "group": tool_errors.UNAVAILABLE,
                }
                _log_failure(name, tool_errors.UNAVAILABLE, None, res["error"], None)
            else:
                failure = res.pop("_failure", None)
                if failure is not None:
                    _log_failure(name, *failure)
                    _note_group(failure[0])
        return name, res

    results: Dict[str, Dict[str, Any]] = {}
    if first is not None and first in calls:
        name, res = await one(first, calls[first])
        results[name] = res
        if not res.get("ok") and res.get("systemic"):
            for other in calls:
                if other in skip:
                    results[other] = _skipped(NOT_WITH_KEY)
                elif other != first:
                    results[other] = _skipped(f"{first} failed: {res.get('error')}"[:_ERROR_MAX])
            return results
    if not skip:
        rest = [one(name, spec) for name, spec in calls.items() if name != first]
        for name, res in await asyncio.gather(*rest):
            results[name] = res
        return results
    rest = [one(name, spec) for name, spec in calls.items() if name != first and name not in skip]
    gathered = dict(await asyncio.gather(*rest))
    for name in calls:
        if name != first:
            results[name] = _skipped(NOT_WITH_KEY) if name in skip else gathered[name]
    return results


def data_of(result: Optional[Dict[str, Any]]) -> Any:
    """The payload of a successful fetch, else None."""
    if isinstance(result, dict) and result.get("ok"):
        return result.get("data")
    return None


def source_meta(results: Dict[str, Dict[str, Any]], names: Optional[Iterable[str]] = None) -> Dict[str, Dict[str, Any]]:
    """{name: {ok, fetched_at, ms[, error]}} for a report's "sources" field."""
    out: Dict[str, Dict[str, Any]] = {}
    for name in (names if names is not None else results):
        res = results.get(name)
        if res is None:
            continue
        meta = {"ok": bool(res.get("ok")), "fetched_at": res.get("fetched_at"), "ms": res.get("ms")}
        if not res.get("ok"):
            meta["error"] = res.get("error")
        out[name] = meta
    return out
