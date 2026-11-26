"""Property-based tests (Hypothesis) for plan_engine:

    python3 -m unittest discover -s ext -p "test_properties_plan_engine.py" -v

Skipped ("needs hypothesis") where hypothesis is not installed. Runs are derandomized and keep no example
database, so CI sees the same examples every time. The properties: no invariant fails for any input the
engine accepts, the same inputs give the same content_hash, the plan grows with the starting volume, a
blocked date never raises another day's km, and the coach critic's overrides never raise a week's km or a
long run (cap_long included: no reduce-only edit raises a week or a session). The long-run step recounted from
the answer holds after a blocked week, and the first long run after a half-distance triathlon is at most 0.75 x
the last one before it. Rules v2: with recorded loads (tracked by Garmin's ratio or not) every invariant still holds, and the
planned-ratio cap never raises a week, a session or a long run and is deterministic. Starting volumes reach 100 km a week and run days all seven, so the easy-run cap and the peak
ceiling bind.
"""

import dataclasses
import datetime as dt
import math
import os
import unittest

import plan_engine as pe
from test_plan_engine import easy_run_due

try:
    from hypothesis import HealthCheck, assume, given, settings
    from hypothesis import strategies as st
    HAVE_HYPOTHESIS = True
except ImportError:  # the classes below are skipped; the shim only lets the module load
    HAVE_HYPOTHESIS = False

    class _Absent:
        """Stands in for hypothesis.strategies: every attribute, call and | is itself."""

        def __getattr__(self, name):
            return self

        def __call__(self, *args, **kwargs):
            return self

        def __or__(self, other):
            return self

    st = HealthCheck = _Absent()

    def given(*args, **kwargs):
        def keep(fn):
            return fn
        return keep

    settings = given

    def assume(condition):
        return True

NEEDS_HYPOTHESIS = "needs hypothesis"
PROPS = dict(max_examples=150, deadline=None, derandomize=True, database=None,
             suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much])
TODAY = dt.date(2026, 11, 25)


@st.composite
def plan_case(draw):
    today = TODAY + dt.timedelta(days=draw(st.integers(0, 6)))
    weeks = draw(st.integers(2, 20))
    first = pe.week1_start(today)
    race = first + dt.timedelta(days=7 * (weeks - 1) + draw(st.integers(0, 6)))
    run_days = tuple(sorted(draw(st.one_of(st.sets(st.integers(0, 6), min_size=3, max_size=7),
                                           st.just(set(range(7)))))))
    rest = tuple(sorted(set(range(7)) - set(run_days)))
    long_day = draw(st.one_of(st.sampled_from(run_days), st.integers(0, 6)))   # a rest day too (rule 6)
    km_lo, km_hi = draw(st.sampled_from(((8000, 90000), (60000, 100000))))
    km_weeks = tuple(pe.Meters(draw(st.floats(km_lo, km_hi))) for _ in range(8))
    longest = pe.Meters(draw(st.floats(5000, 32000)))
    lt = draw(st.one_of(st.none(), st.floats(2.9, 5.2)))
    b_races = ()
    if draw(st.booleans()) and weeks >= 4:
        b_day = first + dt.timedelta(days=draw(st.integers(7, 7 * (weeks - 2))))
        b_races = (pe.BRace(b_day, draw(st.sampled_from(pe.B_RACE_KINDS))),)
    blocked = tuple(first + dt.timedelta(days=draw(st.integers(0, 7 * weeks)))
                    for _ in range(draw(st.integers(0, 4))))
    fixed = tuple(pe.FixedSession(first + dt.timedelta(days=draw(st.integers(0, 7 * weeks))),
                                  draw(st.sampled_from(("bike", "swim", "run"))), draw(st.booleans()),
                                  draw(st.booleans()), draw(st.one_of(st.none(), st.floats(20, 300))))
                  for _ in range(draw(st.integers(0, 6))))
    acts, garmin = (), ()
    if draw(st.booleans()):   # recorded loads: tracked (gap 0 or 0.1) or not (0.3), any load per minute
        acts, garmin = pe.sample_loads(today, km_weeks, garmin_gap=draw(st.sampled_from((0.0, 0.1, 0.3))),
                                       run_lpm=draw(st.floats(0.8, 2.5)), bike_min=draw(st.sampled_from((0, 90, 300))),
                                       swim_min=draw(st.sampled_from((0, 45))))
    distance = draw(st.sampled_from(("marathon", "half")))
    goal = draw(st.one_of(st.none(), st.integers(5400 if distance == "half" else 9000,
                                                 9000 if distance == "half" else 18000)))
    inputs = pe.PlanInputs(race_date=race, race_distance=distance, goal_time_s=goal, b_races=b_races,
                           run_days=run_days, rest_days=rest, long_run_day=long_day,
                           quality_per_week=draw(st.integers(0, 2)),
                           max_long_km=draw(st.sampled_from((24.0, 28.0, 32.0))), blocked_dates=blocked)
    snap = pe.Snapshot(today=today, week_m=km_weeks, week_runs=(4,) * 8, longest_run_8w_m=longest,
                       lt_speed_mps=pe.MetersPerSecond(lt) if lt else None,
                       prediction_s=(("marathon", 3 * 3600 + 40 * 60), ("half", 3600 + 45 * 60)), fixed=fixed,
                       activity_load=acts, garmin_load=garmin)
    return inputs, snap


