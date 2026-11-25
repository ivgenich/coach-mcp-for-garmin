"""Deep test phase: plan_engine over a scenario matrix, checked by its own invariants and by an independent
oracle of the documented rules (docs/training-plan.md, "Plan rules v1" and "v2").

    cd ext && python3 -m unittest test_deep_engine_matrix -v

Standard library only. The oracle (check_plan) reads only build_plan's JSON answer and the inputs, never the
engine's bookkeeping, so a rule the engine and its own check_invariants() both get wrong still shows up here.
test_deep_engine_properties imports it for the randomized runs.
"""

from __future__ import annotations

import collections
import dataclasses
import datetime as dt
import unittest
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import plan_engine as pe

D = dt.date
R = pe.RULES
TODAY = D(2026, 11, 25)   # a Wednesday
EPS_KM = 0.05


# --- profiles ---------------------------------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class Profile:
    weeks_km: Tuple[float, ...]
    longest_km: Optional[float]
    lt: Optional[float]
    note: str


PROFILES: Dict[str, Profile] = {
    "beginner15": Profile((14, 16, 12, 15, 17, 14, 15, 16), 7.0, 3.0, "a 15 km a week beginner"),
    "owner40": Profile((36, 41, 38, 44, 39, 42, 37, 42), 20.9, 3.85,
                       "the owner's provisional numbers: about 40 km, longest 20.9 km, LT 3.85 m/s"),
    "runner60": Profile((58, 62, 55, 60, 64, 59, 61, 60), 26.0, 4.2, "60 km a week"),
    "runner90": Profile((88, 92, 95, 90, 85, 93, 96, 91), 30.0, 4.6, "90 km a week: the peak ceiling binds"),
    "runner120": Profile((115, 125, 120, 118, 122, 130, 119, 121), 32.0, 5.0, "120 km a week"),
    "very_slow": Profile((25, 28, 22, 26, 27, 24, 25, 26), 14.0, 2.4, "LT 2.4 m/s: bands under the speed limit"),
    "no_lt": Profile((40, 42, 38, 41, 43, 39, 40, 42), 20.0, None, "no lactate-threshold speed"),
    "no_longest": Profile((40, 42, 38, 41, 43, 39, 40, 42), None, 3.8, "no longest run of 8 weeks"),
    "two_weeks": Profile((40, 42), 18.0, 3.8, "only two weeks of history"),
    "one_week": Profile((30,), 12.0, 3.5, "a single week of history"),
    "gaps": Profile((40, 0, 0, 42, 0, 38, 0, 41), 18.0, 3.8, "weeks without a run in the history"),
    "outlier": Profile((40, 42, 160, 41, 39, 43, 40, 42), 20.0, 3.8, "one huge week in the history"),
}


def snapshot(today: dt.date, prof: Profile, *, loads: Optional[Dict[str, Any]] = None,
             fixed: Sequence[pe.FixedSession] = (), predictions: Optional[Sequence[Tuple[str, int]]] = None
             ) -> pe.Snapshot:
    """loads: None (no recorded training load) or sample_loads() keyword arguments ({} tracks Garmin)."""
    week_m = [k * 1000.0 for k in prof.weeks_km]
    acts, garmin = pe.sample_loads(today, week_m, **loads) if loads is not None else ((), ())
    preds = (("marathon", 3 * 3600 + 40 * 60), ("half", 3600 + 45 * 60)) if predictions is None else predictions
    return pe.Snapshot(today=today, week_m=tuple(pe.Meters(m) for m in week_m), week_runs=(4,) * len(week_m),
                       longest_run_8w_m=pe.Meters(prof.longest_km * 1000.0) if prof.longest_km else None,
                       lt_speed_mps=pe.MetersPerSecond(prof.lt) if prof.lt else None,
                       prediction_s=tuple(preds), fixed=tuple(fixed), activity_load=acts, garmin_load=garmin)


def race_in(today: dt.date, weeks: int, weekday: int) -> dt.date:
    """The race day `weekday` of plan week `weeks` (week 1 starts next Monday, today when a Monday)."""
    return pe.week1_start(today) + dt.timedelta(days=7 * (weeks - 1) + weekday)


# --- the oracle -------------------------------------------------------------------------------------------------

def _leaves(steps: Iterable[Dict[str, Any]]) -> Iterable[Dict[str, Any]]:
    for st in steps:
        if st["type"] == "repeat":
            yield from _leaves(st["steps"])
        else:
            yield st


def _day(text: str) -> dt.date:
    return D.fromisoformat(text)


