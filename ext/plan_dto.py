"""Garmin workout DTOs for the plan's sessions: raw dicts with upstream's verified ids.

The plan engine (plan_engine) describes each session as steps (distance or time, a pace band in m/s). This
module turns one session into the JSON Garmin's workout service takes, with the ids upstream garmin_mcp
verified (garmin_mcp/workouts.py: END_CONDITION_TYPE_IDS lap.button=1, time=2, distance=3, iterations=7;
KNOWN_TARGET_TYPE_IDS no.target=1, pace.zone=6; sport running=1), not garminconnect's pydantic models (their
ids are wrong at 0.3.2). Stage 1 writes nothing to Garmin: the DTOs back the preview's "window" (the text is
rendered from the exact DTOs a later commit would send) and two runtime checks:

- dto_valid: upstream's own checks (_normalize_workout_steps, _validate_end_condition_steps,
  _validate_target_type_steps) pass on every DTO, and the normalisation changes nothing;
- cross_engine: every session daily_status.classify_workout calls hard or long (through upstream's
  _curate_workout_details, as get_daily_status reads a calendar workout) is on a day the plan marks hard or
  long, so the plan and the daily report agree on what a hard day is (one direction only).

Pace targets carry the slower speed in targetValueOne and the faster in targetValueTwo; the order Garmin
expects is not verified yet (docs/training-plan.md, "Garmin facts"), and the classifier uses the midpoint.
Upstream is imported lazily: without it the checks answer "skipped" and say so.
"""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

import daily_status as ds