@unittest.skipUnless(HAVE_HYPOTHESIS, NEEDS_HYPOTHESIS)
class PlanProperties(unittest.TestCase):
    @settings(**PROPS)
    @given(plan_case())
    def test_no_invariant_fails(self, case):
        inputs, snap = case
        plan = pe.build_plan(inputs, snap, what_if_runs=False)
        self.assertTrue(plan["invariants"]["ok"], plan["invariants"]["failed"])

    @settings(**PROPS)
    @given(plan_case())
    def test_same_inputs_same_hash(self, case):
        inputs, snap = case
        a = pe.build_plan(inputs, snap, what_if_runs=False)
        b = pe.build_plan(dataclasses.replace(inputs), dataclasses.replace(snap), what_if_runs=False)
        self.assertEqual(a["content_hash"], b["content_hash"])
        self.assertEqual(pe.plan_answer(a), pe.plan_answer(b))

    @settings(**PROPS)
    @given(plan_case(), st.floats(1.0, 1.5))
    def test_targets_grow_with_the_starting_volume(self, case, more):
        inputs, snap = case
        # rule 1 only: the ACWR cap (rules v2) can cut a bigger history's first weeks harder, and its long-run cut
        # lowers the long runs that step from it, so with the cap a bigger start may plan less
        snap = dataclasses.replace(snap, garmin_load=())
        bigger = dataclasses.replace(snap, week_m=tuple(pe.Meters(m * more) for m in snap.week_m))
        a = pe.plan_answer(pe.build_plan(inputs, snap, what_if_runs=False))
        b = pe.plan_answer(pe.build_plan(inputs, bigger, what_if_runs=False))
        for wa, wb in zip(a["weeks"], b["weeks"]):
            # up to the 100 m rounding of each session a capped week counts as placed
            self.assertLessEqual(wa["target_km"], wb["target_km"] * 1.01 + 0.1)

    @settings(**PROPS)
    @given(plan_case(), st.data())
    def test_a_blocked_date_never_raises_another_day(self, case, data):
        inputs, snap = case
        base = pe.plan_answer(pe.build_plan(inputs, snap, what_if_runs=False))
        easy = [(w["start"], s) for w in base["weeks"] for s in w["sessions"] if s["role"] == "easy"]
        assume(easy)
        # a week the ACWR cap cut (rules v2) may keep more km once a blocked date lowers its load: not this rule
        assume(not base["acwr"]["cuts"])
        start, s = data.draw(st.sampled_from(easy))
        day = dt.date.fromisoformat(s["date"])
        edited = pe.build_plan(dataclasses.replace(inputs, blocked_dates=inputs.blocked_dates + (day,)), snap,
                               what_if_runs=False)
        self.assertTrue(edited["invariants"]["ok"], edited["invariants"]["failed"])
        after = pe.plan_answer(edited)
        for wa, wb in zip(base["weeks"], after["weeks"]):
            self.assertLessEqual(wb["planned_km"], wa["planned_km"] + 0.05, wb["start"])
            if wa["start"] != start:
                continue
            self.assertLessEqual(wb["planned_km"], wa["planned_km"] - s["km"] + 0.1)   # both rounded to 0.1
            old = {x["date"]: x["km"] for x in wa["sessions"]}
            self.assertNotIn(s["date"], {x["date"] for x in wb["sessions"]})
            for x in wb["sessions"]:
                self.assertLessEqual(x["km"], old.get(x["date"], 0.0) + 0.05, x["date"])

    @settings(**PROPS)
    @given(plan_case(), st.data())
    def test_overrides_never_raise_week_km_or_a_long_run(self, case, data):
        inputs, snap = case
        base = pe.plan_answer(pe.build_plan(inputs, snap, what_if_runs=False))
        sessions = [s for w in base["weeks"] for s in w["sessions"] if s["role"] not in ("race", "b_race")]
        assume(sessions)
        starts = [dt.date.fromisoformat(w["start"]) for w in base["weeks"]]
        overrides = []
        for _ in range(data.draw(st.integers(1, 5))):
            s = data.draw(st.sampled_from(sessions))
            day = dt.date.fromisoformat(s["date"])
            near = day + dt.timedelta(days=data.draw(st.integers(-3, 3)))
            kind = data.draw(st.sampled_from(pe.OVERRIDE_KINDS))
            if kind == "move":
                overrides.append(pe.Override("move", key=s["key"], date=near))
            elif kind == "swap":
                overrides.append(pe.Override("swap", date=day, date_b=near))
            elif kind == "rest":
                overrides.append(pe.Override("rest", date=day))
            elif kind == "easy":
                overrides.append(pe.Override("easy", key=s["key"]))
            elif kind == "cap_week_km":
                overrides.append(pe.Override("cap_week_km", week_start=data.draw(st.sampled_from(starts)),
                                             km=data.draw(st.floats(0, 120))))
            elif kind == "cap_long":
                overrides.append(pe.Override("cap_long", date=day, km=data.draw(st.floats(0, s["km"] + 2))))
            else:
                overrides.append(pe.Override("long_run_day", week_start=data.draw(st.sampled_from(starts)),
                                             weekday=data.draw(st.integers(0, 6))))
        edited = pe.build_plan(dataclasses.replace(inputs, overrides=tuple(overrides)), snap, what_if_runs=False)
        after = pe.plan_answer(edited)
        self.assertEqual(len(after["overrides"]["applied"]) + len(after["overrides"]["refused"]), len(overrides))
        self.assertTrue(edited["invariants"]["ok"], edited["invariants"]["failed"])
        for wa, wb in zip(base["weeks"], after["weeks"]):
            self.assertLessEqual(wb["planned_km"], wa["planned_km"] + 0.05, wb["start"])
            self.assertLessEqual(wb["long_km"] or 0, wa["long_km"] or 0, wb["start"])