def check_plan(inputs: pe.PlanInputs, snap: pe.Snapshot, plan: Dict[str, Any]) -> List[str]:
    """Every documented rule an answer can be checked against, from the answer alone. [problem strings]."""
    a = pe.plan_answer(plan)
    out: List[str] = []
    today, race = snap.today, inputs.race_date
    weeks = a["weeks"]
    real = [w for w in weeks if w["kind"] != "week0"]
    first = pe.week1_start(today)

    # rule 1: weeks Monday to Sunday from next Monday; the last is the race week; phases from the end
    for i, w in enumerate(real):
        start = _day(w["start"])
        if start != first + dt.timedelta(days=7 * i) or _day(w["end"]) != start + dt.timedelta(days=6):
            out.append(f"week {w['week']}: starts {w['start']}")
        if w["week"] != i + 1:
            out.append(f"week numbering at {w['start']}")
    if not real or not _day(real[-1]["start"]) <= race <= _day(real[-1]["end"]):
        out.append("the race is not in the last week")
    n = len(real)
    taper = 2 if n >= 10 else 1
    peak = 2 if n >= 11 else (1 if n >= 8 else 0)
    rest = max(0, n - taper - peak)
    base = int(rest * 4 / 9 + 0.5)
    want = ["base"] * base + ["build"] * (rest - base) + ["peak"] * peak + ["taper"] * (taper - 1) + ["race"]
    for w, ph in zip(real, want):
        expect_kind = {"taper": ("taper",), "race": ("race",)}.get(ph, ("normal", "deload", "b_race"))
        if w["kind"] not in expect_kind:
            out.append(f"week {w['week']}: kind {w['kind']} in phase {ph}")
        if w["phase"] != ph and not (w["kind"] == "b_race" and w["phase"] == "b_race"):
            out.append(f"week {w['week']}: phase {w['phase']}, rules say {ph}")
    weeks0 = [w for w in weeks if w["kind"] == "week0"]
    if (today.weekday() in (0, 6)) != (not weeks0):
        out.append("week 0 present on a Monday or Sunday, or missing midweek")

    fixed: Dict[dt.date, List[pe.FixedSession]] = collections.defaultdict(list)
    for f in snap.fixed:
        fixed[f.date].append(f)
    b_days = {_day(b["date"]) for b in a["b_races"]}
    minis = [(_day(b["mini_taper"][0]), _day(b["mini_taper"][1])) for b in a["b_races"]]
    recs = [(_day(b["recovery"][0]), _day(b["recovery"][1])) for b in a["b_races"]]
    lt = snap.lt_speed_mps
    easy_cap = 14.0
    if lt and lt * 0.70 >= 1.5 and lt * 0.80 <= 7.0:
        easy_cap = min(easy_cap, int(lt * 0.75 * 74 * 60 / 100) / 10.0)   # 74 minutes at mid easy, 100 m steps

    hard: Dict[dt.date, str] = {}
    runs = collections.Counter()
    races = []
    for w in weeks:
        for s in w["sessions"]:
            d = _day(s["date"])
            if not _day(w["start"]) <= d <= _day(w["end"]):
                out.append(f"{s['key']}: outside its week")
            if s["role"] == "race":
                races.append(d)
                hard[d] = "race"
                continue
            if s["role"] == "b_race":
                if d not in b_days:
                    out.append(f"{s['key']}: a B race session on {d} that is no planned B race")
                hard[d] = "b_race"
                continue
            runs[d] += 1
            if d <= today:
                out.append(f"{s['key']}: on or before today")
            if d >= race:
                out.append(f"{s['key']}: on or after the A race ({s['date']})")
            if d.weekday() in inputs.rest_days:
                out.append(f"{s['key']}: on a rest day")
            if d in inputs.blocked_dates:
                out.append(f"{s['key']}: on a blocked date")
            if d in b_days:
                out.append(f"{s['key']}: on a B race day")
            if any(f.sport == "run" for f in fixed.get(d, ())):
                out.append(f"{s['key']}: on a day with a fixed run")
            if s["hard_or_long"] != (s["role"] in pe.HARD_ROLES):
                out.append(f"{s['key']}: hard_or_long {s['hard_or_long']} for {s['role']}")
            if s["hard_or_long"]:
                hard[d] = "plan"
            if w["kind"] == "week0" and s["role"] != "easy":
                out.append(f"{s['key']}: {s['role']} in week 0")
            if s["role"] == "strides":
                reps = [st for st in s["steps"] if st["type"] == "repeat"]
                if len(reps) != 1 or reps[0]["times"] > 8 or reps[0]["steps"][0]["distance_m"] > 100:
                    out.append(f"{s['key']}: strides beyond 8 x 100 m")
            if s["role"] == "long":
                if s["km"] > min(32.0, inputs.max_long_km) + EPS_KM:
                    out.append(f"{s['key']}: long {s['km']} km over max_long_km / 32 km")
                if lt and s["km"] > 10800 * lt * 0.70 / 1000.0 + EPS_KM:
                    out.append(f"{s['key']}: long {s['km']} km longer than 3 h at the slow edge")
            if s["role"] in pe.QUALITY_ROLES and (race - d).days < 3:
                out.append(f"{s['key']}: quality {(race - d).days} days before the race")
            if s["role"] == "easy" and w["kind"] != "week0" and s["km"] > easy_cap + EPS_KM:
                out.append(f"{s['key']}: easy {s['km']} km over the easy cap {easy_cap}")
            in_mini = any(lo <= d <= hi for lo, hi in minis)
            in_rec = any(lo <= d <= hi for lo, hi in recs)
            if in_mini and s["role"] not in ("easy", "strides"):
                out.append(f"{s['key']}: {s['role']} in a B mini-taper")
            if in_rec and not in_mini and s["role"] != "easy":
                out.append(f"{s['key']}: {s['role']} in a B recovery")
            for st in _leaves(s["steps"]):
                if "speed_mps" in st:
                    lo, hi = st["speed_mps"]
                    if not 1.5 <= lo <= hi <= 7.0:
                        out.append(f"{s['key']}: band {lo}-{hi} outside the speed limits")
                    if s["role"] in ("easy", "long") and lt:
                        f = 1 - inputs.easy_band_slowdown
                        if abs(lo - round(lt * 0.70 * f, 3)) > 0.002 or abs(hi - round(lt * 0.80 * f, 3)) > 0.002:
                            out.append(f"{s['key']}: easy band {lo}-{hi}")
    if races != [race]:
        out.append(f"race sessions on {races}")
    for d, k in runs.items():
        if k > 1:
            out.append(f"{d}: {k} runs")
    # rule 6: no two hard-or-long days touch (the plan's against each other, the races and fixed hard sessions)
    for d, who in hard.items():
        for nb in (d - dt.timedelta(days=1), d + dt.timedelta(days=1)):
            other = hard.get(nb)
            fixed_hard = any(f.hard or f.long for f in fixed.get(nb, ()))
            if who == "plan" and (other is not None or fixed_hard):
                out.append(f"hard days touch: {d} and {nb}")
    # rule 2: the peak ceiling, planned within target
    hist = list(snap.week_m)
    km0 = sum(hist[-4:]) / len(hist[-4:])
    ceiling = max(1.3 * max(hist[-8:]), km0 + 25000.0) / 1000.0
    if abs(a["start"]["km0"] - round(km0 / 1000.0, 1)) > 0.051:
        out.append(f"km0 {a['start']['km0']}, mean of the last 4 weeks {km0 / 1000.0:.1f}")
    for w in real:
        if w["target_km"] > ceiling + EPS_KM or w["planned_km"] > ceiling + EPS_KM:
            out.append(f"week {w['week']}: over the peak ceiling {ceiling:.1f}")
        if w["planned_km"] > w["target_km"] + 0.15:
            out.append(f"week {w['week']}: planned {w['planned_km']} km > target {w['target_km']} km")
    # rule 2: deload cadence (3 normal weeks, 4 when a B week follows)
    run = 0
    for i, w in enumerate(real):
        run = run + 1 if w["kind"] == "normal" else 0
        nxt = real[i + 1]["kind"] if i + 1 < len(real) else None
        if run > 3 + (1 if nxt == "b_race" else 0):
            out.append(f"week {w['week']}: {run} normal weeks in a row")
    # rule 3: within 21 days, no long run longer than 0.80 x the peak long run of a normal week before them
    early = [s["km"] for w in real if w["kind"] == "normal" for s in w["sessions"]
             if s["role"] == "long" and (race - _day(s["date"])).days >= 21]
    # (before any such long run: 0.80 x the longest so far, the 8 weeks' longest run or a long run before it)
    all_longs = [(_day(s["date"]), s["km"]) for w in real for s in w["sessions"] if s["role"] == "long"]
    for w in real:
        for s in w["sessions"]:
            if s["role"] != "long" or (race - _day(s["date"])).days >= 21:
                continue
            top = max(early) if early else max([a["start"]["longest_run_8w_km"] or 0.0]
                                               + [k for d, k in all_longs if d < _day(s["date"])])
            if top and s["km"] > 0.8 * top + 0.1:
                out.append(f"{s['key']}: late long {s['km']} km > 0.8 x {top} km")
    # rule 7: B race week runs <= 8 km; the long run 5-13 days before a B race and the first one after it
    # <= 0.70 x the peak long run so far (0.60 after a full-distance triathlon, which also keeps it >= 10 days
    # out). The reference is the plan as built, so only checked here without overrides (one that shortens an
    # earlier long run leaves the engine's limits where they were).
    for w in weeks:
        for s in w["sessions"]:
            if s["role"] not in ("race", "b_race") and any(lo <= _day(s["date"]) <= hi for lo, hi in minis) \
                    and s["km"] > 8.0 + EPS_KM:
                out.append(f"{s['key']}: {s['km']} km in a B race week")
    if not inputs.overrides:
        peaks = [(d, k) for w in real if w["kind"] == "normal" for s in w["sessions"] if s["role"] == "long"
                 for d, k in [(_day(s["date"]), s["km"])] if (race - d).days >= 21]

        def top_before(day: dt.date) -> float:
            p = [k for d, k in peaks if d < day]
            return max(p) if p else max([a["start"]["longest_run_8w_km"] or 0.0]
                                        + [k for d, k in all_longs if d < day])
        for b in a["b_races"]:
            bd = _day(b["date"])
            after = sorted((d, k) for d, k in all_longs if d > bd)
            for d, k in all_longs:
                if 5 <= (bd - d).days <= 13 and top_before(d) and k > 0.7 * top_before(d) + 0.1:
                    out.append(f"{d}: long {k} km {(bd - d).days} days before a B race")
            if after:
                d, k = after[0]
                f = 0.6 if b["kind"] == "tri_full" else 0.7
                if top_before(d) and k > f * top_before(d) + 0.1:   # no reference (as rule 3): no limit
                    out.append(f"{d}: first long {k} km after a B race")
                if b["kind"] == "tri_full" and (d - bd).days < 10:
                    out.append(f"{d}: a long run {(d - bd).days} days after a full-distance triathlon")
    # rule 4: at least 80% of each week's time easy (by distance without an LT speed)
    for w in real:
        hard_t = total_t = 0.0
        for s in w["sessions"]:
            if s["role"] in ("race", "b_race"):
                continue
            h, t = _times(s, lt is not None)
            hard_t += h
            total_t += t
        if total_t > 0 and hard_t / total_t > 0.2 + 1e-6:
            out.append(f"week {w['week']}: {round(100 * (1 - hard_t / total_t), 1)}% easy")
    # rule 5: mp band clamped to 0.85-0.96 of LT
    if lt and a["race"]["marathon_pace"]:
        m, s_ = a["race"]["marathon_pace"].split(":")
        speed = 1000.0 / (int(m) * 60 + int(s_))
        if not 0.85 * lt - 0.02 <= speed <= 0.96 * lt + 0.02:
            out.append(f"marathon pace {a['race']['marathon_pace']} outside 0.85-0.96 of LT")
    return out


