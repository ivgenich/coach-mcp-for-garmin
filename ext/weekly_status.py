"""get_weekly_status: a deterministic weekly training review (English text).

The week is end_date-6 .. end_date (default end_date: yesterday, local time),
compared with the 7 days before it. One parallel fetch round reads everything
that needs no other answer; two short chains then run side by side: gear
(the profile number comes from the first round) and the long session's
decoupling (the activity comes from the first round). Every number, flag and
the level are computed here; a model only words the result, and
render_text() gives the fixed English text.

Decisions worth knowing:
- HRV: avg_7d is Garmin's own 7-day average (hrvSummary.weeklyAvg) on
  end_date. avg_28d is the mean of the weekly averages on end_date and 7, 14
  and 21 days before it (four 7-day blocks = 28 days; at least two needed).
  balanced_low / balanced_upper are Garmin's baseline on end_date.
- Sleep: the 7 nights filed under the week's dates (Garmin files a night
  under its wake-up date). A night counts only when its DTO carries that
  calendarDate and sleepTimeSeconds > 0: before a sync Garmin returns an
  empty shell.
- Volume: activities are split by their LOCAL start date (startTimeLocal),
  so a session that starts at 23:59 belongs to that day. Hours come from
  "duration", km from "distance".
- Load: Garmin's acuteTrainingLoadDTO on end_date, from the device upstream's
  get_training_status would pick (training._select_training_device);
  chronic_7d_ago comes from end_date-7. When the record belongs to a device
  other than the primary tracker, the load is left null, as upstream does.
- Plan: scheduled = workouts on Garmin's calendar in the week (rest days
  excluded). Garmin rarely links a finished activity to its workout
  (associatedActivityId stays null) and people move sessions inside a week,
  so completed comes from status_common.match_completion_shifts over THIS
  week's activities only: linked by Garmin, or an activity of the same sport
  group (for "other", of the same type) on the planned day, else up to 6 days
  from it; one activity per workout. on_planned_day counts the first two; "linked" keeps Garmin's own
  count. Without the activity list, completed and on_planned_day are null
  unless every workout is linked.
- Long session: the week's longest bike or run of at least 60 minutes. Its
  decoupling comes from upstream's FIT analysis (_parse_fit ->
  session.hr_drift: power:HR, first half vs second half); when that cannot be
  used (no FIT, no fitparse, no power), from the activity's details chart
  (power:HR, else speed:HR). The sign is upstream's: (second-half ratio -
  first-half ratio) / first-half ratio x 100, so NEGATIVE = heart rate rose
  relative to output (drift) and positive = output rose relative to heart
  rate (not drift). |x| < 5 stable, 5-10 moderate drift, > 10 large drift.
- Weight: the latest weigh-in up to end_date within 90 days, with its date.
  weight_change_kg only when that weigh-in falls inside the week: the change
  against the latest weigh-in before the week (same 90 days).
- Gear: active shoes and bikes (device_last_used -> get_gear ->
  get_gear_stats), km against the gear's maximumMeters when one is set.
  Gear uuids, the profile number and device ids are used for the calls only
  and never leave this module (not in the result, the sources or the text).

Pure parts (rules, render_text) import nothing from upstream, so tests run
without it; upstream helpers are imported inside functions, and selfcheck()
checks them at Docker build time.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import re
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import status_common as sc

RULES_VERSION = 1

# Weekly rules v1: starting values, to calibrate after a few weeks of use.
THRESHOLDS: Dict[str, Any] = {
    "hrv_drop_ms": 5,               # hrv_down: avg_7d <= avg_28d - 5 (or avg_7d < balanced_low)
    "sleep_h": 6,                   # a night shorter than this is a short night
    "short_nights": 3,              # short_nights: at least this many short nights
    "volume_jump_ratio": 1.3,       # volume_jump: total_hours >= 1.3 x prev_total_hours ...
    "volume_min_prev_h": 3,         # ... when prev_total_hours >= 3
    "load_high_status": ["HIGH", "VERY_HIGH"],  # load_high: Garmin's acwrStatus
    "completion_pct": 70,           # low_completion: completion_pct < 70 ...
    "completion_min_scheduled": 3,  # ... with at least 3 workouts scheduled
    "gear_pct": 90,                 # gear_near_limit: pct_of_max >= 90
    "long_session_min": 60,         # decoupling: the longest bike/run of at least 60 min
    "decoupling_stable_pct": 5,     # |decoupling_pct| < 5: stable
    "decoupling_large_pct": 10,     # |decoupling_pct| > 10: large drift
}

LEVEL_EN = {"green": "Green", "yellow": "Yellow", "red": "Red", "incomplete": "Incomplete data"}
TEXT_FORMATS = ("", "markdown")
MAX_WORDS = 300

# Whole-report budget (Garmin calls of every round); see status_common.fetch_all.
DEADLINE_S = 60.0
CALL_TIMEOUT_S = 30.0
# FIT download + parse of a long ride: fitparse is slow on a 3-4 h file.
FIT_TIMEOUT_S = 45.0
# 14 days of activities in one call; a triathlete's fortnight is far below it.
ACTIVITY_LIMIT = 100
ACTIVITIES_PATH = "/activitylist-service/activities/search/activities"
WEIGHT_LOOKBACK_DAYS = 90
PLAN_MAX_SHIFT_DAYS = 6  # a planned workout counts when done on any day of its week
# get_gear_stats calls per report (gear with a distance limit first).
GEAR_STATS_MAX = 8
# Days before end_date whose hrvSummary.weeklyAvg feeds avg_7d / avg_28d.
HRV_WEEKS = (0, 7, 14, 21)
# get_activity_details chart size; no polyline (GPS) is requested.
DETAILS_MAX_CHART = 2000
DETAILS_MIN_SAMPLES = 60

SCHEDULE_QUERY = 'query{{workoutScheduleSummariesScalar(startDate:"{start}", endDate:"{end}")}}'

SPORTS = ("run", "bike", "swim", "other")
SPORT_EN = {"run": "run", "bike": "bike", "swim": "swim", "other": "other"}
HRV_STATUS_EN = {"BALANCED": "balanced", "UNBALANCED": "unbalanced", "LOW": "low", "POOR": "poor", "NONE": "no status"}
ACWR_STATUS_EN = {"LOW": "low", "OPTIMAL": "optimal", "HIGH": "high", "VERY_HIGH": "very high", "NONE": "none"}
# Garmin's trainingStatusFeedbackPhrase without its variant suffix (RECOVERY_1 -> RECOVERY).
TRAINING_STATUS_EN = {
    "PRODUCTIVE": "productive", "MAINTAINING": "maintaining", "RECOVERY": "recovery",
    "PEAKING": "peaking", "UNPRODUCTIVE": "unproductive", "DETRAINING": "detraining",
    "OVERREACHING": "overreaching", "STRAINED": "strained", "NO_STATUS": "no status", "PAUSED": "paused",
}
TRAINING_BALANCE_EN = {
    "BALANCED": "balanced", "AEROBIC_LOW_SHORTAGE": "low aerobic shortage",
    "AEROBIC_HIGH_SHORTAGE": "high aerobic shortage", "ANAEROBIC_SHORTAGE": "anaerobic shortage",
    "AEROBIC_LOW_FOCUS": "low aerobic focus", "AEROBIC_HIGH_FOCUS": "high aerobic focus",
    "ANAEROBIC_FOCUS": "anaerobic focus", "NO_DATA": "no data",
}
GEAR_EN = {"shoes": "shoes", "bike": "bike"}

NO_DATA = "no data"


# --- small helpers ------------------------------------------------------------

def _str(value: Any) -> Optional[str]:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _first_num(obj: Any, *keys: str) -> Optional[float]:
    if not isinstance(obj, dict):
        return None
    for key in keys:
        value = sc.num(obj.get(key))
        if value is not None:
            return value
    return None


def _ge(a: float, b: float) -> bool:
    """a >= b, tolerant of float noise (1.3 * 10 is 13.000000000000002)."""
    return a >= b - 1e-9


def _iso_day(value: Any) -> Optional[dt.date]:
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return dt.date.fromisoformat(value[:10])
    except ValueError:
        return None


def _phrase_key(phrase: Optional[str]) -> Optional[str]:
    return re.sub(r"_\d+$", "", phrase) if phrase else None


def _label_map(table: Dict[str, str], code: Optional[str]) -> Optional[str]:
    """English for a Garmin code; the raw code when the table does not know it."""
    if code is None:
        return None
    return table.get(_phrase_key(code), code)


def _utc_stamp() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def _skip(reason: str) -> Dict[str, Any]:
    return {"ok": False, "error": f"skipped: {reason}", "fetched_at": _utc_stamp(), "ms": 0}


def _left(started: float) -> float:
    """Seconds left of the whole-report deadline."""
    return DEADLINE_S - (time.monotonic() - started)


def _method(client: Any, name: str) -> Callable[..., Any]:
    """client.<name>, or a stand-in that raises when called.

    A missing client or method then becomes one failed source instead of an
    exception while the calls are being described.
    """
    try:
        fn = getattr(client, name)
    except Exception as exc:  # noqa: BLE001 - reported as a failed source
        reason = sc.clean_error(exc)

        def unavailable(*_args: Any, **_kwargs: Any) -> Any:
            raise AttributeError(f"Garmin client has no {name}: {reason}")

        return unavailable
    if not callable(fn):
        def not_callable(*_args: Any, **_kwargs: Any) -> Any:
            raise AttributeError(f"Garmin client attribute {name} is not callable")

        return not_callable
    return fn


# Identifiers that may hide in Garmin error text (uuids, long numeric ids).
_ID_TEXT_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}\b|\b\d{7,}\b"
)
# Dict keys that are identifiers (device-keyed maps such as latestTrainingStatusData).
_ID_KEY_RE = re.compile(r"^(\d{4,}|[0-9a-fA-F]{32}|[0-9a-fA-F-]{36})$")


def _scrub(text: Any) -> Any:
    return _ID_TEXT_RE.sub("<id>", text) if isinstance(text, str) else text


def _mask_keys(obj: Any) -> Any:
    """Copy of a payload whose identifier-like keys read "<id>" (for key_paths)."""
    if isinstance(obj, dict):
        return {("<id>" if _ID_KEY_RE.match(str(k)) else k): _mask_keys(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_mask_keys(v) for v in obj[:1]]
    return obj


# --- parsing raw Garmin payloads ----------------------------------------------

def parse_hrv(raw: Any) -> Dict[str, Any]:
    """hrvSummary of /hrv-service/hrv/{date} (upstream training.py:798-812)."""
    summary = sc.dig(raw, "hrvSummary")
    summary = summary if isinstance(summary, dict) else {}
    base = summary.get("baseline") if isinstance(summary.get("baseline"), dict) else {}
    return {
        "weekly_avg": sc.num(summary.get("weeklyAvg")),
        "status": _str(summary.get("status")),
        "balanced_low": sc.num(base.get("balancedLow")),
        "balanced_upper": sc.num(base.get("balancedUpper")),
    }


def parse_sleep(raw: Any, day: dt.date) -> Optional[Dict[str, Any]]:
    """One night filed under `day`, or None when Garmin has no synced night for it."""
    dto = sc.dig(raw, "dailySleepDTO")
    if not isinstance(dto, dict) or dto.get("calendarDate") != day.isoformat():
        return None
    seconds = sc.num(dto.get("sleepTimeSeconds"))
    if seconds is None or seconds <= 0:
        return None
    return {"hours": seconds / 3600.0, "score": sc.num(sc.dig(dto, "sleepScores", "overall", "value"))}


def _activity_start(activity: Dict[str, Any], tz: Any) -> Tuple[Optional[dt.date], Optional[str]]:
    """(local start date, local start text) from startTimeLocal, else from startTimeGMT."""
    local = activity.get("startTimeLocal")
    day = _iso_day(local)
    if day is not None:
        return day, str(local)
    gmt = activity.get("startTimeGMT")
    if isinstance(gmt, str):
        try:
            moment = dt.datetime.fromisoformat(gmt.replace(" ", "T")[:19]).replace(tzinfo=dt.timezone.utc)
        except ValueError:
            return None, None
        moment = moment.astimezone(tz)
        return moment.date(), moment.strftime("%Y-%m-%d %H:%M:%S")
    return None, None


def parse_activities(raw: Any, tz: Any) -> List[Dict[str, Any]]:
    """The activity search list -> [{id, date, start_local, type, sport, name, seconds, meters}].

    "id" is kept for the decoupling call only; it never reaches the result.
    """
    if isinstance(raw, dict):
        raw = raw.get("activityList") or raw.get("activities") or []
    items = raw if isinstance(raw, list) else []
    out: List[Dict[str, Any]] = []
    seen = set()
    for activity in items:
        if not isinstance(activity, dict):
            continue
        aid = activity.get("activityId")
        if aid is not None:
            if aid in seen:
                continue
            seen.add(aid)
        day, start_local = _activity_start(activity, tz)
        if day is None:
            continue
        type_key = sc.dig(activity, "activityType", "typeKey")
        out.append({
            "id": aid,
            "date": day,
            "start_local": start_local,
            "type": type_key,
            "sport": sc.sport_group(type_key),
            "name": _str(activity.get("activityName")),
            "seconds": max(0.0, float(sc.num(activity.get("duration")) or 0.0)),
            "meters": max(0.0, float(sc.num(activity.get("distance")) or 0.0)),
        })
    return out


def _select_device_fallback(latest: Dict[str, Any]) -> Tuple[Dict[str, Any], Optional[str], Optional[bool]]:
    """Garmin's primaryTrainingDevice marker, else the first entry (primary tracker unknown)."""
    entries = [(k, v) for k, v in latest.items() if isinstance(v, dict) and v]
    if not entries:
        return {}, None, None
    key, record = next(((k, v) for k, v in entries if v.get("primaryTrainingDevice") is True), entries[0])
    rid = record.get("deviceId")
    return record, (str(rid) if rid is not None else str(key)), None


