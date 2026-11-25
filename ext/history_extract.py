"""History search: the numbers stored per day and per week (numbers only).

Every value comes from the parsers the status tools already use
(daily_status.extract_sleep / extract_stats / extract_hrv, weekly_status
.parse_activities / parse_training_status and the weekly sections and rules),
so a history week and get_weekly_status agree on the same Garmin answers
(the parity test in test_history_extract.py).

What is stored is an allowlist (DAY_FIELDS, WEEK_FIELDS): numbers, Garmin
status codes checked against CODE_RE, the level, flag ids and "missing" paths.
Never names, notes, GPS, ids (userProfileId and similar are read only for the
account fingerprint and never stored), display names or raw payloads.
check_numbers() enforces the allowlist; history_store calls it on every
upsert, so a field outside it never reaches the database.

Key mode (GARMIN_AUTH=key) has no training status, so the week's load and
fitness fields are masked (null), never zero (mask_for_mode).

A day is "empty" when no source gave a value (no watch worn, before the
account started, or not synced yet). Only an empty day whose every source
answered (no failed call, is_answered_empty) counts toward the backfill stop.
Days are final FINAL_AFTER_DAYS after their date (late syncs). Standard
library only.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional

import daily_status as ds
import status_common as sc
import weekly_status as ws

# Bump when the stored day/week numbers change shape (rebuild job reruns).
NUMBERS_VERSION = 1
# History's own rules version (day part, week part), not the status tools' RULES_VERSION: a change in it
# recomputes and re-embeds all of history (history_jobs.rebuild), so bump it only when a stored number, flag or
# level changes. test_history_extract.StoredNumbersPinned fails when they change without a bump. It started as
# (daily, weekly) = (1, 2); weekly rules v3 changed plan completion only, which history does not store.
RULES_VERSION = (1, 2)

FINAL_AFTER_DAYS = 3          # a day is final 3 days after its date (late syncs)
EMPTY_STOP_DAYS = 14          # backfill stops after 14 answered empty days in a row (account start)
HISTORY_WEEKS = 52            # owner: 1 year of history
HRV_LOOKBACK_DAYS = 21        # the oldest week's 28-day HRV average needs 3 weeks before it
BACKFILL_DAYS = HISTORY_WEEKS * 7 + HRV_LOOKBACK_DAYS
RETENTION_DAYS = BACKFILL_DAYS + 7
ACTIVITY_LIMIT = 100          # one day's activity page; a full page marks the day's activities missing

MODES = ("full", "key")
SPORTS = ws.SPORTS            # run, bike, swim, other
SOURCES = ("stats", "sleep", "hrv", "activities")
LEVELS = ("green", "yellow", "red", "incomplete")
WEEK_FLAGS = ("hrv_down", "short_nights", "volume_jump", "load_high")
DAY_FLAGS = tuple(ds.FLAG_LEVELS)

# Garmin status strings: upper-case words only (HRV status, ACWR status, training status without its _N suffix).
CODE_RE = re.compile(r"^[A-Z_]{1,40}$")
MISSING_RE = re.compile(r"^[a-z0-9_.]{1,40}$")

INT, FLOAT, CODE, MODE, LEVEL, FLAGS, MISSING = "int", "float", "code", "mode", "level", "flags", "missing"

DAY_FIELDS: Dict[str, str] = {
    "mode": MODE,
    "sleep_seconds": INT, "sleep_score": INT, "deep_min": INT, "rem_min": INT, "awake_min": INT,
    "spo2_avg": INT, "spo2_min": INT, "resp_sleep_avg": FLOAT, "overnight_hrv": INT,
    "hrv": INT, "hrv_7d": INT, "hrv_status": CODE, "hrv_low_upper": INT, "hrv_balanced_low": INT,
    "hrv_balanced_upper": INT,
    "rhr": INT, "rhr_7d": INT, "stress_avg": INT, "body_battery_high": INT, "body_battery_low": INT,
    "sessions": INT,
    "missing": MISSING,
}
for _sport in SPORTS:
    DAY_FIELDS.update({f"{_sport}_sessions": INT, f"{_sport}_seconds": FLOAT, f"{_sport}_meters": FLOAT})

WEEK_FIELDS: Dict[str, str] = {
    "mode": MODE, "rules_version": INT, "days_with_data": INT,
    "hrv_avg_7d": FLOAT, "hrv_avg_28d": FLOAT, "hrv_delta_ms": FLOAT, "hrv_status": CODE,
    "hrv_balanced_low": FLOAT, "hrv_balanced_upper": FLOAT, "hrv_weeks_in_avg_28d": INT,
    "sleep_avg_h": FLOAT, "sleep_nights": INT, "nights_under_6h": INT, "sleep_avg_score": FLOAT,
    "rhr_avg": FLOAT, "stress_avg": FLOAT, "body_battery_high_avg": FLOAT,
    "total_hours": FLOAT, "prev_total_hours": FLOAT, "change_pct": FLOAT, "sessions": INT,
    "load_acute": FLOAT, "load_chronic": FLOAT, "acwr": FLOAT, "acwr_status": CODE,
    "load_chronic_7d_ago": FLOAT, "load_chronic_change": FLOAT, "load_optimal_min": FLOAT,
    "load_optimal_max": FLOAT, "vo2max_run": FLOAT, "vo2max_bike": FLOAT, "training_status": CODE,
    "training_balance": CODE,
    "level": LEVEL, "flags": FLAGS, "missing": MISSING,
}
for _sport in SPORTS:
    WEEK_FIELDS.update({f"{_sport}_hours": FLOAT, f"{_sport}_km": FLOAT, f"{_sport}_sessions": INT})

# Fields only full mode can fill (Garmin's Personal API has no training status).
FULL_ONLY_WEEK = ("load_acute", "load_chronic", "acwr", "acwr_status", "load_chronic_7d_ago",
                  "load_chronic_change", "load_optimal_min", "load_optimal_max", "vo2max_run", "vo2max_bike",
                  "training_status", "training_balance")
FULL_ONLY_DAY: tuple = ()
# Week fields kept from the stored training-status answer when a week is rebuilt without Garmin calls.
STORED_LOAD_FIELDS = ("load_acute", "load_chronic", "acwr", "acwr_status", "load_optimal_min", "load_optimal_max",
                      "vo2max_run", "vo2max_bike", "training_status", "training_balance")

# The source each day field comes from (merge_day: an answered source replaces its fields, a failed one keeps them).
FIELD_SOURCE: Dict[str, str] = {
    **dict.fromkeys(("sleep_seconds", "sleep_score", "deep_min", "rem_min", "awake_min", "spo2_avg", "spo2_min",
                     "resp_sleep_avg", "overnight_hrv"), "sleep"),
    **dict.fromkeys(("hrv", "hrv_7d", "hrv_status", "hrv_low_upper", "hrv_balanced_low", "hrv_balanced_upper"),
                    "hrv"),
    **dict.fromkeys(("rhr", "rhr_7d", "stress_avg", "body_battery_high", "body_battery_low"), "stats"),
    "sessions": "activities",
}
for _sport in SPORTS:
    FIELD_SOURCE.update(dict.fromkeys((f"{_sport}_sessions", f"{_sport}_seconds", f"{_sport}_meters"), "activities"))

_METRIC_DAY_FIELDS = tuple(k for k in DAY_FIELDS if k not in ("mode", "missing", "sessions")
                           and not k.startswith(tuple(f"{s}_" for s in SPORTS)))


class NotAllowed(ValueError):
    """A field or value outside the allowlist (the message never holds the value)."""


# --- allowlist ------------------------------------------------------------------------------------------------

def code(value: Any) -> Optional[str]:
    """A Garmin status code, upper-cased, with a trailing _<digits> variant removed, or None when it is not
    CODE_RE (anything else Garmin sends there is dropped, never stored)."""
    if not isinstance(value, str):
        return None
    text = re.sub(r"_\d+$", "", value.strip().upper())
    return text if CODE_RE.fullmatch(text) else None


def _check_value(kind: str, value: Any, known_flags: Iterable[str]) -> bool:
    if value is None:
        return True
    if kind == INT:
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == FLOAT:
        return sc.num(value) is not None and abs(value) < 1e9
    if kind == CODE:
        return isinstance(value, str) and CODE_RE.fullmatch(value) is not None
    if kind == MODE:
        return value in MODES
    if kind == LEVEL:
        return value in LEVELS
    if kind == FLAGS:
        known = set(known_flags)
        return isinstance(value, list) and all(isinstance(v, str) and v in known for v in value)
    if kind == MISSING:
        return isinstance(value, list) and len(value) <= 64 and all(
            isinstance(v, str) and MISSING_RE.fullmatch(v) for v in value)
    return False


def check_numbers(kind: str, numbers: Mapping[str, Any]) -> Dict[str, Any]:
    """A copy of numbers when every field is in the allowlist of kind ("day" or "week") with a value of its
    type; raises NotAllowed otherwise. The message names neither the field's value nor the field."""
    if kind == "day":
        fields, flags = DAY_FIELDS, ()
    elif kind == "week":
        fields, flags = WEEK_FIELDS, WEEK_FLAGS
    else:
        raise NotAllowed("history: unknown record kind")
    if not isinstance(numbers, Mapping):
        raise NotAllowed("history: numbers must be a mapping")
    out: Dict[str, Any] = {}
    for key, value in numbers.items():
        if key not in fields:
            raise NotAllowed("history: a field is not in the allowlist")
        if not _check_value(fields[key], value, flags):
            raise NotAllowed("history: a value has the wrong type")
        out[key] = list(value) if isinstance(value, list) else value
    return out


