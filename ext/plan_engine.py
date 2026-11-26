"""The training-plan engine (rules v2): a race plan built by fixed, versioned rules.

Pure and standard library only, like daily_status' rules: build_plan(inputs, snapshot) takes the athlete's
numbers (plan_tools reads them from Garmin) and returns the plan as a JSON-ready dict. The same inputs give
the same plan, byte for byte (content_hash). Code decides every number; a chat model only explains them.
docs/training-plan.md ("Plan rules v2") says the rules in words; RULES holds every number (the ACWR and
race-pacing rules are echoed in their blocks of the answer).

What it does (Engine v1, items 1-11 of the plan; rules v2 adds 12 and 13):
 1. weeks Monday to Sunday up to the A-race week (week 1 starts the next Monday; week 0 is the rest of this
    week, easy runs only); phases taper / peak / base / build;
 2. weekly km: km0 = mean of the last 4 full weeks; +8% per normal week at most, on the last normal week
    as planned (min(target, what was placed); days lost to blocked dates or fixed sessions do not count);
    never above the peak ceiling max(1.3 x the biggest week of the last 8, km0 + 25 km), held once reached; a
    deload (x0.78 of the last normal week) after every 3 normal weeks, and a B-race block counts as one (the
    deload due just before it is skipped); no make-up after a reduced week; taper x0.70 and race week x0.45 of
    the peak week (as planned), the race itself not counted;
 3. long run in normal weeks <= min(max(0.50 x week km, longest run of the last 8 weeks), 3 h at the slow edge
    of its pace band, the longest long run so far + 2 km, 32 km); reduced weeks scale it; no long run within
    21 days of the A race is longer than 0.80 x the peak long run, so the peak long run is >= 21 days out;
 4. quality by phase with dose caps (threshold <= 10%, intervals <= 8%, marathon pace <= 20% of week km) and
    >= 80% of the week's time easy; strides stay <= 8 x 100 m so daily_status' classifier keeps them easy;
 5. pace bands as fractions of the lactate-threshold speed (no LT speed: steps without targets, "missing");
 6. placement: long run, Q1, Q2 so that no two hard-or-long days touch (fixed calendar sessions, the races
    and the next and previous weeks included), with fallbacks that each warn with dates;
 7. B races: a 4-day mini-taper (x0.6, strides only, each run <= 8 km), the race, then easy recovery (x0.5,
    6 days; 7 and a warning for a full-distance triathlon); a long run 5-13 days before a B race and the
    first long run after it <= 0.70 x the peak long run so far (0.60 after a full-distance triathlon, and
    not within 10 days of it: an easy run instead), the km taken off not made up (warning b_race_capped);
    peak weeks inside a B block become B weeks (warning b_race_in_peak); the A taper never moves;
 8. feasibility (never raises growth to meet a goal) with corrected_goal_s;
 9. check_invariants(): every rule above checked on the finished plan;
10. what_if(): goal +-5 min and a long-run share of 0.35 and 0.45;
11. blocked dates (no run, their share of the week dropped, not spread over other days) and overrides that only move or reduce load;
12. planned all-sport ACWR (v2): each day's load = minutes x the sport's load per minute from the recent
    activity list (fixed calendar sessions count with their own sport); acute = the last 7 days, chronic =
    the last 28 days / 4, as Garmin's. While the model tracks Garmin's reported ratio within 0.15 on the
    history, a week whose planned ratio passes 1.3 has its run km cut (never raised, never below 0.6 x its
    target); otherwise the ratio is only reported, with a warning;
13. race pacing (v2): with a course, per-segment paces for the A race at the goal pace, slower on climbs
    (course_profile's climbs) and the flats faster, so the course takes the goal time.

Units: Meters, Seconds and MetersPerSecond are NewTypes, converted to km, minutes and min/km only in the
answer, so mypy catches a km/m or pace/speed mix-up.
"""

from __future__ import annotations

import copy
import dataclasses
import datetime as dt
import hashlib
import itertools
import json
import math
from typing import Any, Callable, Dict, Iterable, List, Mapping, NewType, Optional, Sequence, Set, Tuple

Meters = NewType("Meters", float)
Seconds = NewType("Seconds", float)
MetersPerSecond = NewType("MetersPerSecond", float)

RULES_VERSION = 2

DISTANCES: Dict[str, Meters] = {"marathon": Meters(42195.0), "half": Meters(21097.5)}
B_RACE_KINDS = ("tri_full", "tri_half", "run")
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
ROLES = ("easy", "strides", "long", "threshold", "intervals", "mp", "race", "b_race")
QUALITY_ROLES = ("threshold", "intervals", "mp")
HARD_ROLES = frozenset({"long", "threshold", "intervals", "mp", "race", "b_race"})
OVERRIDE_KINDS = ("move", "swap", "rest", "easy", "cap_week_km", "long_run_day", "cap_km")
# The weekly review (plan_review, Stage 4) may slow the easy and long bands by at most this share.
MAX_EASY_BAND_SLOWDOWN = 0.10

RULES: Dict[str, Any] = {
    "week_start": "monday",
    "km0_weeks": 4,
    "history_weeks": 8,
    "growth_max": 0.08,
    "peak_ceiling": {"of_max_week": 1.3, "over_km0_km": 25},
    "normal_weeks_before_deload": 3,
    "deload_factor": 0.78,
    "taper_factor": 0.70,
    "race_week_factor": 0.45,
    "race_week_pro_rata": True,
    "taper_weeks": {"n_min_2": 10},
    "peak_weeks": {"n_min_2": 11, "n_min_1": 8},
    "base_share_of_rest": 4 / 9,
    "long_share": 0.50,
    "long_max_s": 10800,
    "long_step_m": 2000,
    "max_long_km": 32.0,
    "late_long_days": 21,
    "late_long_of_peak": 0.80,
    "b_mini_taper_days": 4,
    "b_mini_factor": 0.6,
    "b_recovery_days": {"tri_full": 7, "tri_half": 6, "run": 6},
    "b_recovery_factor": 0.5,
    # rule 7: the long runs around a B race and its race-week runs (of the peak long run so far, as rule 3's)
    "b_race": {"long_before_days": [5, 13], "long_before_of_peak": 0.70, "mini_taper_max_km": 8.0,
               "long_after_of_peak": {"tri_full": 0.60, "tri_half": 0.70, "run": 0.70},
               "long_after_min_days": {"tri_full": 10, "tri_half": 0, "run": 0}},
    "dose_caps": {"threshold": 0.10, "intervals": 0.08, "mp": 0.20},
    "easy_share_min": 0.80,
    "bands_of_lt": {"easy": [0.70, 0.80], "long": [0.70, 0.80], "strides": [1.05, 1.10],
                    "threshold": [0.97, 1.03], "intervals": [1.05, 1.10]},
    "mp_band": 0.015,
    "mp_of_lt_sanity": [0.85, 0.96],
    "speed_limits_mps": [1.5, 7.0],
    "strides": {"reps": 6, "m": 100, "jog_s": 60},
    "warmup_m": 2000,
    "cooldown_m": 2000,
    "threshold_jog_s": 90,
    "intervals_jog_s": 120,
    "mp_max_km": 16,
    "mp_min_km": 3,
    "taper_mp_km": 5,
    "easy_min_km": 4.0,
    "easy_max_km": 14.0,
    "easy_max_min": 74,
    "long_min_by_classifier": 75,
    "long_km_by_classifier": 15.0,
    "last_quality_days_before_race": 3,
    "feasibility": {
        "marathon": {"min_weeks": 8, "min_km0": 25, "min_peak_km": 50, "min_long_km": 28,
                     "max_goal_of_lt": 0.95, "corrected_of_lt": 0.93},
        "half": {"min_weeks": 6, "min_km0": 15, "min_peak_km": 30, "min_long_km": 16,
                 "max_goal_of_lt": 1.02, "corrected_of_lt": 0.97},
        "ambitious_pct": 3.0,
        "unrealistic_pct": 7.0,
    },
    "what_if": {"goal_delta_s": 300, "long_shares": [0.35, 0.45]},
    # v2: planned all-sport acute:chronic workload ratio (item 12)
    "acwr": {"max": 1.3, "acute_days": 7, "chronic_days": 28, "track_tolerance": 0.15,
             "check_days_ago": [0, 14, 28], "min_checks": 2, "min_keep_of_target": 0.6, "cut_step": 0.05,
             # a B race's estimated minutes (x the all-sport load per minute); "run": a half marathon at the
             # recorded mean run speed, this default without one
             "b_race_minutes": {"tri_full": 750, "tri_half": 330, "run": 120}},
    # v2: race pacing from a course's climbs (item 13)
    "race_pacing": {"uphill_slowdown_per_grade_pct": 0.03, "course_distance_tolerance": 0.05},
}
# A jog speed for distance estimates of timed recoveries when the LT speed is unknown.
FALLBACK_JOG_MPS = MetersPerSecond(2.5)
EPS_M = 1.0   # rounding slack for the invariants, in metres


class PlanError(ValueError):
    """Inputs the engine cannot plan from; str() is a fixed text with code ids only."""


# --- inputs ---------------------------------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class BRace:
    date: dt.date
    kind: str = "tri_half"


@dataclasses.dataclass(frozen=True)
class Override:
    """One coach-critic edit. kind is one of OVERRIDE_KINDS; the fields it uses:
    move{key, date}, swap{date, date_b}, rest{date}, easy{key}, cap_week_km{week_start, km},
    long_run_day{week_start, weekday}; since Stage 4 (the weekly review) easy{date} names the run on that day
    instead of its key, and cap_km{key or date, km} shortens an easy run, strides run or long run."""

    kind: str
    key: str = ""
    date: Optional[dt.date] = None
    date_b: Optional[dt.date] = None
    week_start: Optional[dt.date] = None
    km: Optional[float] = None
    weekday: Optional[int] = None

    def echo(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"kind": self.kind}
        for name in ("key", "date", "date_b", "week_start", "km", "weekday"):
            value = getattr(self, name)
            if value in (None, ""):
                continue
            if name == "weekday" and isinstance(value, int):
                value = WEEKDAYS[value]
            out[name] = value.isoformat() if isinstance(value, dt.date) else value
        return out


@dataclasses.dataclass(frozen=True)
class PlanInputs:
    race_date: dt.date
    race_distance: str = "marathon"
    goal_time_s: Optional[int] = None
    b_races: Tuple[BRace, ...] = ()
    run_days: Tuple[int, ...] = (1, 2, 3, 5, 6)
    rest_days: Tuple[int, ...] = (0, 4)
    long_run_day: int = 6
    quality_per_week: int = 2
    max_long_km: float = 32.0
    blocked_dates: Tuple[dt.date, ...] = ()
    overrides: Tuple[Override, ...] = ()
    long_share: float = 0.50
    course_id: Optional[int] = None
    easy_band_slowdown: float = 0.0   # Stage 4 (plan_review rule A4): the easy and long bands shown this much slower

    def echo(self) -> Dict[str, Any]:
        """The inputs as JSON (hashed into content_hash); course_id and easy_band_slowdown only when given."""
        out: Dict[str, Any] = {
            "race_date": self.race_date.isoformat(), "race_distance": self.race_distance,
            "goal_time_s": self.goal_time_s,
            "b_races": [{"date": b.date.isoformat(), "kind": b.kind} for b in self.b_races],
            "run_days": [WEEKDAYS[d] for d in sorted(set(self.run_days))],
            "rest_days": [WEEKDAYS[d] for d in sorted(set(self.rest_days))],
            "long_run_day": WEEKDAYS[self.long_run_day], "quality_per_week": self.quality_per_week,
            "max_long_km": self.max_long_km,
            "blocked_dates": sorted(d.isoformat() for d in set(self.blocked_dates)),
            "overrides": [o.echo() for o in self.overrides], "long_share": self.long_share,
        }
        if self.course_id is not None:
            out["course_id"] = self.course_id
        if self.easy_band_slowdown:
            out["easy_band_slowdown"] = self.easy_band_slowdown
        return out


@dataclasses.dataclass(frozen=True)
class FixedSession:
    """A session already on the calendar that is not the plan's own (TrainingPeaks, the coach, by hand)."""

    date: dt.date
    sport: str = "run"
    hard: bool = False
    long: bool = False
    minutes: Optional[float] = None   # the workout's estimated duration (None: the sport's mean session)


@dataclasses.dataclass(frozen=True)
class ActivityLoad:
    """One recorded activity of the recent activity list: its day, sport group, duration and Garmin's
    training load (the planned-load model's per-sport load per minute comes from these)."""

    date: dt.date
    sport: str
    seconds: Seconds
    load: float
    meters: Meters = Meters(0.0)


@dataclasses.dataclass(frozen=True)
class GarminLoad:
    """Garmin's reported acute and chronic training load on one day (its training status)."""

    date: dt.date
    acute: float
    chronic: float


@dataclasses.dataclass(frozen=True)
class Climb:
    start_m: Meters
    end_m: Meters
    gain_m: Meters


@dataclasses.dataclass(frozen=True)
class Course:
    """A race course as course_profile.analyze_course reads it: its length and climbs (no names)."""

    distance_m: Meters
    climbs: Tuple[Climb, ...] = ()


@dataclasses.dataclass(frozen=True)
class Snapshot:
    """The athlete's numbers on `today` (plan_tools.fetch_snapshot reads them from Garmin)."""

    today: dt.date
    week_m: Tuple[Meters, ...] = ()            # run distance of the last full weeks, oldest first
    week_runs: Tuple[int, ...] = ()
    longest_run_8w_m: Optional[Meters] = None
    lt_speed_mps: Optional[MetersPerSecond] = None
    vo2max: Optional[float] = None
    prediction_s: Tuple[Tuple[str, int], ...] = ()   # (distance, seconds): Garmin's race predictions
    acwr: Optional[float] = None
    fixed: Tuple[FixedSession, ...] = ()
    activity_load: Tuple[ActivityLoad, ...] = ()    # every sport, the history weeks up to today
    garmin_load: Tuple[GarminLoad, ...] = ()        # Garmin's acute/chronic on the check days
    course: Optional[Course] = None                 # the A race's course (plan_preview course_id)

    def prediction(self, distance: str) -> Optional[int]:
        return next((s for d, s in self.prediction_s if d == distance), None)


# --- small helpers --------------------------------------------------------------------------------------------

def monday(day: dt.date) -> dt.date:
    return day - dt.timedelta(days=day.weekday())


def week1_start(today: dt.date) -> dt.date:
    """Week 1 starts on the next Monday (today when today is a Monday)."""
    return today if today.weekday() == 0 else monday(today) + dt.timedelta(days=7)


def km(meters: float) -> float:
    return round(meters / 1000.0, 1)


def pace_text(speed: float) -> str:
    seconds = int(round(1000.0 / speed))
    return f"{seconds // 60}:{seconds % 60:02d}"


def band_text(band: Optional[Tuple[MetersPerSecond, MetersPerSecond]]) -> Optional[str]:
    """(low, high) m/s -> "4:20-4:35" min/km (fast first)."""
    if band is None:
        return None
    return f"{pace_text(band[1])}-{pace_text(band[0])}"


def _floor_m(meters: float, step: float = 100.0) -> Meters:
    return Meters(math.floor(meters / step + 1e-9) * step)


