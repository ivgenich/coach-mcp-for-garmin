"""Property-based tests (Hypothesis) for plan_engine:

    python3 -m unittest discover -s ext -p "test_properties_plan_engine.py" -v

Skipped ("needs hypothesis") where hypothesis is not installed. Runs are derandomized and keep no example
database, so CI sees the same examples every time. The properties: no invariant fails for any input the
engine accepts, the same inputs give the same content_hash, the plan grows with the starting volume, a
blocked date never raises another day's km, and the coach critic's overrides never raise a week's km or a
long run. Rules v2: with recorded loads (tracked by Garmin's ratio or not) every invariant still holds, and the
planned-ratio cap never raises a week, a session or a long run and is deterministic. Starting volumes reach 100 km a week and run days all seven, so the easy-run cap and the peak
ceiling bind.
"""

import dataclasses
import datetime as dt
import math
import os
import unittest

import plan_engine as pe

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
    long_day = draw(st.sampled_from(run_days))
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
        for wa, wb in zip(a["weeks"], b["weeks"]):
            self.assertEqual(wa["target_km"], wb["target_km"], wa["start"])
            self.assertLessEqual(wa["planned_km"], wb["planned_km"] + 0.05, wa["start"])
            self.assertLessEqual(wa["long_km"] or 0, wb["long_km"] or 0, wa["start"])
            free_km = {s["key"]: s["km"] for s in wb["sessions"]}
            for s in wa["sessions"]:
                self.assertLessEqual(s["km"], free_km.get(s["key"], 0.0) + 0.05, s["key"])

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
    """Marathon pace, the anchored bands and the half's projection (the owner's real-data test, 2026-11-26)."""

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
        # never faster than the 3K equivalent of the 5K pace, unless the threshold band's floor (0.91 x LT, so
        # get_daily_status still calls it hard) is above the 5K pace (a 10K far slower than LT): then of that
        floor = max(v5k, threshold[1])
        self.assertLessEqual(intervals[1], floor * (5 / 3) ** 0.06 + 0.001)
        if v5k >= 0.97 * snap.lt_speed_mps:
            self.assertLessEqual(intervals[1], v5k * (5 / 3) ** 0.06 + 0.001)
        self.assertGreaterEqual(intervals[0], v5k - 0.001)
        self.assertGreaterEqual(intervals[0], threshold[1] - 0.001)   # intervals above the threshold band
        self.assertGreater((threshold[0] + threshold[1]) / 2, 0.90 * snap.lt_speed_mps)   # still hard daily

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
        weeks = [[(w["kind"], w["target_km"], w["planned_km"]) for w in p["weeks"]] for p in (half, full)]
        self.assertLessEqual(hs["peak_km"] or 0, (fs["peak_km"] or 0) * 1.02 + 0.1, weeks)
        self.assertLessEqual(hs["peak_long_km"] or 0, (fs["peak_long_km"] or 0) * 1.02 + 0.1, weeks)
        self.assertLessEqual(hs["peak_long_km"] or 0, 24.0)


if __name__ == "__main__":
    unittest.main()