@st.composite
def gap_case(draw):
    """A plan case with a whole week blocked (travel) and often a half-distance triathlon B race."""
    inputs, snap = draw(plan_case())
    first = pe.week1_start(snap.today)
    weeks = (pe.monday(inputs.race_date) - first).days // 7 + 1
    blocked = inputs.blocked_dates
    if weeks >= 3 and draw(st.integers(0, 3)):
        wk = first + dt.timedelta(days=7 * draw(st.integers(0, weeks - 2)))
        blocked += tuple(wk + dt.timedelta(days=i) for i in range(7))
    b_races = inputs.b_races
    if weeks >= 5 and draw(st.booleans()):
        b_races = (pe.BRace(first + dt.timedelta(days=draw(st.integers(14, 7 * (weeks - 2)))), "tri_half"),)
    return dataclasses.replace(inputs, blocked_dates=blocked, b_races=b_races), snap


def _first_long_after_problems(plan):
    """After each half-distance triathlon: the first long run <= 0.75 x the last long run before the race (the
    8 weeks' longest run before any), from the answer alone."""
    out = []
    longs = sorted((s["date"], s["km"]) for w in plan["weeks"] for s in w["sessions"] if s["role"] == "long")
    for b in plan["b_races"]:
        if b["kind"] != "tri_half":
            continue
        before = [k for d, k in longs if d < b["date"]]
        after = [(d, k) for d, k in longs if d > b["date"]]
        ref = before[-1] if before else (plan["start"]["longest_run_8w_km"] or 0.0)
        if after and ref and after[0][1] > 0.75 * ref + 0.05:
            out.append(f"{after[0][0]}: first long {after[0][1]} km > 0.75 x {ref} km")
    return out


def _reduce_only(data, plan):
    """1 to 5 edits that only lower load: cap_long, cap_km, cap_week_km, easy and rest, by date where they can."""
    sessions = [s for w in plan["weeks"] for s in w["sessions"] if s["role"] not in ("race", "b_race")]
    starts = [dt.date.fromisoformat(w["start"]) for w in plan["weeks"]]
    longs = [s for s in sessions if s["role"] == "long"] or sessions
    out = []
    for _ in range(data.draw(st.integers(1, 5))):
        kind = data.draw(st.sampled_from(("cap_long", "cap_long", "cap_km", "cap_week_km", "easy", "rest")))
        s = data.draw(st.sampled_from(longs if kind == "cap_long" else sessions))
        day = dt.date.fromisoformat(s["date"])
        if kind == "cap_long":
            out.append(pe.Override("cap_long", date=day, km=data.draw(st.floats(0, s["km"] + 3))))
        elif kind == "cap_km":
            out.append(pe.Override("cap_km", date=day, km=data.draw(st.floats(0, s["km"] + 3))))
        elif kind == "cap_week_km":
            out.append(pe.Override("cap_week_km", week_start=data.draw(st.sampled_from(starts)),
                                   km=data.draw(st.floats(0, 120))))
        elif kind == "easy":
            out.append(pe.Override("easy", date=day))
        else:
            out.append(pe.Override("rest", date=day))
    return tuple(out)