def _times(s: Dict[str, Any], by_time: bool) -> Tuple[float, float]:
    quality = s["role"] in pe.QUALITY_ROLES

    def walk(steps: Iterable[Dict[str, Any]], times: int) -> Tuple[float, float]:
        h = t = 0.0
        for st in steps:
            if st["type"] == "repeat":
                hh, tt = walk(st["steps"], times * st["times"])
            else:
                if "time_s" in st:   # a timed jog: by time, else its distance at the engine's 2.5 m/s jog
                    tt = float(st["time_s"]) * (1.0 if by_time else float(pe.FALLBACK_JOG_MPS))
                else:
                    dist = float(st["distance_m"])
                    sp = st.get("speed_mps")
                    tt = dist / ((sp[0] + sp[1]) / 2) if by_time and sp else dist
                hh = tt if quality and st["type"] == "interval" else 0.0
            h += hh * times
            t += tt * times
        return h, t
    return walk(s["steps"], 1)


def build(inputs: pe.PlanInputs, snap: pe.Snapshot, what_if: bool = False) -> Dict[str, Any]:
    return pe.build_plan(inputs, snap, what_if_runs=what_if)


class Oracle(unittest.TestCase):
    """Shared assertions: every invariant passes and the oracle finds nothing."""

    def assertSound(self, inputs: pe.PlanInputs, snap: pe.Snapshot, plan: Optional[Dict[str, Any]] = None,
                    msg: str = "") -> Dict[str, Any]:
        plan = build(inputs, snap) if plan is None else plan
        self.assertTrue(plan["invariants"]["ok"], f"{msg} {plan['invariants']['failed']}")
        self.assertEqual(plan["invariants"]["checked"], [
            "growth_cap", "no_makeup", "deload_cadence", "deload_depth", "long_cap", "long_step",
            "peak_long_timing", "easy_share", "peak_ceiling", "no_adjacent_hard", "bands_match_roles",
            "b_windows", "a_taper", "last_quality", "rest_days", "one_run_per_day", "acwr_cap"])
        self.assertEqual(check_plan(inputs, snap, plan), [], msg)
        return plan


# --- the matrix --------------------------------------------------------------------------------------------------