# --- steps and sessions ---------------------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class Step:
    """One workout step: kind warmup | interval | recovery | cooldown | repeat. end distance | time; value in
    metres or seconds. band names the pace band of its target (None: no target). A repeat holds children."""

    kind: str
    end: str = "distance"
    value: float = 0.0
    band: Optional[str] = None
    times: int = 1
    children: Tuple["Step", ...] = ()


@dataclasses.dataclass
class Session:
    key: str
    date: dt.date
    role: str
    distance: Meters
    steps: Tuple[Step, ...] = ()
    note: str = ""

    @property
    def hard_or_long(self) -> bool:
        return self.role in HARD_ROLES

    @property
    def is_run(self) -> bool:
        return self.role not in ("b_race",)

    @property
    def counts(self) -> bool:
        """Counted in the week's km (the races are not)."""
        return self.role not in ("race", "b_race")


class Paces:
    """The pace bands of one plan: fractions of the LT speed, marathon pace from the goal."""

    def __init__(self, lt: Optional[MetersPerSecond], mp: Optional[MetersPerSecond], rules: Mapping[str, Any],
                 easy_slowdown: float = 0.0):
        self.lt = lt
        self.mp = mp
        self.rules = rules
        self.easy_slowdown = easy_slowdown   # the easy and long bands this share slower (Stage 4, rule A4)
        lo, hi = rules["speed_limits_mps"]
        self.limits = (float(lo), float(hi))

    def band(self, name: Optional[str]) -> Optional[Tuple[MetersPerSecond, MetersPerSecond]]:
        if name is None or self.lt is None:
            return None
        if name == "mp":
            if self.mp is None:
                return None
            w = float(self.rules["mp_band"])
            low, high = self.mp * (1 - w), self.mp * (1 + w)
        else:
            f_lo, f_hi = self.rules["bands_of_lt"][name]
            low, high = self.lt * f_lo, self.lt * f_hi
            if name in ("easy", "long") and self.easy_slowdown:
                low, high = low * (1 - self.easy_slowdown), high * (1 - self.easy_slowdown)
        if low < self.limits[0] or high > self.limits[1]:
            return None
        return MetersPerSecond(round(low, 3)), MetersPerSecond(round(high, 3))

    def mid(self, name: Optional[str]) -> Optional[MetersPerSecond]:
        b = self.band(name)
        return None if b is None else MetersPerSecond((b[0] + b[1]) / 2)

    def jog(self) -> MetersPerSecond:
        return self.mid("easy") or FALLBACK_JOG_MPS


def step_distance(step: Step, paces: Paces) -> Meters:
    if step.kind == "repeat":
        return Meters(step.times * sum(step_distance(c, paces) for c in step.children))
    if step.end == "distance":
        return Meters(step.value)
    return Meters(step.value * (paces.mid(step.band) or paces.jog()))


def step_seconds(step: Step, paces: Paces) -> Optional[Seconds]:
    """Estimated time at the band's middle; None without the LT speed."""
    if step.kind == "repeat":
        parts = [step_seconds(c, paces) for c in step.children]
        if any(p is None for p in parts):
            return None
        return Seconds(step.times * sum(p for p in parts if p is not None))
    if step.end == "time":
        return Seconds(step.value)
    speed = paces.mid(step.band or "easy")
    return None if speed is None else Seconds(step.value / speed)


def session_seconds(s: Session, paces: Paces) -> Optional[Seconds]:
    parts = [step_seconds(st, paces) for st in s.steps]
    if not parts or any(p is None for p in parts):
        return None
    return Seconds(sum(p for p in parts if p is not None))


def hard_seconds(s: Session, paces: Paces) -> Tuple[float, float]:
    """(seconds or metres at quality pace, total) of a session: the interval steps of a quality role count."""
    def walk(steps: Iterable[Step], times: int) -> Tuple[float, float]:
        hard = total = 0.0
        for st in steps:
            if st.kind == "repeat":
                h, t = walk(st.children, times * st.times)
            else:
                t = float(step_seconds(st, paces) or step_distance(st, paces)) * times
                h = t if st.kind == "interval" and st.band in ("threshold", "intervals", "mp") else 0.0
            hard += h
            total += t
        return hard, total

    if paces.lt is None:   # by distance when no time can be estimated
        def walk_m(steps: Iterable[Step], times: int) -> Tuple[float, float]:
            hard = total = 0.0
            for st in steps:
                if st.kind == "repeat":
                    h, t = walk_m(st.children, times * st.times)
                else:
                    t = float(step_distance(st, paces)) * times
                    h = t if st.kind == "interval" and s.role in QUALITY_ROLES else 0.0
                hard += h
                total += t
            return hard, total
        return walk_m(s.steps, 1)
    return walk(s.steps, 1)


def easy_steps(distance: Meters) -> Tuple[Step, ...]:
    return (Step("interval", "distance", float(distance), "easy"),)


def long_steps(distance: Meters) -> Tuple[Step, ...]:
    return (Step("interval", "distance", float(distance), "long"),)


def strides_steps(distance: Meters, rules: Mapping[str, Any], paces: Paces) -> Tuple[Step, ...]:
    sr = rules["strides"]
    reps = Step("repeat", times=int(sr["reps"]), children=(
        Step("interval", "distance", float(sr["m"]), "strides"),
        Step("recovery", "time", float(sr["jog_s"]), None),
    ))
    rest = Meters(max(1000.0, float(distance) - step_distance(reps, paces)))
    return (Step("interval", "distance", float(_floor_m(rest)), "easy"), reps)


def quality_steps(role: str, work: Meters, rep: Meters, rules: Mapping[str, Any]) -> Tuple[Step, ...]:
    warm = Step("warmup", "distance", float(rules["warmup_m"]), "easy")
    cool = Step("cooldown", "distance", float(rules["cooldown_m"]), "easy")
    if role == "mp":
        return (warm, Step("interval", "distance", float(work), "mp"), cool)
    jog = float(rules["threshold_jog_s"] if role == "threshold" else rules["intervals_jog_s"])
    times = max(1, int(round(work / rep)))
    if times == 1:
        return (warm, Step("interval", "distance", float(rep), role), cool)
    return (warm, Step("repeat", times=times, children=(
        Step("interval", "distance", float(rep), role), Step("recovery", "time", jog, None))), cool)