def _select_device(latest: Any, devices: Any) -> Tuple[Dict[str, Any], Optional[str], Optional[bool]]:
    """The training-status record upstream's get_training_status would read.

    Upstream's _select_training_device (training.py:48-122) prefers the primary
    tracker from get_devices, then Garmin's primaryTrainingDevice marker, then
    the first entry; is_primary is False when the record belongs to another
    device. Without upstream (stdlib tests) the marker / first-entry part runs.
    """
    latest = latest if isinstance(latest, dict) else {}
    try:
        from garmin_mcp.training import _select_training_device
    except ImportError:
        return _select_device_fallback(latest)
    record, reporting_id, _name, is_primary = _select_training_device(latest, devices)
    return (record if isinstance(record, dict) else {}), reporting_id, is_primary


def _balance_record(load_map: Any, reporting_id: Optional[str]) -> Dict[str, Any]:
    """The load-balance record of the reporting device (as upstream: never another device's)."""
    if reporting_id is None or not isinstance(load_map, dict):
        return {}
    for key, data in load_map.items():
        if not isinstance(data, dict) or not data:
            continue
        candidate = str(data["deviceId"]) if data.get("deviceId") is not None else str(key)
        if candidate == reporting_id:
            return data
    return {}


def parse_training_status(raw: Any, devices: Any = None) -> Dict[str, Any]:
    """Load, VO2max, training status and load balance of /metrics-service/.../trainingstatus."""
    raw = raw if isinstance(raw, dict) else {}
    latest = sc.dig(raw, "mostRecentTrainingStatus", "latestTrainingStatusData")
    record, reporting_id, is_primary = _select_device(latest, devices)
    dto = record.get("acuteTrainingLoadDTO")
    # Another device's load is not the athlete's load (upstream drops it the same way).
    dto = dto if isinstance(dto, dict) and is_primary is not False else {}
    balance = _balance_record(
        sc.dig(raw, "mostRecentTrainingLoadBalance", "metricsTrainingLoadBalanceDTOMap"), reporting_id
    )
    return {
        "acute": sc.num(dto.get("dailyTrainingLoadAcute")),
        "chronic": sc.num(dto.get("dailyTrainingLoadChronic")),
        "acwr": sc.acwr_ratio(dto.get("dailyTrainingLoadAcute"), dto.get("dailyTrainingLoadChronic"),
                              dto.get("dailyAcuteChronicWorkloadRatio")),
        "acwr_status": _str(dto.get("acwrStatus")),
        # Despite the field names, Garmin's optimal band for the ACUTE load: about
        # 0.8x and 1.5x the chronic load (the edges of the optimal ACWR range).
        "optimal_min": sc.num(dto.get("minTrainingLoadChronic")),
        "optimal_max": sc.num(dto.get("maxTrainingLoadChronic")),
        "training_status": _str(record.get("trainingStatusFeedbackPhrase")),
        "vo2max_run": _first_num(sc.dig(raw, "mostRecentVO2Max", "generic"), "vo2MaxPreciseValue", "vo2MaxValue"),
        "vo2max_bike": _first_num(sc.dig(raw, "mostRecentVO2Max", "cycling"), "vo2MaxPreciseValue", "vo2MaxValue"),
        "training_balance": _str(balance.get("trainingBalanceFeedbackPhrase")),
        "other_device": is_primary is False,
    }


