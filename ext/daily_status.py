"""get_daily_status: one answer to "how am I today?", decided by code.

Reads last night's sleep, HRV, resting heart rate, Training Readiness,
training load, yesterday's and today's activities and the Garmin calendar for
today and tomorrow, then decides flags, a level (green / yellow / red /
incomplete) and a recommendation with fixed rules (RULES_VERSION,
THRESHOLDS). Code decides every number; a chat model only words them. A
value Garmin has not produced yet is null and listed in "missing", and a
rule that needs it is skipped, never guessed.

Training Readiness is the morning picture. Garmin writes several rows a day
(inputContext): a realtime row just after midnight, before the night is
scored; the wake-up row; one after each workout; more realtime rows later.
readiness and readiness_level come from the newest wake-up row of the date;
before it exists, from the newest row that is not a pre-sleep realtime row
(a post-exercise row still counts); with neither they are null, and the
probe says readiness is not ready yet. The recovery clock (recovery_hours)
and the readiness acute load come from the newest row of the date, because
they move with every workout.

Everything except the adapter in build_daily_status (the Garmin calls) and
selfcheck() is a pure function over Garmin payload dicts, so the unit tests
run on fixtures without garmin_mcp installed. Upstream helpers are imported
lazily: workouts._curate_workout_details (the classifier reads upstream's
curated workout shape), health_wellness._hours_from_recovery_minutes and
training._lactate_threshold_speed_mps.

Names typed in Garmin (activity and workout names) are cleaned where they are
read (text_hygiene.name_or_none: invisible and bidi characters dropped, links
replaced, 100 characters at most), before the hard-name keyword match and the
text; the include_sources debug subtree is cleaned as a whole.
"""

from __future__ import annotations

import copy
import datetime as dt
import importlib
import re
import time
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple
from zoneinfo import ZoneInfo

import mcp2_compat
import status_common as sc
import text_hygiene as th

RULES_VERSION = 1

THRESHOLDS: Dict[str, Any] = {
    "hrv_drop_ms": 10,
    "hrv_trend_days": 3,
    "rhr_rise_bpm": 5,
    "sleep_h": 6,
    "readiness": 40,
    "acwr_yellow": 1.3,
    "acwr_red": 1.5,
    "recovery_h": 6,
    "long_min": {"run": 75, "bike": 150, "swim": 75, "other": 90},
    "long_km": {"run": 15, "bike": 70, "swim": 3.5},
    # A range target is hard from these fractions of the athlete's own
    # thresholds. Power: Coggan's zone 4 starts at 91% of FTP. Heart rate: 95%
    # of the lactate-threshold heart rate. Pace: 90% of Garmin's
    # lactate-threshold speed, on purpose below the textbook ~95% (Friel's
    # zone 4 starts near 105% of threshold pace time): checked against real
    # coach-written plans, over/under and interval main sets sit at 93-94% of
    # Garmin's estimate (the coach's threshold is slower than Garmin's), while
    # easy running stays below ~88%.
    "hard_pace_of_lt_speed": 0.90,
    "hard_power_of_ftp": 0.91,
    "hard_hr_of_lthr": 0.95,
    # The hard steps of a workout must add up to this much work, in seconds
    # or in metres: a few strides do not make a run hard.
    "hard_min_work_s": 300,
    "hard_min_work_m": 1000,
}

# Budgets in seconds (module globals so the tests can shrink them).
CALL_TIMEOUT_S = sc.DEFAULT_CALL_TIMEOUT_S
DEADLINE_S = sc.DEFAULT_DEADLINE_S
# Scheduled workouts of today and tomorrow whose steps are fetched, at most.
MAX_WORKOUT_DETAILS = 4
# The activity search of one day reads pages of ACTIVITY_PAGE, at most ACTIVITY_PAGES of them.
ACTIVITY_PAGE = 20
ACTIVITY_PAGES = 5
# Daily text budget (principle 3: readable on a phone in under a minute).
MAX_WORDS = 150
TEXT_FORMATS = ("markdown",)
NO_VALUE = "no data"

LEVEL_EN = {"green": "Green", "yellow": "Yellow", "red": "Red", "incomplete": "Incomplete data"}

RECOMMENDATION_EN = {
    "as_planned": "Train as planned.",
    "as_planned_may_stop": "Train as planned; stop if it doesn't feel right.",
    "zone2_or_shorten": "Turn the session into Zone 2 or shorten it.",
    "short_zone2_or_rest": "Short Zone 2 or rest.",
    "shorten": "Shorten the session.",
    "no_planned_workout": "No workout planned today.",
    "no_recovery_data": "No sleep or readiness data; decide by feel.",
    "done_today": "Today's workout is already done.",
}
# done_today when more than one workout was planned (and all were done).
DONE_TODAY_PLURAL_EN = "Today's workouts are already done."

FLAG_LEVELS = {
    "low_hrv": "yellow",
    "hrv_low_trend": "red",
    "high_rhr": "yellow",
    "short_sleep": "yellow",
    "sleep_debt": "red",
    "low_readiness": "yellow",
    "load_spike": "red",
    "load_rising": "yellow",
    "recovery_incomplete": "yellow",
}

# Fields the daily text prints. When null they are listed in "missing" even
# if no rule needs them.
TEXT_FIELDS = (
    "recovery.sleep_hours", "recovery.sleep_score", "recovery.hrv", "recovery.hrv_7d",
    "recovery.rhr", "recovery.rhr_7d", "recovery.readiness", "recovery.recovery_hours",
    "load.acwr", "load.acwr_status", "load.yesterday", "today", "tomorrow",
)

ACWR_SPIKE_STATUSES = ("HIGH", "VERY_HIGH")

# Workout classification. A step other than warmup, cooldown, recovery or
# rest is hard when
#   a) a primary or secondary heart-rate / power / pace zone target is >= 4;
#   b) a pace/speed range's midpoint speed is >= hard_pace_of_lt_speed x the
#      running lactate-threshold speed (runs);
#   c) a power range's midpoint is >= hard_power_of_ftp x cycling FTP (rides;
#      a range written in % FTP is compared as it is, any sport);
#   d) a heart-rate bpm range's midpoint is >= hard_hr_of_lthr x the
#      lactate-threshold heart rate;
#   e) when the threshold a range needs is unknown (always for swim pace):
#      only an interval step inside a repeat of >= 2 iterations counts.
# The steps that qualify must also add up to hard_min_work_s seconds or
# hard_min_work_m metres. A range is custom low/high values without a zone
# number. Workouts synced from TrainingPeaks put pace ranges on easy
# single-step runs, which is why a bare range does not make a workout hard.
_HARD_ZONE = 4
# Intensity target types -> what they measure. The lap / 3 s / 10 s / 30 s
# keys only change the averaging window (TrainingPeaks rides arrive as
# power.3s with watt ranges). Cadence, grade, resistance and swim-stroke
# targets say nothing about intensity and are left out.
_TARGET_KINDS = {
    "heart.rate.zone": "HR", "heart.rate.lap": "HR",
    "pace.zone": "pace", "speed.zone": "pace", "speed.lap": "pace",
    "power.zone": "power", "power.between": "power", "power.lap": "power",
    "power.3s": "power", "power.10s": "power", "power.30s": "power",
}
_EASY_STEP_TYPES = ("warmup", "cooldown", "recovery", "recover", "rest")
# Athlete thresholds the range rules use (internal key, key in "thresholds_athlete").
ATHLETE_FIELDS = (("lt_speed_mps", "lt_pace_min_km"), ("lt_hr", "lt_hr"), ("ftp_w", "ftp_w"))
_RANGE_RULES = {  # reference -> (THRESHOLDS factor, words in the reason)
    "lt_speed_mps": ("hard_pace_of_lt_speed", "threshold speed"),
    "lt_hr": ("hard_hr_of_lthr", "threshold HR"),
    "ftp_w": ("hard_power_of_ftp", "FTP"),
    "percent_ftp": ("hard_power_of_ftp", "FTP"),
}
# Plausible running threshold speeds in m/s (8:20 to 2:23 min/km). Garmin's
# raw unit is unusual (cm/ms); a value outside this band means it changed, and
# the threshold is treated as unknown instead of silently misjudging ranges.
_LT_SPEED_MPS_BAND = (2.0, 7.0)
# Unstructured workouts (no steps) are hard only by name, on English keywords.
_HARD_NAME_RE = re.compile(
    r"\binterval|\btempo|\bthreshold|\bvo2|\brac(?:e|es|ing)\b|\bhard\b|\bbrick",
    re.IGNORECASE,
)
# Phrases whose keyword says the opposite, removed before the keyword match: "pre-race" / "post-race" (the run
# before or after a race), "not hard" / "no hard" / "non-hard", and a keyword negated with "-free"
# ("tempo-free"). Other keywords in the name still count: "Intervals with recovery jogs" stays hard.
_SOFT_NAME_RE = re.compile(
    r"\b(?:pre|post)[- ]?rac(?:e|es|ing)\b|\b(?:not|no|non)[- ]hard\b|\b[a-z0-9]+-free\b",
    re.IGNORECASE,
)
# A shakeout is the easy jog around a race: in its name, "race" names the race, not the workout.
_SHAKEOUT_RE = re.compile(r"\bshake[- ]?out", re.IGNORECASE)
_RACE_WORD_RE = re.compile(r"\brac(?:e|es|ing)\b", re.IGNORECASE)

_SPORT_EN = {"run": "run", "bike": "bike", "swim": "swim", "other": "other"}
_OTHER_TYPE_EN = {
    "strength_training": "strength", "yoga": "yoga", "pilates": "pilates", "walking": "walk",
    "hiking": "hike", "multi_sport": "multisport", "mobility": "mobility",
}
_READINESS_LEVEL_EN = {
    "PRIME": "prime", "HIGH": "high", "MODERATE": "moderate", "LOW": "low", "POOR": "poor",
}
_ACWR_STATUS_EN = {"LOW": "low", "OPTIMAL": "optimal", "HIGH": "high", "VERY_HIGH": "very high"}

_TS_RE = re.compile(r"^\s*(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2}))?")
_ID_SEGMENT_RE = re.compile(r"^(?:\d{3,}|[0-9a-fA-F]{8}-[0-9a-fA-F-]{8,})$")


# --- small helpers ------------------------------------------------------------

def _text(value: Any) -> Optional[str]:
    """A non-empty stripped string, else None."""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _status_code(value: Any) -> Optional[str]:
    text = _text(value)
    return text.upper() if text else None


def _pos_int(value: Any) -> Optional[int]:
    """Rounded positive number (Garmin sends 0 or negatives for "no value")."""
    v = sc.num(value)
    return int(round(v)) if v is not None and v > 0 else None


def _nonneg_int(value: Any) -> Optional[int]:
    v = sc.num(value)
    return int(round(v)) if v is not None and v >= 0 else None


def _minutes(seconds: Any) -> Optional[int]:
    v = sc.num(seconds)
    return int(round(v / 60)) if v is not None and v >= 0 else None


def _km(meters: Any) -> Optional[float]:
    v = sc.num(meters)
    return round(v / 1000, 1) if v is not None and v >= 0 else None


def _first_positive(*values: Any) -> Optional[float]:
    for value in values:
        v = sc.num(value)
        if v is not None and v > 0:
            return v
    return None