def mask_for_mode(kind: str, numbers: Dict[str, Any], mode: str) -> Dict[str, Any]:
    """Key mode: the full-only fields of kind set to null (masked, never zero)."""
    out = dict(numbers)
    if mode == "key":
        for key in FULL_ONLY_WEEK if kind == "week" else FULL_ONLY_DAY:
            if key in out:
                out[key] = None
    return out


# --- per day --------------------------------------------------------------------------------------------------

def _int(value: Any) -> Optional[int]:
    v = sc.num(value)
    return int(round(v)) if v is not None else None


def _activity_list(raw: Any) -> Optional[list]:
    if isinstance(raw, dict):
        raw = raw.get("activityList") or raw.get("activities") or []
    return raw if isinstance(raw, list) else None


def profile_id(payloads: Mapping[str, Any]) -> Optional[str]:
    """The Garmin profile id from the day's answers (for the account fingerprint only; never stored)."""
    for value in (sc.dig(payloads.get("stats"), "userProfileId"),
                  sc.dig(payloads.get("sleep"), "dailySleepDTO", "userProfilePK"),
                  sc.dig(payloads.get("hrv"), "userProfilePk")):
        if isinstance(value, bool) or value is None:
            continue
        if isinstance(value, (int, str)) and str(value).strip():
            return str(value).strip()
    return None