def parse_schedule(raw: Any, start: dt.date, end: dt.date) -> Optional[List[Dict[str, Any]]]:
    """Scheduled workouts dated start..end (rest days excluded) -> [{date, sport, type, activity_id}].

    None for an unusable answer. type is workoutType and sport its
    sport_group(), as upstream reads it; activity_id (associatedActivityId)
    stays inside the module.
    """
    if not isinstance(raw, dict) or raw.get("errors") or not isinstance(raw.get("data"), dict):
        return None
    items = raw["data"].get("workoutScheduleSummariesScalar") or []
    if not isinstance(items, list):
        return None
    out = []
    for item in items:
        if not isinstance(item, dict) or item.get("isRestDay"):
            continue
        day = _iso_day(item.get("scheduleDate"))
        if day is None or not start <= day <= end:
            continue
        linked = item.get("associatedActivityId")
        out.append({"date": day, "sport": sc.sport_group(item.get("workoutType")),
                    "type": item.get("workoutType"),
                    "activity_id": str(linked) if linked is not None else None})
    return out


def parse_weigh_ins(raw: Any) -> List[Dict[str, Any]]:
    """dailyWeightSummaries[].allWeightMetrics[] -> [{date, order, kg, fat}] (weight is in grams)."""
    out = []
    for day in sc.dig(raw, "dailyWeightSummaries", default=[]) or []:
        for metric in (day.get("allWeightMetrics") or []) if isinstance(day, dict) else []:
            if not isinstance(metric, dict):
                continue
            grams = sc.num(metric.get("weight"))
            date = _iso_day(metric.get("calendarDate")) or _iso_day(day.get("summaryDate"))
            if grams is None or grams <= 0 or date is None:
                continue
            order = _first_num(metric, "timestampGMT", "date", "samplePk") or 0
            out.append({"date": date, "order": order, "kg": grams / 1000.0, "fat": sc.num(metric.get("bodyFat"))})
    return out


def _gear_kind(type_name: Any) -> Optional[str]:
    text = str(type_name or "").lower()
    if "shoe" in text:
        return "shoes"
    if "bike" in text or "bicycle" in text:
        return "bike"
    return None


def parse_gear(raw: Any) -> List[Dict[str, Any]]:
    """Active shoes and bikes, those with a distance limit first, newest first.

    Each item keeps its uuid for the stats call; the uuid never reaches the result.
    """
    items = raw if isinstance(raw, list) else []
    out = []
    for gear in items:
        if not isinstance(gear, dict):
            continue
        if str(gear.get("gearStatusName") or "").lower() != "active":
            continue
        kind = _gear_kind(gear.get("gearTypeName"))
        uuid = gear.get("uuid")
        if kind is None or not uuid:
            continue
        make_model = " ".join(p for p in (_str(gear.get("gearMakeName")), _str(gear.get("gearModelName"))) if p)
        name = _str(gear.get("displayName")) or _str(gear.get("customMakeModel")) or make_model or GEAR_EN[kind]
        max_m = sc.num(gear.get("maximumMeters"))
        out.append({
            "uuid": str(uuid),
            "name": name,
            "type": kind,
            "max_m": max_m if max_m and max_m > 0 else None,
            "begin": str(gear.get("dateBegin") or ""),
        })
    out.sort(key=lambda g: g["begin"], reverse=True)
    out.sort(key=lambda g: g["max_m"] is None)
    return out


# --- sections -----------------------------------------------------------------

def hrv_section(weeks: List[Dict[str, Any]]) -> Dict[str, Any]:
    """weeks = parse_hrv() of end_date, -7, -14, -21 (HRV_WEEKS order)."""
    values = [w.get("weekly_avg") for w in weeks]
    avg_7d = sc.rnd(values[0]) if values else None
    present = [v for v in values if v is not None]
    avg_28d = sc.rnd(sum(present) / len(present)) if len(present) >= 2 else None
    first = weeks[0] if weeks else {}
    status = first.get("status")
    return {
        "avg_7d": avg_7d,
        "avg_28d": avg_28d,
        "delta_ms": avg_7d - avg_28d if avg_7d is not None and avg_28d is not None else None,
        "status": status,
        "status_en": _label_map(HRV_STATUS_EN, status),
        "balanced_low": sc.rnd(first.get("balanced_low")),
        "balanced_upper": sc.rnd(first.get("balanced_upper")),
        "weeks_in_avg_28d": len(present) if avg_28d is not None else 0,
    }


def sleep_section(nights: List[Optional[Dict[str, Any]]], sleep_h: Optional[float] = None) -> Dict[str, Any]:
    sleep_h = THRESHOLDS["sleep_h"] if sleep_h is None else sleep_h
    got = [n for n in nights if n]
    if not got:
        return {"avg_h": None, "avg_hhmm": None, "nights": 0, "nights_under_6h": None, "avg_score": None}
    hours = [n["hours"] for n in got]
    avg = sum(hours) / len(hours)
    scores = [n["score"] for n in got if n.get("score") is not None]
    return {
        "avg_h": round(avg, 2),
        "avg_hhmm": sc.hours_to_hmm(avg),
        "nights": len(got),
        "nights_under_6h": sum(1 for h in hours if h < sleep_h),
        "avg_score": sc.rnd(sum(scores) / len(scores)) if scores else None,
    }


def _sport_block(acts: List[Dict[str, Any]]) -> Tuple[int, float, float]:
    return len(acts), sum(a["meters"] for a in acts), sum(a["seconds"] for a in acts)


def volume_section(acts: Optional[List[Dict[str, Any]]], start: dt.date, end: dt.date) -> Dict[str, Any]:
    """Sessions, km and hours per sport group, this week and the 7 days before."""
    keys = ("sessions", "km", "hours", "hhmm", "prev_sessions", "prev_km", "prev_hours", "prev_hhmm")
    if acts is None:
        out: Dict[str, Any] = {g: dict.fromkeys(keys) for g in SPORTS}
        out.update(total_hours=None, total_hhmm=None, prev_total_hours=None, prev_total_hhmm=None, change_pct=None)
        return out
    prev_start = start - dt.timedelta(days=7)
    this_week = [a for a in acts if start <= a["date"] <= end]
    prev_week = [a for a in acts if prev_start <= a["date"] < start]
    out = {}
    for group in SPORTS:
        n, meters, seconds = _sport_block([a for a in this_week if a["sport"] == group])
        pn, pmeters, pseconds = _sport_block([a for a in prev_week if a["sport"] == group])
        out[group] = {
            "sessions": n, "km": round(meters / 1000, 1), "hours": round(seconds / 3600, 2),
            "hhmm": sc.hours_to_hmm(seconds / 3600),
            "prev_sessions": pn, "prev_km": round(pmeters / 1000, 1), "prev_hours": round(pseconds / 3600, 2),
            "prev_hhmm": sc.hours_to_hmm(pseconds / 3600),
        }
    total = sum(a["seconds"] for a in this_week) / 3600
    prev = sum(a["seconds"] for a in prev_week) / 3600
    out["total_hours"] = round(total, 2)
    out["total_hhmm"] = sc.hours_to_hmm(total)
    out["prev_total_hours"] = round(prev, 2)
    out["prev_total_hhmm"] = sc.hours_to_hmm(prev)
    out["change_pct"] = sc.rnd((total - prev) / prev * 100) if prev > 0 else None
    return out