def _fmt_threshold(value: Any) -> str:
    """6 -> '6', 1.5 -> '1.5', 6.0 -> '6' (thresholds as written in labels)."""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _parse_local_ts(value: Any) -> Optional[dt.datetime]:
    """Naive datetime from Garmin's "2026-03-18T05:58:41.0" (any fraction, no zone).

    datetime.fromisoformat on Python 3.10 rejects a one-digit fraction.
    """
    if not isinstance(value, str):
        return None
    m = _TS_RE.match(value)
    if not m:
        return None
    y, mo, d, h, mi, s = m.groups()
    try:
        return dt.datetime(int(y), int(mo), int(d), int(h), int(mi), int(s or 0))
    except ValueError:
        return None


def _upstream(module: str, name: str) -> Optional[Callable[..., Any]]:
    """garmin_mcp.<module>.<name>, or None when garmin_mcp is not installed (unit tests)."""
    try:
        mod = importlib.import_module(f"garmin_mcp.{module}")
    except ImportError:
        return None
    return getattr(mod, name, None)


def _hours_from_minutes_fallback(minutes: Any, phrase: Any = None) -> Optional[float]:
    """Stand-in with the contract of upstream's _hours_from_recovery_minutes.

    Used only when garmin_mcp is not importable (plain unit tests); the
    server always uses upstream's (selfcheck() makes sure it exists).
    """
    if phrase == "REACHED_ZERO":
        return 0.0
    v = sc.num(minutes)
    if v is None or v < 0:
        return None
    return round(float(v) / 60.0, 1)


def _hours_from_recovery_minutes(minutes: Any, phrase: Any) -> Optional[float]:
    fn = _upstream("health_wellness", "_hours_from_recovery_minutes") or _hours_from_minutes_fallback
    return fn(minutes, phrase)


def _lt_speed_fallback(value: Any) -> Optional[float]:
    """Stand-in for upstream's training._lactate_threshold_speed_mps (plain unit tests).

    Garmin sends the lactate-threshold speed in cm/ms: m/s = raw x 10.
    """
    v = sc.num(value)
    return v * 10 if v else None


def lt_speed_mps(raw: Any) -> Optional[float]:
    """Running lactate-threshold speed in m/s from Garmin's raw value, or None."""
    v = sc.num(raw)
    if v is None or v <= 0:
        return None
    fn = _upstream("training", "_lactate_threshold_speed_mps") or _lt_speed_fallback
    mps = sc.num(fn(v))
    low, high = _LT_SPEED_MPS_BAND
    return mps if mps is not None and low <= mps <= high else None


def pace_min_km(speed_mps: Any) -> Optional[str]:
    """3.7 m/s -> "4:30" (minutes per km)."""
    v = sc.num(speed_mps)
    if v is None or v <= 0:
        return None
    seconds = int(round(1000 / v))
    return f"{seconds // 60}:{seconds % 60:02d}"


# --- extraction: sleep --------------------------------------------------------

def night(payload: Any, day: str) -> Optional[Dict[str, Any]]:
    """dailySleepDTO of the night that ended on `day`, or None.

    Garmin files a night under its wake date and answers with an empty shell
    (no sleep seconds) until the phone has synced; a DTO of another calendar
    date is not this night either.
    """
    dto = sc.dig(payload, "dailySleepDTO")
    if not isinstance(dto, dict):
        return None
    seconds = sc.num(dto.get("sleepTimeSeconds"))
    if seconds is None or seconds <= 0:
        return None
    if dto.get("calendarDate") != day:
        return None
    return dto


def _clock(value: Any, *, local: bool, zone: ZoneInfo) -> Optional[str]:
    """HH:MM from a Garmin timestamp: epoch ms or an ISO string.

    *TimestampLocal epoch values are the local wall clock written as if it
    were UTC, so they are formatted in UTC; GMT values are converted to zone.
    """
    v = sc.num(value)
    if v is not None and v > 0:
        try:
            moment = dt.datetime.fromtimestamp(v / 1000, dt.timezone.utc)
        except (OverflowError, ValueError, OSError):
            return None
        return (moment if local else moment.astimezone(zone)).strftime("%H:%M")
    parsed = _parse_local_ts(value)
    if parsed is None:
        return None
    if not local:
        parsed = parsed.replace(tzinfo=dt.timezone.utc).astimezone(zone)
    return parsed.strftime("%H:%M")


def sleep_end_local(dto: Optional[Dict[str, Any]], zone: ZoneInfo) -> Optional[str]:
    if not isinstance(dto, dict):
        return None
    return (_clock(dto.get("sleepEndTimestampLocal"), local=True, zone=zone)
            or _clock(dto.get("sleepEndTimestampGMT"), local=False, zone=zone))


def sleep_hours(payload: Any, day: str) -> Optional[float]:
    dto = night(payload, day)
    return round(dto["sleepTimeSeconds"] / 3600, 2) if dto else None


def extract_sleep(payload: Any, day: str, zone: ZoneInfo) -> Dict[str, Any]:
    """Sleep fields of the night that ended on `day` (all None when not synced)."""
    out: Dict[str, Any] = {
        "sleep_hours": None, "sleep_hhmm": None, "sleep_score": None, "rem_min": None,
        "deep_min": None, "awake_min": None, "sleep_end_local": None,
        "spo2_avg": None, "spo2_min": None, "resp_sleep_avg": None, "overnight_hrv": None,
    }
    dto = night(payload, day)
    if dto is None:
        return out
    seconds = dto["sleepTimeSeconds"]
    out["sleep_hours"] = round(seconds / 3600, 2)
    out["sleep_hhmm"] = sc.hours_to_hmm(seconds / 3600)
    out["sleep_score"] = _pos_int(sc.dig(dto, "sleepScores", "overall", "value"))
    out["rem_min"] = _minutes(dto.get("remSleepSeconds"))
    out["deep_min"] = _minutes(dto.get("deepSleepSeconds"))
    out["awake_min"] = _minutes(dto.get("awakeSleepSeconds"))
    out["sleep_end_local"] = sleep_end_local(dto, zone)
    out["spo2_avg"] = _pos_int(sc.dig(payload, "wellnessSpO2SleepSummaryDTO", "averageSpo2"))
    out["spo2_min"] = _pos_int(sc.dig(payload, "wellnessSpO2SleepSummaryDTO", "lowestSpo2"))
    resp = sc.num(dto.get("averageRespirationValue"))
    out["resp_sleep_avg"] = round(resp, 1) if resp is not None and resp > 0 else None
    out["overnight_hrv"] = _pos_int(sc.dig(payload, "avgOvernightHrv"))
    return out


# --- extraction: daily summary, HRV, readiness, load, activities --------------

def extract_stats(payload: Any) -> Dict[str, Any]:
    p = payload if isinstance(payload, dict) else {}
    return {
        "rhr": _pos_int(p.get("restingHeartRate")),
        # Garmin's 7-day average probably includes today; accepted.
        "rhr_7d": _pos_int(p.get("lastSevenDaysAvgRestingHeartRate")),
        "body_battery": _nonneg_int(p.get("bodyBatteryMostRecentValue")),
        # Negative stress values mean "not enough data".
        "stress_avg": _nonneg_int(p.get("averageStressLevel")),
    }


def extract_hrv(payload: Any) -> Dict[str, Any]:
    summary = sc.dig(payload, "hrvSummary")
    s = summary if isinstance(summary, dict) else {}
    baseline = s.get("baseline")
    hrv_range = None
    if isinstance(baseline, dict):
        hrv_range = {
            "low_upper": _pos_int(baseline.get("lowUpper")),
            "balanced_low": _pos_int(baseline.get("balancedLow")),
            "balanced_upper": _pos_int(baseline.get("balancedUpper")),
        }
    return {
        "hrv": _pos_int(s.get("lastNightAvg")),
        "hrv_7d": _pos_int(s.get("weeklyAvg")),
        "hrv_status": _status_code(s.get("status")),
        "hrv_range": hrv_range,
    }


def hrv_trend_point(payload: Any) -> Optional[Tuple[int, int]]:
    """(weeklyAvg, baseline.balancedLow) of one day, or None if either is missing."""
    s = sc.dig(payload, "hrvSummary")
    if not isinstance(s, dict):
        return None
    weekly = _pos_int(s.get("weeklyAvg"))
    low = _pos_int(sc.dig(s, "baseline", "balancedLow"))
    if weekly is None or low is None:
        return None
    return weekly, low


def readiness_snapshots(payload: Any, day: str) -> List[Dict[str, Any]]:
    """Training Readiness snapshots of `day` that carry a score, newest first."""
    if isinstance(payload, dict):
        items: List[Any] = [payload]
    elif isinstance(payload, list):
        items = payload
    else:
        items = []
    snaps = [
        s for s in items
        if isinstance(s, dict) and s.get("calendarDate") == day and sc.num(s.get("score")) is not None
    ]
    # The ISO strings sort chronologically; GMT first, since the local clock
    # of a travelling watch can run out of order.
    snaps.sort(key=lambda s: str(s.get("timestamp") or s.get("timestampLocal") or ""), reverse=True)
    return snaps


# Training Readiness inputContext values: when Garmin wrote the row.
WAKEUP_CONTEXT = "AFTER_WAKEUP_RESET"
POST_EXERCISE_CONTEXT = "AFTER_POST_EXERCISE_RESET"
REALTIME_CONTEXT = "UPDATE_REALTIME_VARIABLES"


def _pre_sleep_row(snap: Dict[str, Any]) -> bool:
    """A realtime row written before the night was scored (it has no sleep score).

    Garmin writes one just after midnight; it describes yesterday evening,
    not this morning.
    """
    return snap.get("inputContext") == REALTIME_CONTEXT and sc.num(snap.get("sleepScore")) is None


