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


# --- owner-like plans: a travel gap, a B race, easy overrides (the owner's test of 2026-11-26) -------------------

@st.composite
def owner_like_case(draw):
    """Owner-like inputs: 4 run days with the long run on one, a marathon in 10-16 weeks with a B race, a travel
    gap (blocked dates, often a week, and sometimes a gap in the recorded history too), a load model that
    tracks Garmin, and 0-3 easy overrides drawn from the plan's own sessions (the property draws them)."""
    today = TODAY + dt.timedelta(days=draw(st.integers(0, 6)))
    weeks = draw(st.integers(10, 16))
    first = pe.week1_start(today)
    race = first + dt.timedelta(days=7 * (weeks - 1) + draw(st.integers(4, 6)))
    run_days = tuple(sorted(draw(st.sets(st.integers(0, 6), min_size=4, max_size=5))))
    rest = tuple(sorted(set(range(7)) - set(run_days)))
    long_day = draw(st.sampled_from(run_days))
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
                       prediction_s=(("marathon", 3 * 3600 + 30 * 60),), activity_load=acts, garmin_load=garmin)
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
        for week, loose in zip(plan["weeks"], uncapped["weeks"]):
            counted = [s for s in week["sessions"] if s["role"] not in ("race", "b_race")]
            hard = sum(_times(s)[0] for s in counted)
            total = sum(_times(s)[1] for s in counted)
            if total > 0:
                self.assertLessEqual(hard / total, 0.2 + 1e-3, week["start"])
            if any(s["role"] in ("easy", "strides") for s in loose["sessions"]):
                self.assertTrue(any(s["role"] in ("easy", "strides") for s in week["sessions"]), week["start"])


if __name__ == "__main__":
    unittest.main()