@unittest.skipUnless(HAVE_HYPOTHESIS, NEEDS_HYPOTHESIS)
class GapAndBRaceProperties(unittest.TestCase):
    """The long-run step after a gap, rule 7 after a half-distance triathlon and the reduce-only overrides,
    each recounted from the answer alone (test_plan_engine.long_step_problems)."""

    @settings(**PROPS)
    @given(gap_case())
    def test_the_long_run_step_recounted_from_the_plan_holds(self, case):
        from test_plan_engine import long_step_problems

        inputs, snap = case
        plan = pe.build_plan(inputs, snap, what_if_runs=False)
        self.assertTrue(plan["invariants"]["ok"], plan["invariants"]["failed"])
        self.assertEqual(long_step_problems(pe.plan_answer(plan)), [])

    @settings(**PROPS)
    @given(gap_case())
    def test_first_long_run_after_a_tri_half_is_at_most_075_of_the_last_before(self, case):
        inputs, snap = case
        plan = pe.plan_answer(pe.build_plan(inputs, snap, what_if_runs=False))
        self.assertEqual(_first_long_after_problems(plan), [])

    @settings(**PROPS)
    @given(gap_case(), st.data())
    def test_reduce_only_overrides_never_raise_a_week_or_a_session(self, case, data):
        from test_plan_engine import long_step_problems

        inputs, snap = case
        base = pe.plan_answer(pe.build_plan(inputs, snap, what_if_runs=False))
        assume(any(s["role"] not in ("race", "b_race") for w in base["weeks"] for s in w["sessions"]))
        overrides = _reduce_only(data, base)
        edited = pe.build_plan(dataclasses.replace(inputs, overrides=overrides), snap, what_if_runs=False)
        after = pe.plan_answer(edited)
        self.assertEqual(len(after["overrides"]["applied"]) + len(after["overrides"]["refused"]), len(overrides))
        self.assertTrue(edited["invariants"]["ok"], edited["invariants"]["failed"])
        km0 = {s["key"]: s["km"] for w in base["weeks"] for s in w["sessions"]}
        for wa, wb in zip(base["weeks"], after["weeks"]):
            self.assertLessEqual(wb["planned_km"], wa["planned_km"] + 0.05, wb["start"])
            self.assertLessEqual(wb["long_km"] or 0, wa["long_km"] or 0, wb["start"])
            for s in wb["sessions"]:
                self.assertLessEqual(s["km"], km0.get(s["key"], 0.0) + 0.05, s["key"])
        self.assertEqual(long_step_problems(after), [])
        self.assertEqual(_first_long_after_problems(after), [])
        for r in after["overrides"]["refused"]:
            self.assertTrue(r["rule"])


@st.composite
def tracked_case(draw):
    """A plan case whose load model tracks Garmin (enforced), often with heavy fixed rides."""
    inputs, snap = draw(plan_case())
    acts, garmin = pe.sample_loads(snap.today, snap.week_m, garmin_gap=0.0, run_lpm=draw(st.floats(0.8, 2.5)),
                                   bike_min=draw(st.sampled_from((0, 90))))
    first = pe.week1_start(snap.today)
    rides = tuple(pe.FixedSession(first + dt.timedelta(days=draw(st.integers(0, 60))), "bike", False, True,
                                  draw(st.floats(60, 360))) for _ in range(draw(st.integers(0, 5))))
    return inputs, dataclasses.replace(snap, activity_load=acts, garmin_load=garmin, fixed=snap.fixed + rides)


@unittest.skipUnless(HAVE_HYPOTHESIS, NEEDS_HYPOTHESIS)
class AcwrProperties(unittest.TestCase):
    """Rules v2 item 12: the planned-ratio cap only ever lowers load, and it is deterministic."""

    @settings(**PROPS)
    @given(tracked_case())
    def test_the_cap_never_raises_load(self, case):
        inputs, snap = case
        held = pe.build_plan(inputs, snap, what_if_runs=False)
        free = pe.build_plan(inputs, dataclasses.replace(snap, garmin_load=()), what_if_runs=False)   # not enforced
        self.assertTrue(held["invariants"]["ok"], held["invariants"]["failed"])
        self.assertTrue(held["acwr"]["enforced"])
        self.assertFalse(free["acwr"]["enforced"])
        a, b = pe.plan_answer(held), pe.plan_answer(free)
        before_cut = {w.number: w.acwr_from[0] / 1000.0 for w in held["_check"]["weeks"]}
        for wa, wb in zip(a["weeks"], b["weeks"]):
            # (a long run cut shifts km to easy runs in the weeks after it: their 100 m rounding can lower the km a
            # week counts as placed, and so the targets after it, never raise them)
            self.assertLessEqual(wa["target_km"], wb["target_km"] + 0.05 + 1e-6, wa["start"])
            # (a week after a shorter long run fills its target with easy runs: never past the target the plan
            # without the cap gives it, 100 m rounding per run at most past what that plan placed)
            self.assertLessEqual(wa["planned_km"], max(wb["planned_km"] + 0.1 * len(wa["sessions"]), wb["target_km"])
                                 + 1e-6, wa["start"])
            self.assertLessEqual(wa["long_km"] or 0, wb["long_km"] or 0, wa["start"])
            if wa["planned_km"] > wb["planned_km"] + 0.1 * len(wa["sessions"]) + 1e-6:
                # the plan without the cap fell short of this week's target (its long run and quality left less
                # than the easy minimum for an easy run) where this one, with a shorter long run, reached it: the
                # weeks after grow from what each placed (no make-up), so from here the two plans differ by rule 1,
                # which growth_cap checks, not by the cap
                break
            placed = max(wa["planned_km"], before_cut.get(wa["week"], 0.0))   # a cut is not a shortfall
            if wa["kind"] == "normal" and placed >= wa["target_km"] - 0.1 * len(wa["sessions"]) - 1e-6 \
                    and wb["planned_km"] < wb["target_km"] - 0.1 * len(wb["sessions"]) - 1e-6:
                # the same: this week placed its target up to the 100 m rounding of each run (before its cuts;
                # week_base counts the target) where the plan without the cap stopped short of its own (a longer
                # long run, the easy-run cap): the weeks after grow from different bases by rule 1
                break
        # a cut only ever shortens or drops a session (a later week only shifts km between its runs as its
        # shorter long run leaves more of its target to the others)
        for w in held["_check"]["weeks"]:
            for _role, was, now, _day in w.acwr_ops.values():
                self.assertLess(now, was)

    @settings(**PROPS)
    @given(tracked_case())
    def test_a_cut_week_ends_at_or_above_its_floor(self, case):
        inputs, snap = case
        plan = pe.build_plan(inputs, snap, what_if_runs=False)
        weeks, ctx = plan["_check"]["weeks"], plan["_check"]["ctx"]
        cut = {c["week"] for c in plan["acwr"]["cuts"]}
        for w in weeks:
            if w.number in cut:
                self.assertGreaterEqual(pe._planned(w), pe._acwr_floor(w, ctx) - pe.EPS_M, w.start)

    @settings(**PROPS)
    @given(tracked_case())
    def test_the_cap_is_deterministic(self, case):
        inputs, snap = case
        a = pe.plan_answer(pe.build_plan(inputs, snap, what_if_runs=False))
        b = pe.plan_answer(pe.build_plan(dataclasses.replace(inputs), dataclasses.replace(snap), what_if_runs=False))
        self.assertEqual(a["content_hash"], b["content_hash"])
        self.assertEqual(a["acwr"], b["acwr"])