class ProfileMatrix(Oracle):
    """Every profile x distance x weeks to race (1..24) x today's weekday, with and without recorded loads."""

    def test_profiles_by_weeks_and_weekday(self):
        n = 0
        for name, prof in PROFILES.items():
            for dist in ("marathon", "half"):
                for weeks in (1, 2, 3, 5, 7, 8, 9, 10, 11, 12, 16, 20, 24):
                    for wd in range(7):
                        today = D(2026, 11, 23) + dt.timedelta(days=wd)
                        # the race on a weekday that turns with the case: every race weekday is met
                        race = race_in(today, weeks, (weeks + wd) % 7)
                        if race <= today:
                            continue
                        loads = {} if (weeks + wd) % 3 == 0 else None
                        snap = snapshot(today, prof, loads=loads)
                        inputs = pe.PlanInputs(race_date=race, race_distance=dist,
                                               goal_time_s=None if wd % 2 else (12600 if dist == "marathon" else 6000))
                        with self.subTest(profile=name, dist=dist, weeks=weeks, today=today.isoformat()):
                            self.assertSound(inputs, snap)
                            n += 1
        self.assertGreater(n, 2000)

    EVERY_DAY = dict(run_days=tuple(range(7)), rest_days=())

    def test_peak_ceiling_binds_for_a_big_runner(self):
        snap = snapshot(TODAY, PROFILES["runner90"])
        plan = self.assertSound(pe.PlanInputs(race_date=race_in(TODAY, 20, 6), **self.EVERY_DAY), snap)
        hist = list(snap.week_m)
        ceiling = max(1.3 * max(hist), sum(hist[-4:]) / 4 + 25000) / 1000
        self.assertIn("peak_capped", [w["id"] for w in plan["warnings"]])
        self.assertAlmostEqual(max(w["target_km"] for w in plan["weeks"]), round(ceiling, 1), delta=0.1)

    def test_a_huge_outlier_week_lifts_only_the_ceiling(self):
        prof = PROFILES["outlier"]
        snap = snapshot(TODAY, prof)
        plan = self.assertSound(pe.PlanInputs(race_date=race_in(TODAY, 16, 6), **self.EVERY_DAY), snap)
        self.assertEqual(plan["start"]["km0"], round(sum(prof.weeks_km[-4:]) / 4, 1))
        self.assertNotIn("peak_capped", [w["id"] for w in plan["warnings"]])
        # the same outlier nine weeks back is out of the 8-week window: the ceiling falls back
        self.assertEqual(pe.peak_ceiling(snap, pe.Meters(41500.0), R)[1], 160000.0)
        older = dataclasses.replace(prof, weeks_km=(160,) + PROFILES["runner90"].weeks_km)
        ceiling, biggest = pe.peak_ceiling(snapshot(TODAY, older), pe.Meters(91250.0), R)
        self.assertEqual(biggest, 96000.0)
        self.assertAlmostEqual(ceiling, 1.3 * 96000.0)
        self.assertSound(pe.PlanInputs(race_date=race_in(TODAY, 20, 6), **self.EVERY_DAY), snapshot(TODAY, older))

    def test_history_edges(self):
        race = race_in(TODAY, 12, 6)
        short = build(pe.PlanInputs(race_date=race), snapshot(TODAY, PROFILES["two_weeks"]))
        self.assertIn("history_weeks", short["missing"])
        self.assertEqual(short["start"]["km0"], 41.0)
        full = build(pe.PlanInputs(race_date=race), snapshot(TODAY, PROFILES["owner40"]))
        self.assertNotIn("history_weeks", full["missing"])
        gaps = build(pe.PlanInputs(race_date=race), snapshot(TODAY, PROFILES["gaps"]))
        self.assertEqual(gaps["start"]["km0"], round((42 + 0 + 38 + 0 + 41) / 4 - 42 / 4, 1))   # the last four
        for empty in ((), (0, 0, 0, 0), (40, 0, 0, 0, 0)):
            with self.subTest(history=empty), self.assertRaises(pe.PlanError) as cm:
                build(pe.PlanInputs(race_date=race),
                      snapshot(TODAY, dataclasses.replace(PROFILES["owner40"], weeks_km=empty)))
            self.assertIn("no starting volume", str(cm.exception))
        nolong = build(pe.PlanInputs(race_date=race), snapshot(TODAY, PROFILES["no_longest"]))
        self.assertIn("longest_run_8w", nolong["missing"])
        self.assertIsNone(nolong["start"]["longest_run_8w_km"])

    def test_no_lt_speed(self):
        plan = self.assertSound(pe.PlanInputs(race_date=race_in(TODAY, 14, 6), goal_time_s=12600),
                                snapshot(TODAY, PROFILES["no_lt"]))
        self.assertTrue({"lt_speed", "pace_targets", "long_run_time_cap"} <= set(plan["missing"]))
        for w in plan["weeks"]:
            for s in w["sessions"]:
                for st in _leaves(s["steps"]):
                    self.assertNotIn("speed_mps", st, s["key"])
        self.assertIsNone(plan["race"]["marathon_pace"])

    def test_very_slow_runner_keeps_the_time_cap(self):
        prof = PROFILES["very_slow"]
        plan = self.assertSound(pe.PlanInputs(race_date=race_in(TODAY, 18, 6)), snapshot(TODAY, prof))
        cap = 10800 * prof.lt * 0.70 / 1000   # 18.1 km
        longs = [s["km"] for w in plan["weeks"] for s in w["sessions"] if s["role"] == "long"]
        self.assertTrue(longs)
        self.assertLessEqual(max(longs), cap + EPS_KM)
        # 0.70 x 2.4 = 1.68 m/s is inside the limits, the steps carry bands
        self.assertTrue(any("speed_mps" in st for w in plan["weeks"] for s in w["sessions"]
                            for st in _leaves(s["steps"])))

    def test_a_runner_under_the_speed_limit_loses_the_easy_band_only(self):
        prof = dataclasses.replace(PROFILES["very_slow"], lt=2.0)   # 0.70 x 2.0 = 1.4 m/s < 1.5
        plan = self.assertSound(pe.PlanInputs(race_date=race_in(TODAY, 18, 6)), snapshot(TODAY, prof))
        easy = [s for w in plan["weeks"] for s in w["sessions"] if s["role"] == "easy"]
        self.assertTrue(easy)
        self.assertTrue(all("speed_mps" not in s["steps"][0] for s in easy))
        longs = [s["km"] for w in plan["weeks"] for s in w["sessions"] if s["role"] == "long"]
        self.assertLessEqual(max(longs), 10800 * 2.0 * 0.70 / 1000 + EPS_KM)


class RaceTiming(Oracle):
    def test_race_this_week_or_today_is_refused(self):
        snap = snapshot(TODAY, PROFILES["owner40"])
        for race in (TODAY, TODAY - dt.timedelta(days=1), D(2026, 11, 28), D(2026, 11, 29)):
            with self.subTest(race=race), self.assertRaises(pe.PlanError) as cm:
                build(pe.PlanInputs(race_date=race), snap)
            self.assertEqual(str(cm.exception), "race_date must be after the end of this week")

    def test_race_next_monday_is_a_one_week_plan(self):
        snap = snapshot(TODAY, PROFILES["owner40"])
        plan = self.assertSound(pe.PlanInputs(race_date=D(2026, 11, 30)), snap)
        real = [w for w in plan["weeks"] if w["kind"] != "week0"]
        self.assertEqual([(w["kind"], w["target_km"]) for w in real], [("race", 0.0)])   # pro rata: 0 days
        self.assertEqual([s["role"] for s in real[0]["sessions"]], ["race"])
        self.assertEqual(plan["feasibility"]["verdict"], "unrealistic")

    def test_race_weekday_sets_the_race_week_pro_rata(self):
        snap = snapshot(TODAY, PROFILES["owner40"])
        for wd in range(7):
            race = race_in(TODAY, 12, wd)
            plan = self.assertSound(pe.PlanInputs(race_date=race), snap, msg=race.isoformat())
            real = [w for w in plan["weeks"] if w["kind"] != "week0"]
            peak = max(w["target_km"] for w in real if w["kind"] == "normal")
            race_week = real[-1]
            with self.subTest(weekday=wd):
                self.assertAlmostEqual(race_week["target_km"], round(0.45 * peak * wd / 7, 1), delta=0.15)
                self.assertAlmostEqual(real[-2]["target_km"], round(0.70 * peak, 1), delta=0.15)
                self.assertEqual(real[-2]["kind"], "taper")
                self.assertFalse([s for s in race_week["sessions"] if s["date"] > race.isoformat()])

    def test_today_on_each_weekday(self):
        race = D(2027, 3, 21)
        for wd in range(7):
            today = D(2026, 11, 23) + dt.timedelta(days=wd)
            snap = snapshot(today, PROFILES["owner40"])
            plan = self.assertSound(pe.PlanInputs(race_date=race), snap, msg=today.isoformat())
            w0 = [w for w in plan["weeks"] if w["kind"] == "week0"]
            with self.subTest(today=today.strftime("%a")):
                if wd in (0, 6):
                    self.assertEqual(w0, [])
                else:
                    self.assertEqual(w0[0]["start"], D(2026, 11, 23).isoformat())
                    self.assertTrue(all(s["date"] > today.isoformat() and s["role"] == "easy"
                                        for s in w0[0]["sessions"]))
                first = [w for w in plan["weeks"] if w["kind"] != "week0"][0]
                self.assertEqual(first["start"], pe.week1_start(today).isoformat())
                if wd == 0:   # a Monday: week 1 is this week, today itself gets nothing
                    self.assertEqual(first["start"], today.isoformat())
                    self.assertFalse([s for s in first["sessions"] if s["date"] <= today.isoformat()])

    def test_marathon_under_eight_weeks_fails_feasibility_but_still_tapers(self):
        snap = snapshot(TODAY, PROFILES["owner40"])
        for weeks in range(1, 8):
            plan = self.assertSound(pe.PlanInputs(race_date=race_in(TODAY, weeks, 6)), snap)
            weeks_check = next(c for c in plan["feasibility"]["checks"] if c["id"] == "weeks")
            self.assertEqual((weeks_check["ok"], weeks_check["value"]), (False, weeks))
            self.assertEqual(plan["feasibility"]["verdict"], "unrealistic")
            self.assertEqual([w for w in plan["weeks"] if w["kind"] != "week0"][-1]["kind"], "race")
        ok = build(pe.PlanInputs(race_date=race_in(TODAY, 8, 6)), snap)
        self.assertTrue(next(c for c in ok["feasibility"]["checks"] if c["id"] == "weeks")["ok"])
        half = build(pe.PlanInputs(race_date=race_in(TODAY, 6, 6), race_distance="half"), snap)
        self.assertTrue(next(c for c in half["feasibility"]["checks"] if c["id"] == "weeks")["ok"])
        half5 = build(pe.PlanInputs(race_date=race_in(TODAY, 5, 6), race_distance="half"), snap)
        self.assertFalse(next(c for c in half5["feasibility"]["checks"] if c["id"] == "weeks")["ok"])

    def test_phase_counts_at_their_boundaries(self):
        expect = {1: (0, 0, 0, 1), 2: (0, 1, 0, 1), 7: (3, 3, 0, 1), 8: (3, 3, 1, 1), 9: (3, 4, 1, 1),
                  10: (3, 4, 1, 2), 11: (3, 4, 2, 2), 24: (9, 11, 2, 2)}
        for n, (b, bu, pk, tp) in expect.items():
            self.assertEqual(pe.phase_counts(n), {"base": b, "build": bu, "peak": pk, "taper": tp}, n)