def day_numbers(day: dt.date, payloads: Mapping[str, Any], tz: Any, mode: str) -> Dict[str, Any]:
    """One day's numbers from its raw answers: payloads[source] for SOURCES, None when that call failed.

    "missing" lists the failed sources only. Garmin's "no data" for a source is an answer: the caller passes it
    as an empty answer ({} / []), not None, so a day where every source answered with nothing has "missing" == []
    (is_answered_empty) on the fetch path and in the stored row alike. Rows stored before this rule may still
    list such a source; they read as not answered empty, the safe side (they never stop the backfill).

    sleep: the night filed under `day`; hrv / stats: Garmin's per-day answers; activities: the activity list
    of `day` (activities that start on another local date are dropped).
    """
    d0 = day.isoformat()
    out: Dict[str, Any] = dict.fromkeys(DAY_FIELDS)
    out["mode"] = mode if mode in MODES else None
    missing: List[str] = [s for s in SOURCES if payloads.get(s) is None]

    sleep_raw = payloads.get("sleep")
    sleep = ds.extract_sleep(sleep_raw, d0, tz)
    dto = ds.night(sleep_raw, d0)
    out["sleep_seconds"] = _int(dto.get("sleepTimeSeconds")) if dto else None
    for key in ("sleep_score", "deep_min", "rem_min", "awake_min", "spo2_avg", "spo2_min", "overnight_hrv"):
        out[key] = sleep[key]
    out["resp_sleep_avg"] = sleep["resp_sleep_avg"]

    hrv = ds.extract_hrv(payloads.get("hrv"))
    out["hrv"], out["hrv_7d"], out["hrv_status"] = hrv["hrv"], hrv["hrv_7d"], code(hrv["hrv_status"])
    rng = hrv["hrv_range"] or {}
    out["hrv_low_upper"] = rng.get("low_upper")
    out["hrv_balanced_low"] = rng.get("balanced_low")
    out["hrv_balanced_upper"] = rng.get("balanced_upper")

    stats_raw = payloads.get("stats")
    stats = ds.extract_stats(stats_raw)
    out["rhr"], out["rhr_7d"], out["stress_avg"] = stats["rhr"], stats["rhr_7d"], stats["stress_avg"]
    p = stats_raw if isinstance(stats_raw, dict) else {}
    out["body_battery_high"] = ds._nonneg_int(p.get("bodyBatteryHighestValue"))
    out["body_battery_low"] = ds._nonneg_int(p.get("bodyBatteryLowestValue"))

    acts_raw = payloads.get("activities")
    items = _activity_list(acts_raw)
    if acts_raw is not None and (items is None or len(items) >= ACTIVITY_LIMIT):
        if "activities" not in missing:
            missing.append("activities")
        items = None
    if items is not None:
        acts = [a for a in ws.parse_activities(items, tz) if a["date"] == day]
        out["sessions"] = len(acts)
        for sport in SPORTS:
            mine = [a for a in acts if a["sport"] == sport]
            out[f"{sport}_sessions"] = len(mine)
            out[f"{sport}_seconds"] = round(sum(a["seconds"] for a in mine), 1)
            out[f"{sport}_meters"] = round(sum(a["meters"] for a in mine), 1)
    out["missing"] = missing
    return check_numbers("day", mask_for_mode("day", out, mode))