# --- travel-gap plans: a travel gap, a B race, easy overrides (a real-data test of 2026-11-26) ------------------

@st.composite
def owner_like_case(draw):
    """Travel-gap inputs: 4 run days with the long run on one, a marathon in 10-16 weeks with a B race, a travel
    gap (blocked dates, often a week, and sometimes a gap in the recorded history too), a load model that
    tracks Garmin, and 0-3 easy overrides drawn from the plan's own sessions (the property draws them)."""
    today = TODAY + dt.timedelta(days=draw(st.integers(0, 6)))
    weeks = draw(st.integers(10, 16))
    first = pe.week1_start(today)
    race = first + dt.timedelta(days=7 * (weeks - 1) + draw(st.integers(4, 6)))
    run_days = tuple(sorted(draw(st.sets(st.integers(0, 6), min_size=4, max_size=5))))
    rest = tuple(sorted(set(range(7)) - set(run_days)))
    long_day = draw(st.one_of(st.sampled_from(run_days), st.integers(0, 6)))   # a rest day too (rule 6)
    km0 = draw(st.floats(30.0, 60.0))
    history = [km0 * draw(st.floats(0.85, 1.15)) for _ in range(8)]
    if draw(st.booleans()):   # a week without runs in the history (travel before today)
        history[draw(st.integers(0, 3))] = 0.0
    km_weeks = tuple(pe.Meters(k * 1000.0) for k in history)
    gap_start = first + dt.timedelta(days=draw(st.integers(0, 7 * (weeks - 3))))
    blocked = tuple(gap_start + dt.timedelta(days=i) for i in range(draw(st.integers(3, 10))))
    b_day = first + dt.timedelta(days=draw(st.integers(14, 7 * (weeks - 2))))
    b_races = (pe.BRace(b_day, draw(st.sampled_from(("tri_half", "run", "tri_full")))),) if draw(st.booleans()) else ()
    acts, garmin = pe.sample_loads(today, km_weeks, garmin_gap=0.0, run_lpm=draw(st.floats(1.2, 2.0)),
                                   bike_min=draw(st.sampled_from((0, 90))), swim_min=draw(st.sampled_from((0, 45))))
    snap = pe.Snapshot(today=today, week_m=km_weeks, week_runs=(4,) * 8,
                       longest_run_8w_m=pe.Meters(draw(st.floats(15000, 28000))),
                       lt_speed_mps=pe.MetersPerSecond(draw(st.floats(3.5, 4.5))),
                       prediction_s=(("marathon", 3 * 3600 + 45 * 60),), activity_load=acts, garmin_load=garmin)
    inputs = pe.PlanInputs(race_date=race, b_races=b_races, run_days=run_days, rest_days=rest, long_run_day=long_day,
                           blocked_dates=blocked)
    return inputs, snap


def _times(s):
    """(quality seconds, total seconds) of a session from its answer steps, by the middle of each pace band."""
    def walk(steps, times):
        hard = total = 0.0
        for st_ in steps:
            if st_["type"] == "repeat":
                h, t = walk(st_["steps"], times * st_["times"])
            else:
                if "time_s" in st_:
                    t = float(st_["time_s"])
                else:
                    sp = st_.get("speed_mps")
                    t = float(st_["distance_m"]) / ((sp[0] + sp[1]) / 2) if sp else float(st_["distance_m"])
                h = t if st_["type"] == "interval" and s["role"] in pe.QUALITY_ROLES else 0.0
            hard += h * times
            total += t * times
        return hard, total
    return walk(s["steps"], 1)