class Dates(Oracle):
    def test_year_boundary(self):
        for today in (D(2026, 12, 23), D(2026, 12, 30), D(2026, 12, 31), D(2027, 1, 1)):
            snap = snapshot(today, PROFILES["owner40"])
            for race in (D(2027, 1, 10), D(2027, 3, 14), D(2027, 4, 4)):
                if race <= pe.week1_start(today):
                    continue
                plan = self.assertSound(pe.PlanInputs(race_date=race), snap, msg=f"{today} {race}")
                starts = [_day(w["start"]) for w in plan["weeks"]]
                self.assertTrue(all(b - a == dt.timedelta(days=7) for a, b in zip(starts, starts[1:])))

    def test_dst_changes_do_not_shift_a_day(self):
        # Israel: 2026-10-25 and 2027-03-26; Europe: 2026-10-25 and 2027-03-28; USA: 2027-03-14
        for today, race in ((D(2026, 10, 14), D(2026, 12, 6)), (D(2027, 2, 24), D(2027, 4, 11)),
                            (D(2027, 3, 1), D(2027, 5, 2)), (D(2026, 10, 21), D(2027, 4, 2))):
            snap = snapshot(today, PROFILES["owner40"], loads={})
            plan = self.assertSound(pe.PlanInputs(race_date=race), snap, msg=f"{today}")
            for w in plan["weeks"]:
                self.assertEqual(_day(w["start"]).weekday(), 0)
                self.assertEqual((_day(w["end"]) - _day(w["start"])).days, 6)
                for s in w["sessions"]:
                    self.assertEqual(s["weekday"], pe.WEEKDAYS[_day(s["date"]).weekday()])

    def test_leap_day(self):
        snap = snapshot(D(2027, 11, 17), PROFILES["owner40"])
        for race, dist in ((D(2028, 2, 29), "marathon"), (D(2028, 3, 5), "half")):
            plan = self.assertSound(pe.PlanInputs(race_date=race, race_distance=dist,
                                                  blocked_dates=(D(2028, 2, 27), D(2028, 2, 28))), snap)
            days = {s["date"] for w in plan["weeks"] for s in w["sessions"]}
            self.assertNotIn("2028-02-27", days)
            self.assertNotIn("2028-02-28", days)
        plan = build(pe.PlanInputs(race_date=D(2028, 2, 29)), snap)
        self.assertEqual(plan["weeks"][-1]["sessions"][-1]["date"], "2028-02-29")
        b = build(pe.PlanInputs(race_date=D(2028, 4, 2), b_races=(pe.BRace(D(2028, 2, 29), "run"),)), snap)
        self.assertEqual(b["b_races"][0]["mini_taper"], ["2028-02-25", "2028-02-28"])
        self.assertEqual(b["b_races"][0]["recovery"], ["2028-03-01", "2028-03-06"])


