"""The weekly plan review (Stage 4, review rules v1): proposals that only hold or reduce load.

Pure and standard library only, like plan_engine: review_week() takes one reviewed week of a stored plan (its
sessions, the tracked links, the athlete's RPE and pain, the linked runs' distance and speed) and the week's
health numbers (weekly_status' flags, the short nights, HRV, Garmin's ACWR, today's daily level) and returns
the metrics and the proposed changes. Each change has a change_id, its rule (A1-A8), a kind, from and to, a
one-sentence reason that cites its number, evidence {metric, value, threshold}, and the edits it stands for in
machine form. docs/training-plan.md "Plan review rules v1" says the rules in words; REVIEW_RULES holds every
number and is echoed in plan_review_week's answer.

A change never raises load: every proposal targets the week after the reviewed one (or today, A8) and is
proposed only when its value is below the plan's (A6's advice is a change of its own when nothing is left to
hold or drop). apply_changes() patches the stored version the athlete reviewed: every stored session stays as
it is, and only the sessions after today a change targets are shortened, dropped or eased (today's only for
A8), at the bands the stored plan already uses. The patch is checked again: patch_problems() (the plan rules a
shortening can break) and no_raise() against the stored plan refuse a change, with the rule that blocks it.

The rules (REVIEW_RULES; one week Monday to Sunday, "next week" the week after it):
  A1 completion of the week's due km (runs linked done or partial, each counted up to its planned km, over the
     planned km of the sessions already settled) under 70% -> next week repeats this week's planned km;
  A2 HRV down or 3 or more short nights (weekly_status.evaluate_flags) -> next week becomes a deload
     (0.78 x this week's planned km); Garmin's ACWR above 1.3 counts only together with another signal (the
     week's mean easy RPE of 6 or more), never alone;
  A3 the week's long run missed -> next week's long run at most the missed length (the plan's cap and its
     2 km step still hold: the run only gets shorter);
  A4 mean easy-run RPE of 6 or more in this week and the week before -> the easy and long bands 3% slower,
     and next week's km 10% lower;
  A5 a quality session run at 2% or more above its planned average speed with RPE 5 or less -> suggest a
     threshold retest (advice: changes no session);
  A6 pain of 4 or more, or sharp pain, in the week -> next week holds this week's km and its quality sessions
     become easy runs, with the fixed text "see a physiotherapist or doctor" (only that advice when there is
     nothing to hold or drop);
  A7 a missed week (no due km done) -> next week at most 1.10 x the km of the last week that was done;
  A8 today's daily level red while the plan has a quality session today -> that session becomes an easy run of
     the same distance.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
from typing import Any, Dict, Iterable, List, Mapping, NewType, Optional, Sequence, Set, Tuple

import plan_engine as pe

Km = NewType("Km", float)
Pct = NewType("Pct", float)
Ratio = NewType("Ratio", float)

REVIEW_RULES_VERSION = 1

REVIEW_RULES: Dict[str, Any] = {
    "completion_min_pct": 70,          # A1: under this share of the due km -> repeat this week's km
    "hrv_drop_ms": 5,                  # A2: weekly_status' hrv_down flag (7-day average this far below the 28-day)
    "short_nights": 3,                 # A2: weekly_status' short_nights flag (nights under 6 h)
    "acwr_max": 1.3,                   # A2: Garmin's ACWR above this counts only with another signal
    "acwr_easy_rpe": 6,                # A2: ... the week's mean easy RPE at or above this
    "deload_factor": float(pe.RULES["deload_factor"]),   # A2: the early deload, of this week's planned km
    "easy_rpe": 6,                     # A4: mean easy RPE at or above this in both weeks
    "easy_band_slowdown": 0.03,        # A4: the easy and long bands this share slower
    "easy_rpe_week_cut": 0.10,         # A4: next week's km this share lower
    "quality_rpe_max": 5,              # A5: RPE at or below this ...
    "quality_speed_over": 1.02,        # A5: ... and the run's average speed at least this x the planned average
    "pain": 4,                         # A6: pain at or above this, or sharp pain
    "missed_week_cap": 1.10,           # A7: next week at most this x the last week that was done
    "red_level": "red",                # A8: daily_status' level
}
ADVICE_PAIN = ("Pain of 4 or more, or sharp pain: if it persists or is sharp, see a physiotherapist or doctor. "
               "Training guidance, not medical advice.")
ADVICE_RETEST = ("A threshold retest (a 30-minute time trial or Garmin's guided lactate-threshold test) would "
                 "update the pace bands; the plan keeps its bands until Garmin reports a new threshold.")

DONE = ("done", "done_early", "done_late")
PARTIAL = ("partial", "partial_early", "partial_late")
EASY_ROLES = ("easy", "strides")
EPS_KM = 0.05
MAX_EASY_BAND_SLOWDOWN = pe.MAX_EASY_BAND_SLOWDOWN


# --- inputs -----------------------------------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class Feedback:
    rpe: int
    pain: int
    sharp: bool


@dataclasses.dataclass(frozen=True)
class Run:
    """A linked activity: its distance and average speed (None when Garmin did not give them)."""

    km: Optional[Km]
    speed_mps: Optional[pe.MetersPerSecond]


@dataclasses.dataclass(frozen=True)
class Health:
    """The week's health numbers. flags: weekly_status.evaluate_flags ids; today_level: daily_status' level
    today (None when not read)."""

    flags: Tuple[str, ...] = ()
    hrv_7d: Optional[float] = None
    hrv_28d: Optional[float] = None
    short_nights: Optional[int] = None
    acwr: Optional[float] = None
    today_level: Optional[str] = None


@dataclasses.dataclass(frozen=True)
class Session:
    key: str
    date: dt.date
    role: str
    km: Km
    minutes: Optional[float]


def sessions_of(body: Any) -> List[Session]:
    """The stored plan body's run sessions (not the races), by date."""
    out = []
    weeks = body.get("weeks") if isinstance(body, dict) else None
    for w in weeks if isinstance(weeks, list) else []:
        for s in (w.get("sessions") or []) if isinstance(w, dict) else []:
            if not isinstance(s, dict) or s.get("role") in ("race", "b_race") or not isinstance(s.get("role"), str):
                continue
            try:
                day = dt.date.fromisoformat(str(s.get("date")))
                km = float(s.get("km") or 0.0)
            except (TypeError, ValueError):
                continue
            minutes = s.get("minutes")
            out.append(Session(str(s.get("key")), day, str(s["role"]), Km(km),
                               float(minutes) if isinstance(minutes, (int, float)) and not isinstance(minutes, bool)
                               else None))
    return sorted(out, key=lambda x: (x.date, x.key))