def quality_work(role: str, target: Meters, rules: Mapping[str, Any], short: bool = False) -> Optional[Tuple[Meters, Meters]]:
    """(work, rep length) for a quality session in a week of `target` metres, inside the dose caps; None
    when the week is too small for it."""
    cap = target * float(rules["dose_caps"][role])
    if role == "threshold":
        rep = Meters(2000.0) if cap >= 4000 else Meters(1000.0)
        reps = min(5, int(cap // rep))
        return (Meters(reps * rep), rep) if reps >= 1 else None
    if role == "intervals":
        for rep_m, lo, hi in ((1000.0, 3, 8), (400.0, 3, 10)):
            reps = int(cap // rep_m)
            if reps >= lo:
                return Meters(min(reps, hi) * rep_m), Meters(rep_m)
        return None
    top = float(rules["taper_mp_km"] if short else rules["mp_max_km"]) * 1000
    work = Meters(min(top, math.floor(cap / 1000.0) * 1000.0))
    return (work, work) if work >= float(rules["mp_min_km"]) * 1000 else None


SESSION_SLOTS = ("run", "race", "b")


def session_key(day: dt.date, slot: str = "run") -> str:
    """A session's key: the day the plan places it and its slot, "YYYYMMDD-run" (a run), "-race" (the A race) or
    "-b" (a B race); plan_store.SESSION_KEY_RE. A plan has at most one session a day (one_run_per_day; no run on a
    race day), so the key is unique, and it does not depend on today: a plan rebuilt on a later day gives the same
    planned session the same key, and the stored rows, links and feedback (matched by key) still name the same
    session. An override that moves a session keeps its key (the day it was planned on)."""
    if slot not in SESSION_SLOTS:
        raise ValueError("plan_engine: unknown session slot")
    return f"{day.strftime('%Y%m%d')}-{slot}"


def make_session(key: str, date: dt.date, role: str, distance: Meters, paces: Paces,
                 rules: Mapping[str, Any], work: Optional[Tuple[Meters, Meters]] = None) -> Session:
    if role == "long":
        steps = long_steps(distance)
    elif role == "strides":
        steps = strides_steps(distance, rules, paces)
    elif role in QUALITY_ROLES and work is not None:
        steps = quality_steps(role, work[0], work[1], rules)
    else:
        steps = easy_steps(distance)
    s = Session(key, date, role, distance, steps)
    s.distance = Meters(round(sum(step_distance(st, paces) for st in steps), 0))
    return s


# --- the plan's skeleton: weeks, phases, B windows ------------------------------------------------------------

@dataclasses.dataclass
class Week:
    number: int
    start: dt.date
    phase: str          # base, build, peak, taper, race, b_race
    kind: str           # normal, deload, b_race, taper, race, week0
    target: Meters = Meters(0.0)
    long_target: Meters = Meters(0.0)
    factor: float = 1.0
    long_ref: Meters = Meters(0.0)   # normal week: the longest long run before it (the 2 km step); reduced week: the long run it scales
    base: Meters = Meters(0.0)       # normal week: the km the next weeks grow from (assign_targets)
    acwr_from: Tuple[float, int] = (0.0, 0)   # (metres, sessions) before an ACWR cut (item 12), 0 without one
    acwr_held: bool = False          # item 12: a ratio stays above the cap and no further cut lowers it
    b_cut: float = 0.0               # rule 7: metres a B-race long-run limit took off (not made up, no shortfall)
    quality: Tuple[str, ...] = ()
    sessions: List[Session] = dataclasses.field(default_factory=list)

    @property
    def end(self) -> dt.date:
        return self.start + dt.timedelta(days=6)

    def days(self) -> List[dt.date]:
        return [self.start + dt.timedelta(days=i) for i in range(7)]

    @property
    def reduced(self) -> bool:
        return self.kind != "normal"


def phase_counts(n: int, rules: Mapping[str, Any] = RULES) -> Dict[str, int]:
    taper = 2 if n >= int(rules["taper_weeks"]["n_min_2"]) else 1
    peak = 2 if n >= int(rules["peak_weeks"]["n_min_2"]) else (1 if n >= int(rules["peak_weeks"]["n_min_1"]) else 0)
    rest = max(0, n - taper - peak)
    base = int(rest * float(rules["base_share_of_rest"]) + 0.5)
    return {"base": base, "build": rest - base, "peak": peak, "taper": taper}


@dataclasses.dataclass(frozen=True)
class BWindow:
    race: BRace
    mini: Tuple[dt.date, ...]
    recovery: Tuple[dt.date, ...]

    def factor(self, day: dt.date, rules: Mapping[str, Any]) -> Optional[float]:
        if day == self.race.date:
            return 0.0
        if day in self.mini:
            return float(rules["b_mini_factor"])
        if day in self.recovery:
            return float(rules["b_recovery_factor"])
        return None

    def days(self) -> Tuple[dt.date, ...]:
        return self.mini + (self.race.date,) + self.recovery


def b_windows(inputs: PlanInputs, first: dt.date, rules: Mapping[str, Any]) -> Tuple[List[BWindow], List[Dict[str, Any]]]:
    out, warnings = [], []
    for b in sorted(inputs.b_races, key=lambda r: r.date):
        # only a B race in the A-race week itself (or before week 1) is not planned around; one in the taper
        # week is (its window may reach into the race week; the A taper does not move: b_race_in_taper)
        if not first <= b.date < monday(inputs.race_date):
            warnings.append({"id": "b_race_ignored", "dates": [b.date.isoformat()],
                             "detail": "a B race before week 1 or in the A-race week is not planned around"})
            continue
        mini = tuple(b.date - dt.timedelta(days=i) for i in range(int(rules["b_mini_taper_days"]), 0, -1))
        rec_days = int(rules["b_recovery_days"].get(b.kind, 6))
        recovery = tuple(b.date + dt.timedelta(days=i) for i in range(1, rec_days + 1))
        out.append(BWindow(b, mini, recovery))
        if b.kind == "tri_full":
            warnings.append({"id": "b_race_long_recovery", "dates": [b.date.isoformat()],
                             "detail": f"a full-distance triathlon: {rec_days} easy days after it"})
    return out, warnings


def skeleton(inputs: PlanInputs, today: dt.date, rules: Mapping[str, Any]) -> Tuple[List[Week], List[BWindow], List[Dict[str, Any]]]:
    first = week1_start(today)
    last = monday(inputs.race_date)
    if inputs.race_date <= today or last < first:
        raise PlanError("race_date must be after the end of this week")
    n = (last - first).days // 7 + 1
    counts = phase_counts(n, rules)
    phases = (["base"] * counts["base"] + ["build"] * counts["build"] + ["peak"] * counts["peak"]
              + ["taper"] * (counts["taper"] - 1) + ["race"])
    windows, warnings = b_windows(inputs, first, rules)
    bdays = {d for w in windows for d in w.days()}
    weeks = []
    for i, phase in enumerate(phases):
        start = first + dt.timedelta(days=7 * i)
        days = {start + dt.timedelta(days=k) for k in range(7)}
        kind = "normal"
        if phase in ("taper", "race"):
            kind = phase
            if days & bdays:
                warnings.append({"id": "b_race_in_taper", "dates": [start.isoformat()],
                                 "detail": "a B race or its recovery falls in the A taper, which does not move"})
        elif days & bdays:
            kind = "b_race"
            if phase == "peak":
                warnings.append({"id": "b_race_in_peak", "dates": [start.isoformat()],
                                 "detail": "the B race or its recovery takes a peak week"})
        weeks.append(Week(i + 1, start, phase if kind != "b_race" else "b_race", kind))
    # Deloads: after 3 normal weeks; a B week counts as one, so a deload due just before it is skipped.
    run = 0
    limit = int(rules["normal_weeks_before_deload"])
    for i, w in enumerate(weeks):
        if w.kind != "normal":
            run = 0
            continue
        nxt = weeks[i + 1].kind if i + 1 < len(weeks) else None
        if run >= limit and nxt != "b_race":
            w.kind = "deload"
            run = 0
        else:
            run += 1
    return weeks, windows, warnings


# --- the plan --------------------------------------------------------------------------------------------------

@dataclasses.dataclass
class Context:
    inputs: PlanInputs
    snapshot: Snapshot
    rules: Mapping[str, Any]
    paces: Paces
    km0: Meters
    longest: Meters
    windows: List[BWindow]
    fixed: Dict[dt.date, List[FixedSession]]
    warnings: List[Dict[str, Any]]
    missing: List[str]
    peak_long: Meters = Meters(0.0)   # the longest long run placed >= 21 days out (before any override)
    load: Optional["LoadModel"] = None   # the planned-load model (item 12)
    all_weeks: List["Week"] = dataclasses.field(default_factory=list)   # week 0 and weeks 1.., as placed
    # rule 7's reference, frozen once the plan is built (before any override): (key, date, metres, peak-able)
    # of every long run, so an override that shortens an earlier long run never tightens a later limit
    built_longs: Optional[Tuple[Tuple[str, dt.date, float, bool], ...]] = None

    def window_factor(self, day: dt.date) -> Optional[float]:
        for w in self.windows:
            f = w.factor(day, self.rules)
            if f is not None:
                return f
        return None

    def b_race_on(self, day: dt.date) -> Optional[BRace]:
        return next((w.race for w in self.windows if w.race.date == day), None)

    def fixed_hard(self, day: dt.date) -> bool:
        return any(f.hard or f.long for f in self.fixed.get(day, ()))

    def fixed_run(self, day: dt.date) -> bool:
        return any(f.sport == "run" for f in self.fixed.get(day, ()))

    def open_day(self, day: dt.date) -> bool:
        """A run day of the plan, blocked or not."""
        i = self.inputs
        return (day.weekday() in i.run_days and day.weekday() not in i.rest_days
                and not self.fixed_run(day) and day != i.race_date and self.b_race_on(day) is None
                and self.snapshot.today < day < i.race_date)

    def runnable(self, day: dt.date) -> bool:
        return self.open_day(day) and day not in self.inputs.blocked_dates

    def event_hard(self, day: dt.date) -> bool:
        """Hard-or-long days the plan does not place: fixed sessions and the races."""
        return self.fixed_hard(day) or day == self.inputs.race_date or self.b_race_on(day) is not None


def long_time_cap(paces: Paces, rules: Mapping[str, Any]) -> Optional[Meters]:
    """3 h at the slow edge of the long-run band; from the LT speed even when the band itself is outside the
    speed limits (a slow runner keeps the cap). None without an LT speed."""
    if paces.lt is None:
        return None
    return Meters(float(rules["long_max_s"]) * paces.lt * float(rules["bands_of_lt"]["long"][0]))


def long_cap(ctx: Context, target: Meters, last_long: Meters) -> Meters:
    r = ctx.rules
    caps = [max(ctx.inputs.long_share * target, ctx.longest), last_long + float(r["long_step_m"]),
            ctx.inputs.max_long_km * 1000.0]
    t = long_time_cap(ctx.paces, r)
    if t is not None:
        caps.append(t)
    return Meters(min(caps))


def late_long_limit(ctx: Context, day: dt.date, weeks: Optional[Sequence[Week]] = None,
                    skip: Optional[Session] = None) -> Optional[Meters]:
    """Rule 3: the most a long run on `day` may be when `day` is within 21 days of the A race, else None.
    0.80 x the peak long run; before one (ctx.peak_long 0: no normal week's long run >= 21 days out), 0.80 x
    the longest long run so far: the longest placed before `day` (in `weeks`, default the plan so far; `skip`
    left out) or the longest run of the last 8 weeks, whichever is longer (the long-run step starts from it
    too, and a late long run itself never sets it: it is already under 0.80 x it). None when both are 0."""
    r = ctx.rules
    if (ctx.inputs.race_date - day).days >= int(r["late_long_days"]):
        return None
    ref = float(ctx.peak_long)
    if ref <= 0:
        ref = max([float(s.distance) for w in (ctx.all_weeks if weeks is None else weeks) for s in w.sessions
                   if s.role == "long" and s.date < day and s is not skip] + [float(ctx.longest)])
    if ref <= 0:
        return None
    return Meters(ref * float(r["late_long_of_peak"]))


def _longs(weeks: Sequence[Week], ctx: Context) -> Tuple[Tuple[str, dt.date, float, bool], ...]:
    """(key, date, metres, peak-able) of every long run in `weeks`; peak-able: in a normal week, 21 days or
    more before the A race (rule 3's peak long run)."""
    late = int(ctx.rules["late_long_days"])
    return tuple((s.key, s.date, float(s.distance), w.kind == "normal" and (ctx.inputs.race_date - s.date).days >= late)
                 for w in weeks for s in w.sessions if s.role == "long")


def b_long_ref(ctx: Context, day: dt.date, weeks: Sequence[Week], skip: Optional[Session] = None) -> float:
    """Rule 7's long-run reference on `day`, rule 3's peak long run as the plan is built: the longest long run
    of a normal week 21 days or more before the A race placed before `day` (`skip` left out); before one, the
    longest run so far (the longest of the last 8 weeks or a long run placed before `day`). From the plan as
    built (ctx.built_longs) once there is one, else from `weeks`, so the engine and check_invariants agree."""
    longs = ctx.built_longs if ctx.built_longs is not None else _longs(weeks, ctx)
    before = [(m, peak) for key, d, m, peak in longs if d < day and (skip is None or key != skip.key)]
    top = max((m for m, peak in before if peak), default=0.0)
    if top > 0:
        return top
    return max([m for m, _p in before] + [float(ctx.longest)])


def b_long_limit(ctx: Context, day: dt.date, weeks: Optional[Sequence[Week]] = None,
                 skip: Optional[Session] = None) -> Tuple[Optional[Meters], Optional[BRace]]:
    """Rule 7 for a long run on `day`: (the most it may be, or None; the B race it is too soon after, or None).
    A long run 5-13 days before a B race: <= 0.70 x b_long_ref; the first long run after a B race (no long run
    between them in `weeks`, default the plan so far; `skip` left out): <= 0.70 x it (0.60 after a full-distance
    triathlon), and a full-distance triathlon's is >= 10 days after it (else the B race is returned)."""
    br = ctx.rules["b_race"]
    weeks = ctx.all_weeks if weeks is None else weeks
    lo, hi = (int(x) for x in br["long_before_days"])
    limit: Optional[Meters] = None
    soon: Optional[BRace] = None
    ref: Optional[float] = None
    for bw in ctx.windows:
        b = bw.race
        before = (b.date - day).days
        if lo <= before <= hi:
            f = float(br["long_before_of_peak"])
        elif before < 0 and not any(s.role == "long" and b.date < s.date < day and s is not skip
                                    for w in weeks for s in w.sessions):
            f = float(br["long_after_of_peak"][b.kind])
            if soon is None and -before < int(br["long_after_min_days"][b.kind]):
                soon = b
        else:
            continue
        if ref is None:
            ref = b_long_ref(ctx, day, weeks, skip)
        if ref > 0:
            limit = Meters(ref * f if limit is None else min(limit, ref * f))
    return limit, soon


def easy_cap(paces: Paces, rules: Mapping[str, Any]) -> Meters:
    """The longest easy run: 14 km, or 74 minutes at the middle of the easy band when that is shorter."""
    cap = float(rules["easy_max_km"]) * 1000.0
    mid = paces.mid("easy")
    if mid is not None:
        cap = min(cap, mid * float(rules["easy_max_min"]) * 60.0)
    return Meters(cap)


def peak_ceiling(snap: Snapshot, km0: Meters, rules: Mapping[str, Any]) -> Tuple[Meters, Meters]:
    """(ceiling, biggest week): no weekly target above max(1.3 x the biggest full week of the last 8 weeks,
    km0 + 25 km)."""
    pc = rules["peak_ceiling"]
    biggest = Meters(max(list(snap.week_m)[-int(rules["history_weeks"]):], default=0.0))
    return Meters(max(float(pc["of_max_week"]) * biggest, km0 + float(pc["over_km0_km"]) * 1000.0)), biggest


def calendar_week(w: Week, ctx: Context) -> bool:
    """A week with a blocked date or a fixed calendar session: what it loses to them is not a cap."""
    return any(d in ctx.inputs.blocked_dates or d in ctx.fixed for d in w.days())


def week_base(target: float, placed: float, n_sessions: int, capacity: Optional[float]) -> Meters:
    """The km the weeks after a normal week grow from: min(target, what the plan placed), where a week with
    calendar items counts what it would have placed without them, and the 100 m rounding of each session is
    not a shortfall."""
    got = placed if capacity is None else max(placed, capacity)
    if got >= target - 100.0 * max(1, n_sessions):
        return Meters(target)
    return Meters(min(target, got))


def assign_targets(weeks: List[Week], ctx: Context, place: Callable[[Week], None],
                   capacity: Optional[Callable[[Week], Tuple[float, int]]] = None) -> None:
    """Each week's km and long-run target, then place(week) fills it; the step and the peak long run follow
    the long runs actually placed (a long run that did not fit does not move the step). Weekly km grow from
    what was placed (week_base), so a week the easy-run cap holds below its target does not lift the weeks
    after it. capacity(week) gives (km, sessions) the week would get without its calendar items."""
    r = ctx.rules
    ceiling, biggest = peak_ceiling(ctx.snapshot, ctx.km0, r)
    capped_warned = False
    last_normal = ctx.km0
    last_long = ctx.longest
    peak = Meters(0.0)
    peak_long = Meters(0.0)
    for w in weeks:
        w.long_ref = last_long
        # Reduced weeks scale the longest long run placed so far (before one is placed: what a normal week
        # would allow now), never the longest run of the history, which the caps may have cut.
        ref_long = peak_long if peak_long > 0 else long_cap(ctx, last_normal, last_long)
        if w.kind != "normal":
            w.long_ref = ref_long
        long_day = w.start + dt.timedelta(days=ctx.inputs.long_run_day)
        late = (ctx.inputs.race_date - long_day).days < int(r["late_long_days"])
        late_cap = late_long_limit(ctx, long_day)
        if w.kind == "normal":
            w.target = Meters(last_normal * (1 + float(r["growth_max"])))
            if w.target > ceiling:
                w.target = ceiling
                if not capped_warned:
                    capped_warned = True
                    pc = r["peak_ceiling"]
                    ctx.warnings.append({
                        "id": "peak_capped", "dates": [w.start.isoformat()],
                        "detail": f"weekly km held at {km(ceiling)} km, the larger of {pc['of_max_week']} x the "
                                  f"biggest week of the last {r['history_weeks']} ({km(biggest)} km) and the start "
                                  f"{km(ctx.km0)} km + {pc['over_km0_km']} km; deloads still apply"})
            long_m = long_cap(ctx, w.target, last_long)
            if late_cap is not None:
                long_m = Meters(min(long_m, late_cap))
            w.long_target = _floor_m(long_m)
        elif w.kind == "deload":
            w.factor = float(r["deload_factor"])
            w.target = Meters(last_normal * w.factor)
            w.long_target = _floor_m(ref_long * w.factor)
        elif w.kind == "b_race":
            factors = [ctx.window_factor(d) for d in w.days()]
            w.factor = sum(1.0 if f is None else f for f in factors) / 7.0
            w.target = Meters(last_normal * w.factor)
            w.long_target = _floor_m(ref_long * w.factor)
        elif w.kind == "taper":
            w.factor = float(r["taper_factor"])
            w.target = Meters((peak or last_normal) * w.factor)
            w.long_target = _floor_m(ref_long * w.factor)
        else:  # race week: no long run
            w.factor = float(r["race_week_factor"])
            # pro rata to the days before the race: no run after it
            w.target = Meters((peak or last_normal) * w.factor * (ctx.inputs.race_date - w.start).days / 7.0)
            w.long_target = Meters(0.0)
        w.long_target = _floor_m(min(w.long_target, w.target))   # never more than the whole week
        if late_cap is not None and w.kind != "normal":
            w.long_target = _floor_m(min(w.long_target, late_cap))
        cap_km: Optional[float] = None
        if w.kind == "normal" and capacity is not None and calendar_week(w, ctx):
            cap_km = capacity(w)[0]
        place(w)
        enforce_acwr(w, ctx)   # item 12: run km cut (never raised) while the planned ratio passes 1.3
        longs = [s for s in w.sessions if s.role == "long"]
        placed = max((s.distance for s in longs), default=0.0)
        if longs:   # the long run may have moved a day: "late" is decided on the day it is on (fill_week caps it)
            late = (ctx.inputs.race_date - longs[0].date).days < int(r["late_long_days"])
        if w.kind == "normal":
            counted = [s for s in w.sessions if s.counts]
            # an ACWR cut is not a shortfall either (the next week is checked again): the km before it count
            got, n_got = float(sum(s.distance for s in counted)), len(counted)
            if w.acwr_from[0] > got:
                got, n_got = w.acwr_from
            got += w.b_cut   # nor is what rule 7 took off a long run before or after a B race
            w.base = week_base(w.target, got, n_got, cap_km)
            last_normal = w.base
            peak = Meters(max(peak, w.base))
        if w.kind == "normal" and not late:
            last_long = Meters(max(last_long, placed))
            peak_long = Meters(max(peak_long, placed))
            ctx.peak_long = peak_long


def assign_quality(weeks: List[Week], ctx: Context) -> None:
    base_n = sum(1 for w in weeks if w.phase == "base")
    build_i = 0
    base_i = 0
    for w in weeks:
        q: Tuple[str, ...] = ()
        if w.phase == "base":
            q = ("strides",) if base_i < (base_n + 1) // 2 else ("threshold",)
            base_i += 1
        elif w.phase == "build":
            q = ("threshold", "mp" if build_i % 2 == 0 else "intervals")
            build_i += 1
        elif w.phase == "peak":
            q = ("threshold", "mp")
        elif w.kind == "taper":
            q = ("mp_short",)
        elif w.kind == "race":
            q = ("strides",)
        if w.kind == "deload":
            q = q[:1]
        if w.kind == "b_race":
            q = ()
        w.quality = q[:max(0, ctx.inputs.quality_per_week)] if w.kind != "race" else q


def _adjacent_ok(day: dt.date, hard_days: Set[dt.date]) -> bool:
    return (day - dt.timedelta(days=1)) not in hard_days and (day + dt.timedelta(days=1)) not in hard_days


def place_week(w: Week, ctx: Context, hard_before: Set[dt.date]) -> Tuple[Optional[dt.date], List[Tuple[str, dt.date]]]:
    """(long run day, [(quality role, day)]) for week `w`: no two hard-or-long days touch, counting fixed
    sessions, the races and `hard_before` (the plan's hard days so far). Fallbacks, in order: drop Q2, move the
    long run one day, turn Q1 into strides, drop the long run; each adds a warning with dates."""
    days = [d for d in w.days() if ctx.runnable(d)]
    gap = int(ctx.rules["last_quality_days_before_race"])
    free = [d for d in days if ctx.window_factor(d) is None and (ctx.inputs.race_date - d).days >= gap]
    taken = set(hard_before) | {d for d in w.days() + [w.start - dt.timedelta(days=1), w.end + dt.timedelta(days=1)]
                                if ctx.event_hard(d)}
    want_long = w.long_target > 0
    if want_long and w.kind == "b_race":
        near = [w.start + dt.timedelta(days=ctx.inputs.long_run_day + k) for k in (0, -1, 1)]
        want_long = any(d in free for d in near)
    pref = w.start + dt.timedelta(days=ctx.inputs.long_run_day)
    long_opts: List[Optional[dt.date]] = [d for d in (pref, pref - dt.timedelta(days=1), pref + dt.timedelta(days=1))
                                          if d in free and w.start <= d <= w.end] if want_long else [None]
    hard_q = [q for q in w.quality if q in QUALITY_ROLES or q == "mp_short"]
    soft_q = [q for q in w.quality if q == "strides"]

    def try_place(long_day: Optional[dt.date], qs: Sequence[str]) -> Optional[List[Tuple[str, dt.date]]]:
        if long_day is not None and not _adjacent_ok(long_day, taken):
            return None
        used = set(taken) | ({long_day} if long_day else set())
        cands = [d for d in free if d != long_day and d not in used]
        for combo in itertools.combinations(cands, len(qs)):
            busy = set(used)
            ok = True
            for d in combo:
                if not _adjacent_ok(d, busy) or d in busy:
                    ok = False
                    break
                busy.add(d)
            if ok:
                return list(zip(qs, combo))
        return None

    attempts: List[Tuple[Optional[dt.date], List[str], str]] = []

    def moved(day: Optional[dt.date]) -> str:
        # off the long-run day, whatever the reason (blocked, a rest or non-run day, a B window, hard days)
        return "long_moved_" if day is not None and day != pref else ""

    if long_opts:
        for alt in long_opts:
            attempts.append((alt, hard_q, moved(alt).rstrip("_")))
            if len(hard_q) > 1:
                attempts.append((alt, hard_q[:1], moved(alt) + "q2_dropped"))
        for alt in long_opts:
            attempts.append((alt, [], moved(alt) + "q1_to_strides" if hard_q else moved(alt).rstrip("_")))
    if not want_long or not long_opts:
        attempts.append((None, hard_q, "long_dropped" if want_long else ""))
        if len(hard_q) > 1:
            attempts.append((None, hard_q[:1], "long_dropped_q2_dropped" if want_long else "q2_dropped"))
        attempts.append((None, [], ("long_dropped_" if want_long else "") + "q1_to_strides" if hard_q
                         else ("long_dropped" if want_long else "")))
    for long_day, qs, why in attempts:
        placed = try_place(long_day, qs)
        if placed is None:
            continue
        if why:
            for part in _why_parts(why):
                ctx.warnings.append({"id": part, "dates": [w.start.isoformat(), w.end.isoformat()],
                                     "detail": _FALLBACK_TEXT[part]})
        if "q1_to_strides" in why or (len(qs) < len(hard_q) and not qs):
            soft_q = ["strides"] + soft_q
        # strides: on any free run day left
        used = set(d for _, d in placed) | ({long_day} if long_day else set())
        for q in soft_q[:1]:
            spot = next((d for d in days if d not in used
                         and (ctx.window_factor(d) is None or d in _mini_days(ctx))), None)
            if spot is not None:
                placed.append((q, spot))
        return long_day, placed
    return None, []


_FALLBACK_TEXT = {
    "q2_dropped": "the second quality session did not fit without two hard days in a row",
    "long_moved": "the long run moved one day to keep hard days apart",
    "q1_to_strides": "the quality session became strides to keep hard days apart",
    "long_dropped": "no day for the long run without two hard days in a row (or the days are blocked)",
}


def _why_parts(why: str) -> List[str]:
    out = []
    for part in ("long_dropped", "long_moved", "q2_dropped", "q1_to_strides"):
        if part in why:
            out.append(part)
    return out


def fill_week(w: Week, ctx: Context, hard_before: Set[dt.date], index: Dict[str, int]) -> None:
    r, paces = ctx.rules, ctx.paces
    long_day, placed = place_week(w, ctx, hard_before)
    sessions: List[Session] = []
    w.b_cut = 0.0
    easy_max = Meters(_floor_m(easy_cap(paces, r)))
    if long_day is not None:
        # rule 3 on the day the long run landed (it may have moved into the last 21 days): capped before the
        # 80% easy check and the easy runs, which then fill the week's km
        long_m = w.long_target
        late_cap = late_long_limit(ctx, long_day)
        if late_cap is not None:
            long_m = _floor_m(min(long_m, late_cap))
        # rule 7 around a B race: the km it takes off stay off (b_cut), the easy runs do not fill them
        b_cap, soon = b_long_limit(ctx, long_day)
        role, new_m = "long", long_m
        if soon is not None:
            role, new_m = "easy", Meters(min(long_m, easy_max))
        elif b_cap is not None:
            new_m = Meters(min(long_m, _floor_m(b_cap)))
        if new_m < long_m:
            w.b_cut = float(long_m - new_m)
            ctx.warnings.append({
                "id": "b_race_capped", "dates": [long_day.isoformat()],
                "detail": (f"the long run is an easy run: under {r['b_race']['long_after_min_days'][soon.kind]} days "
                           f"after the B race on {soon.date.isoformat()}") if soon is not None else
                          f"the long run is held at {km(new_m)} km around a B race; the km are not made up"})
        sessions.append(make_session(session_key(long_day), long_day, role, new_m, paces, r))
    for role, day in placed:
        if role == "strides":
            continue
        short = role == "mp_short"
        real = "mp" if short else role
        work = quality_work(real, w.target, r, short=short)
        if work is None:
            ctx.warnings.append({"id": "quality_too_small", "dates": [day.isoformat()],
                                 "detail": f"the week is too small for a {real} session"})
            continue
        sessions.append(make_session(session_key(day), day, real, Meters(0.0), paces, r, work))
    used = {s.date for s in sessions}
    strides_day = next((d for role, d in placed if role == "strides"), None)
    # Easy days: every runnable day left; mini-taper days carry strides ("strides only").
    # A blocked date keeps its share of the week's km and drops it: the other days do not make it up.
    easy_days = [d for d in w.days() if ctx.open_day(d) and d not in used]
    _limit_easy_share(w, sessions, ctx)
    left = w.target - w.b_cut - sum(s.distance for s in sessions)
    easy_min = float(r["easy_min_km"]) * 1000.0
    if easy_days and left >= easy_min:
        k = min(len(easy_days), int(left // easy_min))
        # Spread the runs: skip days next to hard days first.
        hard = {s.date for s in sessions if s.hard_or_long} | {d for d in w.days() if ctx.event_hard(d)}
        ranked = sorted(easy_days, key=lambda d: (not _adjacent_ok(d, hard), d))[:k]
        # Rule 7 per day: a B window day (mini-taper 0.6, recovery 0.5) gets that share of a full day's easy
        # km in the same week; a window day that would fall under the easy minimum is left out (the latest of
        # the lowest share first) and the others share its km. Without window days: an even split.
        weight = {d: ctx.window_factor(d) or 1.0 for d in ranked}
        while len(ranked) > 1 and _floor_m(left * min(weight[d] for d in ranked)
                                           / sum(weight[d] for d in ranked)) < easy_min:
            ranked.remove(max(ranked, key=lambda d: (-weight[d], d)))
        total_w = sum(weight[d] for d in ranked)
        per_day = {d: _floor_m(left * weight[d] / total_w) for d in ranked}
        if any(p > easy_max for p in per_day.values()):
            per_day = {d: Meters(min(p, _floor_m(easy_max))) for d, p in per_day.items()}
            ctx.warnings.append({"id": "volume_capped", "dates": [w.start.isoformat()],
                                 "detail": "easy runs stop at the easy cap, so the week ends below its target"})
        _cap_mini_days(per_day, ctx)
        for day in sorted(ranked):
            if day in ctx.inputs.blocked_dates:
                continue
            role = "strides" if (day == strides_day or (ctx.window_factor(day) is not None
                                                        and day in _mini_days(ctx))) else "easy"
            sessions.append(make_session(session_key(day), day, role, per_day[day], paces, r))
    for d in w.days():
        b = ctx.b_race_on(d)
        if b is not None:
            sessions.append(Session(session_key(d, "b"), d, "b_race", Meters(0.0), (), b.kind))
        if d == ctx.inputs.race_date:
            dist = DISTANCES[ctx.inputs.race_distance]
            sessions.append(Session(session_key(d, "race"), d, "race", dist, (Step("interval", "distance", float(dist), "mp"),)))
    sessions.sort(key=lambda s: (s.date, s.key))
    w.sessions = sessions
    if sum(s.distance for s in sessions if s.counts) > w.target + EPS_M:
        _cap_week(w, w.target, ctx)   # a small week: the fixed parts of the sessions add up past its target
    _limit_easy_share(w, w.sessions, ctx, estimate_rest=False)
    for s in w.sessions:
        if s.hard_or_long:
            hard_before.add(s.date)


def _mini_days(ctx: Context) -> Set[dt.date]:
    return {d for w in ctx.windows for d in w.mini}


def _cap_mini_days(per_day: Dict[dt.date, Meters], ctx: Context) -> None:
    """Rule 7: a run on a B mini-taper day is at most 8 km (only lowered; the km are not made up)."""
    cap = _floor_m(float(ctx.rules["b_race"]["mini_taper_max_km"]) * 1000.0)
    mini = _mini_days(ctx)
    capped = sorted(d for d, m in per_day.items() if d in mini and m > cap)
    for d in capped:
        per_day[d] = cap
    if capped:
        ctx.warnings.append({"id": "b_race_capped", "dates": [d.isoformat() for d in capped],
                             "detail": f"race-week runs before a B race are held at {km(cap)} km; "
                                       "the km are not made up"})


def _limit_easy_share(w: Week, sessions: List[Session], ctx: Context, estimate_rest: bool = True) -> None:
    """Shrink marathon-pace work (then drop the last quality session) until the quality work of the week's
    planned sessions is at most 20% of the week's estimated time."""
    r, paces = ctx.rules, ctx.paces
    limit = 1.0 - float(r["easy_share_min"])
    for _ in range(40):
        qual = [s for s in sessions if s.role in QUALITY_ROLES]
        if not qual:
            return
        hard = sum(hard_seconds(s, paces)[0] for s in qual)
        total_q = sum(hard_seconds(s, paces)[1] for s in sessions)
        # the easy runs are not placed yet: estimate them from the rest of the week's target
        rest = max(0.0, w.target - w.b_cut - sum(s.distance for s in sessions)) if estimate_rest else 0.0
        speed = paces.mid("easy")
        total = total_q + (rest / speed if speed and paces.lt is not None else rest)
        if total <= 0 or hard / total <= limit:
            return
        mp = next((s for s in qual if s.role == "mp"), None)
        if mp is not None:
            work = next(st for st in mp.steps if st.kind == "interval")
            if work.value - 1000 >= float(r["mp_min_km"]) * 1000:
                i = sessions.index(mp)
                sessions[i] = make_session(mp.key, mp.date, "mp", Meters(0.0), paces, r,
                                           (Meters(work.value - 1000), Meters(work.value - 1000)))
                continue
        sessions.remove(qual[-1])
        ctx.warnings.append({"id": "quality_dropped_easy_share", "dates": [qual[-1].date.isoformat()],
                             "detail": "a quality session was dropped to keep 80% of the week's time easy"})


def week0(ctx: Context) -> Optional[Week]:
    today = ctx.snapshot.today
    if today.weekday() == 6 or week1_start(today) == today:
        return None
    w = Week(0, monday(today), "base", "week0")
    days = [d for d in w.days() if ctx.runnable(d)]
    if not days:
        return w
    per_day = ctx.km0 / max(1, len(set(ctx.inputs.run_days) - set(ctx.inputs.rest_days)))
    easy_max = float(ctx.rules["easy_max_km"]) * 1000.0
    mid = ctx.paces.mid("easy")
    if mid is not None:
        easy_max = min(easy_max, mid * float(ctx.rules["easy_max_min"]) * 60.0)
    per = _floor_m(min(per_day, easy_max))
    if per >= float(ctx.rules["easy_min_km"]) * 1000:
        each = {d: per for d in days}
        _cap_mini_days(each, ctx)   # rule 7: a B race early in week 1 has its mini-taper here
        w.sessions = [make_session(session_key(d), d, "easy", each[d], ctx.paces, ctx.rules)
                      for d in days]
    w.target = Meters(sum(s.distance for s in w.sessions))
    return w


def marathon_pace(inputs: PlanInputs, snap: Snapshot, rules: Mapping[str, Any],
                  warnings: List[Dict[str, Any]]) -> Optional[MetersPerSecond]:
    dist = DISTANCES[inputs.race_distance]
    goal = inputs.goal_time_s or snap.prediction(inputs.race_distance)
    if not goal or snap.lt_speed_mps is None:
        return None
    mp = dist / goal
    lo, hi = rules["mp_of_lt_sanity"]
    ratio = mp / snap.lt_speed_mps
    if not lo <= ratio <= hi:
        mp = snap.lt_speed_mps * (lo if ratio < lo else hi)
        warnings.append({"id": "mp_clamped", "dates": [],
                         "detail": f"goal pace at {round(ratio * 100)}% of threshold speed; MP sessions use "
                                   f"{round((lo if ratio < lo else hi) * 100)}%"})
    return MetersPerSecond(mp)


def context(inputs: PlanInputs, snap: Snapshot, rules: Mapping[str, Any]) -> Context:
    check_inputs(inputs)
    warnings: List[Dict[str, Any]] = []
    missing: List[str] = []
    weeks_m = list(snap.week_m)[-int(rules["km0_weeks"]):]
    if not weeks_m or sum(weeks_m) <= 0:
        raise PlanError("no runs in the last weeks, so the plan has no starting volume")
    km0 = Meters(sum(weeks_m) / len(weeks_m))
    if len(weeks_m) < int(rules["km0_weeks"]):
        missing.append("history_weeks")
    if snap.lt_speed_mps is None:
        missing += ["lt_speed", "pace_targets", "long_run_time_cap"]
    mp = marathon_pace(inputs, snap, rules, warnings)
    if mp is None and snap.lt_speed_mps is not None:
        missing.append("marathon_pace")
    longest = snap.longest_run_8w_m or Meters(0.0)
    if snap.longest_run_8w_m is None:
        missing.append("longest_run_8w")
    fixed: Dict[dt.date, List[FixedSession]] = {}
    for f in snap.fixed:
        fixed.setdefault(f.date, []).append(f)
    return Context(inputs, snap, rules, Paces(snap.lt_speed_mps, mp, rules), km0, longest, [], fixed,
                   warnings, missing)


def check_inputs(i: PlanInputs) -> None:
    if i.race_distance not in DISTANCES:
        raise PlanError("race_distance must be marathon or half")
    if not set(i.run_days) <= set(range(7)) or not set(i.rest_days) <= set(range(7)):
        raise PlanError("run_days and rest_days are weekdays")
    if not set(i.run_days) - set(i.rest_days):
        raise PlanError("no run day is left once the rest days are taken out")
    if i.long_run_day not in range(7):
        raise PlanError("long_run_day must be a weekday")
    if not 0 <= i.quality_per_week <= 2:
        raise PlanError("quality_per_week must be 0, 1 or 2")
    if not 10 <= i.max_long_km <= 32:
        raise PlanError("max_long_km must be from 10 to 32")
    if not 0.2 <= i.long_share <= 0.6:
        raise PlanError("long_share must be from 0.2 to 0.6")
    if i.goal_time_s is not None and not 3600 <= i.goal_time_s <= 8 * 3600:
        raise PlanError("goal_time must be from 1:00:00 to 8:00:00")
    for b in i.b_races:
        if b.kind not in B_RACE_KINDS:
            raise PlanError("a B race kind must be tri_full, tri_half or run")
    for o in i.overrides:
        if o.kind not in OVERRIDE_KINDS:
            raise PlanError("unknown override kind")
    if not 0.0 <= i.easy_band_slowdown <= MAX_EASY_BAND_SLOWDOWN:
        raise PlanError("easy_band_slowdown must be from 0 to 0.1")


def _build_core(inputs: PlanInputs, snap: Snapshot, rules: Mapping[str, Any]) -> Tuple[List[Week], Context]:
    ctx = context(inputs, snap, rules)
    weeks, windows, warnings = skeleton(inputs, snap.today, rules)
    ctx.windows = windows
    ctx.warnings.extend(warnings)
    assign_quality(weeks, ctx)
    hard: Set[dt.date] = set()
    index: Dict[str, int] = {}
    w0 = week0(ctx)
    all_weeks = ([w0] if w0 is not None else []) + weeks
    ctx.all_weeks = all_weeks
    ctx.load = load_model(snap, ctx)
    if w0 is not None:
        enforce_acwr(w0, ctx)
    assign_targets(weeks, ctx, lambda w: fill_week(w, ctx, hard, index),
                   lambda w: _capacity(w, ctx, hard, index))
    ctx.built_longs = _longs(all_weeks, ctx)
    return all_weeks, ctx


def _capacity(w: Week, ctx: Context, hard: Set[dt.date], index: Dict[str, int]) -> Tuple[float, int]:
    """(km, sessions) week `w` would get with no blocked dates and no fixed calendar sessions; a dry run that
    changes nothing (its warnings are dropped)."""
    dry_ctx = dataclasses.replace(ctx, inputs=dataclasses.replace(ctx.inputs, blocked_dates=()), fixed={},
                                  warnings=[])
    dry = dataclasses.replace(w, sessions=[])
    fill_week(dry, dry_ctx, set(hard), dict(index))
    counted = [s for s in dry.sessions if s.counts]
    return sum(s.distance for s in counted) + dry.b_cut, len(counted)


# --- planned all-sport ACWR (rules v2, item 12) ---------------------------------------------------------------

@dataclasses.dataclass
class LoadModel:
    """Planned daily training load: minutes x the sport's load per minute from the recent activity list.
    Days up to today carry the recorded loads, later days the plan's runs, the B races and the fixed calendar
    sessions. enforced: the model tracked Garmin's reported ratio within the tolerance on enough check days."""

    lpm: Dict[str, float]                  # load per minute by sport group ("all": every sport)
    run_mps: Optional[float]               # the recorded runs' mean speed (minutes of a run without LT speed)
    actual: Dict[dt.date, float]           # recorded load per day, up to today
    fixed: Dict[dt.date, float]            # fixed calendar sessions' planned load per day (today if not done yet)
    checks: List[Dict[str, Any]]
    enforced: bool
    reason: Optional[str]
    cuts: List[Dict[str, Any]] = dataclasses.field(default_factory=list)


def _ratio(acute: float, chronic: float) -> Optional[float]:
    return acute / chronic if chronic > 0 else None


def acwr_on(day: dt.date, loads: Mapping[dt.date, float], rules: Mapping[str, Any] = RULES) -> Optional[float]:
    """Acute:chronic ratio on `day`: the load of the last 7 days over the last 28 days' load / 4 (both
    ending on `day`), as Garmin's weekly scale; None while the chronic load is 0."""
    a = rules["acwr"]
    acute_n, chronic_n = int(a["acute_days"]), int(a["chronic_days"])
    acute = sum(loads.get(day - dt.timedelta(days=i), 0.0) for i in range(acute_n))
    chronic = sum(loads.get(day - dt.timedelta(days=i), 0.0) for i in range(chronic_n)) * acute_n / chronic_n
    return _ratio(acute, chronic)


def load_model(snap: Snapshot, ctx: Context) -> LoadModel:
    """The planned-load model of this snapshot and whether it is enforced (see LoadModel)."""
    rules = ctx.rules
    a = rules["acwr"]
    minutes: Dict[str, float] = {}
    loads: Dict[str, float] = {}
    counts: Dict[str, int] = {}
    run_m = run_s = 0.0
    actual: Dict[dt.date, float] = {}
    for act in snap.activity_load:
        if act.date > snap.today or act.seconds <= 0 or act.load < 0:
            continue
        actual[act.date] = actual.get(act.date, 0.0) + act.load
        for sport in (act.sport, "all"):
            minutes[sport] = minutes.get(sport, 0.0) + act.seconds / 60.0
            loads[sport] = loads.get(sport, 0.0) + act.load
            counts[sport] = counts.get(sport, 0) + 1
        if act.sport == "run" and act.meters > 0:
            run_m += act.meters
            run_s += act.seconds
    lpm = {k: loads[k] / minutes[k] for k in sorted(minutes) if minutes[k] > 0}
    mean_min = {k: minutes[k] / counts[k] for k in minutes}
    fixed: Dict[dt.date, float] = {}
    for f in snap.fixed:
        # a session today counts while nothing is recorded today (not done yet)
        if f.date < snap.today or (f.date == snap.today and snap.today in actual):
            continue
        rate = lpm.get(f.sport, lpm.get("all"))
        mins = f.minutes if f.minutes is not None and f.minutes > 0 else mean_min.get(f.sport, mean_min.get("all"))
        if rate is not None and mins is not None:
            fixed[f.date] = fixed.get(f.date, 0.0) + rate * mins
    checks: List[Dict[str, Any]] = []
    gaps: List[float] = []   # unrounded: the tolerance is compared with the real gap, not the shown one
    for g in sorted(snap.garmin_load, key=lambda x: x.date, reverse=True):
        model = acwr_on(g.date, actual, rules)
        garmin = _ratio(g.acute, g.chronic)
        if model is None or garmin is None:
            continue
        gaps.append(abs(model - garmin))
        checks.append({"date": g.date.isoformat(), "model": round(model, 2), "garmin": round(garmin, 2),
                       "gap": round(abs(model - garmin), 2)})
    tol = float(a["track_tolerance"])
    need = int(a["min_checks"])
    reason: Optional[str] = None
    close = sum(1 for gap in gaps if gap <= tol + 1e-9)
    if "run" not in lpm:
        reason = "no recorded run with a Garmin training load, so planned runs have no load"
    elif len(checks) < need:
        reason = (f"Garmin's acute and chronic load were read on {len(checks)} of the check days (at least "
                  f"{need} needed), so the model could not be compared with Garmin's")
    elif close < need:
        reason = (f"the model is within {tol} of Garmin's ratio on {close} of {len(checks)} check days (at least "
                  f"{need} needed), so the planned ratio is only reported")
    run_mps = run_m / run_s if run_s > 0 else None
    return LoadModel(lpm, run_mps, actual, fixed, checks, reason is None, reason)


def b_race_minutes(b: BRace, model: LoadModel, rules: Mapping[str, Any] = RULES) -> float:
    """A B race's estimated duration: the documented default of its kind (rules acwr.b_race_minutes); a run
    race is a half marathon at the recorded mean run speed (else that default)."""
    table = rules["acwr"]["b_race_minutes"]
    if b.kind == "run" and model.run_mps:
        return float(DISTANCES["half"]) / model.run_mps / 60.0
    return float(table[b.kind])


def session_load(s: Session, ctx: Context) -> float:
    """A planned session's load: a run's estimated minutes (pace bands, else the recorded mean run speed) x
    the run load per minute; a B race's estimated minutes x the all-sport load per minute. The A race is not
    counted."""
    model = ctx.load
    if model is None:
        return 0.0
    if s.role == "b_race":
        b = ctx.b_race_on(s.date)
        rate = model.lpm.get("all")
        return b_race_minutes(b, model, ctx.rules) * rate if b is not None and rate is not None else 0.0
    if not s.counts or "run" not in model.lpm:
        return 0.0
    secs = session_seconds(s, ctx.paces)
    if secs is None:
        if not model.run_mps:
            return 0.0
        secs = Seconds(s.distance / model.run_mps)
    return float(secs) / 60.0 * model.lpm["run"]


def day_loads(weeks: Sequence[Week], ctx: Context) -> Dict[dt.date, float]:
    """Load per day: recorded up to today, then the fixed sessions, the B races and the plan's runs."""
    model = ctx.load
    if model is None:
        return {}
    out = dict(model.actual)
    for d, v in model.fixed.items():
        out[d] = out.get(d, 0.0) + v
    for w in weeks:
        for s in w.sessions:
            if s.date > ctx.snapshot.today:
                out[s.date] = out.get(s.date, 0.0) + session_load(s, ctx)
    return out


def _cuttable(w: Week) -> List[Session]:
    return [s for s in w.sessions if s.role in ("easy", "strides")]


def _report_days(w: Week, ctx: Context) -> List[dt.date]:
    """Week w's days whose planned ratio is reported: after today, before the A race."""
    return [d for d in w.days() if ctx.snapshot.today < d < ctx.inputs.race_date]


def _acwr_days(w: Week, ctx: Context) -> List[dt.date]:
    """The days the cap acts on: the reported days a cut of the week can lower, i.e. on or after its first
    easy run (whose 7-day window then holds it), not a B race day (its own load is not the plan's to cut)."""
    cut = [s.date for s in _cuttable(w)]
    if not cut:
        return []
    first = min(cut)
    return [d for d in _report_days(w, ctx) if d >= first and ctx.b_race_on(d) is None]


def _acwr_floor(w: Week, ctx: Context) -> float:
    return float(ctx.rules["acwr"]["min_keep_of_target"]) * float(w.target)


def acwr_over(w: Week, ctx: Context, loads: Mapping[dt.date, float]) -> List[Tuple[dt.date, float]]:
    """(day, planned ratio) of the days the cap acts on in week w that are above the limit."""
    limit = float(ctx.rules["acwr"]["max"])
    out = []
    for d in _acwr_days(w, ctx):
        r = acwr_on(d, loads, ctx.rules)
        if r is not None and r > limit + 1e-9:
            out.append((d, r))
    return out


def _planned(w: Week) -> float:
    return float(sum(s.distance for s in w.sessions if s.counts))


def _acwr_step(w: Week, ctx: Context, day: dt.date, floor: float, step: float) -> bool:
    """One cut for the ratio on `day`: the longest easy run of week w on or before it gets `step` metres
    shorter (100 m steps), or goes when it would fall under the easy minimum, never taking the week under
    `floor`; a quality session goes too if the week would fall under 80% easy, and the step is undone if
    that takes the week under the floor. True when the week got smaller."""
    r, paces = ctx.rules, ctx.paces
    cands = [s for s in _cuttable(w) if s.date <= day]
    if not cands:
        return False
    saved = list(w.sessions)
    planned = _planned(w)
    room = planned - floor
    s = max(cands, key=lambda x: (x.distance, x.key))
    cut = math.floor(min(step, room) / 100.0 + 1e-9) * 100.0
    if cut < 100.0:
        return False
    new_d = float(s.distance) - cut
    easy_min = float(r["easy_min_km"]) * 1000.0
    if new_d < easy_min:
        if s.distance <= room + EPS_M:
            w.sessions.remove(s)
        elif s.distance - easy_min >= 100.0:
            new_d = easy_min
        else:
            return False
    if s in w.sessions:
        w.sessions[w.sessions.index(s)] = make_session(s.key, s.date, s.role, Meters(new_d), paces, r)
    _limit_easy_share(w, w.sessions, ctx, estimate_rest=False)   # shorter easy runs: keep 80% easy
    after = _planned(w)
    if after < floor - EPS_M or after >= planned - EPS_M:
        w.sessions = saved
        return False
    return True


def enforce_acwr(w: Week, ctx: Context) -> bool:
    """Item 12: while the model is enforced and a day the cap acts on (on or after the week's first easy
    run) has a planned ratio above the limit, cut the week's easy runs on or before the highest such day in
    steps of 5% of the week's km. The long run and quality stay (the long-run step is untouched); the week
    never ends under 0.6 x its target. It stops, with the warning acwr_floor and w.acwr_held, when no step
    is left or a step no longer lowers the highest ratio (that step is undone). True when it cut. Never
    raises a session."""
    model = ctx.load
    w.acwr_held = False
    if model is None or not model.enforced:
        return False
    a = ctx.rules["acwr"]
    floor = _acwr_floor(w, ctx)
    before = _planned(w)
    n_before = sum(1 for s in w.sessions if s.counts)
    step = float(a["cut_step"]) * before
    first: Optional[Tuple[dt.date, float]] = None
    for _ in range(60):
        over = acwr_over(w, ctx, day_loads(ctx.all_weeks, ctx))
        if not over:
            break
        first = first or over[0]
        top_day, top = max(over, key=lambda x: (x[1], x[0]))
        saved = list(w.sessions)
        lowered = False
        if _acwr_step(w, ctx, top_day, floor, step):
            loads = day_loads(ctx.all_weeks, ctx)
            now = max((acwr_on(d, loads, ctx.rules) or 0.0 for d, _ in over), default=0.0)
            lowered = now < top - 1e-9
            if not lowered:
                w.sessions = saved
        if not lowered:
            w.acwr_held = True
            ctx.warnings.append({"id": "acwr_floor", "dates": [d.isoformat() for d, _ in over[:5]],
                                 "detail": f"the planned acute:chronic ratio stays above {a['max']} (up to "
                                           f"{round(top, 2)}): no further cut of the week's easy runs lowers it "
                                           f"(they stop at {a['min_keep_of_target']} x the week's target)"})
            break
    after = _planned(w)
    if after >= before - EPS_M or first is None:
        return False
    if before > w.acwr_from[0]:
        w.acwr_from = (before, n_before)
    model.cuts.append({"week": w.number, "from_km": km(before), "to_km": km(after), "date": first[0].isoformat(),
                       "ratio": round(first[1], 2)})
    ctx.warnings.append({"id": "acwr_cut", "dates": [w.start.isoformat()],
                         "detail": f"run km cut from {km(before)} to {km(after)} km: the planned acute:chronic "
                                   f"ratio reached {round(first[1], 2)} on {first[0].isoformat()} (limit {a['max']})"})
    return True


def acwr_answer(weeks: Sequence[Week], ctx: Context) -> Dict[str, Any]:
    """The answer's "acwr" block: whether item 12 is enforced and why not, the model's numbers, the checks
    against Garmin, the B races' modelled load, the cuts and the highest planned ratio (every day after today
    and before the A race). Adds acwr_not_enforced / acwr_high warnings."""
    model = ctx.load
    a = ctx.rules["acwr"]
    rules_echo = {k: a[k] for k in sorted(a)}
    if model is None:   # pragma: no cover - _build_core always builds one
        return {"enforced": False, "rules": rules_echo}
    loads = day_loads(weeks, ctx)
    top: Optional[Tuple[float, dt.date]] = None
    over: List[dt.date] = []
    for w in weeks:
        for d in _report_days(w, ctx):
            r = acwr_on(d, loads, ctx.rules)
            if r is None:
                continue
            if top is None or r > top[0] + 1e-12:
                top = (r, d)
            if r > float(a["max"]) + 1e-9:
                over.append(d)
    if not model.enforced:
        ctx.warnings.append({"id": "acwr_not_enforced", "dates": [], "detail": str(model.reason)})
        if over:
            ctx.warnings.append({"id": "acwr_high", "dates": [d.isoformat() for d in over[:5]],
                                 "detail": f"the planned acute:chronic ratio passes {a['max']} on {len(over)} "
                                           f"days (up to {round(top[0], 2) if top else None}); not enforced"})
    b_loads = []
    rate = model.lpm.get("all")
    for bw in ctx.windows:
        mins = b_race_minutes(bw.race, model, ctx.rules)
        b_loads.append({"date": bw.race.date.isoformat(), "kind": bw.race.kind, "minutes": int(round(mins)),
                        "load": int(round(mins * rate)) if rate is not None else None})
    return {"enforced": model.enforced, "reason": model.reason, "rules": rules_echo,
            "load_per_min": {k: round(v, 3) for k, v in model.lpm.items()},
            "checks": model.checks, "b_races": b_loads, "cuts": model.cuts,
            "planned_max": {"ratio": round(top[0], 2), "date": top[1].isoformat()} if top else None,
            "days_over": len(over)}


def check_acwr(weeks: Sequence[Week], ctx: Context) -> List[str]:
    """Invariant acwr_cap: while enforced, no day the cap acts on is above the limit unless its week is at
    its floor or the engine held it (no further cut lowered the ratio)."""
    model = ctx.load
    if model is None or not model.enforced:
        return []
    loads = day_loads(weeks, ctx)
    out = []
    for w in weeks:
        if w.acwr_held or _planned(w) <= _acwr_floor(w, ctx) + EPS_M:
            continue
        out += [f"{d.isoformat()}: ratio {round(r, 2)}" for d, r in acwr_over(w, ctx, loads)]
    return out


def reenforce_acwr(weeks: Sequence[Week], ctx: Context) -> None:
    """After an override: cut the weeks again, in order, where the edit pushed the planned ratio up (a
    smaller week lowers the chronic load of the weeks after it). Only ever cuts."""
    for w in weeks:
        enforce_acwr(w, ctx)


# --- race pacing (rules v2, item 13) --------------------------------------------------------------------------

def race_pacing(inputs: PlanInputs, snap: Snapshot, rules: Mapping[str, Any],
                warnings: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Per-segment paces for the A race on its course, at the goal time (else Garmin's prediction): each
    climb slower by 3% per 1% of its average grade, the rest of the course (flat or rolling) at one pace,
    so the whole course takes the goal time scaled to the course length. None without a course_id."""
    if inputs.course_id is None and snap.course is None:
        return None
    rp = rules["race_pacing"]
    out: Dict[str, Any] = {"course_id": inputs.course_id, "rules": {k: rp[k] for k in sorted(rp)},
                           "segments": [], "missing": []}
    course = snap.course
    goal = inputs.goal_time_s or snap.prediction(inputs.race_distance)
    if course is None or course.distance_m <= 0:
        out["missing"].append("course")
        return out
    if not goal:
        out["missing"].append("goal_time")
        return out
    race_m = float(DISTANCES[inputs.race_distance])
    total_m = float(course.distance_m)
    if abs(total_m - race_m) / race_m > float(rp["course_distance_tolerance"]):
        warnings.append({"id": "course_distance_mismatch", "dates": [inputs.race_date.isoformat()],
                         "detail": f"the course is {km(total_m)} km, the race {km(race_m)} km: paced over the "
                                   f"course at the goal pace"})
    k = float(rp["uphill_slowdown_per_grade_pct"])
    target_s = goal * total_m / race_m
    parts: List[Tuple[float, float, Optional[float], float]] = []   # (from m, to m, grade %, gain m)
    at = 0.0
    for c in sorted(course.climbs, key=lambda x: (x.start_m, x.end_m)):
        lo, hi = max(at, float(c.start_m)), min(total_m, float(c.end_m))
        if hi - lo < 1.0:
            continue
        if lo > at:
            parts.append((at, lo, None, 0.0))
        grade = max(0.0, float(c.gain_m)) / (float(c.end_m) - float(c.start_m)) * 100.0
        parts.append((lo, hi, grade, float(c.gain_m) * (hi - lo) / (float(c.end_m) - float(c.start_m))))
        at = hi
    if at < total_m:
        parts.append((at, total_m, None, 0.0))
    effort = sum((hi - lo) * (1.0 + k * (g or 0.0)) for lo, hi, g, _ in parts)
    flat = effort / target_s   # m/s on the flat
    clock = 0.0
    for lo, hi, g, gain in parts:
        speed = flat / (1.0 + k * (g or 0.0))
        start = int(round(clock))
        clock += (hi - lo) / speed
        # each segment's time is the rounded clock at its end minus at its start: they add up to the finish
        seg: Dict[str, Any] = {"from_km": round(lo / 1000.0, 2), "to_km": round(hi / 1000.0, 2),
                               "kind": "flat" if g is None else "climb", "pace": pace_text(speed),
                               "time_s": int(round(clock)) - start, "at": hms(int(round(clock)))}
        if g is not None:
            seg["grade_pct"] = round(g, 1)
            seg["gain_m"] = int(round(gain))
        out["segments"].append(seg)
    out.update({"course_km": km(total_m), "goal_time": hms(int(goal)), "goal_pace": pace_text(race_m / goal),
                "flat_pace": pace_text(flat), "finish": hms(int(round(clock)))})
    return out


# --- overrides (Engine v1 item 11) ----------------------------------------------------------------------------

def _hard_days(weeks: Sequence[Week], ctx: Context) -> Set[dt.date]:
    out = {s.date for w in weeks for s in w.sessions if s.hard_or_long}
    return out | {d for d in ctx.fixed if ctx.fixed_hard(d)}


def _find(weeks: Sequence[Week], key: str) -> Optional[Tuple[Week, Session]]:
    for w in weeks:
        for s in w.sessions:
            if s.key == key:
                return w, s
    return None


def _on(weeks: Sequence[Week], day: dt.date) -> List[Tuple[Week, Session]]:
    return [(w, s) for w in weeks for s in w.sessions if s.date == day]


def _refuse(o: Override, rule: str, value: Any = None, limit: Any = None) -> Dict[str, Any]:
    return {"edit": o.echo(), "rule": rule, "value": value, "limit": limit}


def _day_ok_for(s: Session, day: dt.date, w: Week, weeks: Sequence[Week], ctx: Context,
                ignore: Sequence[Session] = ()) -> Optional[Tuple[str, Any, Any]]:
    """None when session `s` may move to `day`, else (rule, value, limit)."""
    if not w.start <= day <= w.end:
        return ("move_within_week", day.isoformat(), f"{w.start.isoformat()}..{w.end.isoformat()}")
    if day.weekday() in ctx.inputs.rest_days:
        return ("rest_days", WEEKDAYS[day.weekday()], "no run on a rest day")
    if day in ctx.inputs.blocked_dates:
        return ("blocked_dates", day.isoformat(), "no run on a blocked date")
    if day <= ctx.snapshot.today:
        return ("past_day", day.isoformat(), ctx.snapshot.today.isoformat())
    if day > ctx.inputs.race_date:   # rule 2: the race week has no run after the race
        return ("after_race", day.isoformat(), ctx.inputs.race_date.isoformat())
    if ctx.event_hard(day) and (day == ctx.inputs.race_date or ctx.b_race_on(day)):
        return ("race_day", day.isoformat(), "no other run on a race day")
    if ctx.fixed_run(day):
        return ("one_run_per_day", day.isoformat(), "a run is already on the calendar")
    if any(o is not s and o not in ignore and o.is_run for _, o in _on(weeks, day)):
        return ("one_run_per_day", day.isoformat(), "the plan already has a run that day")
    mini_cap = float(ctx.rules["b_race"]["mini_taper_max_km"]) * 1000.0
    if day in _mini_days(ctx) and s.distance > mini_cap + EPS_M:   # rule 7: B race week runs
        return ("b_window", km(s.distance), km(mini_cap))
    if s.hard_or_long:
        if ctx.window_factor(day) is not None:
            return ("b_window", day.isoformat(), "only easy runs and strides around a B race")
        limit = late_long_limit(ctx, day, weeks, skip=s) if s.role == "long" else None
        if limit is not None and s.distance > limit + EPS_M:
            return ("peak_long_timing", km(s.distance), km(limit))
        if s.role == "long":   # rule 7: the long runs before and after a B race
            b_limit, soon = b_long_limit(ctx, day, weeks, skip=s)
            if soon is not None:
                return ("b_window", day.isoformat(), f"no long run within "
                        f"{ctx.rules['b_race']['long_after_min_days'][soon.kind]} days after the B race")
            if b_limit is not None and s.distance > b_limit + EPS_M:
                return ("b_window", km(s.distance), km(b_limit))
        hard = _hard_days(weeks, ctx) - {s.date} - {x.date for x in ignore if x.hard_or_long}
        if not _adjacent_ok(day, hard):
            return ("no_adjacent_hard", day.isoformat(), "no two hard-or-long days in a row")
    return None


def apply_overrides(weeks: List[Week], ctx: Context) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """The coach critic's overrides, one at a time and in order. Each is checked by its own rule first; then
    every invariant runs, and an edit that makes one fail that passed before is undone and refused with that
    rule. ([applied edits], [{edit, rule, value, limit}])."""
    applied: List[Dict[str, Any]] = []
    refused: List[Dict[str, Any]] = []
    for o in ctx.inputs.overrides:
        saved = [copy.deepcopy(w.sessions) for w in weeks]
        saved_from = [(w.acwr_from, w.acwr_held) for w in weeks]
        saved_warnings, saved_cuts = len(ctx.warnings), len(ctx.load.cuts) if ctx.load else 0
        before = {i["id"] for i in check_invariants({"_check": {"weeks": weeks, "ctx": ctx}}, ctx.rules)
                  if not i["ok"]}
        ok, no = _apply_one(weeks, ctx, o)
        if ok:
            reenforce_acwr(weeks, ctx)   # a moved or smaller session can raise a later day's planned ratio
            after = [i for i in check_invariants({"_check": {"weeks": weeks, "ctx": ctx}}, ctx.rules)
                     if not i["ok"] and i["id"] not in before]
            if after:
                for w, sessions, was in zip(weeks, saved, saved_from):
                    w.sessions = sessions
                    w.acwr_from, w.acwr_held = was
                del ctx.warnings[saved_warnings:]
                if ctx.load:
                    del ctx.load.cuts[saved_cuts:]
                refused.append(_refuse(o, after[0]["id"], after[0]["detail"], "the plan's rules"))
                continue
        applied += ok
        refused += no
    return applied, refused


def _apply_one(weeks: List[Week], ctx: Context, edit: Override) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    applied: List[Dict[str, Any]] = []
    refused: List[Dict[str, Any]] = []
    paces, r = ctx.paces, ctx.rules
    for o in (edit,):
        if o.kind in ("move", "easy", "cap_km"):
            found = _find(weeks, o.key) if o.key or o.kind == "move" else _run_on(weeks, o.date)
            if found is None or found[1].role in ("race", "b_race"):
                refused.append(_refuse(o, "unknown_session", o.key or (o.date.isoformat() if o.date else None),
                                       "a session key (or a day with a run) of this plan"))
                continue
            w, s = found
            if o.kind == "move":
                if o.date is None:
                    refused.append(_refuse(o, "date_required"))
                    continue
                why = _day_ok_for(s, o.date, w, weeks, ctx)
                if why:
                    refused.append(_refuse(o, *why))
                    continue
                s.date = o.date
            elif o.kind == "cap_km":
                why_cap = _cap_km_refusal(o, s)
                if why_cap:
                    refused.append(_refuse(o, *why_cap))
                    continue
                assert o.km is not None
                w.sessions[w.sessions.index(s)] = make_session(s.key, s.date, s.role, _floor_m(o.km * 1000.0),
                                                               paces, r)
            else:
                if s.role in ("easy",):
                    refused.append(_refuse(o, "already_easy", s.key))
                    continue
                cap = Meters(float(r["easy_max_km"]) * 1000.0)
                mid = paces.mid("easy")
                if mid is not None:
                    cap = Meters(min(cap, mid * float(r["easy_max_min"]) * 60.0))
                new = make_session(s.key, s.date, "easy", _floor_m(min(s.distance, cap)), paces, r)
                w.sessions[w.sessions.index(s)] = new
            applied.append(o.echo())
        elif o.kind == "swap":
            if o.date is None or o.date_b is None or o.date == o.date_b:
                refused.append(_refuse(o, "two_dates_required"))
                continue
            a = [(w, s) for w, s in _on(weeks, o.date) if s.role not in ("race", "b_race")]
            b = [(w, s) for w, s in _on(weeks, o.date_b) if s.role not in ("race", "b_race")]
            if not a and not b:
                refused.append(_refuse(o, "nothing_to_swap"))
                continue
            moving = [s for _, s in a + b]
            problem = None
            for wk, s in a:
                problem = problem or _day_ok_for(s, o.date_b, wk, weeks, ctx, ignore=moving)
            for wk, s in b:
                problem = problem or _day_ok_for(s, o.date, wk, weeks, ctx, ignore=moving)
            if problem is None:
                # adjacency once both moved
                for _, s in a:
                    s.date = o.date_b
                for _, s in b:
                    s.date = o.date
                if _adjacent_hard_pairs(weeks, ctx):
                    for _, s in a:
                        s.date = o.date
                    for _, s in b:
                        s.date = o.date_b
                    problem = ("no_adjacent_hard", f"{o.date.isoformat()},{o.date_b.isoformat()}",
                               "no two hard-or-long days in a row")
            if problem:
                refused.append(_refuse(o, *problem))
                continue
            applied.append(o.echo())
        elif o.kind == "rest":
            if o.date is None:
                refused.append(_refuse(o, "date_required"))
                continue
            if any(s.role in ("race", "b_race") for _w, s in _on(weeks, o.date)):
                refused.append(_refuse(o, "race_day", o.date.isoformat()))
                continue
            here = [(w, s) for w, s in _on(weeks, o.date) if s.role not in ("race", "b_race")]
            if not here:
                refused.append(_refuse(o, "no_run_that_day", o.date.isoformat()))
                continue
            for w, s in here:
                w.sessions.remove(s)
            applied.append(o.echo())
        elif o.kind == "cap_week_km":
            w2 = next((w for w in weeks if o.week_start is not None and w.start == o.week_start), None)
            if w2 is None or o.km is None:
                refused.append(_refuse(o, "unknown_week", o.week_start.isoformat() if o.week_start else None,
                                       "a Monday of this plan"))
                continue
            planned = km(sum(s.distance for s in w2.sessions if s.counts))
            if o.km > planned:
                refused.append(_refuse(o, "raises_load", o.km, planned))
                continue
            _cap_week(w2, Meters(o.km * 1000.0), ctx)
            applied.append(o.echo())
        else:  # long_run_day
            w3 = next((w for w in weeks if o.week_start is not None and w.start == o.week_start), None)
            if w3 is None or o.weekday is None:
                refused.append(_refuse(o, "unknown_week", o.week_start.isoformat() if o.week_start else None,
                                       "a Monday of this plan"))
                continue
            long_s = next((s for s in w3.sessions if s.role == "long"), None)
            if long_s is None:
                refused.append(_refuse(o, "no_long_run", o.week_start.isoformat() if o.week_start else None))
                continue
            day = w3.start + dt.timedelta(days=o.weekday)
            others = [s for _, s in _on(weeks, day) if s is not long_s and s.role in ("easy", "strides")]
            why = _day_ok_for(long_s, day, w3, weeks, ctx, ignore=others)
            if why:
                refused.append(_refuse(o, *why))
                continue
            for s in others:   # the easy run that day changes place with the long run
                s.date = long_s.date
            long_s.date = day
            applied.append(o.echo())
    for w in weeks:
        w.sessions.sort(key=lambda s: (s.date, s.key))
    return applied, refused


def _run_on(weeks: Sequence[Week], day: Optional[dt.date]) -> Optional[Tuple[Week, Session]]:
    """The plan's run on `day` (an override that names a day instead of a key), or None."""
    if day is None:
        return None
    return next(((w, s) for w, s in _on(weeks, day) if s.is_run and s.role not in ("race", "b_race")), None)


def _cap_km_refusal(o: Override, s: Session) -> Optional[Tuple[str, Any, Any]]:
    """None when cap_km may shorten session `s` to o.km, else (rule, value, limit): only easy, strides and long
    runs, never longer than planned, never under the easy minimum (a rest override drops a run)."""
    if o.km is None:
        return ("km_required", None, None)
    if s.role not in ("easy", "strides", "long"):
        return ("cap_km_role", s.role, "easy, strides or long")
    if o.km * 1000.0 > s.distance + EPS_M:
        return ("raises_load", o.km, km(s.distance))
    if o.km < float(RULES["easy_min_km"]):
        return ("easy_min_km", o.km, float(RULES["easy_min_km"]))
    return None


def _cap_week(w: Week, limit: Meters, ctx: Context) -> None:
    """Cut the week to `limit` metres: easy runs first (shortest dropped below the easy minimum), then the long
    run (not below the longest run of 8 weeks... unless needed), then the quality sessions from the last."""
    paces, r = ctx.paces, ctx.rules

    def total() -> float:
        return sum(s.distance for s in w.sessions if s.counts)

    easy_min = float(r["easy_min_km"]) * 1000.0
    while total() > limit + EPS_M:
        excess = total() - limit
        easies = [s for s in w.sessions if s.role in ("easy", "strides")]
        if easies:
            s = max(easies, key=lambda x: (x.distance, x.key))
            new_d = _floor_m(s.distance - excess)
            if new_d < easy_min:
                w.sessions.remove(s)
            else:
                w.sessions[w.sessions.index(s)] = make_session(s.key, s.date, s.role, new_d, paces, r)
            continue
        long_s = next((s for s in w.sessions if s.role == "long"), None)
        if long_s is not None:
            new_d = _floor_m(long_s.distance - excess)
            if new_d < easy_min:
                w.sessions.remove(long_s)
            else:
                w.sessions[w.sessions.index(long_s)] = make_session(long_s.key, long_s.date, "long", new_d, paces, r)
            continue
        qual = [s for s in w.sessions if s.role in QUALITY_ROLES]
        if not qual:
            return
        w.sessions.remove(qual[-1])


# --- invariants (Engine v1 item 9) ----------------------------------------------------------------------------

def _adjacent_hard_pairs(weeks: Sequence[Week], ctx: Context) -> List[Tuple[dt.date, dt.date]]:
    planned = {s.date for w in weeks for s in w.sessions if s.hard_or_long and s.role not in ("race", "b_race")}
    hard = _hard_days(weeks, ctx) | {ctx.inputs.race_date} | {w.race.date for w in ctx.windows}
    out = []
    for d in sorted(hard):
        nxt = d + dt.timedelta(days=1)
        if nxt in hard and (d in planned or nxt in planned):
            out.append((d, nxt))
    return out


def check_invariants(plan: Mapping[str, Any], rules: Mapping[str, Any] = RULES) -> List[Dict[str, Any]]:
    """Every rule checked on a finished plan dict (build_plan's answer): [{id, ok, detail}] in a fixed order.
    The plan carries what the checks need ("_check" is internal and never part of the answer)."""
    chk = plan["_check"]
    weeks: List[Week] = chk["weeks"]
    ctx: Context = chk["ctx"]
    paces = ctx.paces
    out: List[Dict[str, Any]] = []

    def add(rule: str, problems: List[str]) -> None:
        out.append({"id": rule, "ok": not problems, "detail": "; ".join(problems[:5])})

    def planned(w: Week) -> float:
        return sum(s.distance for s in w.sessions if s.counts)

    growth = 1 + float(rules["growth_max"])
    ceiling, _biggest = peak_ceiling(ctx.snapshot, ctx.km0, rules)
    step = float(rules["long_step_m"])
    days_late = int(rules["late_long_days"])
    t_cap = long_time_cap(paces, rules)
    # Recomputed here, not taken from the engine's own bookkeeping: the km the weeks grow from (min of the
    # target and what was placed plus the blocked days' share), and the longest long run placed so far.
    last_normal = float(ctx.km0)
    peak = 0.0
    run_long = float(ctx.longest)
    peak_long = 0.0
    probs: Dict[str, List[str]] = {k: [] for k in ("growth_cap", "deload_depth", "no_makeup", "b_windows",
                                                    "a_taper", "long_cap", "long_step", "easy_share",
                                                    "peak_ceiling")}
    prev_reduced = False
    for w in weeks:
        if w.kind == "week0":
            continue
        p = planned(w)
        long_s = [s for s in w.sessions if s.role == "long"]
        lmax = max((s.distance for s in long_s), default=0.0)
        if max(p, float(w.target)) > ceiling + EPS_M:
            probs["peak_ceiling"].append(f"week {w.number}: {km(max(p, float(w.target)))} km > {km(ceiling)} km")
        if w.kind == "normal":
            if p > last_normal * growth + EPS_M:
                probs["no_makeup" if prev_reduced else "growth_cap"].append(
                    f"week {w.number}: {km(p)} km > {km(last_normal * growth)} km")
            limit = min([max(ctx.inputs.long_share * w.target, ctx.longest), run_long + step,
                         ctx.inputs.max_long_km * 1000] + ([float(t_cap)] if t_cap is not None else [])) + EPS_M
            if lmax > limit:
                probs["long_cap"].append(f"week {w.number}: long {km(lmax)} km > {km(limit)} km")
            if lmax > run_long + step + EPS_M:
                probs["long_step"].append(f"week {w.number}: long {km(lmax)} km > {km(run_long)} + 2 km")
            # the engine's base may only be lower than the target it could have had; before any override it
            # is at most what was placed plus the blocked days' share
            target = min(last_normal * growth, float(ceiling))
            # (what rule 7 took off a long run around a B race is no shortfall either)
            got = w.acwr_from if w.acwr_from[0] > p else (p, sum(1 for s in w.sessions if s.counts))
            if not ctx.inputs.overrides and not calendar_week(w, ctx) \
                    and w.base > week_base(float(w.target), got[0] + w.b_cut, got[1], None) + EPS_M:
                probs["growth_cap"].append(f"week {w.number}: grows from {km(w.base)} km, placed {km(p)} km")
            last_normal = min(target, float(w.base))
            peak = max(peak, last_normal)
            if long_s and (ctx.inputs.race_date - long_s[0].date).days >= days_late:
                run_long = max(run_long, lmax)
                peak_long = max(peak_long, lmax)
            prev_reduced = False
        else:
            prev_reduced = True
            if w.kind == "deload" and p > last_normal * float(rules["deload_factor"]) + EPS_M:
                probs["deload_depth"].append(f"week {w.number}: {km(p)} km")
            if w.kind == "b_race" and p > last_normal * w.factor + EPS_M:
                probs["b_windows"].append(f"week {w.number}: {km(p)} km")
            if w.kind in ("taper", "race"):
                f = float(rules["taper_factor" if w.kind == "taper" else "race_week_factor"])
                if p > (peak or last_normal) * f + EPS_M:
                    probs["a_taper"].append(f"week {w.number}: {km(p)} km > {km((peak or last_normal) * f)} km")
            ref = peak_long if peak_long > 0 else float(long_cap(ctx, Meters(last_normal), Meters(run_long)))
            if lmax > ref + EPS_M:
                probs["long_step"].append(f"week {w.number}: long {km(lmax)} km in a reduced week")
        hard = sum(hard_seconds(s, paces)[0] for s in w.sessions if s.counts)
        total = sum(hard_seconds(s, paces)[1] for s in w.sessions if s.counts)
        if total > 0 and hard / total > 1 - float(rules["easy_share_min"]) + 1e-6:
            probs["easy_share"].append(f"week {w.number}: {round(100 * (1 - hard / total))}% easy")
    for k in ("growth_cap", "no_makeup"):
        add(k, probs[k])
    # deload cadence: at most 3 normal weeks in a row, 4 when a B week follows
    cad: List[str] = []
    run = 0
    real = [w for w in weeks if w.kind != "week0"]
    for i, w in enumerate(real):
        run = run + 1 if w.kind == "normal" else 0
        nxt = real[i + 1].kind if i + 1 < len(real) else None
        allowed = int(rules["normal_weeks_before_deload"]) + (1 if nxt == "b_race" else 0)
        if run > allowed and w.phase not in ("taper", "race"):
            cad.append(f"week {w.number}: {run} normal weeks in a row")
    add("deload_cadence", cad)
    add("deload_depth", probs["deload_depth"])
    add("long_cap", probs["long_cap"])
    add("long_step", probs["long_step"])
    # peak long run >= 21 days before the A race and outside the B windows
    days_out = int(rules["late_long_days"])
    late: List[str] = []
    for w in real:
        for s in w.sessions:
            if s.role != "long":
                continue
            cap = late_long_limit(dataclasses.replace(ctx, rules=rules), s.date, weeks)
            if cap is not None and s.distance > cap + EPS_M:
                late.append(f"{s.date.isoformat()}: {km(s.distance)} km within {days_out} days of the race")
            if ctx.window_factor(s.date) is not None:
                late.append(f"{s.date.isoformat()}: a long run inside a B window")
    add("peak_long_timing", late)
    add("easy_share", probs["easy_share"])
    add("peak_ceiling", probs["peak_ceiling"])
    pairs = _adjacent_hard_pairs(weeks, ctx)
    add("no_adjacent_hard", [f"{a.isoformat()} and {b.isoformat()}" for a, b in pairs])
    bands: List[str] = []
    lo_lim, hi_lim = (float(x) for x in rules["speed_limits_mps"])
    for w in weeks:
        for s in w.sessions:
            for st in _leaves(s.steps):
                band = paces.band(st.band) if st.band else None
                if band is not None and not (lo_lim <= band[0] <= band[1] <= hi_lim):
                    bands.append(f"{s.key}: {band}")
                if st.band and s.role in ("easy", "long") and st.band not in ("easy", "long"):
                    bands.append(f"{s.key}: {st.band} step in an {s.role} run")
                if s.role in QUALITY_ROLES and st.kind == "interval" and st.band != s.role:
                    bands.append(f"{s.key}: {st.band} work in a {s.role} session")
    add("bands_match_roles", bands)
    bw: List[str] = []
    mini_cap = float(rules["b_race"]["mini_taper_max_km"]) * 1000.0
    for w in weeks:
        for s in w.sessions:
            wf = ctx.window_factor(s.date)
            if wf is None or s.role in ("b_race", "race"):   # a B recovery may reach the A race day
                continue
            mini = s.date in _mini_days(ctx)
            if s.role not in (("strides", "easy") if mini else ("easy",)):
                bw.append(f"{s.date.isoformat()}: {s.role} in a B window")
            if mini and s.distance > mini_cap + EPS_M:
                bw.append(f"{s.date.isoformat()}: {km(s.distance)} km in a B race week > {km(mini_cap)} km")
    # rule 7: the long run 5-13 days before a B race and the first one after it
    b_ctx = dataclasses.replace(ctx, rules=rules)
    for w in real:
        for s in w.sessions:
            if s.role != "long":
                continue
            b_limit, soon = b_long_limit(b_ctx, s.date, weeks, skip=s)
            if soon is not None:
                bw.append(f"{s.date.isoformat()}: a long run {(s.date - soon.date).days} days after a {soon.kind}")
            if b_limit is not None and s.distance > b_limit + EPS_M:
                bw.append(f"{s.date.isoformat()}: long {km(s.distance)} km around a B race > {km(b_limit)} km")
    add("b_windows", bw + probs["b_windows"])
    add("a_taper", probs["a_taper"])
    lq: List[str] = []
    quals = [s for w in weeks for s in w.sessions if s.role in QUALITY_ROLES]
    for s in quals:
        if 0 <= (ctx.inputs.race_date - s.date).days < int(rules["last_quality_days_before_race"]):
            lq.append(f"{s.date.isoformat()}: quality {(ctx.inputs.race_date - s.date).days} days before the race")
    add("last_quality", lq)
    rd: List[str] = []
    days_seen: Dict[dt.date, int] = {}
    for w in weeks:
        for s in w.sessions:
            if s.role in ("b_race", "race"):   # a race on the calendar may be the race itself
                continue
            days_seen[s.date] = days_seen.get(s.date, 0) + 1
            if s.date.weekday() in ctx.inputs.rest_days or s.date in ctx.inputs.blocked_dates:
                rd.append(f"{s.date.isoformat()}: a run on a rest day or blocked date")
    add("rest_days", rd)
    keys_seen: Dict[str, int] = {}
    for w in weeks:
        for s in w.sessions:
            keys_seen[s.key] = keys_seen.get(s.key, 0) + 1
    add("one_run_per_day", [f"{d.isoformat()}: {n} runs" for d, n in sorted(days_seen.items())
                            if n > 1 or (n >= 1 and ctx.fixed_run(d))]
        + [f"key {k}: {n} sessions" for k, n in sorted(keys_seen.items()) if n > 1])   # session_key is per day
    add("acwr_cap", check_acwr(weeks, ctx))   # v2
    return out


def _leaves(steps: Iterable[Step]) -> Iterable[Step]:
    for st in steps:
        if st.kind == "repeat":
            yield from _leaves(st.children)
        else:
            yield st


# --- feasibility (Engine v1 item 8) ---------------------------------------------------------------------------

def feasibility(inputs: PlanInputs, snap: Snapshot, weeks_to_race: Optional[int], peak_km: Optional[float],
                longest_planned_km: Optional[float], km0_km: Optional[float],
                rules: Mapping[str, Any] = RULES) -> Dict[str, Any]:
    """Checks that warn or fail; never changes the plan. verdict: feasible | ambitious | unrealistic."""
    f = rules["feasibility"][inputs.race_distance]
    dist = DISTANCES[inputs.race_distance]
    lt = snap.lt_speed_mps
    pred = snap.prediction(inputs.race_distance)
    goal = inputs.goal_time_s
    checks: List[Dict[str, Any]] = []
    missing: List[str] = []

    def check(cid: str, level: str, failed: bool, value: Any, limit: Any) -> None:
        checks.append({"id": cid, "level": level, "ok": not failed, "value": value, "limit": limit})

    if weeks_to_race is not None:
        check("weeks", "fail", weeks_to_race < int(f["min_weeks"]), weeks_to_race, int(f["min_weeks"]))
    if km0_km is not None:
        check("base_km", "warn", km0_km < float(f["min_km0"]), km0_km, float(f["min_km0"]))
    if peak_km is not None:
        check("peak_km", "warn", peak_km < float(f["min_peak_km"]), peak_km, float(f["min_peak_km"]))
    if longest_planned_km is not None:
        check("long_run_km", "warn", longest_planned_km < float(f["min_long_km"]), longest_planned_km,
              float(f["min_long_km"]))
    if goal:
        if lt is not None:
            ratio = round((dist / goal) / lt, 3)
            check("goal_vs_threshold", "fail", ratio > float(f["max_goal_of_lt"]), ratio, float(f["max_goal_of_lt"]))
        else:
            missing.append("goal_vs_threshold")
        if pred:
            pct = round(100.0 * (pred - goal) / pred, 1)
            fr = rules["feasibility"]
            check("goal_vs_prediction", "warn", pct > float(fr["ambitious_pct"]), pct, float(fr["ambitious_pct"]))
            check("goal_unrealistic", "fail", pct > float(fr["unrealistic_pct"]), pct, float(fr["unrealistic_pct"]))
        else:
            missing.append("goal_vs_prediction")
    corrected: Optional[int] = None
    options = [float(pred)] if pred else []
    if lt is not None:
        options.append(dist / (float(f["corrected_of_lt"]) * lt))
    if options:
        corrected = int(math.ceil(max(options) / 60.0) * 60)
    failed = [c for c in checks if not c["ok"]]
    verdict = ("unrealistic" if any(c["level"] == "fail" for c in failed)
               else "ambitious" if failed else "feasible")
    return {"verdict": verdict, "checks": checks, "corrected_goal_s": corrected,
            "corrected_goal": hms(corrected) if corrected else None, "missing": missing}


def hms(seconds: int) -> str:
    return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


# --- the answer -----------------------------------------------------------------------------------------------

def step_json(st: Step, paces: Paces) -> Dict[str, Any]:
    if st.kind == "repeat":
        return {"type": "repeat", "times": st.times, "steps": [step_json(c, paces) for c in st.children]}
    out: Dict[str, Any] = {"type": st.kind}
    if st.end == "distance":
        out["distance_m"] = int(round(st.value))
    else:
        out["time_s"] = int(round(st.value))
    band = paces.band(st.band)
    if band is not None:
        out["speed_mps"] = [band[0], band[1]]
        out["pace"] = band_text(band)
    elif st.band:
        out["target"] = st.band
    return out


def structure_text(s: Session, paces: Paces) -> str:
    def one(st: Step) -> str:
        if st.kind == "repeat":
            return f"{st.times} x (" + ", ".join(one(c) for c in st.children) + ")"
        size = f"{km(st.value)} km" if st.end == "distance" and st.value >= 1000 else (
            f"{int(st.value)} m" if st.end == "distance" else f"{int(st.value)} s")
        what = {"warmup": "warm-up", "cooldown": "cool-down", "recovery": "jog"}.get(st.kind, st.band or "run")
        pace = band_text(paces.band(st.band))
        return f"{size} {what}" + (f" at {pace}/km" if pace and st.kind != "recovery" else "")
    return ", ".join(one(st) for st in s.steps)


def session_json(s: Session, paces: Paces) -> Dict[str, Any]:
    secs = session_seconds(s, paces)
    main = next((st for st in _leaves(s.steps) if st.kind == "interval"), None)
    return {"key": s.key, "date": s.date.isoformat(), "weekday": WEEKDAYS[s.date.weekday()], "role": s.role,
            "km": km(s.distance), "minutes": int(round(secs / 60)) if secs is not None and s.role != "b_race" else None,
            "pace": band_text(paces.band(main.band)) if main is not None else None,
            "hard_or_long": s.hard_or_long, "structure": structure_text(s, paces) if s.steps else s.note,
            "steps": [step_json(st, paces) for st in s.steps]}


def week_json(w: Week, paces: Paces) -> Dict[str, Any]:
    longs = [s.distance for s in w.sessions if s.role == "long"]
    return {"week": w.number, "start": w.start.isoformat(), "end": w.end.isoformat(), "phase": w.phase,
            "kind": w.kind, "target_km": km(w.target),
            "planned_km": km(sum(s.distance for s in w.sessions if s.counts)),
            "long_km": km(max(longs)) if longs else None,
            "sessions": [session_json(s, paces) for s in w.sessions]}


def content_hash(rules_version: int, inputs: Mapping[str, Any], weeks: Any) -> str:
    text = json.dumps({"rules_version": rules_version, "inputs": inputs, "weeks": weeks}, sort_keys=True,
                      separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(text.encode("ascii")).hexdigest()


def summary(weeks: Sequence[Week]) -> Dict[str, Optional[float]]:
    real = [w for w in weeks if w.kind != "week0"]
    longs = [s.distance for w in real for s in w.sessions if s.role == "long"]
    return {"peak_km": km(max((w.target for w in real), default=0.0)),
            "peak_long_km": km(max(longs)) if longs else None, "weeks": float(len(real))}


def build_plan(inputs: PlanInputs, snap: Snapshot, rules: Mapping[str, Any] = RULES, *,
               what_if_runs: bool = True) -> Dict[str, Any]:
    """The plan for `inputs` on `snap` as a JSON-ready dict. Raises PlanError for inputs it cannot plan from.
    The "_check" key holds the objects check_invariants() reads; plan_answer() drops it."""
    weeks, ctx = _build_core(inputs, snap, rules)
    applied, refused = apply_overrides(weeks, ctx)
    paces = ctx.paces
    if inputs.easy_band_slowdown:
        # Stage 4 (plan_review A4): the easy and long runs keep their planned distances, only their pace band
        # (and so their minutes) is slower in the answer and the workouts; every rule above ran on the plan's bands.
        paces = Paces(ctx.paces.lt, ctx.paces.mp, rules, inputs.easy_band_slowdown)
    week_list = [week_json(w, paces) for w in weeks]
    echo = inputs.echo()
    summ = summary(weeks)
    real = [w for w in weeks if w.kind != "week0"]
    feas = feasibility(inputs, snap, len(real), summ["peak_km"], summ["peak_long_km"], km(ctx.km0), rules)
    acwr = acwr_answer(weeks, ctx)
    pacing = race_pacing(inputs, snap, rules, ctx.warnings)
    if ctx.load is not None and not ctx.load.lpm:
        ctx.missing.append("training_load")
    elif ctx.load is not None and not ctx.load.checks:
        ctx.missing.append("garmin_load")
    plan: Dict[str, Any] = {
        "kind": "training_plan", "rules_version": RULES_VERSION,
        "race": {"date": inputs.race_date.isoformat(), "distance": inputs.race_distance,
                 "goal_time": hms(inputs.goal_time_s) if inputs.goal_time_s else None,
                 "marathon_pace": pace_text(paces.mp) if paces.mp else None},
        "b_races": [{"date": w.race.date.isoformat(), "kind": w.race.kind,
                     "mini_taper": [w.mini[0].isoformat(), w.mini[-1].isoformat()],
                     "recovery": [w.recovery[0].isoformat(), w.recovery[-1].isoformat()]} for w in ctx.windows],
        "start": {"km0": km(ctx.km0), "longest_run_8w_km": km(ctx.longest) if snap.longest_run_8w_m else None,
                  "lt_pace": pace_text(snap.lt_speed_mps) if snap.lt_speed_mps else None},
        "summary": summ,
        "weeks": week_list,
        "warnings": _dedupe(ctx.warnings),
        "feasibility": feas,
        "missing": sorted(set(ctx.missing)),
        "overrides": {"applied": applied, "refused": refused},
        "acwr": acwr,
        "inputs": echo,
        "content_hash": content_hash(RULES_VERSION, echo, week_list),
        "_check": {"weeks": weeks, "ctx": ctx},
    }
    if pacing is not None:
        plan["race_pacing"] = pacing
    inv = check_invariants(plan, rules)
    plan["invariants"] = {"ok": all(i["ok"] for i in inv), "failed": [i for i in inv if not i["ok"]],
                          "checked": [i["id"] for i in inv]}
    if what_if_runs:
        plan["what_if"] = what_if(inputs, snap, plan, rules)
    return plan


def _dedupe(warnings: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for w in warnings:
        if w not in out:
            out.append(w)
    return out


def plan_answer(plan: Mapping[str, Any]) -> Dict[str, Any]:
    """The plan without its internal "_check" objects (deep-copied, JSON-ready)."""
    return {k: copy.deepcopy(v) for k, v in plan.items() if k != "_check"}


# --- what-if (Engine v1 item 10) ------------------------------------------------------------------------------

def what_if(inputs: PlanInputs, snap: Snapshot, base: Mapping[str, Any],
            rules: Mapping[str, Any] = RULES) -> List[Dict[str, Any]]:
    variants: List[Tuple[str, PlanInputs]] = []
    delta = int(rules["what_if"]["goal_delta_s"])
    if inputs.goal_time_s:
        for sign, name in ((-1, "goal_minus_5min"), (1, "goal_plus_5min")):
            g = inputs.goal_time_s + sign * delta
            if 3600 <= g <= 8 * 3600:
                variants.append((name, dataclasses.replace(inputs, goal_time_s=g)))
    for share in rules["what_if"]["long_shares"]:
        variants.append((f"long_share_{share:.2f}", dataclasses.replace(inputs, long_share=float(share))))
    base_ids = {w["id"] for w in base["warnings"]}
    out = []
    for name, var in variants:
        other = build_plan(var, snap, rules, what_if_runs=False)
        ids = {w["id"] for w in other["warnings"]}
        bs, os_ = base["summary"], other["summary"]
        out.append({
            "variant": name,
            "goal_time": hms(var.goal_time_s) if var.goal_time_s else None,
            "long_share": var.long_share,
            "peak_km": os_["peak_km"], "peak_long_km": os_["peak_long_km"],
            "delta_peak_km": round((os_["peak_km"] or 0) - (bs["peak_km"] or 0), 1),
            "delta_peak_long_km": round((os_["peak_long_km"] or 0) - (bs["peak_long_km"] or 0), 1),
            "verdict": other["feasibility"]["verdict"],
            "warnings_added": sorted(ids - base_ids), "warnings_removed": sorted(base_ids - ids),
            "invariants_ok": other["invariants"]["ok"],
        })
    return out


# --- assess (plan_assess) --------------------------------------------------------------------------------------

def assess(inputs: PlanInputs, snap: Snapshot, rules: Mapping[str, Any] = RULES) -> Dict[str, Any]:
    """plan_assess' numbers: the history, the start values and the feasibility of the goal (with the peak
    and longest run the engine would plan), and what is missing."""
    check_inputs(inputs)
    weeks_m = list(snap.week_m)
    recent = weeks_m[-int(rules["km0_weeks"]):]
    km0 = km(sum(recent) / len(recent)) if recent and sum(recent) > 0 else None
    peak = longest = None
    n: Optional[int] = None
    missing: List[str] = []
    try:
        plan = build_plan(inputs, snap, rules, what_if_runs=False)
        peak, longest = plan["summary"]["peak_km"], plan["summary"]["peak_long_km"]
        n = int(plan["summary"]["weeks"] or 0)
        missing = list(plan["missing"])
    except PlanError as exc:
        missing.append(f"plan: {exc}")
        first = week1_start(snap.today)
        if inputs.race_date > first:
            n = (monday(inputs.race_date) - first).days // 7 + 1
    feas = feasibility(inputs, snap, n, peak, longest, km0, rules)
    pred = snap.prediction(inputs.race_distance)
    return {
        "kind": "plan_assessment", "rules_version": RULES_VERSION,
        "race": {"date": inputs.race_date.isoformat(), "distance": inputs.race_distance,
                 "goal_time": hms(inputs.goal_time_s) if inputs.goal_time_s else None},
        "weeks_to_race": n,
        "history": [{"km": km(m), "runs": r} for m, r in zip(weeks_m, snap.week_runs)],
        "avg_km_4w": km0,
        "longest_run_8w_km": km(snap.longest_run_8w_m) if snap.longest_run_8w_m else None,
        "lt_speed_mps": round(snap.lt_speed_mps, 3) if snap.lt_speed_mps else None,
        "lt_pace": pace_text(snap.lt_speed_mps) if snap.lt_speed_mps else None,
        "vo2max": snap.vo2max,
        "prediction": hms(pred) if pred else None,
        "prediction_s": pred,
        "acwr": snap.acwr,
        "projected_peak_km": peak, "projected_long_km": longest,
        "feasibility": feas,
        "missing": sorted(set(missing + feas["missing"])),
    }


# --- selfcheck -------------------------------------------------------------------------------------------------

def sample_loads(today: dt.date, week_m: Sequence[float], *, garmin_gap: float = 0.03, run_lpm: float = 1.6,
                 bike_min: float = 90.0, swim_min: float = 45.0,
                 rules: Mapping[str, Any] = RULES) -> Tuple[Tuple[ActivityLoad, ...], Tuple[GarminLoad, ...]]:
    """Representative recorded loads for the full weeks before today's (oldest first in week_m): runs on
    Tue, Wed, Thu, Sat and a long run on Sunday (35%) at 3.0 m/s, a ride on Monday and a swim on Friday; and
    Garmin's acute/chronic on the check days, the model's ratio raised by garmin_gap. Test and selfcheck data."""
    acts: List[ActivityLoad] = []
    first = monday(today) - dt.timedelta(days=7 * len(week_m))
    for i, m in enumerate(week_m):
        start = first + dt.timedelta(days=7 * i)
        shares = ((1, 0.1625), (2, 0.1625), (3, 0.1625), (5, 0.1625), (6, 0.35))
        for offset, share in shares:
            meters = Meters(float(m) * share)
            secs = Seconds(meters / 3.0)
            acts.append(ActivityLoad(start + dt.timedelta(days=offset), "run", secs, round(secs / 60 * run_lpm, 1),
                                     meters))
        if bike_min:
            acts.append(ActivityLoad(start, "bike", Seconds(bike_min * 60), round(bike_min * 1.1, 1)))
        if swim_min:
            acts.append(ActivityLoad(start + dt.timedelta(days=4), "swim", Seconds(swim_min * 60),
                                     round(swim_min * 0.9, 1)))
    daily: Dict[dt.date, float] = {}
    for a in acts:
        daily[a.date] = daily.get(a.date, 0.0) + a.load
    garmin = []
    for back in rules["acwr"]["check_days_ago"]:
        day = today - dt.timedelta(days=int(back))
        acute = sum(daily.get(day - dt.timedelta(days=k), 0.0) for k in range(7))
        chronic = sum(daily.get(day - dt.timedelta(days=k), 0.0) for k in range(28)) / 4
        if chronic > 0:
            garmin.append(GarminLoad(day, round(acute + garmin_gap * chronic, 1), round(chronic, 1)))
    return tuple(acts), tuple(garmin)


def sample_snapshot(today: dt.date = dt.date(2026, 11, 25)) -> Snapshot:
    weeks = tuple(Meters(m) for m in (36000.0, 41000.0, 38000.0, 44000.0, 39000.0, 42000.0, 37000.0, 42000.0))
    acts, garmin = sample_loads(today, weeks)
    return Snapshot(today=today, week_m=weeks, week_runs=(4, 5, 4, 5, 4, 5, 4, 5),
                    longest_run_8w_m=Meters(20900.0), lt_speed_mps=MetersPerSecond(3.85),
                    prediction_s=(("marathon", 3 * 3600 + 35 * 60), ("half", 3600 + 42 * 60)),
                    activity_load=acts, garmin_load=garmin)


def selfcheck() -> None:
    """A sample plan (marathon with a B race) builds, is deterministic and passes every invariant."""
    inputs = PlanInputs(race_date=dt.date(2027, 2, 26), goal_time_s=3 * 3600 + 30 * 60,
                        b_races=(BRace(dt.date(2027, 1, 29), "tri_half"),))
    a = build_plan(inputs, sample_snapshot(), what_if_runs=False)
    b = build_plan(inputs, sample_snapshot(), what_if_runs=False)
    if a["content_hash"] != b["content_hash"]:
        raise RuntimeError("plan_engine: the same inputs gave two plans")
    if not a["invariants"]["ok"]:
        raise RuntimeError("plan_engine: the sample plan fails " +
                           ", ".join(i["id"] for i in a["invariants"]["failed"]))