def is_empty_day(numbers: Mapping[str, Any]) -> bool:
    """True when no source gave a value (no metric and no session)."""
    if any(numbers.get(k) is not None for k in _METRIC_DAY_FIELDS):
        return False
    return not numbers.get("sessions")


def is_answered_empty(numbers: Mapping[str, Any]) -> bool:
    """True for an empty day whose every source answered ("missing" is empty): no watch data that day for sure.
    A day with a failed source (Garmin down, a network error) is never this, so it cannot stop the backfill."""
    return not (numbers.get("missing") or []) and is_empty_day(numbers)


def is_final_day(day: dt.date, today: dt.date) -> bool:
    return day <= today - dt.timedelta(days=FINAL_AFTER_DAYS)


def week_start(day: dt.date) -> dt.date:
    """Monday of the week that holds day (weeks run Monday to Sunday)."""
    return day - dt.timedelta(days=day.weekday())


def load_numbers(raw: Any, mode: str) -> Dict[str, Any]:
    """The stored load/fitness fields from one get_training_status answer (full mode; all null in key mode)."""
    if mode != "full":
        return dict.fromkeys(STORED_LOAD_FIELDS)
    ts = ws.parse_training_status(raw, None)
    return {
        "load_acute": ts["acute"], "load_chronic": ts["chronic"], "acwr": ts["acwr"],
        "acwr_status": code(ts["acwr_status"]), "load_optimal_min": ts["optimal_min"],
        "load_optimal_max": ts["optimal_max"], "vo2max_run": ts["vo2max_run"], "vo2max_bike": ts["vo2max_bike"],
        "training_status": code(ts["training_status"]), "training_balance": code(ts["training_balance"]),
    }


# --- per week -------------------------------------------------------------------------------------------------

def _mean(values: Iterable[Any], ndigits: int = 1) -> Optional[float]:
    got = [v for v in values if sc.num(v) is not None]
    return round(sum(got) / len(got), ndigits) if got else None


def _pseudo_activities(days: Mapping[dt.date, Mapping[str, Any]], first: dt.date, last: dt.date) -> List[dict]:
    """weekly_status.volume_section's input rebuilt from per-day totals: one entry per session, the day's
    sport totals on the first entry, so the counts and the sums are the ones Garmin's list gives."""
    acts: List[dict] = []
    day = first
    while day <= last:
        n = days.get(day) or {}
        for sport in SPORTS:
            count = n.get(f"{sport}_sessions") or 0
            for i in range(count):
                acts.append({"date": day, "sport": sport,
                             "seconds": float(n.get(f"{sport}_seconds") or 0.0) if i == 0 else 0.0,
                             "meters": float(n.get(f"{sport}_meters") or 0.0) if i == 0 else 0.0})
        day += dt.timedelta(days=1)
    return acts