class BRaces(Oracle):
    RACE = D(2027, 3, 21)   # a Sunday, 17 weeks of plan from TODAY

    def plan(self, *b, prof="owner40", loads=None):
        snap = snapshot(TODAY, PROFILES[prof], loads=loads)
        inputs = pe.PlanInputs(race_date=self.RACE, goal_time_s=12600, b_races=tuple(b))
        return inputs, snap, self.assertSound(inputs, snap, msg=str(b))

    def test_each_kind_at_each_place(self):
        weeks = [w for w in build(pe.PlanInputs(race_date=self.RACE), snapshot(TODAY, PROFILES["owner40"]))["weeks"]
                 if w["kind"] != "week0"]
        places = {"week1": _day(weeks[0]["start"]) + dt.timedelta(days=5),
                  "mid": _day(weeks[6]["start"]) + dt.timedelta(days=6),
                  "peak": _day(next(w for w in weeks if w["phase"] == "peak")["start"]) + dt.timedelta(days=2),
                  "taper": _day(next(w for w in weeks if w["phase"] == "taper")["start"]) + dt.timedelta(days=0),
                  "week_before_a_saturday": self.RACE - dt.timedelta(days=8)}
        for where, day in places.items():
            for kind in pe.B_RACE_KINDS:
                for loads in (None, {}):
                    with self.subTest(where=where, kind=kind, loads=loads):
                        _i, _s, plan = self.plan(pe.BRace(day, kind), loads=loads)
                        self.assertEqual([b["date"] for b in plan["b_races"]], [day.isoformat()])
                        ids = [w["id"] for w in plan["warnings"]]
                        self.assertEqual("b_race_long_recovery" in ids, kind == "tri_full")
                        rec = plan["b_races"][0]["recovery"]
                        self.assertEqual((_day(rec[1]) - _day(rec[0])).days + 1, 7 if kind == "tri_full" else 6)
                        if where == "peak":
                            self.assertIn("b_race_in_peak", ids)
                        if where in ("taper", "week_before_a_saturday"):
                            self.assertIn("b_race_in_taper", ids)
                        if loads is not None:
                            self.assertEqual([b["date"] for b in plan["acwr"]["b_races"]], [day.isoformat()])

    def test_same_week_as_the_a_race_and_before_week1_are_ignored(self):
        for day in (self.RACE, self.RACE - dt.timedelta(days=1), self.RACE - dt.timedelta(days=6),
                    TODAY + dt.timedelta(days=2), TODAY - dt.timedelta(days=3)):
            with self.subTest(day=day):
                _i, _s, plan = self.plan(pe.BRace(day, "run"))
                self.assertEqual(plan["b_races"], [])
                self.assertIn("b_race_ignored", [w["id"] for w in plan["warnings"]])

    def test_a_b_race_in_the_taper_week_before_a_sunday_race_is_planned_around(self):
        day = self.RACE - dt.timedelta(days=7)   # Sunday 2027-03-14: the taper week, not the race week
        self.assertNotEqual(pe.monday(day), pe.monday(self.RACE))
        _i, _s, plan = self.plan(pe.BRace(day, "run"))
        on_day = [s["role"] for w in plan["weeks"] for s in w["sessions"] if s["date"] == day.isoformat()]
        self.assertEqual(on_day, ["b_race"])
        self.assertNotIn("b_race_ignored", [w["id"] for w in plan["warnings"]])

    def test_window_days_are_scaled_per_day(self):
        # Rule 7 per day: a mini-taper day runs 0.6 and a recovery day 0.5 of a full easy day of the same week,
        # also where the recovery reaches into the next week (a Wednesday B race: recovery to Tuesday).
        for day in (D(2027, 1, 13), D(2027, 1, 16)):
            for kind in pe.B_RACE_KINDS:
                with self.subTest(day=day, kind=kind):
                    _i, _s, plan = self.plan(pe.BRace(day, kind))
                    b = plan["b_races"][0]
                    mini = (_day(b["mini_taper"][0]), _day(b["mini_taper"][1]))
                    rec = (_day(b["recovery"][0]), _day(b["recovery"][1]))
                    checked = 0
                    for w in plan["weeks"]:
                        # (a long run turned easy under 10 days after a full-distance triathlon is no easy day)
                        runs = [(_day(s["date"]), s["km"]) for s in w["sessions"] if s["role"] in ("easy", "strides")
                                and not s["key"].endswith("-long")]
                        full = [k for d, k in runs if not (mini[0] <= d <= mini[1] or rec[0] <= d <= rec[1])]
                        if not full:
                            continue
                        for d, k in runs:
                            f = 0.6 if mini[0] <= d <= mini[1] else 0.5 if rec[0] <= d <= rec[1] else None
                            if f is not None:
                                checked += 1
                                want = f * max(full) if f == 0.5 else min(f * max(full), 8.0)   # the 8 km cap
                                self.assertAlmostEqual(k, want, delta=0.15, msg=(d, k, full))
                    if day.weekday() == 2:   # the recovery ends on a Tuesday: the week after has full days
                        self.assertGreater(checked, 0)

    def test_two_b_races(self):
        for gap in (3, 6, 10, 21):
            first = D(2027, 1, 9)
            with self.subTest(gap=gap):
                _i, _s, plan = self.plan(pe.BRace(first, "tri_half"), pe.BRace(first + dt.timedelta(days=gap), "run"))
                self.assertEqual(len(plan["b_races"]), 2)
                self.assertEqual(sorted(s["date"] for w in plan["weeks"] for s in w["sessions"]
                                        if s["role"] == "b_race"),
                                 [first.isoformat(), (first + dt.timedelta(days=gap)).isoformat()])

    def test_b_race_at_week1_start_puts_its_mini_taper_in_week0(self):
        _i, _s, plan = self.plan(pe.BRace(D(2026, 11, 30), "tri_half"))
        self.assertEqual(plan["b_races"][0]["mini_taper"], ["2026-11-26", "2026-11-29"])
        w0 = plan["weeks"][0]
        self.assertEqual(w0["kind"], "week0")
        self.assertTrue(all(s["role"] == "easy" for s in w0["sessions"]))
        self.assertEqual(plan["weeks"][1]["kind"], "b_race")

    def test_a_b_race_skips_the_deload_due_before_it(self):
        weeks = [w for w in build(pe.PlanInputs(race_date=self.RACE), snapshot(TODAY, PROFILES["owner40"]))["weeks"]
                 if w["kind"] != "week0"]
        deload = next(w for w in weeks if w["kind"] == "deload")   # week 4
        b_day = _day(deload["start"]) + dt.timedelta(days=7 + 5)   # Saturday of week 5
        _i, _s, plan = self.plan(pe.BRace(b_day, "run"))
        kinds = [w["kind"] for w in plan["weeks"] if w["kind"] != "week0"]
        self.assertEqual(kinds[3], "normal")
        self.assertEqual(kinds[4], "b_race")