def _in_week(sessions: Iterable[Session], start: dt.date) -> List[Session]:
    end = start + dt.timedelta(days=6)
    return [s for s in sessions if start <= s.date <= end]


def _km(values: Iterable[float]) -> Km:
    return Km(round(sum(values), 1))


def _mean(values: Sequence[int]) -> Optional[float]:
    return round(sum(values) / len(values), 1) if values else None


def change_id(plan_id: str, week_start: dt.date, rule: str, kind: str) -> str:
    text = f"{plan_id}|{week_start.isoformat()}|{rule}|{kind}|v{REVIEW_RULES_VERSION}"
    return "c" + hashlib.sha256(text.encode("ascii")).hexdigest()[:16]


# --- the week's numbers -----------------------------------------------------------------------------------------

@dataclasses.dataclass
class WeekFacts:
    start: dt.date
    sessions: List[Session]
    planned_km: Km
    due_km: Km
    done_km: Km
    completion_pct: Optional[Pct]
    counts: Dict[str, int]
    long: Optional[Session]
    long_status: Optional[str]
    easy_rpe: Optional[float]


def week_facts(sessions: Sequence[Session], start: dt.date, links: Mapping[str, Optional[str]],
               linked: Mapping[str, Optional[int]], runs: Mapping[int, Run],
               feedback: Mapping[str, Feedback]) -> WeekFacts:
    """One week's planned, due and done km. A session is due once it has a link status; a done or partial
    run counts its distance up to the session's planned km (a done run without a distance counts the planned
    km, a partial one nothing)."""
    week = _in_week(sessions, start)
    counts = {"planned": len(week), "done": 0, "partial": 0, "missed": 0, "replaced": 0, "open": 0}
    due = done = 0.0
    for s in week:
        status = links.get(s.key)
        if status is None:
            counts["open"] += 1
            continue
        due += s.km
        if status in DONE + PARTIAL:
            counts["done" if status in DONE else "partial"] += 1
            aid = linked.get(s.key)
            run = runs.get(aid) if aid is not None else None
            got = run.km if run is not None and run.km is not None else (s.km if status in DONE else 0.0)
            done += min(float(got), float(s.km))
        else:
            counts["missed" if status == "missed" else "replaced"] += 1
    long = next((s for s in week if s.role == "long"), None)
    rpes = [feedback[s.key].rpe for s in week if s.role in EASY_ROLES and s.key in feedback]
    return WeekFacts(start, week, _km(s.km for s in week), Km(round(due, 1)), Km(round(done, 1)),
                     Pct(round(100.0 * done / due, 1)) if due > 0 else None, counts, long,
                     links.get(long.key) if long is not None else None, _mean(rpes))


# --- the rules ---------------------------------------------------------------------------------------------------