def _activities_known(days: Mapping[dt.date, Mapping[str, Any]], first: dt.date, last: dt.date) -> bool:
    day = first
    while day <= last:
        n = days.get(day)
        if n is None or "activities" in (n.get("missing") or []) or n.get("sessions") is None:
            return False
        day += dt.timedelta(days=1)
    return True


def week_numbers(start: dt.date, days: Mapping[dt.date, Mapping[str, Any]], *, mode: str,
                 load: Optional[Mapping[str, Any]] = None, chronic_week_ago: Optional[float] = None,
                 end: Optional[dt.date] = None) -> Dict[str, Any]:
    """A week's numbers from stored day numbers (start .. end, default start + 6).

    days maps date -> day numbers and should hold end-27 .. end (the 28-day HRV average reads the days end,
    end-7, end-14, end-21; the volume change reads the 7 days before start). load is load_numbers() of the
    training status on end (full mode), chronic_week_ago the stored load_chronic of the week before.
    Flags and level come from weekly_status.evaluate_flags / compute_level over the HRV, sleep, volume and load
    sections (plan, gear and the long session are not part of history).
    """
    end = end or start + dt.timedelta(days=6)

    def hrv_point(day: dt.date) -> Dict[str, Any]:
        n = days.get(day) or {}
        return {"weekly_avg": n.get("hrv_7d"), "status": n.get("hrv_status"),
                "balanced_low": n.get("hrv_balanced_low"), "balanced_upper": n.get("hrv_balanced_upper")}

    hrv = ws.hrv_section([hrv_point(end - dt.timedelta(days=b)) for b in ws.HRV_WEEKS])
    week_days = [start + dt.timedelta(days=i) for i in range((end - start).days + 1)]
    nights = []
    for day in week_days:
        n = days.get(day) or {}
        secs = n.get("sleep_seconds")
        nights.append({"hours": secs / 3600.0, "score": n.get("sleep_score")} if secs else None)
    sleep = ws.sleep_section(nights)

    prev_start = start - dt.timedelta(days=7)
    this_known = _activities_known(days, start, end)
    prev_known = _activities_known(days, prev_start, start - dt.timedelta(days=1))
    acts = _pseudo_activities(days, prev_start, end) if this_known else None
    volume = ws.volume_section(acts, start, end)
    if this_known and not prev_known:
        volume["prev_total_hours"] = None
        volume["change_pct"] = None

    load = dict(load or dict.fromkeys(STORED_LOAD_FIELDS)) if mode == "full" else dict.fromkeys(STORED_LOAD_FIELDS)
    ts_now = {"acute": load.get("load_acute"), "chronic": load.get("load_chronic"), "acwr": load.get("acwr"),
              "acwr_status": load.get("acwr_status"), "optimal_min": load.get("load_optimal_min"),
              "optimal_max": load.get("load_optimal_max")}
    load_sec = ws.load_section(ts_now, {"chronic": chronic_week_ago if mode == "full" else None})

    status = {"hrv": hrv, "sleep": sleep, "volume": volume, "load": load_sec, "plan": {}, "gear": []}
    rule_flags = [f for f in ws.evaluate_flags(status) if f["id"] in WEEK_FLAGS]
    flags = [f["id"] for f in rule_flags]
    volume_missing = acts is None
    level = ws.compute_level(rule_flags, hrv_missing=hrv["avg_7d"] is None, sleep_missing=not sleep["nights"],
                             volume_missing=volume_missing)

    rows = [days.get(d) or {} for d in week_days]
    out: Dict[str, Any] = {
        "mode": mode, "rules_version": RULES_VERSION[1],
        "days_with_data": sum(1 for n in rows if n and not is_empty_day(n)),
        "hrv_avg_7d": hrv["avg_7d"], "hrv_avg_28d": hrv["avg_28d"], "hrv_delta_ms": hrv["delta_ms"],
        "hrv_status": code(hrv["status"]), "hrv_balanced_low": hrv["balanced_low"],
        "hrv_balanced_upper": hrv["balanced_upper"], "hrv_weeks_in_avg_28d": hrv["weeks_in_avg_28d"],
        "sleep_avg_h": sleep["avg_h"], "sleep_nights": sleep["nights"], "nights_under_6h": sleep["nights_under_6h"],
        "sleep_avg_score": sleep["avg_score"],
        "rhr_avg": _mean(n.get("rhr") for n in rows), "stress_avg": _mean(n.get("stress_avg") for n in rows),
        "body_battery_high_avg": _mean(n.get("body_battery_high") for n in rows),
        "total_hours": volume["total_hours"], "prev_total_hours": volume["prev_total_hours"],
        "change_pct": volume["change_pct"],
        "sessions": sum(volume[s]["sessions"] for s in SPORTS) if not volume_missing else None,
        "load_acute": load_sec["acute"], "load_chronic": load_sec["chronic"], "acwr": load_sec["acwr"],
        "acwr_status": code(load_sec["acwr_status"]), "load_chronic_7d_ago": load_sec["chronic_7d_ago"],
        "load_chronic_change": load_sec["chronic_change"], "load_optimal_min": load_sec["optimal_min"],
        "load_optimal_max": load_sec["optimal_max"],
        "vo2max_run": sc.rnd(load.get("vo2max_run"), 1), "vo2max_bike": sc.rnd(load.get("vo2max_bike"), 1),
        "training_status": code(load.get("training_status")), "training_balance": code(load.get("training_balance")),
        "level": level, "flags": flags,
    }
    for sport in SPORTS:
        block = volume[sport]
        out[f"{sport}_hours"], out[f"{sport}_km"], out[f"{sport}_sessions"] = (
            block["hours"], block["km"], block["sessions"])
    missing = [f"hrv.{k}" for k in ("avg_7d", "avg_28d", "status") if hrv[k] is None]
    if not sleep["nights"]:
        missing.append("sleep")
    if volume_missing:
        missing.append("volume")
    elif volume["prev_total_hours"] is None:
        missing.append("volume.prev_total_hours")
    if mode == "key":
        missing.append("load")
    else:
        missing += [f"load.{k}" for k in ("acute", "chronic", "acwr", "acwr_status") if load_sec[k] is None]
    missing += ["plan", "gear", "long_session"]
    out["missing"] = missing
    return check_numbers("week", mask_for_mode("week", out, mode))