def load_section(now_ts: Dict[str, Any], week_ago_ts: Dict[str, Any]) -> Dict[str, Any]:
    acute = sc.rnd(now_ts.get("acute"))
    chronic = sc.rnd(now_ts.get("chronic"))
    chronic_7d_ago = sc.rnd(week_ago_ts.get("chronic"))
    status = now_ts.get("acwr_status")
    return {
        "acute": acute,
        "chronic": chronic,
        "acwr": sc.rnd(now_ts.get("acwr"), 2),
        "acwr_status": status,
        "acwr_status_en": _label_map(ACWR_STATUS_EN, status),
        "chronic_7d_ago": chronic_7d_ago,
        "chronic_change": chronic - chronic_7d_ago if chronic is not None and chronic_7d_ago is not None else None,
        "optimal_min": sc.rnd(now_ts.get("optimal_min")),
        "optimal_max": sc.rnd(now_ts.get("optimal_max")),
    }


def fitness_section(now_ts: Dict[str, Any]) -> Dict[str, Any]:
    status = now_ts.get("training_status")
    balance = now_ts.get("training_balance")
    return {
        "vo2max_run": sc.rnd(now_ts.get("vo2max_run"), 1),
        "vo2max_bike": sc.rnd(now_ts.get("vo2max_bike"), 1),
        "training_status": status,
        "training_status_en": _label_map(TRAINING_STATUS_EN, status),
        "training_balance": balance,
        "training_balance_en": _label_map(TRAINING_BALANCE_EN, balance),
    }


def _match_sport(item: Dict[str, Any]) -> Any:
    """The sport key for completion matching: the group for run/bike/swim; for
    "other" its own type key, lower-cased, so a walk never completes a strength workout."""
    type_key = item.get("type")
    if item.get("sport") == "other" and isinstance(type_key, str) and type_key.strip():
        return type_key.strip().lower()
    return item.get("sport")


def plan_section(items: Optional[List[Dict[str, Any]]], acts: Optional[List[Dict[str, Any]]],
                 start: dt.date, end: dt.date) -> Dict[str, Any]:
    """{scheduled, completed, on_planned_day, linked, completion_pct} for the week start..end.

    sc.match_completion_shifts over THIS week's activities only (local start
    date in start..end; the previous week's never count), up to
    PLAN_MAX_SHIFT_DAYS from the planned day. completed = done on any day of
    the week; on_planned_day = linked by Garmin or done on the planned day.
    Without the activity list only Garmin's links are known, a lower bound, so
    both stay null unless every workout is linked.
    """
    if items is None:
        return {"scheduled": None, "completed": None, "on_planned_day": None, "linked": None,
                "completion_pct": None}
    scheduled = len(items)
    linked = sum(1 for i in items if i["activity_id"] is not None)
    if acts is None:
        completed = on_planned_day = scheduled if linked == scheduled else None
    else:
        week = sorted((a for a in acts if start <= a["date"] <= end),
                      key=lambda a: (a["date"], a.get("start_local") or ""))
        shifts = sc.match_completion_shifts(
            [{"date": i["date"].isoformat(), "sport": _match_sport(i), "activity_id": i["activity_id"]}
             for i in items],
            [{"date": a["date"].isoformat(), "sport": _match_sport(a),
              "id": str(a["id"]) if a["id"] is not None else None} for a in week],
            max_shift_days=PLAN_MAX_SHIFT_DAYS,
        )
        completed = sum(1 for s in shifts if s is not None)
        on_planned_day = sum(1 for s in shifts if s == 0)
    return {
        "scheduled": scheduled,
        "completed": completed,
        "on_planned_day": on_planned_day,
        "linked": linked,
        "completion_pct": sc.rnd(completed / scheduled * 100) if scheduled and completed is not None else None,
    }


def body_section(weighs: Optional[List[Dict[str, Any]]], start: dt.date, end: dt.date) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "weight_kg": None, "weight_change_kg": None, "body_fat_pct": None,
        "weighed_on": None, "weighed_on_en": None, "compared_with": None,
    }
    if not weighs:
        return out
    key = lambda w: (w["date"], w["order"])  # noqa: E731
    upto_end = [w for w in weighs if w["date"] <= end]
    if not upto_end:
        return out
    latest = max(upto_end, key=key)
    out["weight_kg"] = round(latest["kg"], 1)
    out["body_fat_pct"] = sc.rnd(latest["fat"], 1)
    out["weighed_on"] = latest["date"].isoformat()
    out["weighed_on_en"] = sc.en_date(latest["date"])
    before = [w for w in weighs if w["date"] < start]
    if latest["date"] >= start and before:
        ref = max(before, key=key)
        out["weight_change_kg"] = round(latest["kg"] - ref["kg"], 1)
        out["compared_with"] = ref["date"].isoformat()
    return out


def gear_section(items: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], bool]:
    """items = parse_gear() entries with "stats" -> (gear list, every stats call answered)."""
    out = []
    complete = True
    for item in items:
        meters = sc.num(sc.dig(item.get("stats"), "totalDistance"))
        if meters is None:
            complete = False
        km = meters / 1000 if meters is not None else None
        max_km = item["max_m"] / 1000 if item.get("max_m") else None
        out.append({
            "name": item["name"],
            "type": item["type"],
            "km": sc.rnd(km),
            "max_km": sc.rnd(max_km),
            "pct_of_max": sc.rnd(km / max_km * 100) if km is not None and max_km else None,
        })
    out.sort(key=lambda g: -(g["km"] or 0))
    out.sort(key=lambda g: -(g["pct_of_max"] if g["pct_of_max"] is not None else -1))
    return out, complete