def morning_snapshot(snaps: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The row that holds the morning's readiness, from snaps (newest first), or None.

    The newest wake-up row; until there is one, the newest row that is not a
    pre-sleep realtime row (a post-exercise row counts). None = no morning
    readiness yet.
    """
    for snap in snaps:
        if snap.get("inputContext") == WAKEUP_CONTEXT:
            return snap
    for snap in snaps:
        if not _pre_sleep_row(snap):
            return snap
    return None


def snapshot_time(snap: Dict[str, Any], zone: ZoneInfo) -> Optional[dt.datetime]:
    """When a readiness snapshot was taken, as an aware datetime.

    The GMT "timestamp" is used when present. "timestampLocal" is the
    watch's wall clock: reading it in the report's zone is off by the
    offset whenever the watch is in another time zone, so it is only the
    fallback.
    """
    gmt = snap.get("timestamp")
    v = sc.num(gmt)
    if v is not None and v > 0:
        try:
            return dt.datetime.fromtimestamp(v / 1000, dt.timezone.utc)
        except (OverflowError, ValueError, OSError):
            return None
    parsed = _parse_local_ts(gmt)
    if parsed is not None:
        return parsed.replace(tzinfo=dt.timezone.utc)
    local = _parse_local_ts(snap.get("timestampLocal"))
    return local.replace(tzinfo=zone) if local is not None else None


def primary_device_id(training_status: Any) -> Optional[str]:
    """Id of the device Garmin marks as the primary training device, or None.

    Internal only: it selects that watch's readiness rows and never reaches
    the status.
    """
    latest = sc.dig(training_status, "mostRecentTrainingStatus", "latestTrainingStatusData")
    if not isinstance(latest, dict):
        return None
    for key, record in latest.items():
        if isinstance(record, dict) and record.get("primaryTrainingDevice") is True:
            device = record.get("deviceId")
            return str(device if device is not None else key)
    return None


def extract_readiness(
    payload: Any, day: str, *, zone: ZoneInfo, now: dt.datetime, is_today: bool,
    device_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Readiness of the morning; acute load and recovery hours from the newest snapshot of `day`.

    Score and level come from morning_snapshot() (the wake-up row; null
    before there is a usable row). Acute load and the recovery clock come
    from the newest row, since every workout resets them. With several
    watches each one writes its own rows: when device_id (the primary
    training device) has rows for the day, only its rows are read,
    otherwise all rows. recoveryTime is the recovery clock at snapshot time;
    for today the hours elapsed since then are subtracted (floor 0).
    """
    out: Dict[str, Any] = {"readiness": None, "readiness_level": None, "recovery_hours": None, "acute_load": None}
    snaps = readiness_snapshots(payload, day)
    if device_id is not None:
        own = [s for s in snaps if s.get("deviceId") is not None and str(s.get("deviceId")) == device_id]
        snaps = own or snaps
    if not snaps:
        return out
    morning = morning_snapshot(snaps)
    if morning is not None:
        out["readiness"] = _nonneg_int(morning.get("score"))
        out["readiness_level"] = _status_code(morning.get("level"))
    newest = snaps[0]
    out["acute_load"] = _nonneg_int(newest.get("acuteLoad"))
    # recoveryTimeChangePhrase says how the clock changed at this snapshot.
    # On a post-exercise row REACHED_ZERO means the clock had run out BEFORE
    # the workout, and recoveryTime is the NEW recovery (seen on live rows:
    # many hours right after a workout that followed a wake-up row with about
    # a minute left, counting down at the later rows). Upstream's helper
    # reads REACHED_ZERO as 0, which is right for the other rows only.
    phrase = newest.get("recoveryTimeChangePhrase")
    if newest.get("inputContext") == POST_EXERCISE_CONTEXT:
        phrase = None
    hours = _hours_from_recovery_minutes(newest.get("recoveryTime"), phrase)
    if hours is not None and is_today and hours > 0:
        taken = snapshot_time(newest, zone)
        if taken is not None:
            elapsed = (now - taken).total_seconds() / 3600
            if elapsed > 0:
                hours = max(0.0, hours - elapsed)
    out["recovery_hours"] = round(float(hours), 1) if hours is not None else None
    return out


def pick_training_device(latest: Any) -> Dict[str, Any]:
    """The record upstream's training._select_training_device picks without a device list.

    That is the record marked primaryTrainingDevice, else the first non-empty
    one. Upstream can also match the primary tracker from get_devices(); this
    report skips that extra Garmin call (with one watch both pick the same).
    """
    entries = [v for v in latest.values() if isinstance(v, dict) and v] if isinstance(latest, dict) else []
    for record in entries:
        if record.get("primaryTrainingDevice") is True:
            return record
    return entries[0] if entries else {}


def extract_load(payload: Any) -> Dict[str, Any]:
    latest = sc.dig(payload, "mostRecentTrainingStatus", "latestTrainingStatusData")
    record = pick_training_device(latest)
    dto = record.get("acuteTrainingLoadDTO")
    d = dto if isinstance(dto, dict) else {}
    return {
        "acute": _nonneg_int(d.get("dailyTrainingLoadAcute")),
        "chronic": _nonneg_int(d.get("dailyTrainingLoadChronic")),
        "acwr": sc.acwr_ratio(d.get("dailyTrainingLoadAcute"), d.get("dailyTrainingLoadChronic"),
                              d.get("dailyAcuteChronicWorkloadRatio")),
        "acwr_status": _status_code(d.get("acwrStatus")),
        "acwr_percent": _nonneg_int(d.get("acwrPercent")),
    }


def extract_sessions(payload: Any) -> Optional[List[Dict[str, Any]]]:
    """Completed activities from the activity-list search, oldest first.

    None when the fetch failed (unknown), [] when Garmin listed nothing.
    """
    if payload is None:
        return None
    if not isinstance(payload, list):
        return []  # 204 No Content arrives as {}
    sessions = []
    for a in sorted((x for x in payload if isinstance(x, dict)), key=lambda x: str(x.get("startTimeLocal") or "")):
        type_key = _text(sc.dig(a, "activityType", "typeKey"))
        aerobic = sc.num(a.get("aerobicTrainingEffect"))
        anaerobic = sc.num(a.get("anaerobicTrainingEffect"))
        sessions.append({
            "sport": sc.sport_group(type_key),
            "type": type_key,
            "name": th.name_or_none(a.get("activityName")),
            "minutes": _minutes(a.get("duration")),
            "km": _km(a.get("distance")),
            "avg_hr": _pos_int(a.get("averageHR")),
            "max_hr": _pos_int(a.get("maxHR")),
            "load": _nonneg_int(a.get("activityTrainingLoad")),
            "aerobic_te": round(aerobic, 1) if aerobic is not None else None,
            "anaerobic_te": round(anaerobic, 1) if anaerobic is not None else None,
        })
    return sessions


def completion_sport(type_key: Any) -> str:
    """The "sport" status_common.match_completions pairs on.

    run / bike / swim pair by sport group. "other" lumps strength, yoga,
    walking and the rest together, so those pair by their own type key: a
    walk must not complete a strength workout.
    """
    group = sc.sport_group(type_key)
    if group != "other":
        return group
    return (_text(type_key) or "other").lower()


def completion_activities(payload: Any) -> List[Dict[str, Any]]:
    """A day's activities in status_common.match_completions' shape, in start order.

    The activity ids only pair activities with workouts; they never reach
    the status.
    """
    if not isinstance(payload, list):
        return []
    out = []
    acts = sorted((x for x in payload if isinstance(x, dict)), key=lambda x: str(x.get("startTimeLocal") or ""))
    for i, a in enumerate(acts):
        start = _text(a.get("startTimeLocal"))
        activity_id = a.get("activityId")
        out.append({
            "date": start[:10] if start else None,
            "sport": completion_sport(sc.dig(a, "activityType", "typeKey")),
            # Without an id each activity still pairs with one workout only.
            "id": activity_id if activity_id is not None else f"#{i}",
        })
    return out


def athlete_thresholds(lactate: Any, ftp: Any) -> Dict[str, Any]:
    """The athlete's thresholds the range rules compare with (None = unknown).

    lactate is garminconnect's get_lactate_threshold() payload (latest:
    {"speed_and_heart_rate": {...}, "power": {...}}), ftp is
    get_cycling_ftp()'s (a dict, or a list of them).
    """
    shr = sc.dig(lactate, "speed_and_heart_rate")
    shr = shr if isinstance(shr, dict) else {}
    records = ftp if isinstance(ftp, list) else [ftp]
    ftp_w = next((w for w in (_pos_int(r.get("functionalThresholdPower"))
                              for r in records if isinstance(r, dict)) if w), None)
    return {"lt_speed_mps": lt_speed_mps(shr.get("speed")), "lt_hr": _pos_int(shr.get("heartRate")), "ftp_w": ftp_w}


def athlete_out(athlete: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """The "thresholds_athlete" field: no ids, pace as m:ss per km."""
    a = athlete or {}
    return {"lt_pace_min_km": pace_min_km(a.get("lt_speed_mps")), "lt_hr": a.get("lt_hr"), "ftp_w": a.get("ftp_w")}


# --- schedule and workout classification --------------------------------------

def schedule_query(start: str, end: str) -> Dict[str, str]:
    """The calendar query upstream's get_scheduled_workouts sends (dates are validated)."""
    return {"query": f'query{{workoutScheduleSummariesScalar(startDate:"{start}", endDate:"{end}")}}'}


def schedule_entries(payload: Any) -> Optional[List[Dict[str, Any]]]:
    """Scheduled workouts from the GraphQL answer, rest days left out.

    None when the answer carries no "data" object (a GraphQL error).
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        return None
    items = payload["data"].get("workoutScheduleSummariesScalar") or []
    if not isinstance(items, list):
        return None
    return [e for e in items if isinstance(e, dict) and not e.get("isRestDay")]


def _repeat_count(step: Dict[str, Any]) -> int:
    count = sc.num(step.get("repeat_count"))
    if count is None and step.get("end_condition") == "iterations":
        count = sc.num(step.get("end_condition_value"))
    return int(count) if count is not None and count >= 1 else 1


def leaf_steps(curated: Optional[Dict[str, Any]]) -> Iterator[Tuple[Dict[str, Any], int]]:
    """(step, times it runs) for every executable step of a curated workout.

    Repeat groups are flattened and their counts multiplied (nested repeats too).
    """
    def walk(steps: Any, times: int) -> Iterator[Tuple[Dict[str, Any], int]]:
        for step in steps if isinstance(steps, list) else []:
            if not isinstance(step, dict):
                continue
            if isinstance(step.get("steps"), list) and step["steps"]:
                yield from walk(step["steps"], times * _repeat_count(step))
            elif step.get("type") != "repeat":
                yield step, times

    for segment in (curated or {}).get("segments") or []:
        if isinstance(segment, dict):
            yield from walk(segment.get("steps"), 1)


def _sum_steps(leaves: List[Tuple[Dict[str, Any], int]], condition: str) -> Optional[float]:
    values = [
        sc.num(step.get("end_condition_value")) * times
        for step, times in leaves
        if step.get("end_condition") == condition and sc.num(step.get("end_condition_value")) is not None
    ]
    return sum(values) if values else None


def _total_seconds(leaves: List[Tuple[Dict[str, Any], int]]) -> Optional[float]:
    """A workout's duration from its steps, only when every step ends on time.

    The timed steps alone are not the duration: a swim of distance repeats
    with timed rests would show just its rests, and a lap-button step has no
    length at all. Then the duration is unknown.
    """
    total = 0.0
    for step, times in leaves:
        value = sc.num(step.get("end_condition_value"))
        if step.get("end_condition") != "time" or value is None:
            return None
        total += value * times
    return total if leaves else None


def _target_kind(table: Dict[str, str], value: Any) -> Optional[str]:
    return table.get(value) if isinstance(value, str) else None


def step_ranges(step: Dict[str, Any]) -> Iterator[Tuple[str, str, float, Optional[str]]]:
    """(label, kind, midpoint, unit) of each explicit low/high target of a step, primary first."""
    for prefix, label in (("", ""), ("secondary_", "secondary ")):
        kind = _target_kind(_TARGET_KINDS, step.get(f"{prefix}target_type"))
        if not kind or step.get(f"{prefix}target_zone") is not None:
            continue
        low = sc.num(step.get(f"{prefix}target_value_low"))
        high = sc.num(step.get(f"{prefix}target_value_high"))
        if low is None or high is None or low <= 0 or high <= 0:
            continue
        yield label, kind, (low + high) / 2, _text(step.get(f"{prefix}target_value_unit"))


def range_reference(kind: str, sport: str, unit: Optional[str]) -> Optional[str]:
    """What a range target is compared with.

    An athlete threshold key, "percent_ftp" (a range already in % FTP), or
    None when no threshold applies and the repeat fallback decides: swim
    pace, speed on a bike, watts outside cycling (cycling FTP does not fit
    running power), heart rate in percent.
    """
    if unit == "percent":
        return "percent_ftp" if kind == "power" else None
    if kind == "power":
        return "ftp_w" if sport == "bike" else None
    if kind == "HR":
        return "lt_hr"
    return "lt_speed_mps" if sport == "run" else None


def _counted_steps(cur: Optional[Dict[str, Any]]) -> Iterator[Tuple[Dict[str, Any], int]]:
    for step, times in leaf_steps(cur):
        if step.get("type") not in _EASY_STEP_TYPES:
            yield step, times


def thresholds_needed(workouts: List[Tuple[str, Optional[Dict[str, Any]]]]) -> List[str]:
    """Athlete thresholds the range targets of these (sport, curated workout) pairs need."""
    found = {
        range_reference(kind, sport, unit)
        for sport, cur in workouts
        for step, _ in _counted_steps(cur)
        for _, kind, _, unit in step_ranges(step)
    }
    return [key for key, _ in ATHLETE_FIELDS if key in found]


def hard_from_steps(
    leaves: List[Tuple[Dict[str, Any], int]],
    sport: str = "other",
    athlete: Optional[Dict[str, Any]] = None,
    thresholds: Dict[str, Any] = THRESHOLDS,
) -> Tuple[bool, str]:
    """(hard, reason); rules a-e above.

    The reason names the rule of the first step that qualifies and how much
    work the qualifying steps add up to. They must reach hard_min_work_s
    seconds or hard_min_work_m metres (step value x repeat iterations); a
    qualifying step that ends on the lap button, or any other open end, is
    enough by itself.
    """
    athlete = athlete or {}
    best: Optional[Tuple[float, str]] = None  # highest range below its threshold, for the reason
    first: Optional[str] = None
    seconds = metres = 0.0
    open_ended = False
    for step, times in leaves:
        step_type = step.get("type")
        if step_type in _EASY_STEP_TYPES:
            continue
        rule: Optional[str] = None
        for prefix, label in (("", ""), ("secondary_", "secondary ")):
            kind = _target_kind(_TARGET_KINDS, step.get(f"{prefix}target_type"))
            zone_number = sc.num(step.get(f"{prefix}target_zone"))
            if kind and zone_number is not None and zone_number >= _HARD_ZONE:
                rule = f"{label}{kind} zone {int(zone_number)}"
                break
        if rule is None:
            for label, kind, mid, unit in step_ranges(step):
                ref = range_reference(kind, sport, unit)
                base = 100.0 if ref == "percent_ftp" else (sc.num(athlete.get(ref)) if ref else None)
                if base:
                    factor_key, words = _RANGE_RULES[ref]
                    # Rounded so that a range exactly at the threshold counts.
                    ratio = round(mid / base, 4)
                    text = f"{label}{kind} range at {round(ratio * 100)}% of {words}"
                    if ratio >= thresholds[factor_key]:
                        rule = text
                        break
                    if best is None or ratio > best[0]:
                        best = (ratio, text)
                elif step_type == "interval" and times >= 2:
                    why = "swim pace" if sport == "swim" and kind == "pace" else "no threshold"
                    rule = f"{label}{kind} range inside a repeat ({why})"
                    break
        if rule is None:
            continue
        if first is None:
            first = f"{step_type} step {rule}"
        value = sc.num(step.get("end_condition_value"))
        if step.get("end_condition") == "time" and value is not None:
            seconds += value * times
        elif step.get("end_condition") == "distance" and value is not None:
            metres += value * times
        else:
            open_ended = True

    if first is None:
        reason = f"no step at zone {_HARD_ZONE}+ or at threshold intensity"
        return False, reason + (f"; highest {best[1]}" if best else "")
    parts = ([f"{int(round(seconds))} s"] if seconds else []) + ([f"{int(round(metres))} m"] if metres else [])
    parts += ["an open-ended step"] if open_ended else []
    work = " + ".join(parts) or "0 s"
    min_s, min_m = thresholds["hard_min_work_s"], thresholds["hard_min_work_m"]
    if open_ended or seconds >= min_s or metres >= min_m:
        return True, f"{first}; hard work {work}"
    return False, (f"{first}, but only {work} of hard work "
                   f"(under {_fmt_threshold(min_s)} s and {_fmt_threshold(min_m)} m)")


def hard_from_name(name: Optional[str]) -> Tuple[bool, str]:
    text = _SOFT_NAME_RE.sub(" ", name or "")
    if _SHAKEOUT_RE.search(text):
        text = _RACE_WORD_RE.sub(" ", text)
    match = _HARD_NAME_RE.search(text)
    if match:
        return True, f"name keyword '{match.group(0)}'"
    return False, "no hard keyword in the name"


def long_check(sport: str, minutes: Optional[int], km: Optional[float],
               thresholds: Dict[str, Any] = THRESHOLDS) -> Tuple[bool, str]:
    long_min = thresholds["long_min"].get(sport, thresholds["long_min"]["other"])
    if minutes is not None and minutes >= long_min:
        return True, f"{minutes} min >= {_fmt_threshold(long_min)} min ({sport})"
    long_km = thresholds["long_km"].get(sport)
    if long_km is not None and km is not None and km >= long_km:
        return True, f"{km} km >= {_fmt_threshold(long_km)} km ({sport})"
    return False, f"below {_fmt_threshold(long_min)} min" + (
        f" and {_fmt_threshold(long_km)} km" if long_km is not None else "")


def workout_sport(entry: Optional[Dict[str, Any]], curated: Optional[Dict[str, Any]]) -> str:
    e = entry if isinstance(entry, dict) else {}
    c = curated if isinstance(curated, dict) else {}
    return sc.sport_group(e.get("workoutType") or c.get("sport"))


def classify_workout(
    entry: Optional[Dict[str, Any]],
    curated: Optional[Dict[str, Any]],
    thresholds: Dict[str, Any] = THRESHOLDS,
    *,
    athlete: Optional[Dict[str, Any]] = None,
    unavailable: str = "workout details unavailable",
) -> Dict[str, Any]:
    """One planned workout: estimate, class and why.

    entry is the calendar summary (workoutScheduleSummariesScalar item),
    curated upstream's _curate_workout_details() of the workout, or None when
    the details could not be read. Without details the workout is judged like
    an unstructured one (name keywords and the estimate) and `structured` is
    null. athlete holds the thresholds range targets are compared with
    (athlete_thresholds()). "completed" here is Garmin's own link only;
    mark_completed() settles it with the day's activities.
    """
    entry = entry if isinstance(entry, dict) else {}
    cur = curated if isinstance(curated, dict) else None
    # Cleaned before the keyword match: "tem<U+200B>po" reads "tempo" (hard), as the user sees it.
    name = th.name_or_none(entry.get("workoutName")) or th.name_or_none((cur or {}).get("name"))
    sport = workout_sport(entry, cur)
    leaves = list(leaf_steps(cur)) if cur is not None else []
    structured = None if cur is None else bool(leaves)

    seconds = _first_positive(
        entry.get("estimatedDurationInSecs"),
        (cur or {}).get("estimated_duration_seconds"),
        _total_seconds(leaves),
    )
    meters = _first_positive(
        entry.get("estimatedDistanceInMeters"),
        (cur or {}).get("estimated_distance_meters"),
        _sum_steps(leaves, "distance"),
    )
    minutes = _minutes(seconds)
    km = _km(meters)

    if structured:
        hard, hard_reason = hard_from_steps(leaves, sport, athlete, thresholds)
    else:
        hard, hard_reason = hard_from_name(name)
    long_, long_reason = long_check(sport, minutes, km, thresholds)

    reasons = [r for r, on in ((hard_reason, hard), (long_reason, long_)) if on] or [hard_reason, long_reason]
    if structured is None:
        reasons.insert(0, unavailable)
    return {
        "name": name,
        "sport": sport,
        "km": km,
        "minutes": minutes,
        "class": "hard_or_long" if hard or long_ else "easy",
        "hard": hard,
        "long": long_,
        "structured": structured,
        "reason": "; ".join(reasons),
        "completed": entry.get("associatedActivityId") is not None,
    }


def mark_completed(
    workouts: List[Dict[str, Any]], entries: List[Dict[str, Any]], activities: List[Dict[str, Any]]
) -> None:
    """Set each classified workout's "completed" with status_common.match_completions.

    Done = linked by Garmin, or an activity of the same sport group on the
    same local date (one activity per workout; "other" sports pair by type
    key, see completion_sport). entries are the calendar items the workouts
    were classified from, in the same order; activities come from
    completion_activities().
    """
    scheduled = [
        {
            "date": e.get("scheduleDate"),
            "sport": w.get("sport") if w.get("sport") in ("run", "bike", "swim")
            else completion_sport(e.get("workoutType")),
            "activity_id": e.get("associatedActivityId"),
        }
        for e, w in zip(entries, workouts)
    ]
    for w, done in zip(workouts, sc.match_completions(scheduled, activities)):
        w["completed"] = bool(done)


# --- rules --------------------------------------------------------------------

def flag_label(flag_id: str, thresholds: Dict[str, Any] = THRESHOLDS, *, by_status: bool = False) -> str:
    """English flag label, "Name: detail"; each one states its threshold."""
    t = {k: _fmt_threshold(v) for k, v in thresholds.items() if not isinstance(v, dict)}
    labels = {
        "low_hrv": f"Low HRV: {t['hrv_drop_ms']} ms or more below the 7-day average",
        "hrv_low_trend": f"Low HRV trend: below the balanced range {t['hrv_trend_days']} days in a row",
        "high_rhr": f"High resting HR: {t['rhr_rise_bpm']} bpm or more above the 7-day average",
        "short_sleep": f"Short sleep: under {t['sleep_h']} h",
        "sleep_debt": f"Sleep debt: under {t['sleep_h']} h two nights in a row",
        "low_readiness": f"Low readiness: Training Readiness below {t['readiness']}",
        "load_spike": (
            "Load spike: ACWR status HIGH or VERY_HIGH" if by_status
            else f"Load spike: ACWR above {t['acwr_red']}"
        ),
        "load_rising": f"Load rising: ACWR above {t['acwr_yellow']}",
        "recovery_incomplete": f"Recovery not finished: {t['recovery_h']} h or more on a hard or long day",
    }
    return labels[flag_id]


def _flag(flag_id: str, thresholds: Dict[str, Any], **kw: Any) -> Dict[str, str]:
    return {"id": flag_id, "level": FLAG_LEVELS[flag_id], "label_en": flag_label(flag_id, thresholds, **kw)}


def _has_hard_or_long(workouts: Optional[List[Dict[str, Any]]]) -> bool:
    return any(w.get("class") == "hard_or_long" for w in workouts or [])


def _not_done(workouts: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """Workouts still ahead: the day's rules ignore the ones already completed."""
    return [w for w in workouts or [] if not w.get("completed")]


def evaluate_rules(
    recovery: Dict[str, Any],
    load: Dict[str, Any],
    today: Optional[List[Dict[str, Any]]],
    hrv_trend: Optional[List[Optional[Tuple[int, int]]]] = None,
    thresholds: Dict[str, Any] = THRESHOLDS,
) -> Tuple[List[Dict[str, str]], List[str]]:
    """Daily rules v1 -> (flags, missing input paths).

    A rule with a null input is skipped and its null inputs are returned as
    status paths. hrv_trend holds Garmin's (weeklyAvg, balancedLow) per day,
    the report day first; the earlier days have no field of their own in the
    status, so they are reported as "hrv_trend.d-1", "hrv_trend.d-2".
    """
    t = thresholds
    flags: List[Dict[str, str]] = []
    missing: List[str] = []

    def need(*inputs: Tuple[str, Any]) -> bool:
        absent = [path for path, value in inputs if value is None]
        missing.extend(p for p in absent if p not in missing)
        return not absent

    r, ld = recovery, load
    hrv, hrv_7d = r.get("hrv"), r.get("hrv_7d")
    if need(("recovery.hrv", hrv), ("recovery.hrv_7d", hrv_7d)):
        if hrv <= hrv_7d - t["hrv_drop_ms"]:
            flags.append(_flag("low_hrv", t))

    hrv_range = r.get("hrv_range")
    balanced_low = hrv_range.get("balanced_low") if isinstance(hrv_range, dict) else None
    low_path = "recovery.hrv_range.balanced_low" if isinstance(hrv_range, dict) else "recovery.hrv_range"
    days = int(t["hrv_trend_days"])
    earlier = list(hrv_trend[1:days]) if hrv_trend else []
    earlier += [None] * (days - 1 - len(earlier))
    inputs = [("recovery.hrv_7d", hrv_7d), (low_path, balanced_low)]
    inputs += [(f"hrv_trend.d-{i}", point) for i, point in enumerate(earlier, 1)]
    if need(*inputs):
        points = [(hrv_7d, balanced_low)] + earlier
        if all(weekly < low for weekly, low in points):
            flags.append(_flag("hrv_low_trend", t))

    rhr, rhr_7d = r.get("rhr"), r.get("rhr_7d")
    if need(("recovery.rhr", rhr), ("recovery.rhr_7d", rhr_7d)):
        if rhr >= rhr_7d + t["rhr_rise_bpm"]:
            flags.append(_flag("high_rhr", t))

    slept, slept_prev = r.get("sleep_hours"), r.get("sleep_hours_prev")
    debt = False
    if need(("recovery.sleep_hours", slept), ("recovery.sleep_hours_prev", slept_prev)):
        debt = slept < t["sleep_h"] and slept_prev < t["sleep_h"]
    if need(("recovery.sleep_hours", slept)) and slept < t["sleep_h"] and not debt:
        flags.append(_flag("short_sleep", t))
    if debt:
        flags.append(_flag("sleep_debt", t))

    readiness = r.get("readiness")
    if need(("recovery.readiness", readiness)) and readiness < t["readiness"]:
        flags.append(_flag("low_readiness", t))

    # load_spike: Garmin's own status when it sends one, else the ratio.
    # load_rising: the ratio alone, whatever the status says short of a
    # spike (Garmin always sends a status, so a rule that waited for a
    # missing one would never fire).
    status, acwr = ld.get("acwr_status"), ld.get("acwr")
    spike = False
    if status is not None:
        if status in ACWR_SPIKE_STATUSES:
            spike = True
            flags.append(_flag("load_spike", t, by_status=True))
    else:
        need(("load.acwr_status", None))
        if acwr is not None and acwr > t["acwr_red"]:
            spike = True
            flags.append(_flag("load_spike", t))
    if not spike and need(("load.acwr", acwr)) and acwr > t["acwr_yellow"]:
        flags.append(_flag("load_rising", t))

    # Garmin's calendar has a date but no clock time, so the design's
    # "recovery time longer than the time until the workout" is approximated
    # by "recovery_h or more hours left on a hard or long day". A workout
    # already completed no longer makes the day hard.
    if need(("today", today)) and _has_hard_or_long(_not_done(today)):
        recovery_hours = r.get("recovery_hours")
        if need(("recovery.recovery_hours", recovery_hours)) and recovery_hours >= t["recovery_h"]:
            flags.append(_flag("recovery_incomplete", t))
    return flags, missing


def level_of(flags: List[Dict[str, str]], recovery: Dict[str, Any]) -> str:
    if any(f.get("level") == "red" for f in flags):
        return "red"
    if recovery.get("sleep_hours") is None and recovery.get("readiness") is None:
        return "incomplete"
    yellows = sum(1 for f in flags if f.get("level") == "yellow")
    if yellows >= 3:
        return "red"
    return "yellow" if yellows else "green"


def recommend(level: str, flags: List[Dict[str, str]], today: Optional[List[Dict[str, Any]]]) -> str:
    """Recommendation code for the day (keep / ease / rest; never a decision).

    today=None means the calendar could not be read: the day is then treated
    as a hard one, the cautious reading, instead of "nothing planned". Only
    workouts not yet completed decide the hard-day column; when all of
    today's are completed the code is done_today.
    """
    if today is not None and not today:
        return "no_planned_workout"
    if today and all(w.get("completed") for w in today):
        return "done_today"
    if level == "incomplete":
        return "no_recovery_data"
    hard_day = today is None or _has_hard_or_long(_not_done(today))
    if level == "green":
        return "as_planned"
    if level == "yellow":
        if not hard_day:
            return "as_planned"
        yellows = sum(1 for f in flags if f.get("level") == "yellow")
        return "as_planned_may_stop" if yellows <= 1 else "zone2_or_shorten"
    return "short_zone2_or_rest" if hard_day else "shorten"


def recommendation_en(code: str, today: Optional[List[Dict[str, Any]]]) -> str:
    if code == "done_today" and len(today or []) > 1:
        return DONE_TODAY_PLURAL_EN
    return RECOMMENDATION_EN[code]


# --- assembling the status ----------------------------------------------------

def _path_value(status: Dict[str, Any], path: str) -> Any:
    return sc.dig(status, *path.split("."))


def daily_status_from(
    payloads: Dict[str, Any],
    *,
    day: dt.date,
    zone: ZoneInfo,
    now: dt.datetime,
    today: Optional[List[Dict[str, Any]]],
    tomorrow: Optional[List[Dict[str, Any]]],
    thresholds: Dict[str, Any] = THRESHOLDS,
    athlete: Optional[Dict[str, Any]] = None,
    athlete_needed: Tuple[str, ...] = (),
) -> Dict[str, Any]:
    """The daily status dict (no sources/debug/text) from raw payloads by source name.

    Sources: stats, sleep, sleep_prev, readiness, hrv, hrv_d1.., training_status,
    activities_prev (None = not fetched). today/tomorrow are classified
    workouts with "completed" settled (None = calendar unknown). athlete is
    athlete_thresholds(); athlete_needed lists the thresholds a range target
    needed (a null one is listed in "missing").
    """
    d0 = day.isoformat()
    sleep = extract_sleep(payloads.get("sleep"), d0, zone)
    hrv = extract_hrv(payloads.get("hrv"))
    stats = extract_stats(payloads.get("stats"))
    ready = extract_readiness(
        payloads.get("readiness"), d0, zone=zone, now=now, is_today=day == now.date(),
        device_id=primary_device_id(payloads.get("training_status")),
    )
    load = extract_load(payloads.get("training_status"))

    hrv_value = hrv["hrv"] if hrv["hrv"] is not None else sleep["overnight_hrv"]
    recovery = {
        "sleep_hours": sleep["sleep_hours"],
        "sleep_hhmm": sleep["sleep_hhmm"],
        "sleep_hours_prev": sleep_hours(payloads.get("sleep_prev"), (day - dt.timedelta(days=1)).isoformat()),
        "sleep_score": sleep["sleep_score"],
        "rem_min": sleep["rem_min"],
        "deep_min": sleep["deep_min"],
        "awake_min": sleep["awake_min"],
        "sleep_end_local": sleep["sleep_end_local"],
        "hrv": hrv_value,
        "hrv_7d": hrv["hrv_7d"],
        "hrv_delta": hrv_value - hrv["hrv_7d"] if hrv_value is not None and hrv["hrv_7d"] is not None else None,
        "hrv_status": hrv["hrv_status"],
        "hrv_range": hrv["hrv_range"],
        "rhr": stats["rhr"],
        "rhr_7d": stats["rhr_7d"],
        "rhr_delta": stats["rhr"] - stats["rhr_7d"] if stats["rhr"] is not None and stats["rhr_7d"] is not None else None,
        "readiness": ready["readiness"],
        "readiness_level": ready["readiness_level"],
        "recovery_hours": ready["recovery_hours"],
        "body_battery": stats["body_battery"],
        "stress_avg": stats["stress_avg"],
        "spo2_avg": sleep["spo2_avg"],
        "spo2_min": sleep["spo2_min"],
        "resp_sleep_avg": sleep["resp_sleep_avg"],
    }
    load_out = dict(load)
    load_out["readiness_acute_load"] = ready["acute_load"]
    load_out["yesterday"] = extract_sessions(payloads.get("activities_prev"))

    days = int(thresholds["hrv_trend_days"])
    trend = [hrv_trend_point(payloads.get("hrv"))]
    trend += [hrv_trend_point(payloads.get(f"hrv_d{i}")) for i in range(1, days)]
    flags, rule_missing = evaluate_rules(recovery, load_out, today, trend, thresholds)
    level = level_of(flags, recovery)
    code = recommend(level, flags, today)

    status: Dict[str, Any] = {
        "kind": "daily_status",
        "rules_version": RULES_VERSION,
        "date": d0,
        "weekday_en": sc.en_weekday(day),
        "generated_at": now.replace(microsecond=0).isoformat(),
        "tz": zone.key,
        "level": level,
        "level_en": LEVEL_EN[level],
        "recommendation": code,
        "recommendation_en": recommendation_en(code, today),
        "recovery": recovery,
        "load": load_out,
        "today": today,
        "tomorrow": tomorrow,
        "flags": flags,
        "missing": [],
        "thresholds": copy.deepcopy(thresholds),
        "thresholds_athlete": athlete_out(athlete),
    }
    missing = [p for p in TEXT_FIELDS if _path_value(status, p) is None]
    missing += [p for p in rule_missing if p not in missing]
    for key, workouts in (("today", today), ("tomorrow", tomorrow)):
        for i, w in enumerate(workouts or []):
            if w.get("structured") is None:
                missing.append(f"{key}[{i}].structured")
    for key, field in ATHLETE_FIELDS:
        if key in athlete_needed and (athlete or {}).get(key) is None:
            missing.append(f"thresholds_athlete.{field}")
    status["missing"] = missing
    return status


# --- debug payloads (include_sources) -----------------------------------------

_SLIM_SLEEP = {
    "dailySleepDTO": {
        "calendarDate": None, "sleepTimeSeconds": None, "deepSleepSeconds": None,
        "remSleepSeconds": None, "awakeSleepSeconds": None, "sleepEndTimestampLocal": None,
        "sleepEndTimestampGMT": None, "averageRespirationValue": None,
        "sleepScores": {"overall": {"value": None}},
    },
    "avgOvernightHrv": None,
    "wellnessSpO2SleepSummaryDTO": {"averageSpo2": None, "lowestSpo2": None},
}
_SLIM_HRV = {"hrvSummary": {
    "calendarDate": None, "lastNightAvg": None, "weeklyAvg": None, "status": None,
    "baseline": {"lowUpper": None, "balancedLow": None, "balancedUpper": None},
}}
_SLIM_ACTIVITIES = [{
    "activityType": {"typeKey": None}, "startTimeLocal": None, "duration": None, "distance": None,
    "averageHR": None, "maxHR": None, "activityTrainingLoad": None,
    "aerobicTrainingEffect": None, "anaerobicTrainingEffect": None,
}]
_SLIM_SPECS: Dict[str, Any] = {
    "stats": {
        "calendarDate": None, "restingHeartRate": None, "lastSevenDaysAvgRestingHeartRate": None,
        "bodyBatteryMostRecentValue": None, "averageStressLevel": None,
    },
    "sleep": _SLIM_SLEEP,
    "sleep_prev": _SLIM_SLEEP,
    "readiness": [{
        "calendarDate": None, "timestamp": None, "timestampLocal": None, "inputContext": None, "score": None,
        "level": None, "sleepScore": None,
        "recoveryTime": None, "recoveryTimeChangePhrase": None, "acuteLoad": None,
    }],
    "hrv": _SLIM_HRV,
    "schedule": {"data": {"workoutScheduleSummariesScalar": [{
        "scheduleDate": None, "workoutType": None, "estimatedDurationInSecs": None,
        "estimatedDistanceInMeters": None, "isRestDay": None,
    }]}},
    "activities_prev": _SLIM_ACTIVITIES,
    "activities_today": _SLIM_ACTIVITIES,
    # Body weight and power-to-weight are left out on purpose.
    "lactate_threshold": {
        "speed_and_heart_rate": {"calendarDate": None, "speed": None, "heartRate": None, "heartRateCycling": None},
        "power": {"calendarDate": None, "functionalThresholdPower": None, "sport": None, "isStale": None},
    },
    "cycling_ftp": {
        "calendarDate": None, "functionalThresholdPower": None, "sport": None, "isStale": None,
        "biometricSourceType": None,
    },
}
_SLIM_DEVICE = {
    "calendarDate": None, "primaryTrainingDevice": None, "trainingStatusFeedbackPhrase": None,
    "acuteTrainingLoadDTO": {
        "dailyTrainingLoadAcute": None, "dailyTrainingLoadChronic": None,
        "dailyAcuteChronicWorkloadRatio": None, "acwrStatus": None, "acwrPercent": None,
    },
}
# Curated workout keys that name or identify rather than describe the workout.
_WORKOUT_DROP_KEYS = frozenset({"id", "uuid", "name", "description", "created_date", "updated_date", "provider"})


def _slim(obj: Any, spec: Any) -> Any:
    """Keep only the spec's keys (a whitelist, so no identifier slips through)."""
    if spec is None:
        return None if isinstance(obj, (dict, list)) else obj
    if isinstance(spec, list):
        if isinstance(obj, dict):
            obj = [obj]
        return [_slim(x, spec[0]) for x in obj[:20]] if isinstance(obj, list) else None
    if not isinstance(obj, dict):
        return None
    return {k: _slim(obj[k], sub) for k, sub in spec.items() if k in obj}


def _drop_keys(obj: Any, drop: frozenset) -> Any:
    if isinstance(obj, dict):
        return {k: _drop_keys(v, drop) for k, v in obj.items() if k not in drop}
    if isinstance(obj, list):
        return [_drop_keys(x, drop) for x in obj]
    return obj


def safe_key_paths(raw: Any) -> List[str]:
    """status_common.key_paths with id-like keys masked.

    Garmin keys some maps by device id (latestTrainingStatusData.<id>), so a
    key path can carry an identifier.
    """
    out: List[str] = []
    for path in sc.key_paths(raw):
        parts = []
        for seg in path.split("."):
            core = seg.rstrip("[]")
            parts.append("<id>" + seg[len(core):] if _ID_SEGMENT_RE.match(core) else seg)
        masked = ".".join(parts)
        if masked not in out:
            out.append(masked)
    return out


def _debug_entry(name: str, raw: Any, curated: Any = None) -> Dict[str, Any]:
    if name.startswith("workout_"):
        slim = _drop_keys(curated, _WORKOUT_DROP_KEYS) if isinstance(curated, dict) else None
    elif name == "training_status":
        latest = sc.dig(raw, "mostRecentTrainingStatus", "latestTrainingStatusData")
        devices = [v for v in latest.values() if isinstance(v, dict) and v] if isinstance(latest, dict) else []
        slim = {"devices": len(devices), "selected": _slim(pick_training_device(latest), _SLIM_DEVICE)}
    else:
        spec = _SLIM_SPECS.get(name) or (_SLIM_HRV if name.startswith("hrv") else None)
        if isinstance(spec, dict) and isinstance(raw, list):
            spec = [spec]  # get_cycling_ftp may answer with a list of records
        slim = _slim(raw, spec) if spec is not None else None
    return {"slim": slim, "keys": safe_key_paths(raw)}


# --- Garmin calls (the adapter) -----------------------------------------------

def _day_activities_call(client: Any) -> Callable[[str], Any]:
    """The activity search of one local day, page by page: a full page of ACTIVITY_PAGE asks for the next
    one, up to ACTIVITY_PAGES pages, so a busy day is never cut silently at the first page. One source,
    one worker thread: the pages share its timeout."""
    search = sc.activities_call(client)

    def invoke(day: str) -> Any:
        out: List[Any] = []
        for page in range(ACTIVITY_PAGES):
            raw = search(params={"startDate": day, "endDate": day, "start": str(page * ACTIVITY_PAGE),
                                 "limit": str(ACTIVITY_PAGE)})
            if not isinstance(raw, list):
                return raw if page == 0 else out   # not a list (an empty 204 answer is {}): nothing more
            out.extend(raw)
            if len(raw) < ACTIVITY_PAGE:
                break
        return out

    invoke.__name__ = "connectapi"
    return invoke


def _workout_call(client: Any, entry: Dict[str, Any]) -> Optional[sc.Call]:
    workout_id = entry.get("workoutId")
    if isinstance(workout_id, int) and not isinstance(workout_id, bool) and workout_id > 0:
        return sc.call(sc.client_method(client, "get_workout_by_id"), workout_id)
    if isinstance(workout_id, str) and workout_id.isdigit():
        return sc.call(sc.client_method(client, "get_workout_by_id"), int(workout_id))
    uuid = _text(entry.get("workoutUuid"))
    if uuid:
        # Garmin Coach workouts have only a uuid; upstream's get_workout_by_id
        # reads them from this endpoint.
        return sc.call(sc.client_method(client, "connectapi"), f"workout-service/fbt-adaptive/{uuid}")
    return None


def _zone(name: Optional[str]) -> ZoneInfo:
    # sc.zone raises BadArgument with a one-line fix for an unset REPORT_TZ or
    # an unknown zone; get_daily_status returns it as "Error: ..." text.
    return sc.zone(name)


def _report_day(date: Any, zone: ZoneInfo, now: dt.datetime) -> dt.date:
    if date is None or not str(date).strip():
        return now.date()
    return sc.parse_date(str(date), zone)


# Key mode (GARMIN_AUTH=key): the sources Garmin's Personal API does not serve. They are never called; each
# source reads "skipped: not available with a Garmin Personal API key" (status_common.fetch_all(skip=)).
# Without the calendar, today and tomorrow are None, so no workout or threshold call follows and recommend()
# takes its cautious path.
KEY_MODE_SKIPS_PROBE = frozenset({"readiness"})
KEY_MODE_SKIPS_FULL = frozenset({"readiness", "training_status", "schedule"})


async def _probe(client: Any, day: dt.date, zone: ZoneInfo, now: dt.datetime, key_mode: bool = False):
    d0 = day.isoformat()
    calls = {
        "sleep": sc.call(sc.client_method(client, "get_sleep_data"), d0),
        "readiness": sc.call(sc.client_method(client, "get_training_readiness"), d0),
    }
    skip = KEY_MODE_SKIPS_PROBE if key_mode else frozenset()
    results = await sc.fetch_all(calls, first="sleep", timeout_s=CALL_TIMEOUT_S, deadline_s=DEADLINE_S, skip=skip)
    failed = sc.systemic_failure(results, "sleep")
    if failed:
        raise sc.SystemicFailure(failed)
    dto = night(sc.data_of(results.get("sleep")), d0)
    status = {
        "kind": "daily_probe",
        "date": d0,
        "generated_at": now.replace(microsecond=0).isoformat(),
        "tz": zone.key,
        "sleep_synced": dto is not None,
        "sleep_end_local": sleep_end_local(dto, zone),
        # The same row choice as the full status: a pre-sleep realtime row
        # alone is not this morning's readiness.
        # null in key mode: Garmin's Personal API has no readiness, so "not ready" would be wrong.
        "readiness_ready": None if key_mode else (
            morning_snapshot(readiness_snapshots(sc.data_of(results.get("readiness")), d0)) is not None),
        "sources": sc.source_meta(results, calls),
    }
    raws = {name: (sc.data_of(res), None) for name, res in results.items() if res.get("ok")}
    return status, raws


async def _full(client: Any, day: dt.date, zone: ZoneInfo, now: dt.datetime, started: float,
                key_mode: bool = False):
    d0 = day.isoformat()
    d_prev = (day - dt.timedelta(days=1)).isoformat()
    d_next = (day + dt.timedelta(days=1)).isoformat()
    calls: Dict[str, sc.Call] = {
        "stats": sc.call(sc.client_method(client, "get_stats"), d0),
        "sleep": sc.call(sc.client_method(client, "get_sleep_data"), d0),
        # Sleep is filed under the wake date: the night before is D-1's.
        "sleep_prev": sc.call(sc.client_method(client, "get_sleep_data"), d_prev),
        "readiness": sc.call(sc.client_method(client, "get_training_readiness"), d0),
        "hrv": sc.call(sc.client_method(client, "get_hrv_data"), d0),
    }
    for i in range(1, int(THRESHOLDS["hrv_trend_days"])):
        calls[f"hrv_d{i}"] = sc.call(sc.client_method(client, "get_hrv_data"), (day - dt.timedelta(days=i)).isoformat())
    calls["training_status"] = sc.call(sc.client_method(client, "get_training_status"), d0)
    calls["schedule"] = sc.call(sc.client_method(client, "query_garmin_graphql"), schedule_query(d0, d_next))
    calls["activities_prev"] = sc.call(_day_activities_call(client), d_prev)
    # Today's finished activities mark planned workouts as done.
    calls["activities_today"] = sc.call(_day_activities_call(client), d0)

    skip = KEY_MODE_SKIPS_FULL if key_mode else frozenset()
    results = await sc.fetch_all(calls, first="stats", timeout_s=CALL_TIMEOUT_S, deadline_s=DEADLINE_S, skip=skip)
    failed = sc.systemic_failure(results, "stats")
    if failed:
        raise sc.SystemicFailure(failed)
    payloads = {name: sc.data_of(res) for name, res in results.items()}

    entries = schedule_entries(payloads.get("schedule"))
    if entries is None and results.get("schedule", {}).get("ok"):
        # GraphQL errors arrive as HTTP 200 with "data": null: a failed source with the Garmin unavailable phrase
        # and one log line, like any other failed call.
        results["schedule"] = sc.unusable_answer(results["schedule"], "schedule", "GraphQL answer without data")
    planned: Dict[str, Optional[List[Dict[str, Any]]]] = {
        d0: None if entries is None else [e for e in entries if e.get("scheduleDate") == d0],
        d_next: None if entries is None else [e for e in entries if e.get("scheduleDate") == d_next],
    }

    # Second round: step structure of the planned workouts, within what is
    # left of the report deadline.
    curate = _upstream("workouts", "_curate_workout_details")
    detail_calls: Dict[str, sc.Call] = {}
    source_of: Dict[int, str] = {}   # id(entry) -> source name
    by_key: Dict[Any, str] = {}      # same workout twice -> one call
    for label, d in (("today", d0), ("tomorrow", d_next)):
        for i, entry in enumerate(planned[d] or [], 1):
            key = entry.get("workoutId") or entry.get("workoutUuid")
            if key is not None and key in by_key:
                source_of[id(entry)] = by_key[key]
                continue
            if curate is None or len(detail_calls) >= MAX_WORKOUT_DETAILS:
                continue
            spec = _workout_call(client, entry)
            if spec is None:
                continue
            name = f"workout_{label}_{i}"
            detail_calls[name] = spec
            source_of[id(entry)] = by_key[key] = name
    details: Dict[str, Dict[str, Any]] = {}
    if detail_calls:
        remaining = DEADLINE_S - (time.monotonic() - started)
        details = await sc.fetch_all(detail_calls, timeout_s=CALL_TIMEOUT_S, deadline_s=max(0.0, remaining))

    curated_by_source: Dict[str, Optional[Dict[str, Any]]] = {}
    for name, res in details.items():
        raw = sc.data_of(res)
        curated = None
        if isinstance(raw, dict) and raw:
            try:
                curated = curate(raw)
            except Exception:  # noqa: BLE001 - an unreadable workout is "details unavailable"
                curated = None
        curated_by_source[name] = curated

    def curated_of(entry: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        source = source_of.get(id(entry))
        return curated_by_source.get(source) if source else None

    # The athlete's thresholds, only when a range target of today's or
    # tomorrow's workouts needs them (known once the steps are read), within
    # what is left of the report deadline.
    needed = thresholds_needed([
        (workout_sport(e, curated_of(e)), curated_of(e))
        for d in (d0, d_next) for e in planned[d] or [] if curated_of(e) is not None
    ])
    threshold_calls: Dict[str, sc.Call] = {}
    if "lt_speed_mps" in needed or "lt_hr" in needed:
        threshold_calls["lactate_threshold"] = sc.call(sc.client_method(client, "get_lactate_threshold"))
    if "ftp_w" in needed:
        threshold_calls["cycling_ftp"] = sc.call(sc.client_method(client, "get_cycling_ftp"))
    fetched: Dict[str, Dict[str, Any]] = {}
    if threshold_calls:
        remaining = DEADLINE_S - (time.monotonic() - started)
        fetched = await sc.fetch_all(threshold_calls, timeout_s=CALL_TIMEOUT_S, deadline_s=max(0.0, remaining))
    athlete = athlete_thresholds(sc.data_of(fetched.get("lactate_threshold")), sc.data_of(fetched.get("cycling_ftp")))

    def classified(d: str) -> Optional[List[Dict[str, Any]]]:
        if planned[d] is None:
            return None
        out = []
        for entry in planned[d]:
            source = source_of.get(id(entry))
            if source is None:
                reason = "workout details not fetched"
            elif not details.get(source, {}).get("ok"):
                reason = "workout details unavailable"
            else:
                reason = "workout details unreadable"
            out.append(classify_workout(entry, curated_of(entry), athlete=athlete, unavailable=reason))
        # Tomorrow's activities are not fetched: only Garmin's own links count there.
        activities = completion_activities(payloads.get("activities_today")) if d == d0 else []
        mark_completed(out, planned[d], activities)
        return out

    status = daily_status_from(
        payloads, day=day, zone=zone, now=now, today=classified(d0), tomorrow=classified(d_next),
        athlete=athlete, athlete_needed=tuple(needed),
    )
    status["sources"] = {
        **sc.source_meta(results, calls), **sc.source_meta(details, detail_calls),
        **sc.source_meta(fetched, threshold_calls),
    }
    raws = {name: (sc.data_of(res), None) for name, res in results.items() if res.get("ok")}
    raws.update({
        name: (sc.data_of(res), curated_by_source.get(name))
        for name, res in details.items() if res.get("ok")
    })
    raws.update({name: (sc.data_of(res), None) for name, res in fetched.items() if res.get("ok")})
    return status, raws


async def build_daily_status(
    client: Any,
    date: str = "",
    *,
    tz: Optional[str] = None,
    include_sources: bool = False,
    probe: bool = False,
    text_format: str = "",
    now: Optional[dt.datetime] = None,
    key_mode: bool = False,
) -> Dict[str, Any]:
    """The daily status of `date` (default: today in tz, else in REPORT_TZ, which is required).

    client is upstream's Garmin proxy (or a fake): only its methods are
    called, in worker threads, under a per-call timeout and one deadline.
    A failed Garmin call does not raise: its fields are null, listed in
    "missing", and the source has ok=false with a fixed phrase. Raises
    BadArgument (a ValueError) only for bad arguments (a malformed date, an
    unknown text_format or time zone) and for an unset REPORT_TZ when tz is
    not given, and raises SystemicFailure when the first call fails
    systemically (login, rate limit, network, stall; no other call was made).

    key_mode (GARMIN_AUTH=key): the KEY_MODE_SKIPS_* sources are never called
    and the answer carries "key_mode_note" (status_common.KEY_MODE_NOTE), also
    as the last line of the markdown text. Argument checks do not change.
    """
    zone = _zone(tz)
    now = sc.local_now(zone, now)
    day = _report_day(date, zone, now)
    fmt = sc.text_format(text_format)
    started = time.monotonic()
    if probe:
        status, raws = await _probe(client, day, zone, now, key_mode)
    else:
        status, raws = await _full(client, day, zone, now, started, key_mode)
    if key_mode:
        status["key_mode_note"] = sc.KEY_MODE_NOTE
    if include_sources:
        # Key paths and slims hold Garmin strings (curated workout steps, payload key names); our answers skip
        # the tool-call wrapper's cleaning, so the subtree is cleaned here.
        debug = {name: _debug_entry(name, raw, curated) for name, (raw, curated) in raws.items()}
        status["debug"] = th.clean_json(debug)[0]
    if fmt:
        status["text"] = render_text(status, fmt)
    return status


# --- text ---------------------------------------------------------------------

class _Fmt:
    """Markdown escaping, bold and bullets for the report text."""

    def esc(self, text: Any) -> str:
        return sc.md_escape("" if text is None else str(text))

    def bold(self, text: Any) -> str:
        return f"**{self.esc(text)}**"

    def join(self, title: str, lines: List[str]) -> str:
        return "\n".join([title, ""] + [f"- {line}" for line in lines])


def _title_date(status: Dict[str, Any]) -> str:
    raw = status.get("date") or ""
    try:
        day = dt.date.fromisoformat(str(raw))
    except ValueError:
        return str(raw)
    return f"{sc.en_weekday(day, short=True)} {sc.en_date(day)}"


_NAME_MAX_CHARS = 40


def _short_name(name: Any, max_words: int) -> Optional[str]:
    """Workout name on one line: at most max_words words and ~40 characters."""
    text = _text(name)
    if not text or max_words <= 0:
        return None
    words = text.split()
    kept: List[str] = []
    for word in words[:max_words]:
        if kept and len(" ".join(kept + [word])) > _NAME_MAX_CHARS:
            break
        # Never ends inside an emoji sequence or a combining mark: no lone joiner, no half family or flag.
        kept.append(th.cut(word, _NAME_MAX_CHARS))
    short = " ".join(kept)
    return short if len(kept) == len(words) and short == " ".join(words) else short + "…"


def _sport_txt(item: Dict[str, Any]) -> str:
    sport = item.get("sport") or "other"
    if sport == "other":
        return _OTHER_TYPE_EN.get(str(item.get("type") or "").lower(), _SPORT_EN["other"])
    return _SPORT_EN.get(sport, _SPORT_EN["other"])


def _class_txt(w: Dict[str, Any]) -> str:
    hard, long_ = bool(w.get("hard")), bool(w.get("long"))
    if hard and long_:
        return "hard and long"
    if hard:
        return "hard"
    return "long" if long_ else "easy"


def _workout_txt(w: Dict[str, Any], name_words: int) -> str:
    parts = [_sport_txt(w)]
    if w.get("minutes") is not None:
        parts.append(f"{sc.num_txt(w['minutes'])} min")
    if w.get("km"):
        parts.append(f"{sc.num_txt(w['km'])} km")
    parts.append(_class_txt(w))
    if w.get("completed"):
        parts.append("done")
    name = sc.isolate(_short_name(w.get("name"), name_words))
    return f"{name} ({', '.join(parts)})" if name else ", ".join(parts)


def _session_txt(s: Dict[str, Any], detail: bool) -> str:
    text = _sport_txt(s)
    if s.get("minutes") is not None:
        text += f" {sc.num_txt(s['minutes'])} min"
    if detail and s.get("km"):
        text += f" {sc.num_txt(s['km'])} km"
    if detail and s.get("load") is not None:
        text += f", load {sc.num_txt(s['load'])}"
    return text


def _metric_vs_avg(label: str, value: Any, avg: Any) -> str:
    if value is None and avg is None:
        return f"{label}: {NO_VALUE}"
    return f"{label}: {sc.num_txt(value)} (7-day avg: {sc.num_txt(avg)})"


def _workouts_txt(workouts: List[Dict[str, Any]], name_words: int, max_items: int) -> str:
    shown = "; ".join(_workout_txt(w, name_words) for w in workouts[:max_items])
    extra = len(workouts) - max_items
    return shown + (f"; {extra} more" if extra > 0 else "")


def _daily_lines(status: Dict[str, Any], f: _Fmt, compact: int) -> Tuple[str, List[Tuple[str, str]]]:
    """Title and (key, line) pairs; compact 0..2 shortens names and lists."""
    rec = status.get("recovery") or {}
    load = status.get("load") or {}
    name_words = (6, 3, 0)[compact]
    max_items = (10, 3, 2)[compact]
    title = f.bold(f"Daily status, {_title_date(status)}")
    lines = [("level", f"Level: {f.bold(status.get('level_en') or LEVEL_EN.get(status.get('level'), NO_VALUE))}")]

    sleep = f"Sleep: {rec.get('sleep_hhmm') or NO_VALUE}"
    if rec.get("sleep_hhmm"):
        sleep += f" (score {sc.num_txt(rec.get('sleep_score'))})"
    lines.append(("sleep", f.esc(sleep)))
    lines.append(("hrv", f.esc(_metric_vs_avg("HRV", rec.get("hrv"), rec.get("hrv_7d")))))
    lines.append(("rhr", f.esc(_metric_vs_avg("Resting HR", rec.get("rhr"), rec.get("rhr_7d")))))

    ready = f"Readiness: {sc.num_txt(rec.get('readiness'))}"
    level_word = _READINESS_LEVEL_EN.get(rec.get("readiness_level") or "")
    if rec.get("readiness") is not None and level_word:
        ready += f" ({level_word})"
    hours = rec.get("recovery_hours")
    ready += f", recovery: {sc.num_txt(hours)}" + (" h" if hours is not None else "")
    lines.append(("readiness", f.esc(ready)))

    acwr_status = load.get("acwr_status")
    acwr = f"Load: ACWR {sc.num_txt(load.get('acwr'))}"
    acwr += f" ({_ACWR_STATUS_EN.get(acwr_status, acwr_status)})" if acwr_status else f" (status: {NO_VALUE})"
    lines.append(("load", f.esc(acwr)))

    yesterday = load.get("yesterday")
    if yesterday is None:
        lines.append(("yesterday", f.esc(f"Yesterday: {NO_VALUE}")))
    elif not yesterday:
        lines.append(("yesterday", f.esc("Yesterday: no workouts logged")))
    elif compact >= 2:
        count = len(yesterday)
        lines.append(("yesterday", f.esc(f"Yesterday: {count} workout{'' if count == 1 else 's'}")))
    else:
        lines.append(("yesterday", f.esc("Yesterday: " + "; ".join(_session_txt(s, compact == 0) for s in yesterday))))

    today = status.get("today")
    if today is None:
        lines.append(("today", f.esc(f"Today: {NO_VALUE}")))
    elif today:
        lines.append(("today", f.esc("Today: " + _workouts_txt(today, name_words, max_items))))
    rec_text = status.get("recommendation_en") or RECOMMENDATION_EN.get(status.get("recommendation"), NO_VALUE)
    lines.append(("recommendation", f"Recommendation: {f.bold(rec_text)}"))

    # The "tomorrow" line only when tomorrow holds a hard or long workout.
    hard_tomorrow = [w for w in status.get("tomorrow") or [] if w.get("class") == "hard_or_long"]
    if hard_tomorrow:
        lines.append(("tomorrow", f.esc("Tomorrow: " + _workouts_txt(hard_tomorrow, name_words, max_items))))

    labels = [fl.get("label_en") for fl in status.get("flags") or [] if fl.get("label_en")]
    lines.append(("flags", f.esc("Flags: " + ("; ".join(labels) if labels else "none"))))
    return title, lines


def _probe_lines(status: Dict[str, Any], f: _Fmt) -> Tuple[str, List[str]]:
    title = f.bold(_title_date(status))
    if status.get("sleep_synced"):
        end = status.get("sleep_end_local")
        sleep = "Sleep: synced" + (f", ended at {end}" if end else "")
    else:
        sleep = "Sleep: not synced from the watch yet"
    if status.get("readiness_ready") is None and status.get("key_mode_note"):
        ready = "Readiness: " + sc.NOT_WITH_KEY
    else:
        ready = "Readiness: " + ("has a score" if status.get("readiness_ready") else "no score yet")
    return title, [f.esc(sleep), f.esc(ready)]


def render_text(status: Dict[str, Any], fmt: str = "markdown") -> str:
    """Deterministic English text of a daily status (or probe) dict.

    fmt "markdown" (the only format): **bold**, "- " bullets, Markdown
    special characters escaped. At most MAX_WORDS words: names, yesterday's
    details and long workout lists are shortened when the full text would be
    longer.
    """
    if fmt not in TEXT_FORMATS:
        raise ValueError(f"fmt must be 'markdown', got {fmt!r}")
    f = _Fmt()
    # Key mode: the text ends with the note, and the note counts toward MAX_WORDS.
    note = status.get("key_mode_note")
    end = "\n\n" + f.esc(note) if note else ""
    if status.get("kind") == "daily_probe":
        return f.join(*_probe_lines(status, f)) + end
    for compact in (0, 1, 2):
        title, pairs = _daily_lines(status, f, compact)
        text = f.join(title, [line for _, line in pairs]) + end
        if sc.word_count(text) <= MAX_WORDS:
            return text
    # Not reachable with Garmin data (compact 2 has no names and at most two
    # workouts per line); kept so the limit holds whatever the input.
    for key in ("yesterday", "tomorrow", "today"):
        pairs = [(k, line) for k, line in pairs if k != key]
        text = f.join(title, [line for _, line in pairs]) + end
        if sc.word_count(text) <= MAX_WORDS:
            break
    return text


# --- tool registration and build-time check -----------------------------------

def register_tools(app: Any, get_client: Callable[[], Any], key_mode: bool = False) -> Any:
    """Register get_daily_status on upstream's (filtered) app.

    get_client returns upstream's configured Garmin client at call time.
    key_mode (GARMIN_AUTH=key) is passed to build_daily_status.
    """

    # server_loop: off mcp2_compat's shared tool loop, which upstream's async tools block for each Garmin
    # call; there that time would count against fetch_all's per-call timeout and the report deadline. Every
    # Garmin call here is awaited in a worker thread, so the server's loop stays free.
    @app.tool()
    @mcp2_compat.server_loop
    async def get_daily_status(
        date: str = "",
        include_sources: bool = False,
        probe: bool = False,
        text_format: str = "",
    ) -> str:
        """Today's training status: recovery, load, planned workouts, flags, a level and a recommendation.

        Every number, flag, the level (green / yellow / red / incomplete) and
        the recommendation are computed on the server by fixed rules
        (rules_version; thresholds are in the result). Report them as given:
        do not recompute, re-threshold or change them, and add no numbers
        that are not in the result. HRV and resting HR are compared with the
        athlete's own 7-day averages. null means Garmin has no value yet (the
        field is listed in "missing"): say so instead of guessing. The
        recommendation is advice, not a decision.

        Args:
            date: YYYY-MM-DD; empty means today in the server's time zone.
            include_sources: Add per-source payload key paths (automation and
                debugging only).
            probe: Only check whether last night's sleep and Training
                Readiness have synced (automation only).
            text_format: "markdown" adds a ready-made English summary in
                "text"; empty adds none.
        """
        # Only a bad argument and a failed first call answer as text here. Anything else is a bug: it
        # propagates to launch.py's tool-error wrapper, which logs the traceback under a ref and gives the
        # client a fixed text.
        try:
            status = await build_daily_status(
                get_client(), date,
                include_sources=include_sources, probe=probe, text_format=text_format, key_mode=key_mode,
            )
        except (sc.BadArgument, sc.SystemicFailure) as exc:
            return f"Error: {exc}"
        return sc.compact_json(status)

    return app


def selfcheck() -> None:
    """Fail at Docker build time if an upstream helper this module uses changed.

    Checks that the helpers import, that upstream's curated workout still has
    the keys the classifier reads, and that the Garmin client still has the
    methods build_daily_status calls.
    """
    from garmin_mcp.health_wellness import _hours_from_recovery_minutes as hours_fn
    from garmin_mcp.training import _lactate_threshold_speed_mps as lt_fn
    from garmin_mcp.workouts import _curate_workout_details
    import garminconnect

    if hours_fn(90, None) != 1.5 or hours_fn(90, "REACHED_ZERO") != 0.0:
        raise RuntimeError("daily_status: health_wellness._hours_from_recovery_minutes changed behaviour")
    if abs((sc.num(lt_fn(0.4)) or 0.0) - 4.0) > 1e-9:
        raise RuntimeError("daily_status: training._lactate_threshold_speed_mps changed (expected raw cm/ms x 10)")

    def step(order: int, kind: str, end: str, value: float, target: str, **extra: Any) -> Dict[str, Any]:
        return {
            "type": "ExecutableStepDTO", "stepOrder": order,
            "stepType": {"stepTypeKey": kind}, "endCondition": {"conditionTypeKey": end},
            "endConditionValue": value, "targetType": {"workoutTargetTypeKey": target}, **extra,
        }

    sample = {
        "workoutName": "selfcheck", "sportType": {"sportTypeKey": "running"},
        "estimatedDurationInSecs": 1800, "estimatedDistanceInMeters": 5000.0,
        "workoutSegments": [{"segmentOrder": 1, "sportType": {"sportTypeKey": "running"}, "workoutSteps": [
            step(1, "warmup", "time", 300.0, "no.target"),
            {"type": "RepeatGroupDTO", "stepOrder": 2, "stepType": {"stepTypeKey": "repeat"},
             "numberOfIterations": 3, "endCondition": {"conditionTypeKey": "iterations"},
             "endConditionValue": 3.0, "workoutSteps": [
                 step(3, "interval", "time", 120.0, "heart.rate.zone", zoneNumber=4,
                      secondaryTargetType={"workoutTargetTypeKey": "pace.zone"},
                      secondaryTargetValueOne=3.0, secondaryTargetValueTwo=3.3),
                 step(4, "recovery", "distance", 200.0, "pace.zone", targetValueOne=2.5, targetValueTwo=2.8),
             ]},
            step(5, "cooldown", "time", 300.0, "power.zone", targetValueOne=50.0, targetValueTwo=60.0,
                 targetValueUnit={"unitKey": "percent"}),
        ]}],
    }
    curated = _curate_workout_details(sample)
    leaves = list(leaf_steps(curated))
    interval = leaves[1][0] if len(leaves) == 4 else {}
    problems = []
    if curated.get("estimated_duration_seconds") != 1800 or curated.get("estimated_distance_meters") != 5000.0:
        problems.append("estimate keys")
    if [times for _, times in leaves] != [1, 3, 3, 1]:
        problems.append("repeat groups (steps / repeat_count)")
    if (interval.get("type"), interval.get("end_condition"), interval.get("end_condition_value")) != (
            "interval", "time", 120.0):
        problems.append("step type / end_condition / end_condition_value")
    if (interval.get("target_type"), interval.get("target_zone")) != ("heart.rate.zone", 4):
        problems.append("target_type / target_zone")
    if (interval.get("secondary_target_type"), interval.get("secondary_target_value_low"),
            interval.get("secondary_target_value_high")) != ("pace.zone", 3.0, 3.3):
        problems.append("secondary_target_*")
    if len(leaves) == 4 and (leaves[2][0].get("target_value_low"), leaves[2][0].get("target_value_high")) != (2.5, 2.8):
        problems.append("target_value_low / target_value_high")
    if len(leaves) == 4 and leaves[3][0].get("target_value_unit") != "percent":
        problems.append("target_value_unit")
    if problems:
        raise RuntimeError(
            "daily_status: workouts._curate_workout_details output changed: " + ", ".join(problems)
        )

    needed = ("get_stats", "get_sleep_data", "get_training_readiness", "get_hrv_data", "get_training_status",
              "query_garmin_graphql", "get_workout_by_id", "connectapi", "get_lactate_threshold", "get_cycling_ftp")
    absent = [name for name in needed if not callable(getattr(garminconnect.Garmin, name, None))]
    if absent:
        raise RuntimeError("daily_status: garminconnect.Garmin lacks " + ", ".join(absent))