def _change(plan_id: str, week: dt.date, rule: str, kind: str, frm: Any, to: Any, reason: str,
            metric: str, value: Any, threshold: Any, edits: List[Dict[str, Any]],
            advice: Optional[str] = None, also: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    evidence: Dict[str, Any] = {"metric": metric, "value": value, "threshold": threshold}
    if also:
        evidence["also"] = also
    out: Dict[str, Any] = {"change_id": change_id(plan_id, week, rule, kind), "rule": rule, "kind": kind,
                           "from": frm, "to": to, "reason": reason, "evidence": evidence, "edits": edits}
    if advice:
        out["advice"] = advice
    return out


def _fmt(x: float) -> str:
    return f"{x:.1f}"


def review_week(plan_id: str, body: Mapping[str, Any], week_start: dt.date, today: dt.date,
                links: Mapping[str, Optional[str]], linked: Mapping[str, Optional[int]], runs: Mapping[int, Run],
                feedback: Mapping[str, Feedback], health: Health,
                rules: Mapping[str, Any] = REVIEW_RULES) -> Dict[str, Any]:
    """{metrics, changes}: the review of the week from week_start (a Monday) of the stored plan `body`.
    links: session key -> link status (None: not settled); linked: key -> linked activity id; runs: activity
    id -> Run; feedback: key -> Feedback."""
    sessions = sessions_of(body)
    this = week_facts(sessions, week_start, links, linked, runs, feedback)
    prev = week_facts(sessions, week_start - dt.timedelta(days=7), links, linked, runs, feedback)
    nxt_start = week_start + dt.timedelta(days=7)
    nxt = _in_week(sessions, nxt_start)
    nxt_km = _km(s.km for s in nxt)
    nxt_long = next((s for s in nxt if s.role == "long"), None)
    nxt_quality = [s for s in nxt if s.role in pe.QUALITY_ROLES]
    pains = [(s, feedback[s.key]) for s in this.sessions if s.key in feedback]
    pain_max = max((f.pain for _s, f in pains), default=None)
    sharp = any(f.sharp for _s, f in pains)
    changes: List[Dict[str, Any]] = []
    # Only the next week can change, and only once it has not ended (its sessions may still be rebuilt).
    future = bool(nxt) and nxt_start + dt.timedelta(days=6) >= today

    def cap_next(rule: str, kind: str, to_km: float, reason: str, metric: str, value: Any, threshold: Any,
                 advice: Optional[str] = None, also: Optional[List[Dict[str, Any]]] = None) -> bool:
        to = Km(round(to_km, 1))
        if not (future and to < nxt_km - EPS_KM):
            return False
        changes.append(_change(plan_id, week_start, rule, kind, {"week_start": nxt_start.isoformat(), "km": nxt_km},
                               {"week_start": nxt_start.isoformat(), "km": to},
                               reason.format(to=_fmt(to), frm=_fmt(nxt_km)), metric, value, threshold,
                               [{"op": "cap_week", "week_start": nxt_start.isoformat(), "km": to}], advice, also))
        return True

    # A1: completion of the due km
    if this.completion_pct is not None and this.completion_pct < float(rules["completion_min_pct"]):
        cap_next("A1", "repeat_volume", this.planned_km,
                 f"You ran {_fmt(this.completion_pct)}% of this week's due km (under {rules['completion_min_pct']}%), "
                 "so next week repeats this week's {to} km instead of {frm} km.",
                 "completion_pct", this.completion_pct, rules["completion_min_pct"])
    # A2: HRV down or short nights; ACWR only with another signal
    acwr_high = health.acwr is not None and health.acwr > float(rules["acwr_max"])
    acwr_ev = {"metric": "acwr", "value": health.acwr, "threshold": rules["acwr_max"]} if acwr_high else None
    deload_to = this.planned_km * float(rules["deload_factor"])
    a2_text = " Next week becomes a deload at {to} km instead of {frm} km."
    if "hrv_down" in health.flags:
        drop = round(health.hrv_28d - health.hrv_7d, 1) if health.hrv_7d is not None and health.hrv_28d is not None \
            else None
        lead = (f"Your 7-day HRV average is {_fmt(drop)} ms below the 28-day average (threshold {rules['hrv_drop_ms']} ms)."
                if drop is not None else "Your 7-day HRV average is below Garmin's balanced range.")
        cap_next("A2", "early_deload", deload_to, lead + a2_text, "hrv_drop_ms", drop, rules["hrv_drop_ms"],
                 also=[acwr_ev] if acwr_ev else None)
    elif "short_nights" in health.flags:
        cap_next("A2", "early_deload", deload_to,
                 f"You had {health.short_nights} nights under 6 h this week (threshold {rules['short_nights']})."
                 + a2_text, "short_nights", health.short_nights, rules["short_nights"],
                 also=[acwr_ev] if acwr_ev else None)
    elif acwr_high and this.easy_rpe is not None and this.easy_rpe >= float(rules["acwr_easy_rpe"]):
        cap_next("A2", "early_deload", deload_to,
                 f"Garmin's ACWR is {health.acwr} (above {rules['acwr_max']}) and your easy runs felt RPE "
                 f"{_fmt(this.easy_rpe)} (threshold {rules['acwr_easy_rpe']})." + a2_text,
                 "acwr", health.acwr, rules["acwr_max"],
                 also=[{"metric": "easy_rpe_mean", "value": this.easy_rpe, "threshold": rules["acwr_easy_rpe"]}])
    # A3: a missed long run
    if this.long is not None and this.long_status == "missed" and future and nxt_long is not None \
            and this.long.km < nxt_long.km - EPS_KM:
        changes.append(_change(
            plan_id, week_start, "A3", "long_run_km", {"key": nxt_long.key, "date": nxt_long.date.isoformat(),
                                                       "km": nxt_long.km},
            {"key": nxt_long.key, "date": nxt_long.date.isoformat(), "km": this.long.km},
            f"This week's {_fmt(this.long.km)} km long run was missed, so next week's long run stays at "
            f"{_fmt(this.long.km)} km instead of {_fmt(nxt_long.km)} km.",
            "missed_long_km", this.long.km, nxt_long.km,
            [{"op": "cap_long", "week_start": nxt_start.isoformat(), "km": this.long.km}]))
    # A4: easy runs felt hard two weeks in a row
    if this.easy_rpe is not None and prev.easy_rpe is not None \
            and this.easy_rpe >= float(rules["easy_rpe"]) and prev.easy_rpe >= float(rules["easy_rpe"]):
        slow = float(rules["easy_band_slowdown"])
        current = float(body.get("inputs", {}).get("easy_band_slowdown") or 0.0) if isinstance(body.get("inputs"),
                                                                                              dict) else 0.0
        target = round(min(MAX_EASY_BAND_SLOWDOWN, current + slow), 3)
        ev = ("easy_rpe_mean", this.easy_rpe, rules["easy_rpe"])
        lead = (f"Your easy runs averaged RPE {_fmt(this.easy_rpe)} this week and {_fmt(prev.easy_rpe)} last week "
                f"(threshold {rules['easy_rpe']})")
        if target > current + 1e-9:
            changes.append(_change(plan_id, week_start, "A4", "easy_band", {"slowdown": current},
                                   {"slowdown": target},
                                   lead + f", so the easy and long paces become {round(slow * 100)}% slower.",
                                   *ev, [{"op": "easy_band", "slowdown": target}]))
        cut = float(rules["easy_rpe_week_cut"])
        if future and nxt_km > 0:
            to = Km(round(nxt_km * (1 - cut), 1))
            changes.append(_change(plan_id, week_start, "A4", "cut_week", {"week_start": nxt_start.isoformat(),
                                                                           "km": nxt_km},
                                   {"week_start": nxt_start.isoformat(), "km": to},
                                   lead + f", so next week is {round(cut * 100)}% shorter: {_fmt(to)} km instead "
                                   f"of {_fmt(nxt_km)} km.", *ev,
                                   [{"op": "cut_week", "week_start": nxt_start.isoformat(), "share": cut}]))
    # A5: quality above target at a low RPE
    best: Optional[Tuple[float, Session, int]] = None
    for s in this.sessions:
        if s.role not in pe.QUALITY_ROLES or s.key not in feedback or not s.minutes or s.km <= 0:
            continue
        if links.get(s.key) not in DONE + PARTIAL or feedback[s.key].rpe > int(rules["quality_rpe_max"]):
            continue
        aid = linked.get(s.key)
        run = runs.get(aid) if aid is not None else None
        if run is None or run.speed_mps is None:
            continue
        ratio = round(run.speed_mps / (s.km * 1000.0 / (s.minutes * 60.0)), 3)
        if ratio >= float(rules["quality_speed_over"]) and (best is None or ratio > best[0]):
            best = (ratio, s, feedback[s.key].rpe)
    if best is not None:
        ratio, s, rpe = best
        changes.append(_change(plan_id, week_start, "A5", "threshold_retest", {"key": s.key},
                               {"suggest": "threshold_retest"},
                               f"Your {s.role} session on {s.date.isoformat()} averaged {round((ratio - 1) * 100, 1)}% "
                               f"faster than planned at RPE {rpe} (thresholds {round((float(rules['quality_speed_over']) - 1) * 100)}% "
                               f"and RPE {rules['quality_rpe_max']}), so a threshold retest is suggested.",
                               "quality_speed_vs_plan", ratio, rules["quality_speed_over"], [], ADVICE_RETEST))
    # A6: pain
    if (pain_max is not None and pain_max >= int(rules["pain"])) or sharp:
        what = f"pain {pain_max}" + (" (sharp)" if sharp else "")
        metric, value, threshold = (("sharp", True, True) if sharp and (pain_max or 0) < int(rules["pain"])
                                    else ("pain", pain_max, rules["pain"]))
        held = cap_next("A6", "hold_progression", min(this.planned_km, nxt_km),
                        f"You reported {what} this week (threshold {rules['pain']} or sharp), so next week holds "
                        "this week's {to} km instead of {frm} km.", metric, value, threshold, ADVICE_PAIN)
        if not held and not (future and nxt_quality):
            # Nothing left to hold or drop (or no next week): the advice is still a change of its own.
            changes.append(_change(
                plan_id, week_start, "A6", "pain_advice", {"pain": pain_max, "sharp": sharp},
                {"advice": "see a physiotherapist or doctor"},
                f"You reported {what} this week (threshold {rules['pain']} or sharp). Next week already holds "
                "this week's load, so the plan stays as it is; if the pain persists or is sharp, see a "
                "physiotherapist or doctor.", metric, value, threshold, [], ADVICE_PAIN))
        if future and nxt_quality:
            changes.append(_change(
                plan_id, week_start, "A6", "drop_quality",
                {"week_start": nxt_start.isoformat(), "quality": [s.role for s in nxt_quality]},
                {"week_start": nxt_start.isoformat(), "quality": []},
                f"You reported {what} this week (threshold {rules['pain']} or sharp), so next week's "
                f"{len(nxt_quality)} quality session{'s' if len(nxt_quality) != 1 else ''} become easy runs.",
                metric, value, threshold, [{"op": "easy_quality", "week_start": nxt_start.isoformat()}],
                ADVICE_PAIN))
    # A7: after a missed week, at most 1.10 x the last week that was done
    if this.due_km > 0 and this.done_km <= 0:
        back = week_start - dt.timedelta(days=7)
        first = min((s.date for s in sessions), default=week_start)
        while back >= pe.monday(first):
            facts = week_facts(sessions, back, links, linked, runs, feedback)
            if facts.done_km > 0:
                cap_next("A7", "cap_after_missed_week", facts.done_km * float(rules["missed_week_cap"]),
                         f"This week was missed, so next week is at most {rules['missed_week_cap']} x the "
                         f"{_fmt(facts.done_km)} km of the week of {back.isoformat()}: {{to}} km instead of {{frm}} km.",
                         "last_done_week_km", facts.done_km, rules["missed_week_cap"])
                break
            back -= dt.timedelta(days=7)
    # A8: a red day with a quality session today
    todays = next((s for s in sessions if s.date == today and s.role in pe.QUALITY_ROLES), None)
    if todays is not None and health.today_level == rules["red_level"]:
        changes.append(_change(plan_id, week_start, "A8", "easy_today",
                               {"key": todays.key, "date": today.isoformat(), "role": todays.role},
                               {"key": todays.key, "date": today.isoformat(), "role": "easy"},
                               f"Today's daily status is red (rule level {rules['red_level']}), so today's "
                               f"{todays.role} session becomes an easy run of the same distance (or rest).",
                               "daily_level",
                               health.today_level,
                               rules["red_level"], [{"op": "easy_day", "date": today.isoformat()}]))
    metrics = {
        "week_start": week_start.isoformat(), "planned_km": this.planned_km, "due_km": this.due_km,
        "done_km": this.done_km, "completion_pct": this.completion_pct, "sessions": this.counts,
        "long_run": ({"key": this.long.key, "km": this.long.km, "status": this.long_status}
                     if this.long is not None else None),
        "easy_rpe_mean": this.easy_rpe, "easy_rpe_mean_prev_week": prev.easy_rpe,
        "pain_max": pain_max, "sharp": sharp,
        "hrv": {"avg_7d": health.hrv_7d, "avg_28d": health.hrv_28d}, "short_nights": health.short_nights,
        "acwr": health.acwr, "flags": list(health.flags), "today_level": health.today_level,
        "next_week": {"week_start": nxt_start.isoformat(), "planned_km": nxt_km if nxt else None},
    }
    return {"rules_version": REVIEW_RULES_VERSION, "metrics": metrics, "changes": changes}

# --- accepted changes -> a patched version of the stored plan ------------------------------------------------------
#
# The accepted changes patch the stored version the athlete reviewed: every stored session stays as it is (the rest
# of the current week too), and only the sessions a change targets are shortened, dropped or turned into easy runs
# at the bands the stored plan already uses. Nothing is rebuilt from today's numbers.

def inputs_from_echo(echo: Mapping[str, Any]) -> pe.PlanInputs:
    """PlanInputs from PlanInputs.echo() (a stored version's inputs). Raises ValueError or KeyError when the
    stored inputs are not an echo."""
    def day(value: Any) -> Optional[dt.date]:
        return dt.date.fromisoformat(str(value)) if value else None

    overrides = []
    for o in echo.get("overrides") or []:
        weekday = o.get("weekday")
        km_value = o.get("km")
        overrides.append(pe.Override(
            kind=str(o["kind"]), key=str(o.get("key") or ""), date=day(o.get("date")), date_b=day(o.get("date_b")),
            week_start=day(o.get("week_start")), km=float(km_value) if km_value is not None else None,
            weekday=pe.WEEKDAYS.index(weekday) if isinstance(weekday, str) else None))
    goal = echo.get("goal_time_s")
    course = echo.get("course_id")
    return pe.PlanInputs(
        race_date=dt.date.fromisoformat(str(echo["race_date"])), race_distance=str(echo["race_distance"]),
        goal_time_s=int(goal) if goal is not None else None,
        b_races=tuple(pe.BRace(dt.date.fromisoformat(str(b["date"])), str(b["kind"])) for b in echo.get("b_races") or []),
        run_days=tuple(pe.WEEKDAYS.index(d) for d in echo["run_days"]),
        rest_days=tuple(pe.WEEKDAYS.index(d) for d in echo["rest_days"]),
        long_run_day=pe.WEEKDAYS.index(str(echo["long_run_day"])), quality_per_week=int(echo["quality_per_week"]),
        max_long_km=float(echo["max_long_km"]),
        blocked_dates=tuple(dt.date.fromisoformat(str(d)) for d in echo.get("blocked_dates") or []),
        overrides=tuple(overrides), long_share=float(echo.get("long_share", 0.5)),
        course_id=int(course) if course is not None else None,
        easy_band_slowdown=float(echo.get("easy_band_slowdown") or 0.0))


def _weeks(plan: Mapping[str, Any]) -> List[Dict[str, Any]]:
    weeks = plan.get("weeks")
    return [w for w in weeks if isinstance(w, dict)] if isinstance(weeks, list) else []


def _week_of(plan: Mapping[str, Any], start: str) -> Optional[Dict[str, Any]]:
    return next((w for w in _weeks(plan) if w.get("start") == start
                 or (str(w.get("start")) <= start <= str(w.get("end")) and w.get("kind") == "week0")), None)


def _runs(week: Mapping[str, Any]) -> List[Dict[str, Any]]:
    return [s for s in week.get("sessions") or [] if isinstance(s, dict) and s.get("role") not in ("race", "b_race")]


def done_km(body: Mapping[str, Any], links: Mapping[str, Optional[str]],
            linked: Optional[Mapping[str, Optional[int]]] = None,
            runs: Optional[Mapping[int, Run]] = None) -> Dict[str, float]:
    """Session key -> the km already run for it (as week_facts counts them: a done or partial run up to the
    planned km; a done run without a distance the planned km, a partial one nothing). Unlisted keys: nothing."""
    out: Dict[str, float] = {}
    for s in sessions_of(body):
        status = links.get(s.key)
        if status not in DONE + PARTIAL:
            continue
        aid = (linked or {}).get(s.key)
        run = (runs or {}).get(aid) if aid is not None else None
        got = run.km if run is not None and run.km is not None else (s.km if status in DONE else 0.0)
        out[s.key] = min(float(got), float(s.km))
    return out


class _StoredPaces(pe.Paces):
    """The pace bands the stored plan's own sessions carry (easy, long, strides), so a patched session gets the
    bands its neighbours have; the easy and long bands `factor` times (the easy-band slowdown)."""

    def __init__(self, bands: Mapping[str, Tuple[float, float]], factor: float = 1.0) -> None:
        super().__init__(pe.MetersPerSecond(1.0) if bands else None, None, pe.RULES)
        self.bands = dict(bands)
        self.factor = factor

    def band(self, name: Optional[str]) -> Optional[Tuple[pe.MetersPerSecond, pe.MetersPerSecond]]:
        b = self.bands.get(name or "")
        if b is None:
            return None
        f = self.factor if name in ("easy", "long") else 1.0
        return pe.MetersPerSecond(round(b[0] * f, 3)), pe.MetersPerSecond(round(b[1] * f, 3))


def _speed(step: Any) -> Optional[Tuple[float, float]]:
    v = step.get("speed_mps") if isinstance(step, dict) else None
    return (float(v[0]), float(v[1])) if isinstance(v, list) and len(v) == 2 else None


def _bands(body: Mapping[str, Any]) -> Dict[str, Tuple[float, float]]:
    out: Dict[str, Tuple[float, float]] = {}
    for w in _weeks(body):
        for s in _runs(w):
            steps = [st for st in s.get("steps") or [] if isinstance(st, dict)]
            first = _speed(steps[0]) if steps else None
            if s.get("role") in ("easy", "long") and first:
                out.setdefault(str(s["role"]), first)
            if s.get("role") == "strides":
                if first:
                    out.setdefault("easy", first)
                for st in steps:
                    for c in st.get("steps") or [] if st.get("type") == "repeat" else []:
                        if isinstance(c, dict) and c.get("type") == "interval" and _speed(c):
                            out.setdefault("strides", _speed(c) or (0.0, 0.0))
    return out


def _floor_m(meters: float) -> float:
    return float(int(max(meters, 0.0) // 100) * 100)


def _remake(s: Mapping[str, Any], role: str, meters: float, paces: pe.Paces) -> Dict[str, Any]:
    """Session `s` as `role` over `meters`, at the bands `paces` gives (plan_engine's own session builder)."""
    made = pe.make_session(str(s["key"]), dt.date.fromisoformat(str(s["date"])), role, pe.Meters(meters), paces,
                           pe.RULES)
    return pe.session_json(made, paces)


def _totals(week: Dict[str, Any]) -> None:
    runs = _runs(week)
    week["planned_km"] = round(sum(float(s.get("km") or 0.0) for s in runs), 1)
    longs = [float(s.get("km") or 0.0) for s in runs if s.get("role") == "long"]
    week["long_km"] = max(longs) if longs else None
    week["sessions"] = sorted(week.get("sessions") or [], key=lambda s: (str(s.get("date")), str(s.get("key"))))


class _Refused(Exception):
    def __init__(self, rule: str, value: Any, limit: Any) -> None:
        super().__init__(rule)
        self.rule, self.value, self.limit = rule, value, limit


def _week_for(body: Dict[str, Any], start: str, today: dt.date) -> Dict[str, Any]:
    week = _week_of(body, start)
    if week is None or str(week.get("end")) < today.isoformat():
        raise _Refused("past_week", start, today.isoformat())
    return week


def _cap_week(body: Dict[str, Any], week: Dict[str, Any], limit_km: float, today: dt.date,
              done: Optional[Mapping[str, float]]) -> bool:
    """Cut the week's runs after today to `limit_km` for the whole week, the runs up to today counted as run
    (`done`: the km already run; without it, as planned). Easy runs first, the longest first (dropped below the
    easy minimum), then the long run, then the quality sessions from the last; like plan_engine's week cap."""
    paces = _StoredPaces(_bands(body))
    easy_min = float(pe.RULES["easy_min_km"]) * 1000.0
    fixed = 0.0
    for s in _runs(week):
        if str(s["date"]) < today.isoformat():
            fixed += float(s.get("km") or 0.0) if done is None else float(done.get(str(s["key"]), 0.0))
        elif str(s["date"]) == today.isoformat():
            fixed += float(s.get("km") or 0.0)
    limit = limit_km * 1000.0 - fixed * 1000.0
    changed = False
    for _ in range(40):
        future = [s for s in _runs(week) if str(s["date"]) > today.isoformat()]
        total = sum(float(s.get("km") or 0.0) * 1000.0 for s in future)
        if total <= max(limit, 0.0) + 50.0:
            break
        excess = total - max(limit, 0.0)
        easies = [s for s in future if s.get("role") in EASY_ROLES]
        longs = [s for s in future if s.get("role") == "long"]
        quality = [s for s in future if s.get("role") in pe.QUALITY_ROLES]
        pick = (max(easies, key=lambda x: (float(x.get("km") or 0.0), str(x["key"]))) if easies
                else longs[0] if longs else quality[-1] if quality else None)
        if pick is None:
            break
        sessions = week["sessions"]
        new_m = _floor_m(float(pick.get("km") or 0.0) * 1000.0 - excess)
        if pick.get("role") in pe.QUALITY_ROLES or new_m < easy_min:
            sessions.remove(pick)
        else:
            sessions[sessions.index(pick)] = _remake(pick, str(pick["role"]), new_m, paces)
        changed = True
    _totals(week)
    return changed


def _long_steps(base: Mapping[str, Any], after: str, km: float) -> List[Tuple[str, float]]:
    """A shorter long run on `after` keeps the plan's 2 km long-run step: each later long run of a normal
    week is capped at the shortened one plus one step per such long run since (only where the plan has more),
    so the step rule never refuses the change. [(date, km)]; they only lower load."""
    step = float(pe.RULES["long_step_m"]) / 1000.0
    out: List[Tuple[str, float]] = []
    limit = km
    for w in _weeks(base):
        if w.get("kind") != "normal":   # a reduced week's long run is held under the longest so far anyway
            continue
        for s in _runs(w):
            if s.get("role") != "long" or str(s.get("date")) <= after:
                continue
            limit = round(limit + step, 1)
            if float(s.get("km") or 0.0) <= limit + EPS_KM:
                return out
            out.append((str(s["date"]), limit))
    return out


def _keep_long_steps(body: Dict[str, Any], today: dt.date) -> bool:
    """After a long run got shorter (or was dropped): each later long run of a normal week at most the longest
    run before it + 2 km (plan_engine's long_step), so the shortening never breaks the step rule further on.
    Only lowers; True when a run changed."""
    step = float(pe.RULES["long_step_m"]) / 1000.0
    start = body.get("start") if isinstance(body.get("start"), dict) else {}
    longest = float((start or {}).get("longest_run_8w_km") or 0.0)
    changed = False
    for w in _weeks(body):
        if w.get("kind") != "normal":
            continue
        for s in [x for x in _runs(w) if x.get("role") == "long"]:
            limit = round(longest + step, 1) if longest else None
            if limit is not None and str(s["date"]) > today.isoformat() and float(s.get("km") or 0.0) > limit + EPS_KM \
                    and limit >= float(pe.RULES["easy_min_km"]):
                changed = _cap_run(body, str(s["date"]), limit) or changed
        lmax = max((float(x.get("km") or 0.0) for x in _runs(w) if x.get("role") == "long"), default=0.0)
        longest = max(longest, lmax)
    return changed


def _cap_run(body: Dict[str, Any], day: str, km: float) -> bool:
    for w in _weeks(body):
        for s in _runs(w):
            if str(s.get("date")) == day and s.get("role") in ("easy", "strides", "long") \
                    and km < float(s.get("km") or 0.0) - EPS_KM:
                if km < float(pe.RULES["easy_min_km"]):
                    raise _Refused("easy_min_km", km, float(pe.RULES["easy_min_km"]))
                w["sessions"][w["sessions"].index(s)] = _remake(s, str(s["role"]), _floor_m(km * 1000.0),
                                                                _StoredPaces(_bands(body)))
                _totals(w)
                return True
    return False


def _make_easy(body: Dict[str, Any], week: Dict[str, Any], s: Dict[str, Any]) -> None:
    week["sessions"][week["sessions"].index(s)] = _remake(s, "easy", float(s.get("km") or 0.0) * 1000.0,
                                                          _StoredPaces(_bands(body)))
    _totals(week)


def _slow_down(body: Dict[str, Any], slowdown: float, today: dt.date) -> bool:
    inputs = body.setdefault("inputs", {})
    current = float(inputs.get("easy_band_slowdown") or 0.0)
    target = min(MAX_EASY_BAND_SLOWDOWN, float(slowdown))
    if target <= current + 1e-9:
        return False
    paces = _StoredPaces(_bands(body), (1 - target) / (1 - current))
    mid = paces.mid("easy")
    # An easy run stays under easy_max_min at the slower band (plan_engine's own cap), so the daily report
    # still reads it as easy, not long.
    cap = _floor_m(mid * float(pe.RULES["easy_max_min"]) * 60.0) if mid is not None else None
    for w in _weeks(body):
        for s in _runs(w):
            if str(s["date"]) > today.isoformat() and s.get("role") in ("easy", "strides", "long"):
                meters = float(s.get("km") or 0.0) * 1000.0
                if s.get("role") == "easy" and cap is not None and meters > cap:
                    meters = cap
                new = _remake(s, str(s["role"]), meters, paces)
                if s.get("role") == "strides":
                    # the timed jogs cover less ground at the slower band and the easy part is rounded down to
                    # 100 m: give that 100 m back when it keeps the stored distance (never longer), so strides
                    # at the easy minimum stay at it
                    more = _remake(s, "strides", meters + 100.0, paces)
                    if float(new["km"]) < float(s.get("km") or 0.0) and \
                            abs(float(more["km"]) - float(s.get("km") or 0.0)) < 1e-9:
                        new = more
                w["sessions"][w["sessions"].index(s)] = new
        _totals(w)
    inputs["easy_band_slowdown"] = round(target, 3)
    return True


def _apply_edit(body: Dict[str, Any], e: Mapping[str, Any], today: dt.date,
                done: Optional[Mapping[str, float]], stored: Optional[Mapping[str, Any]] = None) -> bool:
    """Apply one edit to `body` in place; True when it changed a session. Raises _Refused. A cut_week share is
    of the week's km in `stored` (the version reviewed, default `body`), so two cuts of one week keep the lower
    in either order."""
    op = e.get("op")
    if op == "easy_band":
        return _slow_down(body, float(e["slowdown"]), today)
    if op == "easy_day":
        day = str(e["date"])
        if day < today.isoformat():
            raise _Refused("past_day", day, today.isoformat())
        for w in _weeks(body):
            for s in _runs(w):
                if str(s.get("date")) == day and s.get("role") in pe.QUALITY_ROLES:
                    _make_easy(body, w, s)
                    return True
        return False
    if op not in ("cap_week", "cut_week", "cap_long", "easy_quality"):
        raise _Refused("unknown_edit", op, None)
    week = _week_for(body, str(e.get("week_start")), today)
    if op in ("cap_week", "cut_week"):
        if op == "cap_week":
            limit = float(e["km"])
        else:
            base = _week_of(stored, str(e.get("week_start"))) if stored is not None else None
            limit = float((base or week).get("planned_km") or 0.0) * (1 - float(e["share"]))
        long_before = [s for s in _runs(week) if s.get("role") == "long"]
        changed = _cap_week(body, week, round(limit, 1), today, done)
        if changed and long_before != [s for s in _runs(week) if s.get("role") == "long"]:
            _keep_long_steps(body, today)
        return changed
    if op == "cap_long":
        long = next((s for s in _runs(week) if s.get("role") == "long" and str(s["date"]) > today.isoformat()), None)
        if long is None or float(e["km"]) >= float(long.get("km") or 0.0) - EPS_KM:
            return False
        changed = _cap_run(body, str(long["date"]), float(e["km"]))
        return _keep_long_steps(body, today) or changed
    changed = False
    for s in list(_runs(week)):
        if s.get("role") in pe.QUALITY_ROLES and str(s["date"]) > today.isoformat():
            _make_easy(body, week, s)
            changed = True
    return changed


def _hard_days(plan: Mapping[str, Any], after: dt.date) -> Set[str]:
    return {str(s.get("date")) for w in _weeks(plan) for s in _runs(w)
            if s.get("hard_or_long", s.get("role") in pe.HARD_ROLES) and str(s.get("date")) > after.isoformat()}


def _by_date(plan: Mapping[str, Any], after: dt.date) -> Dict[str, Tuple[float, Optional[float], int]]:
    """Calendar week (Monday) -> (km of runs after `after`, the long run's km, quality sessions)."""
    out: Dict[str, Tuple[float, Optional[float], int]] = {}
    for w in _weeks(plan):
        for s in _runs(w):
            if str(s.get("date")) <= after.isoformat():
                continue
            monday = pe.monday(dt.date.fromisoformat(str(s["date"]))).isoformat()
            total, long, quality = out.get(monday, (0.0, None, 0))
            km = float(s.get("km") or 0.0)
            if s.get("role") == "long":
                long = max(long or 0.0, km)
            out[monday] = (total + km, long, quality + (1 if s.get("role") in pe.QUALITY_ROLES else 0))
    return out


def no_raise(base: Mapping[str, Any], new: Mapping[str, Any], today: dt.date) -> List[str]:
    """Problems when `new` has more load than `base` after today: a calendar week with more km, a longer long
    run or more quality sessions, or a hard-or-long day `base` does not have. Empty: it only holds or
    reduces."""
    problems = []
    a, b = _by_date(base, today), _by_date(new, today)
    for monday, (km, long, quality) in sorted(b.items()):
        km0, long0, quality0 = a.get(monday, (0.0, None, 0))
        if km > km0 + EPS_KM:
            problems.append(f"week of {monday}: {round(km, 1)} km > {round(km0, 1)} km")
        if long is not None and long > (long0 or 0.0) + EPS_KM:
            problems.append(f"week of {monday}: long run {long} km > {long0} km")
        if quality > quality0:
            problems.append(f"week of {monday}: {quality} quality sessions > {quality0}")
    extra = sorted(_hard_days(new, today) - _hard_days(base, today))
    if extra:
        problems.append("new hard or long days: " + ", ".join(extra[:5]))
    return problems


def _step_time(step: Mapping[str, Any], by_time: bool) -> float:
    if step.get("type") == "repeat":
        return float(step.get("times") or 1) * sum(_step_time(c, by_time) for c in step.get("steps") or []
                                                   if isinstance(c, dict))
    if "time_s" in step:
        return float(step["time_s"]) if by_time else 0.0
    dist = float(step.get("distance_m") or 0.0)
    speed = _speed(step)
    return dist / ((speed[0] + speed[1]) / 2) if by_time and speed else dist


def _hard_time(step: Mapping[str, Any], by_time: bool, quality: bool) -> float:
    if step.get("type") == "repeat":
        return float(step.get("times") or 1) * sum(_hard_time(c, by_time, quality) for c in step.get("steps") or []
                                                   if isinstance(c, dict))
    return _step_time(step, by_time) if quality and step.get("type") == "interval" else 0.0


def patch_problems(body: Mapping[str, Any], today: dt.date) -> Dict[str, str]:
    """The plan rules a patch that only shortens or eases sessions could break, checked on the stored session
    JSON from today on: long_step (a normal week's long run at most the longest run so far + 2 km), easy_min_km
    (no easy, strides or long run under the easy minimum) and easy_share (quality work at most 20% of a week's
    time). {rule: detail}."""
    rules = pe.RULES
    out: Dict[str, List[str]] = {}
    step_km = float(rules["long_step_m"]) / 1000.0
    start = body.get("start") if isinstance(body.get("start"), dict) else {}
    longest = float((start or {}).get("longest_run_8w_km") or 0.0)
    by_time = all(_speed(st) is not None or "time_s" in st or st.get("type") == "repeat"
                  for w in _weeks(body) for s in _runs(w) for st in s.get("steps") or [] if isinstance(st, dict))
    for w in _weeks(body):
        runs = _runs(w)
        later = [s for s in runs if str(s.get("date")) > today.isoformat()]
        lmax = max((float(s.get("km") or 0.0) for s in runs if s.get("role") == "long"), default=0.0)
        if w.get("kind") == "normal":
            if later and longest and lmax > longest + step_km + EPS_KM:
                out.setdefault("long_step", []).append(f"week of {w.get('start')}: long {lmax} km > "
                                                       f"{round(longest, 1)} + {step_km} km")
            longest = max(longest, lmax)
        for s in later:
            if s.get("role") in ("easy", "strides", "long") and float(s.get("km") or 0.0) < \
                    float(rules["easy_min_km"]) - EPS_KM:
                out.setdefault("easy_min_km", []).append(f"{s.get('date')}: {s.get('km')} km")
        if not later:
            continue
        hard = total = 0.0
        for s in runs:
            quality = s.get("role") in pe.QUALITY_ROLES
            for st in s.get("steps") or []:
                if isinstance(st, dict):
                    hard += _hard_time(st, by_time, quality)
                    total += _step_time(st, by_time)
        if total > 0 and hard / total > 1 - float(rules["easy_share_min"]) + 1e-6:
            out.setdefault("easy_share", []).append(f"week of {w.get('start')}: {round(100 * (1 - hard / total))}% easy")
    return {k: "; ".join(v[:5]) for k, v in out.items()}


def apply_changes(body: Mapping[str, Any], changes: Sequence[Mapping[str, Any]], today: dt.date,
                  done: Optional[Mapping[str, float]] = None
                  ) -> Tuple[Dict[str, Any], List[str], List[Dict[str, Any]], List[str]]:
    """(the stored plan `body` patched with the accepted changes, accepted ids, refused [{change_id, rule,
    value, limit}], applied ids). Each change is tried in order on the plan patched so far: its edits change
    only the sessions after today they target (today's only for an easy_day edit of today); a change whose
    patch breaks a plan rule the stored plan kept (patch_problems) or has more load than the stored plan
    (no_raise: rule raises_load) is undone and refused. Accepted are the applied changes and the ones that
    needed no edit (advice, or the plan already at or below them); applied only those that changed a session.
    A lower cap refused leaves the next one of the same week to apply. The new content_hash covers the patched
    weeks and the inputs (with the easy-band slowdown)."""
    import copy

    stored = dict(body)
    work: Dict[str, Any] = copy.deepcopy(stored)
    before = patch_problems(stored, today)
    accepted: List[str] = []
    applied: List[str] = []
    refused: List[Dict[str, Any]] = []
    for c in changes:
        cid = str(c["change_id"])
        trial = copy.deepcopy(work)
        try:
            changed = False
            for e in c.get("edits") or []:
                changed = _apply_edit(trial, e, today, done, stored) or changed
        except _Refused as exc:
            refused.append({"change_id": cid, "rule": exc.rule, "value": exc.value, "limit": exc.limit})
            continue
        except (KeyError, TypeError, ValueError):
            refused.append({"change_id": cid, "rule": "bad_edit", "value": None, "limit": None})
            continue
        if changed:
            new = {k: v for k, v in patch_problems(trial, today).items() if k not in before}
            raised = no_raise(stored, trial, today)
            if new:
                rule = sorted(new)[0]
                refused.append({"change_id": cid, "rule": rule, "value": new[rule], "limit": "the plan's rules"})
                continue
            if raised:
                refused.append({"change_id": cid, "rule": "raises_load", "value": raised[0],
                                "limit": "never above the plan"})
                continue
            work = trial
            applied.append(cid)
        accepted.append(cid)
    if applied:
        review = dict(work.get("review") or {}) if isinstance(work.get("review"), dict) else {}
        review["rules_version"] = REVIEW_RULES_VERSION
        review["applied"] = list(review.get("applied") or []) + applied
        work["review"] = review
        work["content_hash"] = pe.content_hash(int(work.get("rules_version") or pe.RULES_VERSION),
                                               work.get("inputs") or {}, work.get("weeks") or [])
    return work, accepted, refused, applied
