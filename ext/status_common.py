"""Shared helpers for the status tools in ext/ (daily_status, weekly_status).

Standard library only, so the unit tests run without garmin_mcp installed.

The Garmin client upstream hands us (garmin_mcp's _GarminProxy) is synchronous
and bounds each call with its own 90 s timeout. fetch_all() runs many such
calls in worker threads, a few at a time, under a shorter per-call budget, and
turns every failure into a recorded source with ok=False instead of an
exception: one missing Garmin endpoint never sinks a whole report, and missing
data is reported as missing, never guessed.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import re
import time
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# The report time zone is REQUIRED and has no default: a baked-in zone would be
# one person's location in a public build and would silently shift "today" for
# everyone else. launch.py --check-config refuses to start the server without a
# valid REPORT_TZ (check_report_tz below). If a tool still runs without one
# (launch.py started by hand, a test), zone() raises ValueError with a one-line
# fix and the tool returns it as "Error: REPORT_TZ is not set: ..." instead of
# crashing or guessing a zone.
TZ_ENV = "REPORT_TZ"
TZ_EXAMPLE = "Europe/London"
_TZ_SHOWN_MAX = 64  # an echoed bad value is cut to this many characters
# Per-call budget inside a report. Upstream's proxy allows 90 s per call; a
# report would rather mark one stalled endpoint missing than wait that long.
DEFAULT_CALL_TIMEOUT_S = 30.0
# Concurrent Garmin calls per report: enough to finish in a few seconds, few
# enough not to look like a burst to Garmin's rate limiter.
DEFAULT_CONCURRENCY = 6
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
# requests/urllib3 wording, matched here.
_SYSTEMIC_MARKERS = (
    "authentication", "garmin login", "garmin-mcp-auth", "rate limit",
    "toomanyrequests", "429", "timeout", "timed out", "did not return within",
    "max retries exceeded", "failed to establish a new connection",
    "name resolution", "nodename nor servname", "name or service not known",
    "connection refused", "connection reset", "connection aborted",
    "remote end closed", "network is unreachable",
)
_UPSTREAM_HINTS = (
    "garmin connect may be unreachable; check your network connection or try again later",
)

WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")  # date.weekday() order
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


# --- time ---------------------------------------------------------------------

def _tz_unset_error() -> str:
    return (f"{TZ_ENV} is not set: set it to your IANA time zone name "
            f"(for example {TZ_EXAMPLE}) and restart the server")


def _load_zone(name: Any) -> ZoneInfo:
    """ZoneInfo(name), or ValueError("unknown time zone ...") for anything else.

    ZoneInfo raises ZoneInfoNotFoundError (a KeyError), ValueError, TypeError
    or OSError depending on the input; callers get one ValueError type.
    """
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, TypeError, OSError):
        shown = repr(name)[:_TZ_SHOWN_MAX]
        raise ValueError(f"unknown time zone {shown}: use an IANA name such as {TZ_EXAMPLE}") from None


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
    """REPORT_TZ (surrounding spaces dropped). ValueError when unset or blank."""
    name = (os.environ.get(TZ_ENV) or "").strip()
    if not name:
        raise ValueError(_tz_unset_error())
    return name


def zone(name: Optional[str] = None) -> ZoneInfo:
    """The zone `name`, else REPORT_TZ. ValueError (clear text) when unset or unknown."""
    if name:
        return _load_zone(name)
    env_name = tz_name()  # raises when REPORT_TZ is unset
    try:
        return _load_zone(env_name)
    except ValueError as exc:
        raise ValueError(f"{TZ_ENV}: {exc}") from None


def now_local(tz: ZoneInfo) -> dt.datetime:
    return dt.datetime.now(tz)


def today_local(tz: ZoneInfo) -> dt.date:
    return now_local(tz).date()


def parse_date(value: Optional[str], tz: ZoneInfo) -> dt.date:
    """'' or None -> today in tz; 'YYYY-MM-DD' -> that date; else ValueError."""
    if value is None or not str(value).strip():
        return today_local(tz)
    try:
        return dt.date.fromisoformat(str(value).strip())
    except ValueError:
        raise ValueError(f"date must be YYYY-MM-DD, got {value!r}") from None


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


_RTL_RE = re.compile("[\u0590-\u08FF\uFB1D-\uFDFF\uFE70-\uFEFF]")


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
    open_water_swimming and friends.
    """
    key = str(type_key or "").lower()
    if "swim" in key:
        return "swim"
    if "run" in key:
        return "run"
    if any(part in key for part in ("cycl", "bik", "ride")):
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


