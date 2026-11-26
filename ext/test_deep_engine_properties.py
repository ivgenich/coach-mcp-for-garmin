"""Deep test phase: randomized plans, overrides and weekly reviews (Hypothesis), checked by the engine's own
invariants and by test_deep_engine_matrix's independent oracle.

    cd ext && python3 -m unittest test_deep_engine_properties -v
    DEEP_EXAMPLES=20000 python3 -m unittest test_deep_engine_properties   # the heavy run

Skipped ("needs hypothesis") where hypothesis is not installed. Derandomized, no example database, so every
run sees the same examples; the default (DEEP_EXAMPLES unset) is a fast run. Cases cover every profile of the
matrix, 1-24 weeks to race, any race and today weekday across a year (the year boundary, DST and the 2028
leap day included), 1-7 run days with overlapping rest days, B races of every kind anywhere (also before week 1
and in the race week), blocked dates (single days and whole weeks), fixed calendar sessions of every sport,
recorded loads that track Garmin or not, and the easy-band slowdown.

One known defect is filtered out, not hidden: test_deep_engine_matrix's expected failure
test_a_long_run_moved_into_the_last_21_days_keeps_80_percent_easy (an easy_share failure in a week whose long run
moved off its day into the last 21 days). _known_trim_bug() matches exactly that and nothing else.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import os
import unittest
from typing import Any, Dict, List, Tuple

import plan_engine as pe
import plan_review as pr
from test_deep_engine_matrix import PROFILES, check_plan, snapshot

try:
    from hypothesis import HealthCheck, assume, given, settings
    from hypothesis import strategies as st
    HAVE_HYPOTHESIS = True
except ImportError:  # the classes below are skipped; the shim only lets the module load
    HAVE_HYPOTHESIS = False

    class _Absent:
        """Stands in for hypothesis.strategies: every attribute, call and | is itself."""

        def __getattr__(self, name: str) -> "_Absent":
            return self

        def __call__(self, *args: Any, **kwargs: Any) -> "_Absent":
            return self

        def __or__(self, other: Any) -> "_Absent":
            return self

    st = HealthCheck = _Absent()

    def given(*args: Any, **kwargs: Any) -> Any:
        def keep(fn: Any) -> Any:
            return fn
        return keep

    settings = given

    def assume(condition: Any) -> bool:
        return True

NEEDS_HYPOTHESIS = "needs hypothesis"
EXAMPLES = int(os.environ.get("DEEP_EXAMPLES") or 0)
PROPS = dict(deadline=None, derandomize=True, database=None,
             suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much, HealthCheck.data_too_large])
D = dt.date
BASE_DAY = D(2026, 11, 23)


def props(default: int) -> Dict[str, Any]:
    return dict(PROPS, max_examples=EXAMPLES or default)


@st.composite
def plan_case(draw: Any) -> Tuple[pe.PlanInputs, pe.Snapshot]:
    today = BASE_DAY + dt.timedelta(days=draw(st.integers(0, 830)))   # to 2029-03: two year ends, a leap day
    weeks = draw(st.integers(1, 24))
    first = pe.week1_start(today)
    race = first + dt.timedelta(days=7 * (weeks - 1) + draw(st.integers(0, 6)))
    assume(race > today)
    prof = PROFILES[draw(st.sampled_from(sorted(PROFILES)))]
    scale = draw(st.sampled_from((0.5, 1.0, 1.0, 1.5)))
    prof = dataclasses.replace(prof, weeks_km=tuple(k * scale for k in prof.weeks_km))
    assume(sum(prof.weeks_km[-4:]) > 0)
    run_days = tuple(sorted(draw(st.sets(st.integers(0, 6), min_size=1, max_size=7))))
    rest = tuple(sorted(draw(st.sets(st.integers(0, 6), max_size=4))))
    assume(set(run_days) - set(rest))
    long_day = draw(st.integers(0, 6))
    blocked: Tuple[dt.date, ...] = tuple(first + dt.timedelta(days=draw(st.integers(-3, 7 * weeks)))
                                         for _ in range(draw(st.integers(0, 5))))
    if draw(st.integers(0, 9)) == 0:
        wk = first + dt.timedelta(days=7 * draw(st.integers(0, weeks - 1)))
        blocked += tuple(wk + dt.timedelta(days=i) for i in range(7))
    b_races = tuple(pe.BRace(first + dt.timedelta(days=draw(st.integers(-4, 7 * weeks + 1))),
                             draw(st.sampled_from(pe.B_RACE_KINDS))) for _ in range(draw(st.integers(0, 2))))
    fixed = tuple(pe.FixedSession(today + dt.timedelta(days=draw(st.integers(0, 7 * weeks + 7))),
                                  draw(st.sampled_from(("run", "bike", "swim", "strength"))), draw(st.booleans()),
                                  draw(st.booleans()), draw(st.sampled_from((None, 30.0, 90.0, 300.0))))
                  for _ in range(draw(st.sampled_from((0, 0, 2, 6, 14)))))
    loads = draw(st.sampled_from((None, None, {"garmin_gap": 0.0}, {"garmin_gap": 0.1}, {"garmin_gap": 0.3},
                                  {"garmin_gap": 0.0, "bike_min": 300.0, "run_lpm": 2.5})))
    snap = snapshot(today, prof, loads=loads, fixed=fixed,
                    predictions=(("marathon", draw(st.integers(9000, 20000))), ("half", draw(st.integers(4000, 10000)))))
    dist = draw(st.sampled_from(("marathon", "half")))
    goal = draw(st.one_of(st.none(), st.integers(3600, 8 * 3600)))
    inputs = pe.PlanInputs(race_date=race, race_distance=dist, goal_time_s=goal, b_races=b_races, run_days=run_days,
                           rest_days=rest, long_run_day=long_day, quality_per_week=draw(st.integers(0, 2)),
                           max_long_km=draw(st.sampled_from((10.0, 14.0, 21.0, 28.0, 32.0))), blocked_dates=blocked,
                           long_share=draw(st.sampled_from((0.2, 0.35, 0.5, 0.6))),
                           easy_band_slowdown=draw(st.sampled_from((0.0, 0.0, 0.03, 0.1))))
    return inputs, snap


def _trim_weeks(inputs: pe.PlanInputs, plan: Dict[str, Any], numbers: List[int]) -> bool:
    """True when each week numbered in `numbers` has its long run moved off the long-run day into the last 21
    days, from a day 21 or more days out (the known defect, test_deep_engine_matrix: the trim after placement)."""
    weeks = plan["_check"]["weeks"]
    for n in numbers:
        w = next(x for x in weeks if x.number == n)
        longs = [s for s in w.sessions if s.role == "long"]
        pref = w.start + dt.timedelta(days=inputs.long_run_day)
        if not longs or longs[0].date == pref or (inputs.race_date - longs[0].date).days >= 21 \
                or (inputs.race_date - pref).days < 21:
            return False
    return bool(numbers)


def _known_trim_bug(inputs: pe.PlanInputs, plan: Dict[str, Any]) -> bool:
    """The only invariant that fails is easy_share, in weeks of the known defect."""
    failed = plan["invariants"]["failed"]
    if not failed or any(f["id"] != "easy_share" for f in failed):
        return False
    return _trim_weeks(inputs, plan, [int(p.split(":")[0].split()[1]) for p in failed[0]["detail"].split("; ")])


def _sound(test: unittest.TestCase, inputs: pe.PlanInputs, snap: pe.Snapshot, plan: Dict[str, Any]) -> None:
    if _known_trim_bug(inputs, plan):
        assume(False)
    test.assertTrue(plan["invariants"]["ok"], plan["invariants"]["failed"])
    problems = check_plan(inputs, snap, plan)
    easy = [p for p in problems if p.startswith("week ") and p.endswith("% easy")]
    if easy and len(easy) == len(problems) and \
            _trim_weeks(inputs, plan, [int(p.split(":")[0].split()[1]) for p in easy]):
        assume(False)
    test.assertEqual(problems, [])


@st.composite
def overrides_for(draw: Any, plan: Dict[str, Any]) -> Tuple[pe.Override, ...]:
    a = pe.plan_answer(plan)
    sessions = [s for w in a["weeks"] for s in w["sessions"] if s["role"] not in ("race", "b_race")]
    starts = [D.fromisoformat(w["start"]) for w in a["weeks"]]
    out: List[pe.Override] = []
    if not sessions:
        return ()
    for _ in range(draw(st.integers(1, 5))):
        s = draw(st.sampled_from(sessions))
        day = D.fromisoformat(s["date"])
        near = day + dt.timedelta(days=draw(st.integers(-6, 6)))
        kind = draw(st.sampled_from(pe.OVERRIDE_KINDS))
        if kind == "move":
            out.append(pe.Override("move", key=s["key"], date=near))
        elif kind == "swap":
            out.append(pe.Override("swap", date=day, date_b=near))
        elif kind == "rest":
            out.append(pe.Override("rest", date=draw(st.sampled_from((day, near)))))
        elif kind == "easy":
            out.append(draw(st.sampled_from((pe.Override("easy", key=s["key"]), pe.Override("easy", date=day)))))
        elif kind == "cap_km":
            out.append(pe.Override("cap_km", key=s["key"], km=draw(st.sampled_from((3.0, 4.0, s["km"] - 1.0,
                                                                                     s["km"], s["km"] + 1.0)))))
        elif kind == "cap_week_km":
            out.append(pe.Override("cap_week_km", week_start=draw(st.sampled_from(starts)),
                                   km=draw(st.floats(-5.0, 150.0, allow_nan=False))))
        elif kind == "cap_long":
            out.append(draw(st.sampled_from((pe.Override("cap_long", date=day, km=s["km"] - 1.0),
                                             pe.Override("cap_long", key=s["key"], km=s["km"] + 1.0),
                                             pe.Override("cap_long", date=day, km=3.0),
                                             pe.Override("cap_long", date=near, km=s["km"])))))
        else:
            out.append(pe.Override("long_run_day", week_start=draw(st.sampled_from(starts)),
                                   weekday=draw(st.integers(0, 6))))
    return tuple(out)


@unittest.skipUnless(HAVE_HYPOTHESIS, NEEDS_HYPOTHESIS)
class EngineProperties(unittest.TestCase):
    @settings(**props(500))
    @given(plan_case())
    def test_every_rule_holds_and_the_plan_is_deterministic(self, case: Tuple[pe.PlanInputs, pe.Snapshot]) -> None:
        inputs, snap = case
        try:
            plan = pe.build_plan(inputs, snap, what_if_runs=False)
        except pe.PlanError as exc:   # only the documented refusal reaches here: the race this week
            self.assertEqual(str(exc), "race_date must be after the end of this week")
            return
        _sound(self, inputs, snap, plan)
        again = pe.build_plan(dataclasses.replace(inputs), dataclasses.replace(snap), what_if_runs=False)
        self.assertEqual(pe.plan_answer(plan), pe.plan_answer(again))
        a = pe.plan_answer(plan)
        self.assertEqual(a["content_hash"], pe.content_hash(pe.RULES_VERSION, a["inputs"], a["weeks"]))
        if a["acwr"]["enforced"]:
            weeks, ctx = plan["_check"]["weeks"], plan["_check"]["ctx"]
            for c in a["acwr"]["cuts"]:
                w = next(x for x in weeks if x.number == c["week"])
                self.assertGreaterEqual(pe._planned(w), pe._acwr_floor(w, ctx) - pe.EPS_M)
                self.assertLess(c["to_km"], c["from_km"])
        else:
            self.assertEqual(a["acwr"]["cuts"], [])

    @settings(**props(250))
    @given(plan_case(), st.data())
    def test_overrides_never_raise_load_and_keep_every_rule(self, case: Tuple[pe.PlanInputs, pe.Snapshot],
                                                            data: Any) -> None:
        inputs, snap = case
        try:
            base = pe.build_plan(inputs, snap, what_if_runs=False)
        except pe.PlanError:
            return
        assume(base["invariants"]["ok"])
        overrides = data.draw(overrides_for(base))
        assume(overrides)
        edited_inputs = dataclasses.replace(inputs, overrides=overrides)
        edited = pe.build_plan(edited_inputs, snap, what_if_runs=False)
        a, b = pe.plan_answer(base), pe.plan_answer(edited)
        self.assertEqual(len(b["overrides"]["applied"]) + len(b["overrides"]["refused"]), len(overrides))
        self.assertTrue(edited["invariants"]["ok"], edited["invariants"]["failed"])
        problems = [p for p in check_plan(edited_inputs, snap, edited)
                    # the known defect (test_no_override_puts_a_run_after_the_a_race): a moved run after the race
                    if "on or after the A race" not in p
                    # an edit may drop or ease the peak long run itself; the 0.80 limit keeps the peak long run
                    # placed before any override (plan_engine Context.peak_long), the oracle recounts it
                    and ": late long " not in p]
        self.assertEqual(problems, [])
        km0 = {s["key"]: s["km"] for w in a["weeks"] for s in w["sessions"]}
        for wa, wb in zip(a["weeks"], b["weeks"]):
            self.assertLessEqual(wb["planned_km"], wa["planned_km"] + 0.05, wb["start"])
            self.assertLessEqual(wb["long_km"] or 0.0, wa["long_km"] or 0.0, wb["start"])
            self.assertLessEqual(sum(1 for s in wb["sessions"] if s["role"] in pe.QUALITY_ROLES),
                                 sum(1 for s in wa["sessions"] if s["role"] in pe.QUALITY_ROLES))
            for s in wb["sessions"]:
                self.assertLessEqual(s["km"], km0.get(s["key"], 0.0) + 0.05, s["key"])
        for r in b["overrides"]["refused"]:
            self.assertTrue(r["rule"])

    @settings(**props(60))
    @given(plan_case())
    def test_what_if_variants_keep_every_rule(self, case: Tuple[pe.PlanInputs, pe.Snapshot]) -> None:
        inputs, snap = case
        try:
            plan = pe.build_plan(inputs, snap)
        except pe.PlanError:
            return
        names = [v["variant"] for v in plan["what_if"]]
        self.assertEqual(names[-2:], ["long_share_0.35", "long_share_0.45"])
        for v in plan["what_if"]:
            if v["invariants_ok"]:
                continue
            goal = inputs.goal_time_s
            delta = {"goal_minus_5min": -300, "goal_plus_5min": 300}.get(v["variant"], 0)
            variant = dataclasses.replace(inputs, long_share=v["long_share"],
                                          goal_time_s=goal + delta if goal is not None else None)
            self.assertTrue(_known_trim_bug(variant, pe.build_plan(variant, snap, what_if_runs=False)), v)


# --- weekly reviews on engine-built plans ---------------------------------------------------------------------------

_STORED: Dict[Tuple[str, int, int], Dict[str, Any]] = {}


def stored(profile: str, built_offset: int, variant: int) -> Dict[str, Any]:
    key = (profile, built_offset, variant)
    if key not in _STORED:
        built = D(2026, 11, 25) + dt.timedelta(days=built_offset)
        inputs = [pe.PlanInputs(race_date=D(2027, 3, 21), goal_time_s=12600,
                                b_races=(pe.BRace(D(2027, 1, 16), "tri_half"),)),
                  pe.PlanInputs(race_date=D(2027, 3, 6), race_distance="half", run_days=(1, 3, 5, 6),
                                rest_days=(0, 2, 4), quality_per_week=1),
                  pe.PlanInputs(race_date=D(2027, 2, 28), run_days=tuple(range(7)), rest_days=(),
                                blocked_dates=(D(2026, 12, 25), D(2027, 1, 1)))][variant]
        snap = snapshot(built, PROFILES[profile], loads={} if variant != 1 else None)
        _STORED[key] = pe.plan_answer(pe.build_plan(inputs, snap, what_if_runs=False))
    return _STORED[key]


STATUSES = (None, "done", "done_early", "done_late", "partial", "partial_late", "missed", "replaced")


@st.composite
def review_case(draw: Any) -> Tuple[Dict[str, Any], dt.date, dt.date, Any, Any, Any, Any, pr.Health]:
    body = stored(draw(st.sampled_from(("beginner15", "club40", "runner60", "no_lt"))),
                  draw(st.sampled_from((0, 5, 12))), draw(st.integers(0, 2)))
    sessions = pr.sessions_of(body)
    first = pe.monday(min(s.date for s in sessions))
    week = first + dt.timedelta(days=7 * draw(st.integers(0, 14)))
    today = week + dt.timedelta(days=draw(st.integers(0, 16)))
    links: Dict[str, Any] = {}
    linked: Dict[str, Any] = {}
    runs: Dict[int, pr.Run] = {}
    fb: Dict[str, pr.Feedback] = {}
    for i, s in enumerate(sessions):
        if s.date >= today:
            continue
        status = draw(st.sampled_from(STATUSES))
        links[s.key] = status
        if status and status.startswith(("done", "partial")) and draw(st.booleans()):
            linked[s.key] = 1000 + i
            runs[1000 + i] = pr.Run(pr.Km(round(s.km * draw(st.floats(0.0, 1.5)), 1)) if draw(st.booleans()) else None,
                                    pe.MetersPerSecond(draw(st.floats(2.0, 5.5))) if draw(st.booleans()) else None)
        if draw(st.booleans()):
            fb[s.key] = pr.Feedback(draw(st.integers(1, 10)), draw(st.integers(0, 10)), draw(st.booleans()))
    flags = tuple(f for f in ("hrv_down", "short_nights") if draw(st.booleans()))
    health = pr.Health(flags=flags, hrv_7d=draw(st.one_of(st.none(), st.floats(20, 80))),
                       hrv_28d=draw(st.one_of(st.none(), st.floats(20, 80))),
                       short_nights=draw(st.one_of(st.none(), st.integers(0, 7))),
                       acwr=draw(st.one_of(st.none(), st.floats(0.5, 2.0))),
                       today_level=draw(st.sampled_from((None, "green", "yellow", "red"))))
    return body, week, today, links, linked, runs, fb, health


@unittest.skipUnless(HAVE_HYPOTHESIS, NEEDS_HYPOTHESIS)
class ReviewProperties(unittest.TestCase):
    @settings(**props(500))
    @given(review_case(), st.randoms(use_true_random=False), st.booleans())
    def test_reviews_only_lower_and_applying_never_raises(self, case: Any, rnd: Any, with_done: bool) -> None:
        body, week, today, links, linked, runs, fb, health = case
        out = pr.review_week("e" * 32, body, week, today, links, linked, runs, fb, health)
        nxt = week + dt.timedelta(days=7)
        for c in out["changes"]:
            self.assertIn(c["rule"], ("A1", "A2", "A3", "A4", "A5", "A6", "A7", "A8"))
            self.assertTrue(c["reason"])
            if c["rule"] == "A6":
                self.assertEqual(c["advice"], pr.ADVICE_PAIN)
            if isinstance(c["from"], dict) and "km" in c["from"]:
                self.assertLess(c["to"]["km"], c["from"]["km"])
            for e in c["edits"]:
                if "week_start" in e:
                    self.assertEqual(e["week_start"], nxt.isoformat())
                if e["op"] == "easy_day":
                    self.assertEqual(e["date"], today.isoformat())
        pains = [f for k, f in fb.items() if week.isoformat() <= next(
            s.date.isoformat() for s in pr.sessions_of(body) if s.key == k) <= (week + dt.timedelta(days=6)).isoformat()]
        if any(f.pain >= 4 or f.sharp for f in pains):
            self.assertTrue([c for c in out["changes"] if c["rule"] == "A6" and c.get("advice")])
        changes = list(out["changes"])
        rnd.shuffle(changes)
        done = pr.done_km(body, links, linked, runs) if with_done else None
        plan, accepted, refused, applied = pr.apply_changes(body, changes, today, done)
        self.assertEqual(sorted(accepted + [r["change_id"] for r in refused]),
                         sorted(c["change_id"] for c in changes))
        self.assertEqual(pr.no_raise(body, plan, today), [])
        self.assertLessEqual(set(pr.patch_problems(plan, today)), set(pr.patch_problems(body, today)))
        eased = any(c["rule"] == "A8" for c in changes)
        now = {s["key"]: s for w in plan["weeks"] for s in w["sessions"]}
        for w in body["weeks"]:
            for s in w["sessions"]:
                if s["date"] < today.isoformat() or (s["date"] == today.isoformat() and not eased):
                    self.assertEqual(now.get(s["key"]), s)
        if not applied:
            self.assertEqual(plan["weeks"], body["weeks"])


if __name__ == "__main__":
    unittest.main()