RUNNING: Dict[str, Any] = {"sportTypeId": 1, "sportTypeKey": "running"}
STEP_TYPES: Dict[str, Tuple[int, str]] = {
    "warmup": (1, "warmup"), "cooldown": (2, "cooldown"), "interval": (3, "interval"),
    "recovery": (4, "recovery"), "rest": (5, "rest"), "repeat": (6, "repeat"),
}
END_CONDITIONS: Dict[str, int] = {"lap.button": 1, "time": 2, "distance": 3, "iterations": 7}
NO_TARGET: Dict[str, Any] = {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target"}
PACE_TARGET: Dict[str, Any] = {"workoutTargetTypeId": 6, "workoutTargetTypeKey": "pace.zone"}
NAME_PREFIX = "Coach"
ROLE_LABELS = {"easy": "Easy", "strides": "Easy + strides", "long": "Long run", "threshold": "Threshold",
               "intervals": "Intervals", "mp": "Marathon pace"}
SKIPPED = "skipped: upstream garmin_mcp is not installed"


def _upstream() -> Optional[Any]:
    try:
        from garmin_mcp import workouts
    except Exception:  # noqa: BLE001 - the stdlib test leg has no upstream
        return None
    return workouts


def _step(step: Mapping[str, Any], order: List[int]) -> Dict[str, Any]:
    order[0] += 1
    kind = str(step["type"])
    type_id, type_key = STEP_TYPES[kind]
    if kind == "repeat":
        times = int(step["times"])
        out: Dict[str, Any] = {
            "type": "RepeatGroupDTO", "stepOrder": order[0], "stepType": {"stepTypeId": type_id, "stepTypeKey": type_key},
            "numberOfIterations": times, "smartRepeat": False,
            "endCondition": {"conditionTypeId": END_CONDITIONS["iterations"], "conditionTypeKey": "iterations"},
            "endConditionValue": float(times),
        }
        out["workoutSteps"] = [_step(child, order) for child in step["steps"]]
        return out
    if "distance_m" in step:
        end, value = "distance", float(step["distance_m"])
    else:
        end, value = "time", float(step["time_s"])
    out = {"type": "ExecutableStepDTO", "stepOrder": order[0],
           "stepType": {"stepTypeId": type_id, "stepTypeKey": type_key},
           "endCondition": {"conditionTypeId": END_CONDITIONS[end], "conditionTypeKey": end},
           "endConditionValue": value}
    speed = step.get("speed_mps")
    if isinstance(speed, (list, tuple)) and len(speed) == 2:
        out["targetType"] = dict(PACE_TARGET)
        out["targetValueOne"] = float(min(speed))
        out["targetValueTwo"] = float(max(speed))
    else:
        out["targetType"] = dict(NO_TARGET)
    return out


def workout_dto(session: Mapping[str, Any], *, marker: Optional[str] = None) -> Dict[str, Any]:
    """The Garmin workout JSON of one plan session (plan_engine.session_json's shape). Names carry only the
    prefix and the role label: linked apps and coaches see them. `marker` (Stage 2: install tag, plan id,
    key, hash) becomes the description."""
    order = [0]
    dto: Dict[str, Any] = {
        "workoutName": f"{NAME_PREFIX} {ROLE_LABELS.get(str(session['role']), 'Run')}",
        "sportType": dict(RUNNING),
        "workoutSegments": [{"segmentOrder": 1, "sportType": dict(RUNNING),
                             "workoutSteps": [_step(st, order) for st in session["steps"]]}],
    }
    if session.get("km") is not None:
        dto["estimatedDistanceInMeters"] = float(round(float(session["km"]) * 1000))
    if session.get("minutes") is not None:
        dto["estimatedDurationInSecs"] = int(session["minutes"]) * 60
    if marker:
        dto["description"] = marker
    return dto


def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def dto_hash(dto: Mapping[str, Any]) -> str:
    """SHA-256 of the DTO after upstream's normalisation (on a copy: it changes its input), 64 hex."""
    data = copy.deepcopy(dict(dto))
    wk = _upstream()
    if wk is not None:
        wk._normalize_workout_steps(data)
    return hashlib.sha256(canonical(data).encode("ascii")).hexdigest()


def validate(dto: Mapping[str, Any]) -> Optional[List[str]]:
    """Upstream's checks on a copy: [] when they pass and normalising changed nothing, else the problems
    (fixed texts, no values); None without upstream."""
    wk = _upstream()
    if wk is None:
        return None
    data = copy.deepcopy(dict(dto))
    problems = []
    try:
        wk._normalize_workout_steps(data)
        wk._validate_end_condition_steps(data)
        wk._validate_target_type_steps(data)
    except ValueError as exc:
        problems.append(f"upstream check failed: {type(exc).__name__}")
    if data != dict(dto):
        problems.append("upstream normalisation changed the workout")
    return problems


# The step values dto_valid accepts (upstream checks only ids and keys): a distance step 1 m to 50 km, a timed
# step 1 s to 6 h, a repeat 1 to 50 times with steps in it, a pace target with 0 < slower < faster speed inside
# the plan engine's speed limits.
MAX_STEP_M = 50_000.0
MAX_STEP_S = 6 * 3600.0
MAX_REPEATS = 50


def step_value_problems(dto: Mapping[str, Any]) -> List[str]:
    """The DTO's steps with values a workout cannot run (fixed texts, no values): [] when all are sane."""
    import plan_engine as pe   # lazily, as selfcheck(): plan_engine stays free of this module

    lo_lim, hi_lim = (float(x) for x in pe.RULES["speed_limits_mps"])
    problems: List[str] = []

    def num(v: Any) -> Optional[float]:
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None

    def walk(steps: Any) -> None:
        if not isinstance(steps, list) or not steps:
            problems.append("a workout or repeat without steps")
            return
        for st in steps:
            if not isinstance(st, dict):
                problems.append("a step that is not an object")
                continue
            end = (st.get("endCondition") or {}).get("conditionTypeKey")
            value = num(st.get("endConditionValue"))
            if st.get("type") == "RepeatGroupDTO":
                n = num(st.get("numberOfIterations"))
                if n is None or not 1 <= n <= MAX_REPEATS or n != int(n):
                    problems.append("a repeat count outside 1-50")
                walk(st.get("workoutSteps"))
                continue
            limit = {"distance": MAX_STEP_M, "time": MAX_STEP_S}.get(str(end))
            if limit is None:
                problems.append("a step that ends neither by distance nor by time")
            elif value is None or not 1.0 <= value <= limit:
                problems.append(f"a {end} step outside 1-{int(limit)}")
            if (st.get("targetType") or {}).get("workoutTargetTypeKey") == "pace.zone":
                one, two = num(st.get("targetValueOne")), num(st.get("targetValueTwo"))
                if one is None or two is None or not lo_lim <= one < two <= hi_lim:
                    problems.append("a pace target not slower < faster within the speed limits")

    walk([s for seg in dto.get("workoutSegments") or [] if isinstance(seg, dict)
          for s in seg.get("workoutSteps") or []] or None)
    return problems


def classify(dto: Mapping[str, Any], athlete: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """daily_status.classify_workout of the DTO as get_daily_status would read it from the calendar; None
    without upstream."""
    wk = _upstream()
    if wk is None:
        return None
    curate: Callable[[Dict[str, Any]], Dict[str, Any]] = wk._curate_workout_details
    curated = curate(copy.deepcopy(dict(dto)))
    entry = {"workoutName": dto.get("workoutName"), "workoutType": "running",
             "estimatedDurationInSecs": dto.get("estimatedDurationInSecs"),
             "estimatedDistanceInMeters": dto.get("estimatedDistanceInMeters")}
    return ds.classify_workout(entry, curated, athlete=dict(athlete))


def _fmt_step(step: Mapping[str, Any]) -> str:
    if step.get("type") == "RepeatGroupDTO":
        inner = ", ".join(_fmt_step(s) for s in step.get("workoutSteps") or [])
        return f"{int(step.get('numberOfIterations') or 1)} x ({inner})"
    end = (step.get("endCondition") or {}).get("conditionTypeKey")
    value = float(step.get("endConditionValue") or 0)
    size = (f"{value / 1000:.1f} km" if value >= 1000 else f"{int(value)} m") if end == "distance" else f"{int(value)} s"
    kind = (step.get("stepType") or {}).get("stepTypeKey") or "run"
    kind = "run" if kind == "interval" else kind
    text = f"{size} {kind}"
    lo, hi = step.get("targetValueOne"), step.get("targetValueTwo")
    if (step.get("targetType") or {}).get("workoutTargetTypeKey") == "pace.zone" and lo and hi:
        text += f" at {ds.pace_min_km(hi)}-{ds.pace_min_km(lo)}/km"
    return text


def render(dto: Mapping[str, Any]) -> str:
    """One line from the DTO itself: '<name>: 2.0 km warmup at 5:25-6:11/km, 3 x (...)'."""
    steps = [s for seg in dto.get("workoutSegments") or [] for s in seg.get("workoutSteps") or []]
    return f"{dto.get('workoutName')}: " + ", ".join(_fmt_step(s) for s in steps)


def session_dtos(plan: Mapping[str, Any]) -> List[Tuple[Mapping[str, Any], Dict[str, Any]]]:
    """(session, its DTO) for every run of the plan answer (the races are not workouts)."""
    out = []
    for week in plan.get("weeks") or []:
        for s in week.get("sessions") or []:
            if s.get("role") in ("race", "b_race") or not s.get("steps"):
                continue
            out.append((s, workout_dto(s)))
    return out


def check_plan(plan: Mapping[str, Any], lt_speed_mps: Optional[float]) -> List[Dict[str, Any]]:
    """The dto_valid and cross_engine invariants of a plan answer: [{id, ok, detail}]."""
    pairs = session_dtos(plan)
    bad: List[str] = []
    cross: List[str] = []
    if _upstream() is None:
        return [{"id": "dto_valid", "ok": True, "detail": SKIPPED},
                {"id": "cross_engine", "ok": True, "detail": SKIPPED}]
    athlete = {"lt_speed_mps": lt_speed_mps, "lt_hr": None, "ftp_w": None}
    for s, dto in pairs:
        problems = (validate(dto) or []) + step_value_problems(dto)
        if problems:
            bad.append(f"{s['key']}: {problems[0]}")
        c = classify(dto, athlete) or {}
        if (c.get("hard") or c.get("long")) and not s.get("hard_or_long"):
            cross.append(f"{s['key']}: the daily report would call it {'hard' if c.get('hard') else 'long'}")
    return [{"id": "dto_valid", "ok": not bad, "detail": "; ".join(bad[:5])},
            {"id": "cross_engine", "ok": not cross, "detail": "; ".join(cross[:5])}]


def selfcheck() -> None:
    """Fail at Docker build time if upstream's ids, validators or curation changed: the sample plan's DTOs
    pass upstream's checks unchanged and the daily classifier agrees with the plan's hard days."""
    import plan_engine as pe
    from garmin_mcp import workouts

    if (workouts.END_CONDITION_TYPE_IDS.get("time"), workouts.END_CONDITION_TYPE_IDS.get("distance"),
            workouts.END_CONDITION_TYPE_IDS.get("lap.button"), workouts.END_CONDITION_TYPE_IDS.get("iterations")) \
            != (2, 3, 1, 7):
        raise RuntimeError("plan_dto: upstream END_CONDITION_TYPE_IDS changed")
    if "pace.zone" not in workouts.KNOWN_TARGET_TYPE_IDS.get(6, ()) or \
            "no.target" not in workouts.KNOWN_TARGET_TYPE_IDS.get(1, ()):
        raise RuntimeError("plan_dto: upstream KNOWN_TARGET_TYPE_IDS changed")
    import datetime as dt

    inputs = pe.PlanInputs(race_date=dt.date(2027, 2, 26), goal_time_s=3 * 3600 + 30 * 60,
                           b_races=(pe.BRace(dt.date(2027, 1, 29), "tri_half"),))
    snap = pe.sample_snapshot()
    plan = pe.plan_answer(pe.build_plan(inputs, snap, what_if_runs=False))
    failed = [c for c in check_plan(plan, snap.lt_speed_mps) if not c["ok"]]
    if failed:
        raise RuntimeError("plan_dto: " + ", ".join(c["id"] for c in failed) + " fails on the sample plan")