def pick_long_session(acts: Optional[List[Dict[str, Any]]], start: dt.date, end: dt.date,
                      min_minutes: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """The week's longest bike or run lasting at least min_minutes, else None."""
    min_minutes = THRESHOLDS["long_session_min"] if min_minutes is None else min_minutes
    if not acts:
        return None
    candidates = [
        a for a in acts
        if start <= a["date"] <= end and a["sport"] in ("bike", "run") and _ge(a["seconds"], min_minutes * 60)
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda a: (a["seconds"], a["start_local"] or ""))


def interpret_decoupling(pct: Optional[float], basis: str = "power",
                         thresholds: Optional[Dict[str, Any]] = None) -> Tuple[Optional[str], Optional[str]]:
    """(code, English) for a decoupling percentage with upstream's sign.

    Negative = heart rate rose relative to output over the session (drift).
    |pct| < stable: stable; up to large: moderate drift; above: large drift.
    Positive beyond stable: output rose relative to heart rate (not drift).
    Judged on the rounded value that is shown.
    """
    if pct is None:
        return None, None
    thresholds = THRESHOLDS if thresholds is None else thresholds
    value = round(pct, 1)
    output = "power" if basis == "power" else "speed"
    if abs(value) < thresholds["decoupling_stable_pct"]:
        return "stable", f"Stable: heart rate stayed in step with {output}"
    if value > 0:
        return "output_rose", f"{output.capitalize()} rose relative to heart rate (not drift)"
    if abs(value) <= thresholds["decoupling_large_pct"]:
        return "moderate_drift", f"Moderate drift: heart rate rose relative to {output}"
    return "large_drift", f"Large drift: heart rate rose a lot relative to {output}"


def decoupling_from_details(details: Any) -> Optional[Dict[str, Any]]:
    """Decoupling from /activity-service/activity/{id}/details (the chart samples).

    Same method as upstream's _compute_hr_drift: keep samples with output and a
    heart rate above 0, split them in two halves by count, compare the mean
    output:HR ratios. Power when the samples carry it, else speed (stops
    excluded). Needs DETAILS_MIN_SAMPLES pairs covering at least half the
    samples, so a short sensor stretch is not taken for the whole session.
    """
    if not isinstance(details, dict):
        return None
    index = {
        d.get("key"): d.get("metricsIndex")
        for d in details.get("metricDescriptors") or []
        if isinstance(d, dict) and isinstance(d.get("metricsIndex"), int)
    }
    rows = [
        r.get("metrics") for r in details.get("activityDetailMetrics") or []
        if isinstance(r, dict) and isinstance(r.get("metrics"), list)
    ]
    hr_i = index.get("directHeartRate")
    if hr_i is None or not rows:
        return None
    for basis, key in (("power", "directPower"), ("speed", "directSpeed")):
        out_i = index.get(key)
        if out_i is None:
            continue
        pairs = []
        for row in rows:
            if len(row) <= max(out_i, hr_i):
                continue
            out, hr = sc.num(row[out_i]), sc.num(row[hr_i])
            if out is None or hr is None or hr <= 0 or out < 0 or (basis == "speed" and out <= 0):
                continue
            pairs.append((out, hr))
        if len(pairs) < max(DETAILS_MIN_SAMPLES, len(rows) / 2) or sum(p for p, _ in pairs) <= 0:
            continue
        mid = len(pairs) // 2

        def ratio(part: List[Tuple[float, float]]) -> float:
            return (sum(p for p, _ in part) / len(part)) / (sum(h for _, h in part) / len(part))

        first, second = ratio(pairs[:mid]), ratio(pairs[mid:])
        if first <= 0:
            continue
        return {"pct": (second - first) / first * 100, "basis": basis, "samples": len(pairs)}
    return None


def long_session_section(act: Optional[Dict[str, Any]], drift: Optional[Dict[str, Any]],
                         thresholds: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    if act is None:
        return None
    pct = sc.rnd(drift.get("pct"), 1) if drift else None
    basis = drift.get("basis") if drift and pct is not None else None
    code, text = interpret_decoupling(pct, basis or "power", thresholds)
    return {
        "date": act["date"].isoformat(),
        "date_en": sc.en_date(act["date"]),
        "weekday_en": sc.en_weekday(act["date"]),
        "sport": act["sport"],
        "type": act["type"],
        "name": act["name"],
        "minutes": sc.rnd(act["seconds"] / 60),
        "hhmm": sc.hours_to_hmm(act["seconds"] / 3600),
        "decoupling_pct": pct,
        "interpretation": text,
        "interpretation_code": code,
        "basis": basis,
        "source": drift.get("source") if drift and pct is not None else None,
    }


# --- rules --------------------------------------------------------------------

def _flag(fid: str, level: str, label: str) -> Dict[str, str]:
    return {"id": fid, "level": level, "label_en": label}


def _num_txt(value: Any) -> str:
    """A JSON number as text: ints as is, floats without trailing zeros."""
    v = sc.num(value)
    if v is None:
        return NO_DATA
    if isinstance(v, int):
        return str(v)
    if float(v).is_integer():
        return str(int(v))
    return f"{v:.2f}".rstrip("0").rstrip(".")


def evaluate_flags(status: Dict[str, Any], thresholds: Optional[Dict[str, Any]] = None) -> List[Dict[str, str]]:
    """Weekly flags v1. A rule whose input is missing is skipped, not counted."""
    t = THRESHOLDS if thresholds is None else thresholds
    flags: List[Dict[str, str]] = []
    hrv = status.get("hrv") or {}
    a7, a28, low = hrv.get("avg_7d"), hrv.get("avg_28d"), hrv.get("balanced_low")
    by_trend = a7 is not None and a28 is not None and a7 <= a28 - t["hrv_drop_ms"]
    by_baseline = a7 is not None and low is not None and a7 < low
    if by_trend:
        flags.append(_flag("hrv_down", "yellow",
                           f"HRV down: 7-day average {_num_txt(t['hrv_drop_ms'])} ms or more below the 28-day average"))
    elif by_baseline:
        flags.append(_flag("hrv_down", "yellow", "HRV down: 7-day average below Garmin's balanced range"))

    short = (status.get("sleep") or {}).get("nights_under_6h")
    if short is not None and short >= t["short_nights"]:
        flags.append(_flag("short_nights", "yellow",
                           f"Short nights: {t['short_nights']} or more nights under {_num_txt(t['sleep_h'])} h"))

    volume = status.get("volume") or {}
    total, prev = volume.get("total_hours"), volume.get("prev_total_hours")
    if total is not None and prev is not None and _ge(prev, t["volume_min_prev_h"]) \
            and _ge(total, t["volume_jump_ratio"] * prev):
        jump = round((t["volume_jump_ratio"] - 1) * 100)
        flags.append(_flag("volume_jump", "yellow", f"Volume jump: {jump}% or more above last week"))

    if (status.get("load") or {}).get("acwr_status") in t["load_high_status"]:
        flags.append(_flag("load_high", "red", "Load high: Garmin marks ACWR as high"))

    plan = status.get("plan") or {}
    scheduled, pct = plan.get("scheduled"), plan.get("completion_pct")
    if scheduled is not None and pct is not None and scheduled >= t["completion_min_scheduled"] \
            and pct < t["completion_pct"]:
        flags.append(_flag("low_completion", "yellow",
                           f"Low plan completion: under {t['completion_pct']}% of planned workouts"
                           " done this week (on any day)"))

    if any(g.get("pct_of_max") is not None and g["pct_of_max"] >= t["gear_pct"] for g in status.get("gear") or []):
        flags.append(_flag("gear_near_limit", "yellow",
                           f"Gear near end of life: {t['gear_pct']}% or more of its set maximum distance"))
    return flags


def compute_level(flags: List[Dict[str, str]], *, hrv_missing: bool, sleep_missing: bool,
                  volume_missing: bool) -> str:
    """As the daily level: red flag -> red; HRV, sleep AND volume missing -> incomplete;
    >= 3 yellow -> red; 1-2 yellow -> yellow; else green."""
    if any(f.get("level") == "red" for f in flags):
        return "red"
    if hrv_missing and sleep_missing and volume_missing:
        return "incomplete"
    yellow = sum(1 for f in flags if f.get("level") == "yellow")
    if yellow >= 3:
        return "red"
    return "yellow" if yellow else "green"


# --- fetching -----------------------------------------------------------------

def _round1_calls(client: Any, start: dt.date, end: dt.date) -> Dict[str, sc.Call]:
    def iso(day: dt.date) -> str:
        return day.isoformat()

    calls: Dict[str, sc.Call] = {
        # Cheap and needed for gear: runs alone first (token refresh, systemic check).
        "device_last_used": sc.call(_method(client, "get_device_last_used")),
        "devices": sc.call(_method(client, "get_devices")),
    }
    for back in HRV_WEEKS:
        day = end - dt.timedelta(days=back)
        calls[f"hrv@{iso(day)}"] = sc.call(_method(client, "get_hrv_data"), iso(day))
    for offset in range(7):
        day = start + dt.timedelta(days=offset)
        calls[f"sleep@{iso(day)}"] = sc.call(_method(client, "get_sleep_data"), iso(day))
    try:
        path = getattr(client, "garmin_connect_activities", None)
    except Exception:  # noqa: BLE001 - fall back to garminconnect's path
        path = None
    params = {
        "startDate": iso(end - dt.timedelta(days=13)), "endDate": iso(end),
        "start": "0", "limit": str(ACTIVITY_LIMIT),
    }
    calls["activities"] = sc.call(_method(client, "connectapi"),
                                  path if isinstance(path, str) and path else ACTIVITIES_PATH, params=params)
    for back in (0, 7):
        day = end - dt.timedelta(days=back)
        calls[f"training_status@{iso(day)}"] = sc.call(_method(client, "get_training_status"), iso(day))
    calls["schedule"] = sc.call(_method(client, "query_garmin_graphql"),
                                {"query": SCHEDULE_QUERY.format(start=iso(start), end=iso(end))})
    calls["weigh_ins"] = sc.call(_method(client, "get_weigh_ins"),
                                 iso(end - dt.timedelta(days=WEIGHT_LOOKBACK_DAYS - 1)), iso(end))
    return calls


def _load_fit_helpers() -> Tuple[Any, Callable[..., Any], bool]:
    """(ORIGINAL download format, upstream _parse_fit, fitparse available)."""
    from garminconnect import Garmin
    from garmin_mcp.activity_analysis import FITPARSE_AVAILABLE, _parse_fit

    return Garmin.ActivityDownloadFormat.ORIGINAL, _parse_fit, bool(FITPARSE_AVAILABLE)


def _fit_drift(client: Any, activity_id: Any) -> Dict[str, Any]:
    """Download the original FIT and read upstream's power:HR drift (session.hr_drift)."""
    fmt, parse_fit, available = _load_fit_helpers()
    if not available:
        raise RuntimeError("fitparse is not installed")
    raw = _method(client, "download_activity")(activity_id, dl_fmt=fmt)
    if not raw:
        raise ValueError("Garmin returned an empty FIT download")
    parsed = parse_fit(bytes(raw), include_records=False)
    drift = sc.dig(parsed, "session", "hr_drift")
    # None when the file has under 60 min of power + heart rate (e.g. no power).
    return {"pct": sc.num(sc.dig(drift, "hr_drift_pct")), "basis": "power"}


async def _decoupling_chain(client: Any, act: Optional[Dict[str, Any]], started: float):
    """-> (source results, debug slims, {"pct", "basis", "source"} or None).

    FIT first (upstream's analysis); the details chart only when the FIT gave
    no drift and the failure was not systemic (rate limit, login, network).
    """
    results: Dict[str, Dict[str, Any]] = {}
    slims: Dict[str, Any] = {}
    if act is None or act.get("id") is None:
        return results, slims, None
    res = await sc.fetch_all({"decoupling_fit": sc.call(_fit_drift, client, act["id"])},
                             timeout_s=FIT_TIMEOUT_S, deadline_s=_left(started))
    results.update(res)
    fit = sc.data_of(res["decoupling_fit"])
    slims["decoupling_fit"] = fit
    if isinstance(fit, dict) and fit.get("pct") is not None:
        return results, slims, dict(fit, source="fit")
    error = res["decoupling_fit"].get("error")
    # fetch_all marks its own timeouts systemic, but a FIT timeout ("timeout
    # after Ns") is more likely a slow parse of a long file than a Garmin
    # problem, so the (deadline-bounded) details call still gets its try.
    if res["decoupling_fit"].get("systemic") and not str(error).startswith("timeout after"):
        results["decoupling_details"] = _skip(f"decoupling_fit failed: {error}"[:200])
        return results, slims, None
    res = await sc.fetch_all(
        {"decoupling_details": sc.call(_method(client, "get_activity_details"), act["id"],
                                       maxchart=DETAILS_MAX_CHART, maxpoly=0)},
        timeout_s=CALL_TIMEOUT_S, deadline_s=_left(started))
    results.update(res)
    drift = decoupling_from_details(sc.data_of(res["decoupling_details"]))
    slims["decoupling_details"] = drift
    return results, slims, (dict(drift, source="details") if drift else None)


async def _gear_chain(client: Any, profile_number: Any, started: float):
    """-> (source results, debug slims, parse_gear() items with "stats" or None if the list failed)."""
    results: Dict[str, Dict[str, Any]] = {}
    slims: Dict[str, Any] = {}
    if profile_number is None:
        results["gear"] = _skip("device_last_used gave no profile number")
        return results, slims, None
    res = await sc.fetch_all({"gear": sc.call(_method(client, "get_gear"), profile_number)},
                             timeout_s=CALL_TIMEOUT_S, deadline_s=_left(started))
    results.update(res)
    raw = sc.data_of(res["gear"])
    if not isinstance(raw, list):
        return results, slims, None
    items = parse_gear(raw)[:GEAR_STATS_MAX]
    slims["gear"] = {"count": len(raw), "active_shoes_bikes": len(items)}
    if not items:
        return results, slims, []
    calls = {f"gear_stats_{i}": sc.call(_method(client, "get_gear_stats"), item["uuid"])
             for i, item in enumerate(items, 1)}
    res = await sc.fetch_all(calls, timeout_s=CALL_TIMEOUT_S, concurrency=4, deadline_s=_left(started))
    results.update(res)
    for i, item in enumerate(items, 1):
        item["stats"] = sc.data_of(res.get(f"gear_stats_{i}"))
        meters = sc.num(sc.dig(item["stats"], "totalDistance"))
        slims[f"gear_stats_{i}"] = {"km": sc.rnd(meters / 1000) if meters is not None else None}
    return results, slims, items


# --- the report ---------------------------------------------------------------

def _local_now(tz: Any, now: Optional[dt.datetime]) -> dt.datetime:
    if now is None:
        return sc.now_local(tz)
    return now.replace(tzinfo=tz) if now.tzinfo is None else now.astimezone(tz)


def _end_date(value: Any, current: dt.datetime) -> dt.date:
    if value is None or not str(value).strip():
        return current.date() - dt.timedelta(days=1)
    try:
        return dt.date.fromisoformat(str(value).strip())
    except ValueError:
        raise ValueError(f"end_date must be YYYY-MM-DD, got {value!r}") from None


def _missing(status: Dict[str, Any], *, volume_ok: bool, plan_ok: bool, gear_ok: bool,
             gear_complete: bool) -> List[str]:
    out: List[str] = []
    hrv = status["hrv"]
    out += [f"hrv.{k}" for k in ("avg_7d", "avg_28d", "status", "balanced_low", "balanced_upper") if hrv[k] is None]
    sleep = status["sleep"]
    if not sleep["nights"]:
        out.append("sleep")
    elif sleep["avg_score"] is None:
        out.append("sleep.avg_score")
    if not volume_ok:
        out.append("volume")
    load = status["load"]
    out += [f"load.{k}" for k in ("acute", "chronic", "acwr", "acwr_status", "chronic_7d_ago",
                                  "optimal_min", "optimal_max") if load[k] is None]
    if not plan_ok:
        out.append("plan")
    elif status["plan"]["completed"] is None:
        out += ["plan.completed", "plan.on_planned_day", "plan.completion_pct"]
    long_session = status["long_session"]
    if long_session is None:
        out.append("long_session")
    elif long_session["decoupling_pct"] is None:
        out.append("long_session.decoupling_pct")
    fitness = status["fitness"]
    out += [f"fitness.{k}" for k in ("vo2max_run", "vo2max_bike", "training_status", "training_balance")
            if fitness[k] is None]
    body = status["body"]
    if body["weight_kg"] is None:
        out.append("body.weight_kg")
    elif body["weight_change_kg"] is None:
        out.append("body.weight_change_kg")
    if not gear_ok:
        out.append("gear")
    elif not gear_complete:
        out.append("gear.km")
    return out


async def build_weekly_status(
    client: Any,
    end_date: str = "",
    *,
    tz: Optional[str] = None,
    include_sources: bool = False,
    text_format: str = "",
    now: Optional[dt.datetime] = None,
) -> Dict[str, Any]:
    """The weekly status dict.

    Raises ValueError for a malformed end_date, an unknown text_format, an
    unknown time zone or (tz not given) an unset REPORT_TZ; every Garmin
    failure ends up in "missing" / "sources" instead.
    """
    if text_format not in TEXT_FORMATS:
        raise ValueError("text_format must be '' or 'markdown'")
    zone = sc.zone(tz)
    current = _local_now(zone, now)
    end = _end_date(end_date, current)
    start = end - dt.timedelta(days=6)
    started = time.monotonic()

    results = await sc.fetch_all(_round1_calls(client, start, end), first="device_last_used",
                                 timeout_s=CALL_TIMEOUT_S, deadline_s=_left(started))
    data = {name: sc.data_of(res) for name, res in results.items()}
    slim: Dict[str, Any] = {}

    # First round -> parsed inputs.
    hrv_weeks = []
    for back in HRV_WEEKS:
        name = f"hrv@{(end - dt.timedelta(days=back)).isoformat()}"
        hrv_weeks.append(parse_hrv(data.get(name)))
        slim[name] = hrv_weeks[-1]
    nights = []
    for offset in range(7):
        day = start + dt.timedelta(days=offset)
        nights.append(parse_sleep(data.get(f"sleep@{day.isoformat()}"), day))
        slim[f"sleep@{day.isoformat()}"] = nights[-1] or {"synced": False}
    volume_ok = bool(results["activities"].get("ok"))
    acts = parse_activities(data["activities"], zone) if volume_ok else None
    slim["activities"] = {"count": len(acts or []), "types": sorted({str(a["type"]) for a in acts or []})}
    devices = data.get("devices")
    slim["devices"] = {"count": len(devices) if isinstance(devices, list) else None}
    ts_now = parse_training_status(data.get(f"training_status@{end.isoformat()}"), devices)
    week_ago = (end - dt.timedelta(days=7)).isoformat()
    ts_week_ago = parse_training_status(data.get(f"training_status@{week_ago}"), devices)
    slim[f"training_status@{end.isoformat()}"] = ts_now
    slim[f"training_status@{week_ago}"] = ts_week_ago
    schedule = parse_schedule(data.get("schedule"), start, end) if results["schedule"].get("ok") else None
    slim["schedule"] = {"in_week": len(schedule) if schedule is not None else None,
                        "linked": sum(1 for s in schedule or [] if s["activity_id"] is not None)}
    weighs = parse_weigh_ins(data.get("weigh_ins")) if results["weigh_ins"].get("ok") else None
    slim["weigh_ins"] = {"count": len(weighs) if weighs is not None else None}
    profile_number = sc.dig(data.get("device_last_used"), "userProfileNumber")
    slim["device_last_used"] = {"profile_number": profile_number is not None}

    # Second round: gear and decoupling chains side by side.
    long_act = pick_long_session(acts, start, end)
    first = results.get("device_last_used") or {}
    if not first.get("ok") and first.get("systemic"):
        # fetch_all already skipped the rest of round 1; do not start more calls.
        reason = f"device_last_used failed: {first.get('error')}"[:200]
        results["gear"] = _skip(reason)
        if long_act is not None:
            results["decoupling_fit"] = _skip(reason)
        gear_items, drift = None, None
    else:
        (gear_results, gear_slims, gear_items), (drift_results, drift_slims, drift) = await asyncio.gather(
            _gear_chain(client, profile_number, started), _decoupling_chain(client, long_act, started)
        )
        results.update(gear_results)
        results.update(drift_results)
        slim.update(gear_slims)
        slim.update(drift_slims)

    gear, gear_complete = gear_section(gear_items) if gear_items is not None else ([], True)
    status: Dict[str, Any] = {
        "kind": "weekly_status",
        "rules_version": RULES_VERSION,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "generated_at": current.replace(microsecond=0).isoformat(),
        "tz": str(zone.key),
        "level": None,
        "level_en": None,
        "hrv": hrv_section(hrv_weeks),
        "sleep": sleep_section(nights),
        "volume": volume_section(acts, start, end),
        "load": load_section(ts_now, ts_week_ago),
        "plan": plan_section(schedule, acts, start, end),
        "long_session": long_session_section(long_act, drift),
        "fitness": fitness_section(ts_now),
        "body": body_section(weighs, start, end),
        "gear": gear,
    }
    flags = evaluate_flags(status)
    level = compute_level(flags, hrv_missing=status["hrv"]["avg_7d"] is None,
                          sleep_missing=not status["sleep"]["nights"], volume_missing=not volume_ok)
    status["level"] = level
    status["level_en"] = LEVEL_EN[level]
    status["flags"] = flags
    status["missing"] = _missing(status, volume_ok=volume_ok, plan_ok=schedule is not None,
                                 gear_ok=gear_items is not None, gear_complete=gear_complete)
    status["thresholds"] = {k: (list(v) if isinstance(v, list) else v) for k, v in THRESHOLDS.items()}
    sources = sc.source_meta(results)
    for meta in sources.values():
        if "error" in meta:
            meta["error"] = _scrub(meta["error"])
    status["sources"] = sources
    if include_sources:
        status["debug"] = {
            name: {"slim": slim.get(name), "keys": sc.key_paths(_mask_keys(sc.data_of(res)))}
            for name, res in results.items() if res.get("ok")
        }
    if text_format:
        try:
            status["text"] = render_text(status, text_format)
        except Exception:  # noqa: BLE001 - a rendering bug must not cost the report
            status["text"] = None
            status["missing"].append("text")
    return status


# --- text ---------------------------------------------------------------------

Segment = Tuple[str, bool]  # (text, bold)


def _signed(value: Any) -> str:
    v = sc.num(value)
    if v is None:
        return NO_DATA
    if v > 0:
        return f"+{_num_txt(v)}"
    if v < 0:
        return f"-{_num_txt(-v)}"
    return "0"


def _hm(value: Any) -> str:
    return value if isinstance(value, str) and value else NO_DATA


def _short(text: Any, limit: int = 30) -> str:
    text = str(text or "")
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _count(count: Any, noun: str) -> str:
    """Like "1 session" or "3 sessions"."""
    count = count or 0
    return f"{_num_txt(count)} {noun}" + ("" if count == 1 else "s")


def _lines(status: Dict[str, Any], compact: bool) -> List[Tuple[str, List[Segment]]]:
    """(kind, segments) lines: kind "head" (bold line), "item" (bullet) or "plain"."""
    t = {**THRESHOLDS, **(status.get("thresholds") or {})}
    n = _num_txt
    lines: List[Tuple[str, List[Segment]]] = []

    def head(text: str) -> None:
        lines.append(("head", [(text, True)]))

    def item(*segs: Any) -> None:
        lines.append(("item", [s if isinstance(s, tuple) else (str(s), False) for s in segs]))

    start, end = _iso_day(status.get("start")), _iso_day(status.get("end"))
    span = f"{sc.en_date(start)}–{sc.en_date(end)}" if start and end else NO_DATA
    head(f"Weekly summary {span}")
    flags = status.get("flags") or []
    level_line: List[Segment] = [("Level: ", False), (status.get("level_en") or NO_DATA, True)]
    if flags:
        level_line.append((f" · flags: {len(flags)}", False))
    lines.append(("plain", level_line))

    head("Recovery")
    hrv = status.get("hrv") or {}
    if hrv.get("avg_7d") is None and hrv.get("avg_28d") is None:
        item(f"HRV: {NO_DATA}")
    else:
        text = f"HRV: 7-day average {n(hrv.get('avg_7d'))} (28-day average {n(hrv.get('avg_28d'))}"
        text += f", change {_signed(hrv.get('delta_ms'))})" if hrv.get("delta_ms") is not None else ")"
        if hrv.get("status_en"):
            text += f" · Garmin: {hrv['status_en']}"
        if not compact and hrv.get("balanced_low") is not None and hrv.get("balanced_upper") is not None:
            text += f" · balanced range {n(hrv['balanced_low'])}–{n(hrv['balanced_upper'])}"
        item(text)
    sleep = status.get("sleep") or {}
    if not sleep.get("nights"):
        item(f"Sleep: {NO_DATA}")
    else:
        text = (f"Sleep: average {_hm(sleep.get('avg_hhmm'))} over {_count(sleep['nights'], 'night')}"
                f" · under {n(t['sleep_h'])} h: {n(sleep.get('nights_under_6h'))}")
        if sleep.get("avg_score") is not None:
            text += f" · average score {n(sleep['avg_score'])}"
        item(text)

    head("Volume (vs last week)")
    volume = status.get("volume") or {}
    if volume.get("total_hours") is None:
        item(f"Volume: {NO_DATA}")
    else:
        shown = False
        for group in SPORTS:
            g = volume.get(group) or {}
            if not g.get("sessions") and not g.get("prev_sessions"):
                continue
            shown = True
            label = SPORT_EN[group].capitalize()
            if group == "other":
                item(f"{label}: {_hm(g.get('hhmm'))} · {_count(g.get('sessions'), 'session')}"
                     f" (last week: {_hm(g.get('prev_hhmm'))})")
            else:
                item(f"{label}: {n(g.get('km'))} km · {_hm(g.get('hhmm'))} · {_count(g.get('sessions'), 'session')}"
                     f" (last week: {n(g.get('prev_km'))} km · {_hm(g.get('prev_hhmm'))})")
        if not shown:
            item("No activities in the last two weeks")
        text = f"Total: {_hm(volume.get('total_hhmm'))} h (last week: {_hm(volume.get('prev_total_hhmm'))}"
        text += f", {_signed(volume['change_pct'])}%)" if volume.get("change_pct") is not None else ")"
        item(text)

    head("Load")
    load = status.get("load") or {}
    if all(load.get(k) is None for k in ("acute", "chronic", "acwr")):
        item(f"Load: {NO_DATA}")
    else:
        text = f"Acute {n(load.get('acute'))} · chronic {n(load.get('chronic'))}"
        if load.get("chronic_7d_ago") is not None:
            text += f" (a week ago {n(load['chronic_7d_ago'])}, {_signed(load.get('chronic_change'))})"
        text += f" · ACWR {n(load.get('acwr'))}"
        if load.get("acwr_status_en"):
            text += f" ({load['acwr_status_en']})"
        item(text)
        if not compact and load.get("optimal_min") is not None and load.get("optimal_max") is not None:
            item(f"Optimal range for acute load: {n(load['optimal_min'])}–{n(load['optimal_max'])}")

    head("Plan and long session")
    plan = status.get("plan") or {}
    if plan.get("scheduled") is None:
        item(f"Plan: {NO_DATA}")
    elif plan["scheduled"] == 0:
        item("Plan: no workouts scheduled")
    elif plan.get("completed") is None:
        item(f"Plan: {n(plan['scheduled'])} scheduled · done: {NO_DATA}")
    else:
        text = (f"Plan: {n(plan['completed'])} of {n(plan['scheduled'])} done"
                f" ({n(plan.get('completion_pct'))}%)")
        on_day = plan.get("on_planned_day")
        if on_day is not None and on_day != plan["completed"]:
            text += f", {n(on_day)} of them on the planned day"
        item(text)
    long_session = status.get("long_session")
    if not long_session:
        if volume.get("total_hours") is None:
            item(f"Long session: {NO_DATA}")
        else:
            item(f"Long session: no ride or run of {n(t['long_session_min'])} min or more")
    else:
        when = " ".join(p for p in (long_session.get("weekday_en"), long_session.get("date_en")) if p)
        text = f"Long session: {SPORT_EN.get(long_session.get('sport'), long_session.get('sport') or '')}"
        if when:
            text += f" on {when}"
        text += f" · {_hm(long_session.get('hhmm'))} h"
        if not compact and long_session.get("name"):
            text += f" ({sc.isolate(_short(long_session['name']))})"
        if long_session.get("decoupling_pct") is None:
            text += f" · HR drift: {NO_DATA}"
        else:
            text += f" · HR drift {_signed(long_session['decoupling_pct'])}%"
            if long_session.get("interpretation"):
                text += f" · {long_session['interpretation']}"
        item(text)

    head("Fitness, weight and gear")
    fitness = status.get("fitness") or {}
    text = (f"VO2max run {n(fitness.get('vo2max_run'))} · bike {n(fitness.get('vo2max_bike'))}"
            f" · training status: {fitness.get('training_status_en') or NO_DATA}")
    if not compact:
        text += f" · load balance: {fitness.get('training_balance_en') or NO_DATA}"
    item(text)
    body = status.get("body") or {}
    if body.get("weight_kg") is None:
        item(f"Weight: {NO_DATA}")
    else:
        text = f"Weight: {n(body['weight_kg'])} kg (weighed {body.get('weighed_on_en') or NO_DATA}"
        if body.get("weight_change_kg") is not None:
            text += f", change {_signed(body['weight_change_kg'])} kg"
        item(text + ")")
    worn = [g for g in status.get("gear") or []
            if g.get("pct_of_max") is not None and g["pct_of_max"] >= t["gear_pct"]]
    for g in worn[:3]:
        item(f"Gear: {sc.isolate(_short(g.get('name')))} {n(g.get('km'))} of {n(g.get('max_km'))} km ({n(g['pct_of_max'])}%)")
    if len(worn) > 3:
        item(f"Gear: {len(worn) - 3} more")

    labels = "; ".join(f.get("label_en", "") for f in flags) if flags else "none"
    lines.append(("plain", [("Flags:", True), (f" {labels}", False)]))
    return lines


def _esc_md(text: str) -> str:
    return re.sub(r"([\\`*_\[\]<>#|])", r"\\\1", text)


def _format(lines: List[Tuple[str, List[Segment]]]) -> str:
    """Markdown (CommonMark): a blank line before every non-bullet line, or a
    heading would merge into the paragraph or bullet above it; the bullets of
    one group stay on consecutive lines.
    """
    out: List[str] = []
    for kind, segs in lines:
        body = "".join(f"**{_esc_md(text)}**" if is_bold and text else _esc_md(text) for text, is_bold in segs)
        if kind != "item" and out:
            out.append("")
        out.append(("- " if kind == "item" else "") + body)
    return "\n".join(out)


def _word_count(text: str) -> int:
    return len(re.sub(r"\*\*", " ", text).split())


def render_text(status: Dict[str, Any], fmt: str = "markdown") -> str:
    """Deterministic English weekly text from the status dict (<= MAX_WORDS words).

    fmt="markdown" (the only format): **bold**, "- " bullets, Markdown
    special characters escaped. Nulls print "no data". Gear lines only for
    gear at or above the limit.
    """
    if fmt != "markdown":
        raise ValueError("fmt must be 'markdown'")
    text = _format(_lines(status, compact=False))
    if _word_count(text) > MAX_WORDS:
        text = _format(_lines(status, compact=True))
    return text


# --- MCP tool -----------------------------------------------------------------

def register_tools(app: Any, get_client: Callable[[], Any]) -> Any:
    """Register get_weekly_status on upstream's (filtered) app.

    get_client returns upstream's configured Garmin client at call time.
    """

    @app.tool()
    async def get_weekly_status(end_date: str = "", include_sources: bool = False, text_format: str = "") -> str:
        """Weekly training review: the 7 days ending end_date vs the 7 days before.

        Covers HRV (7-day vs 28-day average, Garmin status and baseline), sleep
        (average, nights under 6 h, score), volume per sport (sessions, km,
        hours), Garmin training load (acute, chronic and its change over the
        week, ACWR), scheduled workouts done this week (completed = linked by
        Garmin or an activity of the same sport on any day of the week;
        on_planned_day = done on the planned day; linked = Garmin's own count), the
        week's longest ride or run of 60+ min with its heart-rate decoupling,
        VO2max and training status, weight, and shoes/bikes near their
        distance limit. Returns JSON with flags and a level
        (green/yellow/red/incomplete).

        Every number, flag and the level are computed on the server by fixed
        rules (thresholds are in the result). Report them as given; do not
        recompute or change them and do not add thresholds of your own. null
        means no value (listed in "missing"). decoupling_pct: negative = heart
        rate rose relative to power/speed in the second half (drift); within
        +-5% is stable; positive = output rose relative to heart rate.

        Args:
            end_date: Last day of the week, YYYY-MM-DD. Empty = yesterday.
            include_sources: Add raw-source debug details (for automation and
                debugging).
            text_format: "markdown" adds a ready English report in "text"
                (for automation); empty = JSON only.
        """
        if text_format not in TEXT_FORMATS:
            return "Error: text_format must be '' or 'markdown'."
        try:
            status = await build_weekly_status(
                get_client(), end_date, include_sources=include_sources, text_format=text_format
            )
        except ValueError as exc:
            return f"Error: {exc}"
        except Exception as exc:  # noqa: BLE001 - a bug here must not crash the server
            return f"Error: weekly status failed: {_scrub(sc.clean_error(exc))}"
        return sc.compact_json(status)

    return app


# Garmin client methods this module calls (checked by selfcheck at build time).
_CLIENT_METHODS = (
    "connectapi", "get_device_last_used", "get_devices", "get_hrv_data", "get_sleep_data",
    "get_training_status", "query_garmin_graphql", "get_weigh_ins", "get_gear", "get_gear_stats",
    "download_activity", "get_activity_details",
)


def selfcheck() -> None:
    """Import every upstream helper this module uses; raise if one is missing.

    launch.patch() calls it at Docker build time, so an upstream bump that
    drops or renames one fails the build instead of the weekly report.
    """
    from garminconnect import Garmin
    from garmin_mcp.activity_analysis import (  # noqa: F401 - _parse_fit runs both
        FITPARSE_AVAILABLE, _compute_hr_drift, _extract_fit_bytes, _parse_fit,
    )
    from garmin_mcp.training import _select_training_device  # noqa: F401

    missing = [name for name in _CLIENT_METHODS if not callable(getattr(Garmin, name, None))]
    if missing:
        raise RuntimeError(f"garminconnect.Garmin lacks {', '.join(missing)}")
    if not hasattr(Garmin, "ActivityDownloadFormat") or not hasattr(Garmin.ActivityDownloadFormat, "ORIGINAL"):
        raise RuntimeError("garminconnect.Garmin.ActivityDownloadFormat.ORIGINAL is missing")
    if not FITPARSE_AVAILABLE:
        raise RuntimeError("fitparse is not installed (upstream requires it for FIT analysis)")