@unittest.skipUnless(HAVE_HYPOTHESIS, NEEDS_HYPOTHESIS)
class OwnerLikeProperties(unittest.TestCase):
    """Every invariant the plan reports ok holds when recomputed from the final plan: the planned ratio at or
    under 1.3 on every day a planned run can lower (else reported held, with nothing left to cut), 80% of each
    week's time easy, an easy run kept in every week that had one before the cuts; and easing a session changes
    nothing else."""

    @settings(**PROPS)
    @given(owner_like_case(), st.data())
    def test_reported_ok_invariants_hold_when_recomputed(self, case, data):
        inputs, snap = case
        plan0 = pe.build_plan(inputs, snap, what_if_runs=False)
        self.assertTrue(plan0["acwr"]["enforced"])
        keys = [s["key"] for w in plan0["weeks"] for s in w["sessions"]
                if s["role"] in pe.QUALITY_ROLES + ("long",) and s["date"] > snap.today.isoformat()]
        eased = tuple(sorted(set(data.draw(st.lists(st.sampled_from(keys), max_size=3)) if keys else [])))
        plan = pe.build_plan(dataclasses.replace(inputs, overrides=tuple(pe.Override("easy", key=k) for k in eased)),
                             snap, what_if_runs=False)
        applied = {o["key"] for o in plan["overrides"]["applied"]}
        inv = {i["id"]: i for i in pe.check_invariants(plan)}
        self.assertTrue(plan["invariants"]["ok"], plan["invariants"]["failed"])

        # reduce-only overrides: every other session as in the plan without them, the same cuts
        before = {s["key"]: s for w in plan0["weeks"] for s in w["sessions"]}
        after = {s["key"]: s for w in plan["weeks"] for s in w["sessions"]}
        self.assertEqual(set(before), set(after))
        for key, s in before.items():
            if key not in applied:
                self.assertEqual(after[key], s, key)
        self.assertEqual(plan["acwr"]["cuts"], plan0["acwr"]["cuts"])
        self.assertEqual(plan["acwr"]["held"], plan0["acwr"]["held"])

        # acwr_cap: recomputed on the plan as the cap counts it (the plan before the easy overrides)
        self.assertTrue(inv["acwr_cap"]["ok"] and not inv["acwr_cap"].get("not_enforced"))
        ctx = plan0["_check"]["ctx"]
        loads = dict(ctx.load.actual)
        for d, v in ctx.load.fixed.items():
            loads[d] = loads.get(d, 0.0) + v
        runs = set()
        for w in plan0["_check"]["weeks"]:
            for s in w.sessions:
                if s.date > snap.today:
                    loads[s.date] = loads.get(s.date, 0.0) + pe.session_load(s, ctx)
                    if s.counts:
                        runs.add(s.date)
        held = {h["date"] for h in plan["acwr"]["held"]}
        day = snap.today + dt.timedelta(days=1)
        while day < inputs.race_date:
            acute = sum(loads.get(day - dt.timedelta(days=i), 0.0) for i in range(7))
            chronic = sum(loads.get(day - dt.timedelta(days=i), 0.0) for i in range(28)) / 4
            if chronic > 0 and any(day - dt.timedelta(days=i) in runs for i in range(7)) \
                    and day.isoformat() not in held:
                self.assertLessEqual(acute / chronic, 1.3 + 1e-9, day)
            day += dt.timedelta(days=1)

        # easy_share: 80% of each week's time easy, and an easy run wherever the plan without the cap had one
        self.assertTrue(inv["easy_share"]["ok"])
        uncapped = pe.build_plan(inputs, snap, dict(pe.RULES, acwr=dict(pe.RULES["acwr"], max=99.0)),
                                 what_if_runs=False)
        # (a week an easy override took under 80% easy warns override_relaxed instead: easy_share counts it as
        # built, before the override)
        relaxed = {x["dates"][0] for x in plan["warnings"] if x["id"] == "override_relaxed" and x["dates"]}
        for week, loose in zip(plan["weeks"], uncapped["weeks"]):
            if week["start"] in relaxed:
                continue
            counted = [s for s in week["sessions"] if s["role"] not in ("race", "b_race")]
            hard = sum(_times(s)[0] for s in counted)
            total = sum(_times(s)[1] for s in counted)
            if total > 0:
                self.assertLessEqual(hard / total, 0.2 + 1e-3, week["start"])
            # an easy run where the plan without the cap had one, unless rule 4 exempts this week (no run day
            # left or no room in its target beside the quality and long km; easy_run_due): a week the cap made
            # smaller may lose the room
            if any(s["role"] in ("easy", "strides") for s in loose["sessions"]) and easy_run_due(plan, week["start"]):
                self.assertTrue(any(s["role"] in ("easy", "strides") for s in week["sessions"]), week["start"])


# DEEP_EXAMPLES (as test_deep_engine_properties reads it) raises the pace properties' examples
PACE_PROPS = dict(PROPS, max_examples=max(150, int(os.environ.get("DEEP_EXAMPLES") or 0)))


