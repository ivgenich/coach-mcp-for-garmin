"""History search: the documents LangChain indexes, one per day and per week.

Each document is a fixed-template English text over the stored numbers, a
list of tags from fixed rules, and metadata (dates, level, flags, tags and the
numbers). No model writes any of it and no free text from Garmin is in it:
every word comes from this file, every value from history_extract's
allowlist. Numbers appear in the text only as bands ("short", "up a lot"),
so keyword search and the embedder get words; exact numbers are returned
from the metadata, never from the text.

documents are plain dicts {"id", "page_content", "metadata"}; to_langchain()
turns one into a langchain_core Document (imported only there). doc_id() is a
UUID5 of (kind, start), so re-indexing a day or week replaces its vector.
Standard library only.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, Dict, List, Mapping, Optional, Sequence

import daily_status as ds
import history_extract as hx
import history_store as hs
import status_common as sc

TEXT_VERSION = 1
COLLECTION_NAME = hs.COLLECTION_NAME
DOC_NAMESPACE = uuid.UUID("6f3c2a4e-9b1d-5c7e-8a2f-0d4b6e8c1a3f")

WEEK_TAGS = (
    "run_heavy", "bike_heavy", "swim_heavy", "volume_jump", "volume_drop", "taper", "rest_week", "no_training",
    "load_high", "load_low", "hrv_down", "hrv_low", "sleep_debt", "sleep_good", "high_stress", "recovery_low",
    "gap",
)
DAY_TAGS = (
    "short_sleep", "sleep_debt", "long_sleep", "low_hrv", "hrv_low_trend", "high_rhr", "high_stress",
    "rest_day", "run_day", "bike_day", "swim_day", "other_day", "long_session", "double_day", "partial",
)
TAG_RULES = {
    "heavy_share": 0.6,        # <sport>_heavy: the sport is >= 60% of the week's hours ...
    "heavy_min_h": 4,          # ... and at least 4 h
    "volume_drop_pct": -30,    # volume_drop: change_pct <= -30 with prev_total_hours >= 3
    "taper_ratio": 0.6,        # taper: hours <= 60% of the mean of the 2-4 weeks before, level not red
    "rest_week_h": 2,          # rest_week: under 2 h of training
    "sleep_good_h": 7.5,       # sleep_good: average >= 7.5 h over >= 5 nights
    "high_stress": 51,         # Garmin's "high" stress band starts at 51
    "gap_days": 4,             # gap: fewer than 4 days with data or nights synced
    "long_sleep_h": 8,
}


def doc_id(kind: str, start: Any) -> str:
    day = start.isoformat() if isinstance(start, (dt.date, dt.datetime)) else str(start)
    return str(uuid.uuid5(DOC_NAMESPACE, f"{kind}:{day}"))


# --- bands (words, never exact numbers) -----------------------------------------------------------------------

def _hours_band(hours: Optional[float]) -> str:
    if hours is None:
        return "unknown"
    if hours < 2:
        return "very light"
    if hours < 5:
        return "light"
    if hours < 9:
        return "moderate"
    if hours < 13:
        return "heavy"
    return "very heavy"


def _change_band(pct: Optional[float]) -> str:
    if pct is None:
        return "change unknown"
    if pct >= 30:
        return "up a lot"
    if pct >= 10:
        return "up"
    if pct > -10:
        return "stable"
    if pct > -30:
        return "down"
    return "down a lot"


def _sleep_band(hours: Optional[float]) -> str:
    if hours is None:
        return "unknown"
    if hours < 6:
        return "short"
    if hours < 7:
        return "a little short"
    if hours < 8:
        return "normal"
    return "long"


def _hrv_week_band(delta: Optional[float]) -> str:
    if delta is None:
        return "trend unknown"
    if delta <= -5:
        return "below its 28-day average"
    if delta >= 5:
        return "above its 28-day average"
    return "near its 28-day average"


_ACWR_WORDS = {"LOW": "low", "OPTIMAL": "optimal", "HIGH": "high", "VERY_HIGH": "very high", "NONE": "none"}


def _words(ids: Sequence[str]) -> str:
    return ", ".join(ids) if ids else "none"


# --- tags -----------------------------------------------------------------------------------------------------

def week_tags(n: Mapping[str, Any], before: Sequence[Mapping[str, Any]] = ()) -> List[str]:
    """Tags of a week from its numbers and the numbers of up to 4 weeks before it (newest first)."""
    t = TAG_RULES
    tags = set()
    total = n.get("total_hours")
    if total is not None:
        for sport in ("run", "bike", "swim"):
            hours = n.get(f"{sport}_hours") or 0
            if total > 0 and hours >= t["heavy_min_h"] and hours >= t["heavy_share"] * total:
                tags.add(f"{sport}_heavy")
        if total < t["rest_week_h"]:
            tags.add("rest_week")
        if n.get("sessions") == 0 and (n.get("days_with_data") or 0) >= t["gap_days"]:
            tags.add("no_training")
        prior = [w.get("total_hours") for w in list(before)[:4] if w.get("total_hours") is not None]
        if len(prior) >= 2 and total <= t["taper_ratio"] * (sum(prior) / len(prior)) and n.get("level") != "red":
            tags.add("taper")
    pct, prev = n.get("change_pct"), n.get("prev_total_hours")
    if pct is not None and prev is not None and prev >= 3 and pct <= t["volume_drop_pct"]:
        tags.add("volume_drop")
    flags = n.get("flags") or []
    for flag in ("volume_jump", "hrv_down", "load_high"):
        if flag in flags:
            tags.add(flag)
    if "short_nights" in flags:
        tags.add("sleep_debt")
    if n.get("acwr_status") == "LOW":
        tags.add("load_low")
    if n.get("hrv_status") in ("LOW", "POOR"):
        tags.add("hrv_low")
    avg, nights = n.get("sleep_avg_h"), n.get("sleep_nights") or 0
    if avg is not None and avg >= t["sleep_good_h"] and nights >= 5:
        tags.add("sleep_good")
    if (n.get("stress_avg") or 0) >= t["high_stress"]:
        tags.add("high_stress")
    if n.get("level") == "red" or ("hrv_down" in flags and "short_nights" in flags):
        tags.add("recovery_low")
    if nights < t["gap_days"] or (n.get("days_with_data") or 0) < t["gap_days"]:
        tags.add("gap")
    return [tag for tag in WEEK_TAGS if tag in tags]


def day_tags(n: Mapping[str, Any], flags: Sequence[str]) -> List[str]:
    t = TAG_RULES
    tags = {f for f in flags if f in DAY_TAGS}
    secs = n.get("sleep_seconds")
    if secs and secs / 3600 >= t["long_sleep_h"]:
        tags.add("long_sleep")
    if (n.get("stress_avg") or 0) >= t["high_stress"]:
        tags.add("high_stress")
    if n.get("sessions") == 0:
        tags.add("rest_day")
    if (n.get("sessions") or 0) >= 2:
        tags.add("double_day")
    for sport in hx.SPORTS:
        if n.get(f"{sport}_sessions"):
            tags.add(f"{sport}_day")
            minutes = (n.get(f"{sport}_seconds") or 0) / 60
            if minutes >= ds.THRESHOLDS["long_min"][sport]:
                tags.add("long_session")
    if n.get("missing"):
        tags.add("partial")
    return [tag for tag in DAY_TAGS if tag in tags]


# --- texts ----------------------------------------------------------------------------------------------------

def _sports_text(n: Mapping[str, Any], unit: str) -> str:
    parts = []
    for sport in hx.SPORTS:
        count = n.get(f"{sport}_sessions") or 0
        if count:
            parts.append(f"{sport} {count} {'session' if count == 1 else 'sessions'}")
    return ", ".join(parts) if parts else f"no training this {unit}"


def week_text(start: dt.date, n: Mapping[str, Any], tags: Sequence[str]) -> str:
    end = start + dt.timedelta(days=6)
    lines = [
        f"Week {start.isoformat()} to {end.isoformat()}.",
        f"Level {n.get('level') or 'unknown'}. Flags: {_words(n.get('flags') or [])}.",
        f"HRV {_hrv_week_band(n.get('hrv_delta_ms'))}.",
        f"Sleep {_sleep_band(n.get('sleep_avg_h'))}: {n.get('nights_under_6h') or 0} nights under 6 h"
        f" of {n.get('sleep_nights') or 0} synced.",
    ]
    if n.get("total_hours") is None:
        lines.append("Training volume unknown.")
    else:
        lines.append(f"Training {_hours_band(n.get('total_hours'))}, {_change_band(n.get('change_pct'))}"
                     f" on the week before: {_sports_text(n, 'week')}.")
    status = n.get("acwr_status")
    lines.append(f"Load {_ACWR_WORDS.get(status, 'not available')}." if n.get("mode") == "full" and status
                 else "Load not available.")
    lines.append(f"Tags: {' '.join(tags) if tags else 'none'}.")
    return " ".join(lines)


def day_text(day: dt.date, n: Mapping[str, Any], flags: Sequence[str], level: str, tags: Sequence[str]) -> str:
    secs = n.get("sleep_seconds")
    hrv_status = (n.get("hrv_status") or "").lower().replace("_", " ")
    lines = [
        f"Day {day.isoformat()}, {sc.en_weekday(day)}.",
        f"Level {level}. Flags: {_words(list(flags))}.",
        f"Sleep {_sleep_band(secs / 3600 if secs else None)}.",
        f"HRV status {hrv_status}." if hrv_status else "HRV status unknown.",
        f"Training: {_sports_text(n, 'day')}." if n.get("sessions") is not None else "Training unknown.",
        f"Tags: {' '.join(tags) if tags else 'none'}.",
    ]
    return " ".join(lines)


# --- documents ------------------------------------------------------------------------------------------------

def _scalar_numbers(n: Mapping[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in n.items() if not isinstance(v, list) and k != "mode"}


def week_document(row: Mapping[str, Any], before: Sequence[Mapping[str, Any]] = ()) -> Dict[str, Any]:
    """row = a store week row; before = the numbers of the weeks before it, newest first."""
    n = row["numbers"]
    start = row["start"]
    tags = week_tags(n, before)
    return {
        "id": doc_id("week", start),
        "page_content": week_text(start, n, tags),
        "metadata": {
            "kind": "week", "start": start.isoformat(), "end": (start + dt.timedelta(days=6)).isoformat(),
            "mode": n.get("mode"), "level": n.get("level"), "flags": list(n.get("flags") or []), "tags": tags,
            "sports": [s for s in hx.SPORTS if n.get(f"{s}_sessions")], "final": bool(row.get("final")),
            "coverage": {"nights": n.get("sleep_nights") or 0, "days": n.get("days_with_data") or 0},
            "rules_version": n.get("rules_version"), "text_version": TEXT_VERSION,
            "missing": list(n.get("missing") or []), "numbers": _scalar_numbers(n),
        },
    }


def day_document(row: Mapping[str, Any], prev: Optional[Mapping[str, Any]] = None,
                 prev2: Optional[Mapping[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """row = a store day row; prev / prev2 = the numbers of the 1 and 2 days before. None for an empty day."""
    if row.get("empty"):
        return None
    n = row["numbers"]
    day = row["date"]
    derived = hx.day_flags(n, prev, prev2)
    tags = day_tags(n, derived["flags"])
    return {
        "id": doc_id("day", day),
        "page_content": day_text(day, n, derived["flags"], derived["level"], tags),
        "metadata": {
            "kind": "day", "start": day.isoformat(), "end": day.isoformat(), "mode": n.get("mode"),
            "level": derived["level"], "flags": derived["flags"], "tags": tags,
            "sports": [s for s in hx.SPORTS if n.get(f"{s}_sessions")], "final": bool(row.get("final")),
            "rules_version": ds.RULES_VERSION, "text_version": TEXT_VERSION,
            "missing": list(n.get("missing") or []), "numbers": _scalar_numbers(n),
        },
    }


def build_document(store: hs.HistoryStore, account_fp: str, kind: str, start: Any, *,
                   conn: Any = None) -> Optional[Dict[str, Any]]:
    """The document of one stored day or week (reads its neighbours from the store), or None when the row is
    missing or the day is empty. For the embed_doc job."""
    start = hs._date(start)
    if kind == "week":
        rows = store.weeks(account_fp, start - dt.timedelta(days=28), start, conn=conn)
        row = next((r for r in rows if r["start"] == start), None)
        if row is None:
            return None
        before = [r["numbers"] for r in reversed(rows) if r["start"] < start]
        return week_document(row, before)
    if kind == "day":
        rows = {r["date"]: r for r in store.days(account_fp, start - dt.timedelta(days=2), start, conn=conn)}
        row = rows.get(start)
        if row is None:
            return None
        prev = rows.get(start - dt.timedelta(days=1))
        prev2 = rows.get(start - dt.timedelta(days=2))
        return day_document(row, prev and prev["numbers"], prev2 and prev2["numbers"])
    raise ValueError("history: kind must be day or week")


def to_langchain(doc: Mapping[str, Any]):
    """A langchain_core Document (needs the history dependencies)."""
    from langchain_core.documents import Document

    return Document(id=doc["id"], page_content=doc["page_content"], metadata=dict(doc["metadata"]))