def _day_gap(a: Any, b: Any) -> Optional[int]:
    """Whole days between two 'YYYY-MM-DD' strings; None when either is not a date."""
    try:
        return abs((dt.date.fromisoformat(str(a)) - dt.date.fromisoformat(str(b))).days)
    except ValueError:
        return 0 if a is not None and a == b else None


def match_completion_shifts(
    scheduled: List[Dict[str, Any]],
    activities: List[Dict[str, Any]],
    max_shift_days: int = 0,
) -> List[Optional[int]]:
    """Per scheduled workout: how many days away it was done, or None if not done.

    Same inputs as match_completions. 0 = linked by Garmin, or an activity of
    the same sport group on the planned day; n = the nearest unused activity of
    that sport n days before or after (n <= max_shift_days). People move
    sessions inside a week, so the weekly report passes max_shift_days=6 over
    that week's activities; the daily report uses 0. Same-day matches are made
    first for every workout, then 1 day away, and so on, so a moved session
    never takes the activity of a session done on its own day. Each activity
    completes at most one workout.
    """
    linked_ids = {s.get("activity_id") for s in scheduled if s.get("activity_id") is not None}
    used = {i for i, act in enumerate(activities) if act.get("id") is not None and act.get("id") in linked_ids}
    shifts: List[Optional[int]] = [0 if s.get("activity_id") is not None else None for s in scheduled]
    for shift in range(0, max(0, int(max_shift_days)) + 1):
        for n, item in enumerate(scheduled):
            if shifts[n] is not None:
                continue
            for i, act in enumerate(activities):
                if i in used or act.get("sport") != item.get("sport"):
                    continue
                if _day_gap(act.get("date"), item.get("date")) == shift:
                    used.add(i)
                    shifts[n] = shift
                    break
    return shifts


# --- fetching -----------------------------------------------------------------

Call = Tuple[Callable[..., Any], tuple, dict]


def call(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Call:
    """Describe one client call for fetch_all: call(client.get_stats, "2026-11-10")."""
    return (fn, args, kwargs)


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
       {"ok": False, "error": <short text>, "fetched_at", "ms"}.
    """
    started = time.monotonic()
    result: Dict[str, Any] = {"fetched_at": _utc_stamp()}
    try:
        result["data"] = fn(*args, **kwargs)
        result["ok"] = True
    except Exception as exc:  # noqa: BLE001 - every failure becomes "missing"
        result["ok"] = False
        result["error"] = clean_error(exc)
        # Judged on the full text: clean_error() truncates, and a network
        # error's telling words can sit past the cut.
        result["systemic"] = is_systemic_error(f"{type(exc).__name__}: {exc}")
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


async def fetch_all(
    calls: Dict[str, Call],
    *,
    first: Optional[str] = None,
    timeout_s: float = DEFAULT_CALL_TIMEOUT_S,
    concurrency: int = DEFAULT_CONCURRENCY,
    deadline_s: float = DEFAULT_DEADLINE_S,
) -> Dict[str, Dict[str, Any]]:
    """Run named calls in worker threads; never raises.

    `first` runs alone before the rest: garminconnect refreshes its OAuth token
    without a lock, so one serial call lets an expiring token refresh once
    instead of racing in parallel threads. If that call fails with a systemic
    error (auth, rate limit, network, stall), the others are skipped.

    A call that outlives timeout_s, or the time left of deadline_s, is recorded
    as failed; its thread is left to finish on its own. Calls that have not
    started when deadline_s runs out are recorded as skipped.
    """
    loop = asyncio.get_running_loop()
    t_end = loop.time() + max(0.0, float(deadline_s))
    sem = asyncio.Semaphore(max(1, int(concurrency)))

    async def one(name: str, spec: Call) -> Tuple[str, Dict[str, Any]]:
        fn, args, kwargs = spec
        async with sem:
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
                }
        return name, res

    results: Dict[str, Dict[str, Any]] = {}
    if first is not None and first in calls:
        name, res = await one(first, calls[first])
        results[name] = res
        if not res.get("ok") and res.get("systemic"):
            for other in calls:
                if other != first:
                    results[other] = _skipped(f"{first} failed: {res.get('error')}"[:_ERROR_MAX])
            return results
    rest = [one(name, spec) for name, spec in calls.items() if name != first]
    for name, res in await asyncio.gather(*rest):
        results[name] = res
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