@st.composite
def pace_case(draw):
    """An athlete with an LT speed and Garmin predictions in a plausible relation to it (the 5K pace 0.98-1.12
    x LT, the 10K 0.93-0.99 x the 5K pace, the half and marathon anywhere from 0.70 to 1.0 x LT), and a goal or
    none."""
    inputs, snap = draw(plan_case())
    lt = draw(st.floats(2.9, 5.2))
    v5k = lt * draw(st.floats(0.98, 1.12))
    preds = [("5k", int(round(5000 / v5k)))]
    if draw(st.booleans()):
        preds.append(("10k", int(round(10000 / (v5k * draw(st.floats(0.93, 0.99)))))))
    for name, dist in (("half", 21097.5), ("marathon", 42195.0)):
        if draw(st.booleans()):
            preds.append((name, int(round(dist / (lt * draw(st.floats(0.70, 1.0)))))))
    if draw(st.booleans()):
        preds = preds[1:] if len(preds) > 1 and preds[1][0] == "10k" else preds   # sometimes the 10K alone
    snap = dataclasses.replace(snap, lt_speed_mps=pe.MetersPerSecond(lt), prediction_s=tuple(preds))
    return inputs, snap


@unittest.skipUnless(HAVE_HYPOTHESIS, NEEDS_HYPOTHESIS)
class PaceProperties(unittest.TestCase):
    """Marathon pace, the anchored bands and the half's projection (a real-data test, 2026-11-26)."""

    @settings(**PACE_PROPS)
    @given(pace_case())
    def test_mp_is_never_faster_than_feasibility_accepts(self, case):
        inputs, snap = case
        warnings = []
        mp = pe.marathon_pace(inputs, snap, pe.RULES, warnings)
        dist = float(pe.DISTANCES[inputs.race_distance])
        if mp is None:
            self.assertFalse(inputs.goal_time_s or snap.prediction(inputs.race_distance))
            return
        accept = pe.accepted_speed(inputs, snap, pe.RULES)
        self.assertIsNotNone(accept)
        if not inputs.goal_time_s:
            self.assertLessEqual(mp, accept + 1e-9)
            # the finish time MP implies is a goal feasibility does not fail
            goal = int(math.ceil(dist / mp))
            f = pe.feasibility(dataclasses.replace(inputs, goal_time_s=goal), snap, None, None, None, None)
            self.assertNotEqual(f["verdict"], "unrealistic", f["checks"])
            self.assertTrue(all(c["ok"] for c in f["checks"] if c["id"] == "goal_vs_prediction"), f["checks"])
        else:
            goal_speed = dist / inputs.goal_time_s
            self.assertLessEqual(mp, max(goal_speed, accept) + 1e-9)
            if mp > accept + 1e-9:
                self.assertIn("goal_beyond_feasible", [w["id"] for w in warnings])

    @settings(**PACE_PROPS)
    @given(pace_case())
    def test_intervals_are_never_faster_than_the_3k_equivalent_of_the_5k(self, case):
        inputs, snap = case
        plan = pe.build_plan(inputs, snap, what_if_runs=False)
        self.assertTrue(plan["invariants"]["ok"], plan["invariants"]["failed"])
        paces = plan["_check"]["ctx"].paces
        v5k = pe.prediction_speed(snap, "5k", pe.RULES)
        intervals, threshold = paces.band("intervals"), paces.band("threshold")
        assume(intervals is not None and threshold is not None and v5k is not None)
        # rule 5: never faster than the 3K equivalent of the 5K pace, whatever the LT says
        self.assertLessEqual(intervals[1], v5k * (5 / 3) ** 0.06 + 0.001)
        self.assertGreaterEqual(intervals[0], v5k - 0.001)
        self.assertGreaterEqual(intervals[0], threshold[1] - 0.001)   # intervals above the threshold band

    @settings(**PACE_PROPS)
    @given(pace_case())
    def test_threshold_is_never_faster_than_the_10k_pace(self, case):
        inputs, snap = case
        plan = pe.build_plan(inputs, snap, what_if_runs=False)
        self.assertTrue(plan["invariants"]["ok"], plan["invariants"]["failed"])
        threshold = plan["_check"]["ctx"].paces.band("threshold")
        v5k = pe.prediction_speed(snap, "5k", pe.RULES)
        assume(threshold is not None and v5k is not None)
        v10k = min(pe.prediction_speed(snap, "10k", pe.RULES) or v5k, v5k)
        self.assertLessEqual(threshold[1], v10k + 0.001)
        self.assertLess(threshold[0], threshold[1])
        # the 0.91 x LT floor yields to the 10K bound only with a warning
        if threshold[0] < 0.91 * snap.lt_speed_mps - 0.001:
            self.assertIn("threshold_anchor_conflict", [w["id"] for w in plan["warnings"]])
        else:
            self.assertGreater((threshold[0] + threshold[1]) / 2, 0.90 * snap.lt_speed_mps)   # still hard daily

    @settings(**PACE_PROPS)
    @given(pace_case())
    def test_a_goal_feasibility_accepts_is_inside_the_race_pace_band(self, case):
        inputs, snap = case
        assume(inputs.goal_time_s)
        dist = float(pe.DISTANCES[inputs.race_distance])
        goal = dist / inputs.goal_time_s
        lo = float(pe.by_distance(pe.RULES, inputs.race_distance, "mp_of_lt_sanity")[0])
        accept = pe.accepted_speed(inputs, snap, pe.RULES)
        assume(accept is not None and lo * snap.lt_speed_mps <= goal <= accept)
        plan = pe.build_plan(inputs, snap, what_if_runs=False)
        band = plan["_check"]["ctx"].paces.band("mp")
        assume(band is not None)
        self.assertLessEqual(band[0], goal + 0.001)
        self.assertGreaterEqual(band[1], goal - 0.001)
        race = [s for w in plan["weeks"] for s in w["sessions"] if s["role"] == "race"]
        self.assertTrue(race and race[0]["steps"][0]["speed_mps"] == [band[0], band[1]])

    @settings(**PACE_PROPS)
    @given(pace_case(), st.sampled_from((0.0, 0.03, 0.10)))
    def test_easy_and_long_stay_well_slower_than_mp(self, case, slowdown):
        inputs, snap = case
        plan = pe.build_plan(dataclasses.replace(inputs, easy_band_slowdown=slowdown), snap, what_if_runs=False)
        self.assertTrue(plan["invariants"]["ok"], plan["invariants"]["failed"])
        ctx = plan["_check"]["ctx"]
        lt = snap.lt_speed_mps
        for name, gap in (("easy", 30), ("long", 20)):
            band = ctx.paces.band(name)
            assume(band is not None)
            self.assertLess(band[0], band[1])
            self.assertLess((band[0] + band[1]) / 2, 0.90 * lt)   # get_daily_status keeps easy runs easy
            if ctx.paces.mp:
                self.assertGreaterEqual(1000.0 / band[1], 1000.0 / ctx.paces.mp + gap - 0.5, name)
            v_m = pe.prediction_speed(snap, "marathon", pe.RULES)
            if v_m is not None:   # anchored: at most the top fraction of the marathon speed (never above it)
                top = float(pe.RULES["pace_anchors"][f"{name}_of_marathon"][1])
                self.assertLessEqual(band[1], min(v_m, 0.93 * lt) * top + 0.002, name)
        # the answer's bands (the slowdown applied) are never faster than the plan's
        answer = plan["weeks"]
        for w in answer:
            for s in w["sessions"]:
                if s["role"] == "easy" and s["steps"] and "speed_mps" in s["steps"][0]:
                    self.assertLessEqual(s["steps"][0]["speed_mps"][1], ctx.paces.band("easy")[1] + 0.001)

    @settings(**PACE_PROPS)
    @given(plan_case())
    def test_a_half_never_plans_more_than_a_marathon(self, case):
        inputs, snap = case
        # the ACWR cap cuts a marathon's bigger weeks harder (a half week under it may then be the larger)
        snap = dataclasses.replace(snap, garmin_load=())
        half = pe.build_plan(dataclasses.replace(inputs, race_distance="half", goal_time_s=None), snap,
                             what_if_runs=False)
        full = pe.build_plan(dataclasses.replace(inputs, race_distance="marathon", goal_time_s=None), snap,
                             what_if_runs=False)
        self.assertTrue(half["invariants"]["ok"], half["invariants"]["failed"])
        hs, fs = half["summary"], full["summary"]
        # the caps themselves: a half's ceiling and long-run cap are never above a marathon's
        ceiling = pe.peak_ceiling(snap, half["_check"]["ctx"].km0, pe.RULES, "half")[0]
        self.assertLessEqual(ceiling, pe.peak_ceiling(snap, full["_check"]["ctx"].km0, pe.RULES)[0])
        self.assertLessEqual(hs["peak_km"] or 0, pe.km(ceiling) + 0.1)
        self.assertLessEqual(pe.max_long_m(half["_check"]["ctx"]), pe.max_long_m(full["_check"]["ctx"]))
        # the plans: weeks grow from what was placed, so a marathon week the easy minimum held below its target
        # (a 24.1 km long run leaving 3.9 km, under the 4 km easy minimum) grows slower: compare the others.
        # 2%: the race pace of the quality sessions differs, so the 80%-easy check may place a week a little
        # differently
        assume(all(w["planned_km"] >= w["target_km"] - 0.5 for w in full["weeks"] if w["kind"] == "normal"))
        # peak_km is the largest planned week: a long run held at the B race cap leaves its km unplaced (rule:
        # not made up), and a marathon's longer long run loses more of the week to it than a half's, whose
        # shorter one left room for an easy run first (a half 17.5 + 4.0 km, a marathon 17.5 km)
        assume(not any(x.get("id") == "b_race_capped" and "held at" in x.get("detail", "")
                       for x in full["warnings"] if isinstance(x, dict)))
        weeks = [[(w["kind"], w["target_km"], w["planned_km"]) for w in p["weeks"]] for p in (half, full)]
        self.assertLessEqual(hs["peak_km"] or 0, (fs["peak_km"] or 0) * 1.02 + 0.1, weeks)
        self.assertLessEqual(hs["peak_long_km"] or 0, (fs["peak_long_km"] or 0) * 1.02 + 0.1, weeks)
        self.assertLessEqual(hs["peak_long_km"] or 0, 24.0)


if __name__ == "__main__":
    unittest.main()