class Inputs(Oracle):
    RACE = D(2027, 3, 21)

    def snap(self, prof="owner40", **kw):
        return snapshot(TODAY, PROFILES[prof], **kw)

    def test_run_days_one_to_seven_and_long_run_day_each_weekday(self):
        for k in range(1, 8):
            run_days = tuple(range(7 - k, 7)) if k < 7 else tuple(range(7))
            for long_day in range(7):
                for q in (0, 1, 2):
                    inputs = pe.PlanInputs(race_date=self.RACE, run_days=run_days, rest_days=(),
                                           long_run_day=long_day, quality_per_week=q)
                    with self.subTest(run_days=run_days, long_day=long_day, q=q):
                        plan = self.assertSound(inputs, self.snap())
                        for w in plan["weeks"]:
                            for s in w["sessions"]:
                                if s["role"] not in ("race", "b_race"):
                                    self.assertIn(_day(s["date"]).weekday(), run_days)
                        if q == 0:
                            self.assertFalse([s for w in plan["weeks"] for s in w["sessions"]
                                              if s["role"] in pe.QUALITY_ROLES])

    def test_quality_per_week_caps_the_quality_sessions(self):
        for q in (0, 1, 2):
            plan = self.assertSound(pe.PlanInputs(race_date=self.RACE, quality_per_week=q,
                                                  run_days=tuple(range(7)), rest_days=()), self.snap("runner60"))
            for w in plan["weeks"]:
                self.assertLessEqual(sum(1 for s in w["sessions"] if s["role"] in pe.QUALITY_ROLES), q, w["start"])

    def test_rest_days_overlapping_run_days_win(self):
        inputs = pe.PlanInputs(race_date=self.RACE, run_days=(0, 1, 2, 3, 4, 5, 6), rest_days=(0, 2, 4))
        plan = self.assertSound(inputs, self.snap())
        for w in plan["weeks"]:
            for s in w["sessions"]:
                if s["role"] not in ("race", "b_race"):
                    self.assertNotIn(_day(s["date"]).weekday(), (0, 2, 4))
        self.assertEqual(plan["inputs"]["rest_days"], ["mon", "wed", "fri"])
        with self.assertRaises(pe.PlanError):
            build(pe.PlanInputs(race_date=self.RACE, run_days=(1, 3), rest_days=(1, 3)), self.snap())

    def test_long_run_day_on_a_rest_day_goes_to_the_next_run_day(self):
        plan = self.assertSound(pe.PlanInputs(race_date=self.RACE, long_run_day=0), self.snap())   # Monday: rest
        longs = [_day(s["date"]).weekday() for w in plan["weeks"] for s in w["sessions"] if s["role"] == "long"]
        self.assertTrue(longs)
        self.assertEqual(set(longs), {1})   # Tuesday: the Sunday before belongs to the previous week

    def test_max_long_km_low_and_out_of_range(self):
        plan = self.assertSound(pe.PlanInputs(race_date=self.RACE, max_long_km=14.0), self.snap("runner60"))
        longs = [s["km"] for w in plan["weeks"] for s in w["sessions"] if s["role"] == "long"]
        self.assertTrue(longs)
        self.assertLessEqual(max(longs), 14.0)
        self.assertEqual(max(longs), 14.0)
        for bad in (9.9, 32.1, 40.0, 0.0, -1.0):
            with self.subTest(max_long_km=bad), self.assertRaises(pe.PlanError) as cm:
                build(pe.PlanInputs(race_date=self.RACE, max_long_km=bad), self.snap())
            self.assertEqual(str(cm.exception), "max_long_km must be from 10 to 32")
        self.assertSound(pe.PlanInputs(race_date=self.RACE, max_long_km=10.0), self.snap())
        self.assertSound(pe.PlanInputs(race_date=self.RACE, max_long_km=32.0), self.snap("runner120"))

    def test_input_refusals(self):
        bad = [(dict(race_distance="10k"), "race_distance must be marathon or half"),
               (dict(run_days=(7,)), "run_days and rest_days are weekdays"),
               (dict(rest_days=(-1,)), "run_days and rest_days are weekdays"),
               (dict(long_run_day=7), "long_run_day must be a weekday"),
               (dict(quality_per_week=3), "quality_per_week must be 0, 1 or 2"),
               (dict(quality_per_week=-1), "quality_per_week must be 0, 1 or 2"),
               (dict(long_share=0.19), "long_share must be from 0.2 to 0.6"),
               (dict(long_share=0.61), "long_share must be from 0.2 to 0.6"),
               (dict(goal_time_s=3599), "goal_time must be from 1:00:00 to 8:00:00"),
               (dict(goal_time_s=8 * 3600 + 1), "goal_time must be from 1:00:00 to 8:00:00"),
               (dict(b_races=(pe.BRace(D(2027, 1, 9), "ironman"),)), "a B race kind must be tri_full, tri_half or run"),
               (dict(overrides=(pe.Override("delete"),)), "unknown override kind"),
               (dict(easy_band_slowdown=0.11), "easy_band_slowdown must be from 0 to 0.1"),
               (dict(easy_band_slowdown=-0.01), "easy_band_slowdown must be from 0 to 0.1")]
        for kw, text in bad:
            with self.subTest(**{k: str(v) for k, v in kw.items()}), self.assertRaises(pe.PlanError) as cm:
                build(pe.PlanInputs(race_date=self.RACE, **kw), self.snap())
            self.assertEqual(str(cm.exception), text)
        for goal in (3600, 8 * 3600):
            self.assertSound(pe.PlanInputs(race_date=self.RACE, goal_time_s=goal), self.snap())

    def test_blocked_dates(self):
        base = build(pe.PlanInputs(race_date=self.RACE), self.snap())
        week = next(w for w in base["weeks"] if w["kind"] == "normal" and any(s["role"] == "long" for s in w["sessions"]))
        long_day = _day(next(s["date"] for s in week["sessions"] if s["role"] == "long"))
        whole = tuple(_day(week["start"]) + dt.timedelta(days=i) for i in range(7))
        cases = {"long_run_day": (long_day,), "whole_week": whole, "race_day": (self.RACE,),
                 "every_day_of_the_plan": tuple(TODAY + dt.timedelta(days=i) for i in range(1, 120))}
        for name, blocked in cases.items():
            with self.subTest(case=name):
                inputs = pe.PlanInputs(race_date=self.RACE, blocked_dates=blocked)
                plan = self.assertSound(inputs, self.snap())
                days = {s["date"] for w in plan["weeks"] for s in w["sessions"] if s["role"] != "race"}
                self.assertFalse(days & {d.isoformat() for d in blocked})
                self.assertEqual([s["date"] for w in plan["weeks"] for s in w["sessions"] if s["role"] == "race"],
                                 [self.RACE.isoformat()])
        plan = build(pe.PlanInputs(race_date=self.RACE, blocked_dates=whole), self.snap())
        blocked_week = next(w for w in plan["weeks"] if w["start"] == week["start"])
        self.assertEqual(blocked_week["sessions"], [])
        self.assertEqual(blocked_week["target_km"], week["target_km"])   # the target stays, the km are not run

    def _blocked_long(self):
        base = build(pe.PlanInputs(race_date=self.RACE), self.snap())
        week = next(w for w in base["weeks"] if w["kind"] == "normal" and any(s["role"] == "long" for s in w["sessions"]))
        long_day = _day(next(s["date"] for s in week["sessions"] if s["role"] == "long"))
        plan = self.assertSound(pe.PlanInputs(race_date=self.RACE, blocked_dates=(long_day,)), self.snap())
        moved = next(w for w in plan["weeks"] if w["start"] == week["start"])
        return long_day, plan, [s for s in moved["sessions"] if s["role"] == "long"]

    def test_blocked_long_run_day_moves_the_long_run_one_day(self):
        long_day, _plan, longs = self._blocked_long()
        self.assertEqual(len(longs), 1)
        self.assertEqual((_day(longs[0]["date"]) - long_day).days, -1)   # Sunday blocked: Saturday

    def test_blocked_long_run_day_warns_long_moved(self):
        long_day, plan, longs = self._blocked_long()
        self.assertIn("long_moved", [w["id"] for w in plan["warnings"]])

    def test_a_long_run_moved_into_the_last_21_days_keeps_80_percent_easy(self):
        # The owner's provisional numbers, a Saturday marathon, long runs on Saturday, and the Saturday 21
        # days out blocked (Friday is a rest day, so the long run goes to Sunday, 20 days out).
        race = D(2027, 2, 27)
        inputs = pe.PlanInputs(race_date=race, goal_time_s=3 * 3600 + 30 * 60, long_run_day=5,
                               blocked_dates=(race - dt.timedelta(days=21),))
        plan = build(inputs, pe.sample_snapshot(TODAY))
        self.assertTrue(plan["invariants"]["ok"], plan["invariants"]["failed"])

    def test_the_same_trim_without_a_blocked_date(self):
        # the same week with the long run on its day: every rule holds
        race = D(2027, 2, 27)
        inputs = pe.PlanInputs(race_date=race, goal_time_s=3 * 3600 + 30 * 60, long_run_day=5)
        self.assertSound(inputs, pe.sample_snapshot(TODAY))

    def test_late_long_runs_are_capped_before_any_peak_long_run(self):
        # A half 3 or 4 weeks out: every long run is within 21 days, so no normal week sets a peak long run.
        # The 0.80 cap then scales the longest run so far (here the 8 weeks' 26 km), not nothing: no 26 km
        # long run a week before the race.
        prof = PROFILES["runner60"]
        snap = snapshot(TODAY, prof)
        for race in (D(2026, 12, 13), D(2026, 12, 20)):
            with self.subTest(race=race):
                plan = self.assertSound(pe.PlanInputs(race_date=race, race_distance="half"), snap)
                longs = [s for w in plan["weeks"] for s in w["sessions"] if s["role"] == "long"]
                self.assertTrue(longs)
                for s in longs:
                    self.assertLess((race - _day(s["date"])).days, 21)
                    self.assertLessEqual(s["km"], round(0.8 * prof.longest_km, 1) + EPS_KM, s)

    def test_the_late_long_invariant_without_a_peak_long_run(self):
        # check_invariants recomputes the same cap: a late long run raised past it fails peak_long_timing
        race = D(2026, 12, 20)
        plan = build(pe.PlanInputs(race_date=race, race_distance="half"), snapshot(TODAY, PROFILES["runner60"]))
        weeks, ctx = plan["_check"]["weeks"], plan["_check"]["ctx"]
        self.assertEqual(ctx.peak_long, 0)
        w = next(w for w in weeks if any(s.role == "long" for s in w.sessions))
        s = next(s for s in w.sessions if s.role == "long")
        w.sessions[w.sessions.index(s)] = pe.make_session(s.key, s.date, "long", pe.Meters(21400.0), ctx.paces,
                                                          ctx.rules)
        failed = [i["id"] for i in pe.check_invariants(plan) if not i["ok"]]
        self.assertIn("peak_long_timing", failed)
        # the cap the engine, the overrides and the invariant share: 0.80 x the 8 weeks' longest run
        self.assertEqual(pe.late_long_limit(ctx, s.date), 0.8 * 26000.0)


