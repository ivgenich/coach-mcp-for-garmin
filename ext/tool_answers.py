"""What launch.py's server adds to upstream's tools: description notes and answer additions.

ReadOnlyFastMCP.add_tool() (launch.read_only_server_class) ends the description of each tool in TOOL_NOTES
with its note, and call_tool() hands every successful upstream answer to clean_texts(): text_hygiene's clean,
then the fields of ANSWER_ADDITIONS. Standard library and text_hygiene only.
"""

from __future__ import annotations

import json

import text_hygiene

# --- Description notes for upstream tools ---------------------------------------------------------------------
# What upstream's answers leave unclear, said where the model reads it (claude.ai drops server instructions).
# add_tool() ends the description with "\n\n" + the note, before the free-text note. Short: a noted description
# stays under the 2,048 characters Claude Code kept before v2.1.296.
_ACUTE_BAND_NOTE = ("optimal_chronic_load_min/max is the band for acute load (also given as "
                    "optimal_acute_load_min/max), not a target for chronic load.")
_LOCAL_START_NOTE = "Note: start_time is local time, not GMT."
TOOL_NOTES = {
    "get_training_status": ("Note: " + _ACUTE_BAND_NOTE + " Garmin's load_ratio (the ACWR) may be cut to one "
                            "decimal; get_daily_status computes it from the loads to two."),
    "get_training_load_trend": ("Note: " + _ACUTE_BAND_NOTE + " Garmin's acwr may be cut to one decimal; "
                                "get_daily_status computes it from the loads to two."),
    "get_progress_summary_between_dates": (
        "Note: sum/avg/min/max are in Garmin's raw unit, named in unit: cm for distance and elevationGain, ms "
        "for duration and movingDuration. The *_m and *_s fields are the same values in meters and seconds; "
        "for calories, *_rounded are whole kcal."),
    "get_body_battery": "Note: event start_time is GMT, not local time.",
    "get_activity_splits": "Note: lap and length start_time is GMT, not local time.",
    "get_activities": _LOCAL_START_NOTE,
    "get_activities_by_date": _LOCAL_START_NOTE,
    "get_training_readiness": "Note: timestamp is local time, not GMT.",
    "get_vo2max_trend": ("Note: returns one sport only (the one with more readings). The running VO2max is vo2_max "
                         "in get_training_status."),
}


# --- Answer additions for upstream tools ----------------------------------------------------------------------
# Upstream's answers are not this repo's public API (docs/updates.md), so fields are added where an answer is
# easy to misread: never removed or renamed, and only when absent (an upstream fix is not doubled). One function
# per tool: it gets the parsed answer and returns the object to dump, or None for "unchanged". It runs after
# text_hygiene's clean, on successful answers only; an unexpected shape, or any exception, keeps the answer.

# get_progress_summary_between_dates: Garmin's fitnessstats sums are centimeters and milliseconds (live:
# a running sum 3,504,354 for 35,043.5 m, 11,748,244 for 11,748.2 s); upstream converts calories only, unrounded.
_PROGRESS_UNITS = {"distance": ("cm", "_m", 100), "elevationGain": ("cm", "_m", 100),
                   "duration": ("ms", "_s", 1000), "movingDuration": ("ms", "_s", 1000),
                   "calories": ("kcal", "_rounded", None)}
_PROGRESS_STATS = ("sum", "avg", "min", "max")


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _with_after(mapping, after, extra):
    """A copy of dict `mapping` with the absent keys of `extra` placed right after key `after` (at the end
    when `after` is missing); None when nothing is added."""
    extra = {k: v for k, v in extra.items() if k not in mapping}
    if not extra:
        return None
    if after not in mapping:
        return {**mapping, **extra}
    out = {}
    for key, value in mapping.items():
        out[key] = value
        if key == after:
            out.update(extra)
    return out


def _add_progress_units(obj):
    """unit after metric, and converted copies of sum/avg/min/max in each activity type."""
    if not isinstance(obj, dict) or "unit" in obj or obj.get("metric") not in _PROGRESS_UNITS:
        return None
    by_type = obj.get("stats_by_activity_type")
    if not isinstance(by_type, dict):
        return None
    unit, suffix, factor = _PROGRESS_UNITS[obj["metric"]]
    types = {}
    for sport, stats in by_type.items():
        if not isinstance(stats, dict):
            return None
        copies = {}
        for key in _PROGRESS_STATS:
            value = stats.get(key)
            if value is None:
                continue
            if not _number(value):
                return None
            copies[key + suffix] = round(value) if factor is None else round(value / factor, 1)
        types[sport] = _with_after(stats, "max", copies) or stats
    return _with_after({**obj, "stats_by_activity_type": types}, "metric", {"unit": unit})


def _acute_band(entry):
    """optimal_acute_load_min/max (the same values) after optimal_chronic_load_max; None when not there."""
    if not isinstance(entry, dict):
        return None
    extra = {f"optimal_acute_load_{end}": entry[f"optimal_chronic_load_{end}"]
             for end in ("min", "max") if f"optimal_chronic_load_{end}" in entry}
    if not extra:
        return None
    after = "optimal_chronic_load_max" if "optimal_chronic_load_max" in entry else "optimal_chronic_load_min"
    return _with_after(entry, after, extra)


def _add_acute_band_trend(obj):
    if not isinstance(obj, dict) or not isinstance(obj.get("trend"), list):
        return None
    trend = [_acute_band(e) or e for e in obj["trend"]]
    if all(new is old for new, old in zip(trend, obj["trend"])):
        return None
    return {**obj, "trend": trend}


ANSWER_ADDITIONS = {
    "get_progress_summary_between_dates": _add_progress_units,
    "get_training_status": _acute_band,
    "get_training_load_trend": _add_acute_band_trend,
}


def add_to_answer(name, text):
    """`text` (a cleaned upstream answer of tool `name`) with ANSWER_ADDITIONS' fields, dumped as
    text_hygiene.clean_tool_answer dumps (ensure_ascii; indent=2 when the text has a newline, else compact).
    Unchanged for a tool without a row, a text that is not a JSON object, an unexpected shape, or any failure."""
    add = ANSWER_ADDITIONS.get(name)
    if add is None or not isinstance(text, str) or not text.lstrip(" \t\n\r").startswith("{"):
        return text
    try:
        new = add(json.loads(text))
        if new is None:
            return text
        style = {"indent": 2} if "\n" in text else {"separators": (",", ":")}
        return json.dumps(new, ensure_ascii=True, **style)
    except Exception:  # noqa: BLE001 - an addition never costs the answer
        return text


def clean_texts(texts, name=None):
    """text_hygiene.clean_tool_answer() of each str in `texts`; None entries (blocks that are no text) stay None.
    The first block, the answer itself, then gets tool `name`'s ANSWER_ADDITIONS (add_to_answer)."""
    cleaned = [None if t is None else text_hygiene.clean_tool_answer(t) for t in texts]
    if cleaned and cleaned[0] is not None and name in ANSWER_ADDITIONS:
        cleaned[0] = add_to_answer(name, cleaned[0])
    return cleaned