def day_flags(numbers: Mapping[str, Any], prev: Optional[Mapping[str, Any]] = None,
              prev2: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """Daily rules v1 (daily_status.evaluate_rules / level_of) over stored day numbers.

    prev / prev2 are the numbers of the 1 and 2 days before (sleep debt, the 3-day HRV trend). No readiness,
    load or workouts are stored, so those rules are skipped. -> {"flags": [ids], "level": level}.
    """
    prev, prev2 = prev or {}, prev2 or {}
    hrv_value = numbers.get("hrv") if numbers.get("hrv") is not None else numbers.get("overnight_hrv")
    secs, prev_secs = numbers.get("sleep_seconds"), prev.get("sleep_seconds")
    recovery = {
        "hrv": hrv_value, "hrv_7d": numbers.get("hrv_7d"),
        "hrv_range": {"balanced_low": numbers.get("hrv_balanced_low")},
        "rhr": numbers.get("rhr"), "rhr_7d": numbers.get("rhr_7d"),
        "sleep_hours": round(secs / 3600, 2) if secs else None,
        "sleep_hours_prev": round(prev_secs / 3600, 2) if prev_secs else None,
        "readiness": None, "recovery_hours": None,
    }

    def point(n: Mapping[str, Any]):
        w, low = n.get("hrv_7d"), n.get("hrv_balanced_low")
        return (w, low) if w is not None and low is not None else None

    trend = [point(numbers), point(prev), point(prev2)]
    flags, _missing = ds.evaluate_rules(recovery, {}, None, trend)
    ids = [f["id"] for f in flags if f["id"] in DAY_FLAGS]
    return {"flags": ids, "level": ds.level_of(flags, recovery)}