class FixedSessions(Oracle):
    RACE = D(2027, 3, 21)

    def test_heavy_trainingpeaks_weeks(self):
        fixed = []
        for wk in (2, 3, 7, 11):
            start = pe.week1_start(TODAY) + dt.timedelta(days=7 * (wk - 1))
            fixed += [pe.FixedSession(start + dt.timedelta(days=1), "bike", False, True, 240.0),
                      pe.FixedSession(start + dt.timedelta(days=2), "run", True, False, 60.0),
                      pe.FixedSession(start + dt.timedelta(days=3), "swim", False, False, 60.0),
                      pe.FixedSession(start + dt.timedelta(days=5), "run", False, True, 120.0),
                      pe.FixedSession(start + dt.timedelta(days=6), "bike", True, False, None)]
        for loads in (None, {}, {"garmin_gap": 0.3}):
            with self.subTest(loads=loads):
                snap = snapshot(TODAY, PROFILES["owner40"], loads=loads, fixed=fixed)
                plan = self.assertSound(pe.PlanInputs(race_date=self.RACE, goal_time_s=12600), snap)
                if loads == {}:
                    self.assertTrue(plan["acwr"]["enforced"])

    def test_a_hard_fixed_day_next_to_each_planned_quality_day(self):
        base = build(pe.PlanInputs(race_date=self.RACE), snapshot(TODAY, PROFILES["owner40"]))
        hard_days = [_day(s["date"]) for w in base["weeks"] for s in w["sessions"]
                     if s["role"] in pe.QUALITY_ROLES + ("long",)]
        for d in hard_days[:30]:
            for side in (-1, 1):
                fixed = (pe.FixedSession(d + dt.timedelta(days=side), "bike", True, False, 90.0),)
                with self.subTest(day=d, side=side):
                    self.assertSound(pe.PlanInputs(race_date=self.RACE),
                                     snapshot(TODAY, PROFILES["owner40"], fixed=fixed))

    def test_fixed_sessions_on_every_day(self):
        days = [TODAY + dt.timedelta(days=i) for i in range(0, 60)]
        cases = {"easy_swims": [pe.FixedSession(d, "swim", False, False, 45.0) for d in days],
                 "hard_rides": [pe.FixedSession(d, "bike", True, False, 90.0) for d in days],
                 "runs": [pe.FixedSession(d, "run", False, False, 40.0) for d in days],
                 "long_strength": [pe.FixedSession(d, "strength", False, True, None) for d in days[::2]]}
        for name, fixed in cases.items():
            for loads in (None, {}):
                with self.subTest(case=name, loads=loads):
                    snap = snapshot(TODAY, PROFILES["owner40"], fixed=fixed, loads=loads)
                    plan = self.assertSound(pe.PlanInputs(race_date=self.RACE), snap)
                    early = [s for w in plan["weeks"] for s in w["sessions"]
                             if s["date"] < (TODAY + dt.timedelta(days=60)).isoformat()]
                    if name == "runs":
                        self.assertEqual(early, [])
                    if name == "hard_rides":
                        self.assertFalse([s for s in early if s["hard_or_long"]])

    def test_a_fixed_hard_session_the_day_before_week1(self):
        sunday = pe.week1_start(TODAY) - dt.timedelta(days=1)
        fixed = (pe.FixedSession(sunday, "bike", True, True, 180.0),)
        plan = self.assertSound(pe.PlanInputs(race_date=self.RACE, run_days=tuple(range(7)), rest_days=()),
                                snapshot(TODAY, PROFILES["owner40"], fixed=fixed))
        monday = [s for w in plan["weeks"] for s in w["sessions"] if s["date"] == pe.week1_start(TODAY).isoformat()]
        self.assertFalse([s for s in monday if s["hard_or_long"]])


class Determinism(Oracle):
    def test_same_inputs_same_bytes_across_the_matrix(self):
        for name, prof in PROFILES.items():
            for loads in (None, {}):
                snap = snapshot(TODAY, prof, loads=loads)
                inputs = pe.PlanInputs(race_date=D(2027, 3, 21), goal_time_s=12600,
                                       b_races=(pe.BRace(D(2027, 1, 16), "tri_half"),),
                                       blocked_dates=(D(2026, 12, 25), D(2027, 1, 1)))
                a = pe.plan_answer(build(inputs, snap, what_if=True))
                b = pe.plan_answer(build(dataclasses.replace(inputs), dataclasses.replace(snap), what_if=True))
                with self.subTest(profile=name, loads=loads):
                    self.assertEqual(a["content_hash"], b["content_hash"])
                    self.assertEqual(a, b)
                    self.assertEqual(a["content_hash"], pe.content_hash(pe.RULES_VERSION, a["inputs"], a["weeks"]))

    def test_order_of_set_like_inputs_does_not_change_the_plan(self):
        snap = snapshot(TODAY, PROFILES["owner40"])
        a = build(pe.PlanInputs(race_date=D(2027, 3, 21), run_days=(1, 2, 3, 5, 6), rest_days=(0, 4),
                                blocked_dates=(D(2027, 1, 1), D(2026, 12, 25))), snap)
        b = build(pe.PlanInputs(race_date=D(2027, 3, 21), run_days=(6, 5, 3, 2, 1, 1), rest_days=(4, 0),
                                blocked_dates=(D(2026, 12, 25), D(2027, 1, 1), D(2027, 1, 1))), snap)
        self.assertEqual(a["content_hash"], b["content_hash"])

    def test_any_change_in_the_inputs_changes_the_hash(self):
        snap = snapshot(TODAY, PROFILES["owner40"])
        base = pe.PlanInputs(race_date=D(2027, 3, 21))
        variants = [dataclasses.replace(base, goal_time_s=12600), dataclasses.replace(base, quality_per_week=1),
                    dataclasses.replace(base, max_long_km=30.0), dataclasses.replace(base, long_share=0.45),
                    dataclasses.replace(base, course_id=7), dataclasses.replace(base, easy_band_slowdown=0.03),
                    dataclasses.replace(base, blocked_dates=(D(2027, 2, 1),))]
        hashes = {build(v, snap)["content_hash"] for v in variants} | {build(base, snap)["content_hash"]}
        self.assertEqual(len(hashes), len(variants) + 1)


if __name__ == "__main__":
    unittest.main()
