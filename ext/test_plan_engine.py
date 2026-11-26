"""plan_engine: the rules v2, the golden plans (ext/fixtures/plan/), overrides, feasibility and invariants.

Standard library only. The golden plans are rebuilt byte for byte; after a deliberate rule change, rewrite
them with PLAN_GOLDEN_WRITE=1 python -m unittest test_plan_engine and review the diff (RULES_VERSION goes up
when a plan for the same inputs changes).
"""

from __future__ import annotations

import copy
import dataclasses
import datetime as dt
import json
import os
import unittest

import daily_status as ds
import plan_engine as pe

HERE = os.path.dirname(os.path.abspath(__file__))
GOLDEN_DIR = os.path.join(HERE, "fixtures", "plan")
D = dt.date


# --- golden cases ------------------------------------------------------------------------------------------------

def _snap(today, weeks_km, longest_km, lt=None, fixed=(), predictions=(), runs=4, vo2max=None, loads=None):
    """loads: None (no recorded training load), or sample_loads() keyword arguments (e.g. {"garmin_gap": 0.3})."""
    acts, garmin = pe.sample_loads(today, [k * 1000.0 for k in weeks_km], **loads) if loads is not None else ((), ())
    return pe.Snapshot(today=today, week_m=tuple(pe.Meters(k * 1000.0) for k in weeks_km),
                       week_runs=(runs,) * len(weeks_km), longest_run_8w_m=pe.Meters(longest_km * 1000.0),
                       lt_speed_mps=pe.MetersPerSecond(lt) if lt else None, vo2max=vo2max,
                       prediction_s=tuple(predictions), fixed=tuple(fixed), activity_load=acts, garmin_load=garmin)


TODAY = D(2027, 11, 24)
GOLDEN = {
    "G1": {
        "note": ("A spring marathon (A) with a half-distance triathlon as a B race. Fictional sample numbers (about "
                 "40 km a week, longest 19.6 km, LT 3.55 m/s, a weekly ride and swim; the load model tracks Garmin, so "
                 "the planned ratio is held at 1.3): signed off by hand as the release check plan."),
        "inputs": pe.PlanInputs(race_date=D(2028, 2, 25), goal_time_s=3 * 3600 + 52 * 60,
                                b_races=(pe.BRace(D(2028, 1, 28), "tri_half"),)),
        "snapshot": pe.sample_snapshot(TODAY),
    },
    "G2": {
        "note": "A 15 km a week runner aiming at a half marathon in 16 weeks.",
        "inputs": pe.PlanInputs(race_date=D(2028, 3, 19), race_distance="half", run_days=(1, 3, 6),
                                rest_days=(0, 2, 4, 5), long_run_day=6, quality_per_week=1),
        "snapshot": _snap(TODAY, (14, 16, 12, 15, 17, 14, 15, 16), 7.0, lt=3.2, runs=3,
                          predictions=(("half", 2 * 3600 + 5 * 60),)),
    },
    "G3": {
        "note": "A half marathon in 5 weeks: a short build and taper.",
        "inputs": pe.PlanInputs(race_date=D(2027, 12, 30), race_distance="half", goal_time_s=3600 + 45 * 60),
        "snapshot": _snap(TODAY, (30, 32, 28, 34, 31, 33, 30, 35), 16.0, lt=3.9,
                          predictions=(("half", 3600 + 44 * 60),)),
    },
    "G4": {
        "note": ("No lactate-threshold speed: steps without pace targets, lt_speed listed as missing. The load "
                 "model differs from Garmin's ratio by more than 0.15: the planned ratio is only reported."),
        "inputs": pe.PlanInputs(race_date=D(2028, 3, 17)),
        "snapshot": _snap(TODAY, (45, 48, 40, 50, 46, 52, 44, 50), 24.0, loads={"garmin_gap": 0.3}),
    },
    "G5": {
        "note": ("A heavy fixed TrainingPeaks week (13-19 Dec 2027): a long ride, a hard run and a long run. The "
                 "load model tracks Garmin: the planned ratio cuts the run km of that week and the next."),
        "inputs": pe.PlanInputs(race_date=D(2028, 3, 10), goal_time_s=3 * 3600 + 45 * 60),
        "snapshot": _snap(TODAY, (38, 42, 36, 44, 40, 41, 39, 43), 22.0, lt=3.7, loads={},
                          fixed=(pe.FixedSession(D(2027, 12, 14), "bike", False, True, 240.0),
                                 pe.FixedSession(D(2027, 12, 15), "run", True, False, 60.0),
                                 pe.FixedSession(D(2027, 12, 16), "swim", False, False, 60.0),
                                 pe.FixedSession(D(2027, 12, 18), "run", False, True, 120.0))),
    },
}


def golden_path(name):
    return os.path.join(GOLDEN_DIR, f"{name.lower()}.json")


def build(name):
    case = GOLDEN[name]
    return pe.build_plan(case["inputs"], case["snapshot"])


def _write_golden():
    os.makedirs(GOLDEN_DIR, exist_ok=True)
    for name, case in GOLDEN.items():
        doc = {"name": name, "note": case["note"], "plan": pe.plan_answer(build(name))}
        with open(golden_path(name), "w", encoding="ascii") as fh:
            json.dump(doc, fh, indent=1, sort_keys=True, ensure_ascii=True)
            fh.write("\n")


if os.environ.get("PLAN_GOLDEN_WRITE") == "1":  # pragma: no cover - a maintainer step
    _write_golden()


class GoldenPlans(unittest.TestCase):
    def test_each_golden_plan_is_rebuilt_byte_for_byte(self):
        for name in GOLDEN:
            with self.subTest(plan=name):
                with open(golden_path(name), encoding="ascii") as fh:
                    stored = json.load(fh)
                plan = pe.plan_answer(build(name))
                self.assertEqual(stored["plan"]["content_hash"], plan["content_hash"])
                self.assertEqual(stored["plan"], json.loads(json.dumps(plan)))

    def test_every_golden_plan_passes_every_invariant(self):
        for name in GOLDEN:
            with self.subTest(plan=name):
                plan = build(name)
                self.assertTrue(plan["invariants"]["ok"], plan["invariants"]["failed"])
                self.assertTrue(all(v["invariants_ok"] for v in plan["what_if"]))

    def test_g1_is_marked_signed_off(self):
        with open(golden_path("G1"), encoding="ascii") as fh:
            self.assertIn("signed off by hand", json.load(fh)["note"])

    def test_g1_shape(self):
        plan = build("G1")
        weeks = [w for w in plan["weeks"] if w["kind"] != "week0"]
        self.assertEqual(weeks[0]["start"], "2027-11-29")
        self.assertEqual(weeks[-1]["kind"], "race")
        self.assertEqual(weeks[-1]["start"], "2028-02-21")
        self.assertEqual(plan["b_races"][0]["date"], "2028-01-28")
        races = [s for w in plan["weeks"] for s in w["sessions"] if s["role"] in ("race", "b_race")]
        self.assertEqual([(s["date"], s["role"]) for s in races], [("2028-01-28", "b_race"), ("2028-02-25", "race")])
        self.assertLessEqual(plan["summary"]["peak_long_km"], 32)

    def test_g2_starts_from_its_own_volume(self):
        plan = build("G2")
        self.assertAlmostEqual(plan["start"]["km0"], 15.5, places=1)
        first = next(w for w in plan["weeks"] if w["kind"] != "week0")
        self.assertLessEqual(first["target_km"], 15.5 * 1.08 + 0.1)
        self.assertLess(plan["summary"]["peak_km"], 40)

    def test_g3_short_plan_still_tapers(self):
        plan = build("G3")
        kinds = [w["kind"] for w in plan["weeks"] if w["kind"] != "week0"]
        self.assertEqual(len(kinds), 5)
        self.assertEqual(kinds[-1], "race")

    def test_g4_has_no_pace_targets(self):
        plan = build("G4")
        self.assertIn("lt_speed", plan["missing"])
        paces = [s["pace"] for w in plan["weeks"] for s in w["sessions"] if s["role"] not in ("race", "b_race")]
        self.assertTrue(paces and all(p is None for p in paces))

    def test_g5_keeps_clear_of_the_fixed_week(self):
        plan = build("G5")
        hard = {s["date"] for w in plan["weeks"] for s in w["sessions"] if s["hard_or_long"]}
        for fixed in ("2027-12-14", "2027-12-15", "2027-12-18"):
            day = D.fromisoformat(fixed)
            for near in (day - dt.timedelta(days=1), day, day + dt.timedelta(days=1)):
                self.assertNotIn(near.isoformat(), hard)


# --- rules -----------------------------------------------------------------------------------------------------

class Rules(unittest.TestCase):
    def test_constants_agree_with_the_daily_classifier(self):
        r, t = pe.RULES, ds.THRESHOLDS
        self.assertEqual(r["long_min_by_classifier"], t["long_min"]["run"])
        self.assertEqual(r["long_km_by_classifier"], t["long_km"]["run"])
        self.assertLess(r["easy_max_km"], t["long_km"]["run"])
        self.assertLess(r["easy_max_min"], t["long_min"]["run"])
        # 8 x 100 m strides are the most the engine plans: under the classifier's hard work floor.
        self.assertLess(8 * r["strides"]["m"], t["hard_min_work_m"])
        self.assertLess(r["bands_of_lt"]["easy"][1], t["hard_pace_of_lt_speed"])
        self.assertLess(r["bands_of_lt"]["long"][1], t["hard_pace_of_lt_speed"])
        self.assertGreater(r["bands_of_lt"]["threshold"][0], t["hard_pace_of_lt_speed"])

    def test_week1_and_phases(self):
        self.assertEqual(pe.week1_start(D(2027, 11, 24)), D(2027, 11, 29))
        self.assertEqual(pe.week1_start(D(2027, 11, 28)), D(2027, 11, 29))
        self.assertEqual(pe.week1_start(D(2027, 11, 29)), D(2027, 11, 29))   # a Monday starts week 1 itself
        self.assertEqual(pe.phase_counts(13), {"base": 4, "build": 5, "peak": 2, "taper": 2})
        self.assertEqual(pe.phase_counts(5)["peak"], 0)
        self.assertEqual(sum(pe.phase_counts(20).values()), 20)

    def test_same_inputs_same_bytes(self):
        a, b = build("G1"), build("G1")
        self.assertEqual(json.dumps(pe.plan_answer(a), sort_keys=True), json.dumps(pe.plan_answer(b), sort_keys=True))
        other = pe.build_plan(dataclasses.replace(GOLDEN["G1"]["inputs"], goal_time_s=3 * 3600 + 20 * 60),
                              GOLDEN["G1"]["snapshot"])
        self.assertNotEqual(a["content_hash"], other["content_hash"])

    def test_answer_is_plain_json_with_the_rules_version(self):
        plan = pe.plan_answer(build("G1"))
        self.assertNotIn("_check", plan)
        self.assertEqual(plan["rules_version"], pe.RULES_VERSION)
        json.dumps(plan, allow_nan=False)

    def test_growth_and_deloads(self):
        plan = build("G1")
        weeks = [w for w in plan["weeks"] if w["kind"] in ("normal", "deload")]
        last = plan["start"]["km0"]
        for w in weeks:
            if w["kind"] == "normal":
                self.assertLessEqual(w["target_km"], round(last * 1.08, 1) + 0.15)
                last = w["target_km"]
        self.assertIn("deload", [w["kind"] for w in weeks])

    def test_long_run_cap_uses_the_slow_edge_and_32_km(self):
        snap = dataclasses.replace(pe.sample_snapshot(), lt_speed_mps=pe.MetersPerSecond(2.6),
                                   week_m=tuple(pe.Meters(90000.0) for _ in range(8)),
                                   longest_run_8w_m=pe.Meters(30000.0))
        plan = pe.build_plan(pe.PlanInputs(race_date=D(2028, 3, 17)), snap, what_if_runs=False)
        # 3 h at 0.70 x 2.6 m/s = 19.7 km
        self.assertLessEqual(plan["summary"]["peak_long_km"], 19.7)
        self.assertTrue(plan["invariants"]["ok"], plan["invariants"]["failed"])

    def test_check_inputs_refuses_with_fixed_texts(self):
        base = pe.PlanInputs(race_date=D(2028, 3, 17))
        for change in ({"race_distance": "10k"}, {"run_days": (0,), "rest_days": (0,)}, {"quality_per_week": 3},
                       {"max_long_km": 40}, {"long_share": 0.9}, {"goal_time_s": 60},
                       {"b_races": (pe.BRace(D(2027, 12, 31), "ultra"),)}, {"overrides": (pe.Override("grow"),)},
                       {"long_run_day": 9}):
            with self.subTest(change=change):
                with self.assertRaises(pe.PlanError):
                    pe.check_inputs(dataclasses.replace(base, **change))

    def test_blocked_dates_get_no_run(self):
        inputs = dataclasses.replace(GOLDEN["G1"]["inputs"], blocked_dates=(D(2027, 12, 5), D(2027, 12, 12)))
        plan = pe.build_plan(inputs, GOLDEN["G1"]["snapshot"], what_if_runs=False)
        days = {s["date"] for w in plan["weeks"] for s in w["sessions"]}
        self.assertNotIn("2027-12-05", days)
        self.assertNotIn("2027-12-12", days)
        self.assertTrue(plan["invariants"]["ok"])

    def test_feasibility_verdicts(self):
        g1 = build("G1")["feasibility"]
        self.assertIn(g1["verdict"], ("feasible", "ambitious", "unrealistic"))
        fast = pe.build_plan(dataclasses.replace(GOLDEN["G1"]["inputs"], goal_time_s=2 * 3600 + 40 * 60),
                             GOLDEN["G1"]["snapshot"], what_if_runs=False)["feasibility"]
        self.assertEqual(fast["verdict"], "unrealistic")
        self.assertIsNotNone(fast["corrected_goal_s"])
        self.assertEqual(fast["corrected_goal_s"] % 60, 0)

    def test_what_if_variants(self):
        names = [v["variant"] for v in build("G1")["what_if"]]
        self.assertEqual(names, ["goal_minus_5min", "goal_plus_5min", "long_share_0.35", "long_share_0.45"])
        self.assertEqual([v["variant"] for v in build("G4")["what_if"]], ["long_share_0.35", "long_share_0.45"])

    def test_assess(self):
        a = pe.assess(GOLDEN["G1"]["inputs"], GOLDEN["G1"]["snapshot"])
        self.assertEqual(a["kind"], "plan_assessment")
        self.assertEqual(len(a["history"]), 8)
        self.assertEqual(a["avg_km_4w"], 40.0)
        self.assertEqual(a["prediction"], "3:57:00")
        self.assertIsNotNone(a["projected_peak_km"])
        with self.assertRaises(pe.PlanError):
            pe.assess(dataclasses.replace(GOLDEN["G1"]["inputs"], race_distance="10k"), GOLDEN["G1"]["snapshot"])
        empty = dataclasses.replace(GOLDEN["G1"]["snapshot"], week_m=(), week_runs=())
        self.assertTrue(any(m.startswith("plan: ") for m in pe.assess(GOLDEN["G1"]["inputs"], empty)["missing"]))

    def test_hms_and_pace(self):
        self.assertEqual(pe.hms(3 * 3600 + 5 * 60 + 7), "3:05:07")
        self.assertEqual(pe.pace_text(1000 / 300), "5:00")

    def test_selfcheck(self):
        pe.selfcheck()


# --- invariants ------------------------------------------------------------------------------------------------

class Invariants(unittest.TestCase):
    def test_a_longer_long_run_is_caught(self):
        plan = build("G1")
        weeks = plan["_check"]["weeks"]
        w = next(w for w in weeks if w.kind == "normal" and any(s.role == "long" for s in w.sessions))
        s = next(s for s in w.sessions if s.role == "long")
        ctx = plan["_check"]["ctx"]
        w.sessions[w.sessions.index(s)] = pe.make_session(s.key, s.date, "long", pe.Meters(s.distance + 9000),
                                                          ctx.paces, ctx.rules)
        failed = {i["id"] for i in pe.check_invariants(plan) if not i["ok"]}
        self.assertTrue({"long_step", "long_cap"} & failed, failed)

    def test_adjacent_hard_days_are_caught(self):
        plan = build("G1")
        weeks = plan["_check"]["weeks"]
        ctx = plan["_check"]["ctx"]
        w = next(w for w in weeks if w.kind == "normal" and any(s.role == "long" for s in w.sessions))
        long_s = next(s for s in w.sessions if s.role == "long")
        day = long_s.date - dt.timedelta(days=1)
        for s in list(w.sessions):
            if s.date == day:
                w.sessions.remove(s)
        w.sessions.append(pe.make_session("20401113-run", day, "threshold", pe.Meters(8000), ctx.paces, ctx.rules))
        failed = {i["id"] for i in pe.check_invariants(plan) if not i["ok"]}
        self.assertIn("no_adjacent_hard", failed)

    def test_every_invariant_is_reported(self):
        checked = build("G1")["invariants"]["checked"]
        for name in ("growth_cap", "no_makeup", "deload_cadence", "deload_depth", "long_cap", "long_step",
                     "peak_long_timing", "easy_share", "no_adjacent_hard", "bands_match_roles", "b_windows",
                     "a_taper", "last_quality", "rest_days", "one_run_per_day", "peak_ceiling"):
            self.assertIn(name, checked)


def _flat(km0, run_days=(0, 1, 2, 3, 4, 5, 6), lt=4.5, longest_km=25.0):
    snap = pe.Snapshot(today=TODAY, week_m=tuple(pe.Meters(km0 * 1000.0) for _ in range(8)), week_runs=(6,) * 8,
                       longest_run_8w_m=pe.Meters(longest_km * 1000.0), lt_speed_mps=pe.MetersPerSecond(lt),
                       prediction_s=(("marathon", 2 * 3600 + 50 * 60),))
    rest = tuple(d for d in range(7) if d not in run_days)
    return pe.PlanInputs(race_date=D(2028, 2, 25), run_days=tuple(run_days), rest_days=rest), snap


def _base_km(week):
    """The km the next weeks grow from, from the answer: the target unless the week was placed below it by
    more than the 100 m rounding of its sessions."""
    n = max(1, sum(1 for s in week["sessions"] if s["role"] not in ("race", "b_race")))
    return week["target_km"] if week["planned_km"] >= week["target_km"] - 0.1 * n else week["planned_km"]


class Progression(unittest.TestCase):
    """Review 1: blocked dates, capped weeks, the peak ceiling and the recomputed long-run step."""

    def test_a_blocked_date_drops_its_share_and_moves_nothing(self):
        case = GOLDEN["G1"]
        plan = pe.plan_answer(pe.build_plan(case["inputs"], case["snapshot"], what_if_runs=False))
        week = next(w for w in plan["weeks"] if w["start"] == "2027-12-06")
        easy = next(s for s in week["sessions"] if s["role"] == "easy")
        day = D.fromisoformat(easy["date"])
        blocked = pe.plan_answer(pe.build_plan(dataclasses.replace(case["inputs"], blocked_dates=(day,)),
                                               case["snapshot"], what_if_runs=False))
        rested = pe.plan_answer(pe.build_plan(
            dataclasses.replace(case["inputs"], overrides=(pe.Override("rest", date=day),)),
            case["snapshot"], what_if_runs=False))
        wb = next(w for w in blocked["weeks"] if w["start"] == "2027-12-06")
        wr = next(w for w in rested["weeks"] if w["start"] == "2027-12-06")
        others = {(s["date"], s["role"], s["km"]) for s in week["sessions"] if s["date"] != easy["date"]}
        self.assertEqual({(s["date"], s["role"], s["km"]) for s in wb["sessions"]}, others)
        self.assertAlmostEqual(wb["planned_km"], round(week["planned_km"] - easy["km"], 1), places=1)
        self.assertEqual(wb["planned_km"], wr["planned_km"])
        # the weeks after it are the same plan: a blocked date is not a shortfall to grow from
        for wa, wc in zip(plan["weeks"], blocked["weeks"]):
            if wa["start"] != "2027-12-06":
                self.assertEqual(wa["target_km"], wc["target_km"], wa["start"])

    def test_capped_weeks_grow_from_what_was_placed(self):
        inputs, snap = _flat(60, run_days=(0, 1, 3, 5, 6))
        plan = pe.build_plan(inputs, snap, what_if_runs=False)
        self.assertTrue(plan["invariants"]["ok"], plan["invariants"]["failed"])
        self.assertIn("volume_capped", {w["id"] for w in plan["warnings"]})
        weeks = [w for w in plan["weeks"] if w["kind"] != "week0"]
        last, peak = 60.0, 0.0
        for w in weeks:
            if w["kind"] == "normal":
                self.assertLessEqual(w["target_km"], last * 1.08 + 0.1, w["start"])
                last = _base_km(w)
                peak = max(peak, last)
            elif w["kind"] == "deload":
                self.assertLessEqual(w["planned_km"], last * 0.78 + 0.1, w["start"])
            elif w["kind"] == "taper":
                self.assertLessEqual(w["planned_km"], peak * 0.70 + 0.1, w["start"])
                self.assertLessEqual(w["target_km"], peak * 0.70 + 0.1, w["start"])
        capped = [w for w in weeks if w["kind"] == "normal" and w["planned_km"] < w["target_km"] - 1]
        self.assertTrue(capped)
        nxt = weeks[weeks.index(capped[0]) + 1]
        if nxt["kind"] == "normal":
            self.assertLessEqual(nxt["target_km"], capped[0]["planned_km"] * 1.08 + 0.1)

    def test_peak_ceiling_holds_the_weekly_km(self):
        inputs, snap = _flat(80)
        plan = pe.build_plan(inputs, snap, what_if_runs=False)
        self.assertTrue(plan["invariants"]["ok"], plan["invariants"]["failed"])
        self.assertIn("peak_ceiling", plan["invariants"]["checked"])
        ceiling = max(1.3 * 80, 80 + 25)   # 105 km
        weeks = [w for w in plan["weeks"] if w["kind"] != "week0"]
        self.assertTrue(all(w["target_km"] <= ceiling + 0.05 for w in weeks))
        normal = [w["target_km"] for w in weeks if w["kind"] == "normal"]
        self.assertEqual(max(normal), ceiling)
        first = normal.index(ceiling)
        self.assertTrue(all(t == ceiling for t in normal[first:]))   # held once reached
        self.assertTrue(all(w["target_km"] < ceiling for w in weeks if w["kind"] == "deload"))
        warn = [w for w in plan["warnings"] if w["id"] == "peak_capped"]
        self.assertEqual(len(warn), 1)
        self.assertIn("105.0 km", warn[0]["detail"])
        self.assertIn("80.0 km", warn[0]["detail"])
        self.assertEqual(pe.RULES["peak_ceiling"], {"of_max_week": 1.3, "over_km0_km": 25})

    def test_peak_ceiling_uses_the_biggest_week(self):
        snap = _snap(TODAY, (40, 40, 40, 40, 40, 40, 100, 40), 20.0, lt=4.0)
        ceiling, biggest = pe.peak_ceiling(snap, pe.Meters(40000.0), pe.RULES)
        self.assertEqual((ceiling, biggest), (130000.0, 100000.0))
        ceiling, _ = pe.peak_ceiling(_snap(TODAY, (40,) * 8, 20.0), pe.Meters(40000.0), pe.RULES)
        self.assertEqual(ceiling, 65000.0)

    def test_a_week_over_the_ceiling_is_caught(self):
        inputs, snap = _flat(80)
        plan = pe.build_plan(inputs, snap, what_if_runs=False)
        w = next(w for w in plan["_check"]["weeks"] if w.kind == "normal" and w.target >= 104000)
        w.target = pe.Meters(120000.0)
        failed = {i["id"] for i in pe.check_invariants(plan) if not i["ok"]}
        self.assertIn("peak_ceiling", failed)

    def test_long_step_does_not_trust_the_engine_bookkeeping(self):
        plan = build("G1")
        ctx = plan["_check"]["ctx"]
        weeks = [w for w in plan["_check"]["weeks"] if w.kind == "normal"
                 and any(s.role == "long" for s in w.sessions)]
        w = weeks[1]
        before = max(float(ctx.longest), max(s.distance for s in weeks[0].sessions if s.role == "long"))
        s = next(s for s in w.sessions if s.role == "long")
        w.sessions[w.sessions.index(s)] = pe.make_session(s.key, s.date, "long", pe.Meters(before + 2500),
                                                          ctx.paces, ctx.rules)
        w.long_ref = pe.Meters(99000.0)
        failed = {i["id"] for i in pe.check_invariants(plan) if not i["ok"]}
        self.assertIn("long_step", failed)
        self.assertIn("long_cap", failed)

    def test_a_week_grown_from_unplanned_km_is_caught(self):
        inputs, snap = _flat(60, run_days=(0, 1, 3, 5, 6))
        plan = pe.build_plan(inputs, snap, what_if_runs=False)
        w = next(w for w in plan["_check"]["weeks"] if w.kind == "normal"
                 and sum(s.distance for s in w.sessions if s.counts) < w.target - 1000)
        w.base = w.target
        failed = {i["id"] for i in pe.check_invariants(plan) if not i["ok"]}
        self.assertIn("growth_cap", failed)


# --- overrides -------------------------------------------------------------------------------------------------

def _with(*overrides):
    inputs = dataclasses.replace(GOLDEN["G1"]["inputs"], overrides=tuple(overrides))
    return pe.build_plan(inputs, GOLDEN["G1"]["snapshot"], what_if_runs=False)


def _session(plan, key):
    return next(s for w in plan["weeks"] for s in w["sessions"] if s["key"] == key)


def _week_of(plan, key):
    return next(w for w in plan["weeks"] if any(s["key"] == key for s in w["sessions"]))


class Overrides(unittest.TestCase):
    def setUp(self):
        self.base = build("G1")

    def keys(self, role):
        return [s["key"] for w in self.base["weeks"] for s in w["sessions"] if s["role"] == role]

    def test_easy_turns_a_quality_session_easy_and_never_longer(self):
        key = self.keys("threshold")[0]
        plan = _with(pe.Override("easy", key=key))
        self.assertEqual(plan["overrides"]["applied"], [{"kind": "easy", "key": key}])
        s = _session(plan, key)
        self.assertEqual(s["role"], "easy")
        self.assertLessEqual(s["km"], _session(self.base, key)["km"])
        self.assertTrue(plan["invariants"]["ok"])

    def test_cap_week_km_only_lowers(self):
        w = next(w for w in self.base["weeks"] if w["kind"] == "normal" and w["planned_km"] > 30)
        start = D.fromisoformat(w["start"])
        lower = _with(pe.Override("cap_week_km", week_start=start, km=w["planned_km"] - 8))
        self.assertEqual(len(lower["overrides"]["applied"]), 1)
        self.assertLessEqual(next(x for x in lower["weeks"] if x["start"] == w["start"])["planned_km"],
                             w["planned_km"] - 8 + 0.05)
        higher = _with(pe.Override("cap_week_km", week_start=start, km=w["planned_km"] + 20))
        self.assertEqual(higher["overrides"]["refused"][0]["rule"], "raises_load")

    def test_rest_removes_the_day(self):
        key = self.keys("easy")[3]
        day = D.fromisoformat(_session(self.base, key)["date"])
        plan = _with(pe.Override("rest", date=day))
        self.assertNotIn(day.isoformat(), {s["date"] for w in plan["weeks"] for s in w["sessions"]})
        self.assertLessEqual(_week_of(self.base, key)["planned_km"] - 0.05,
                             _week_of(self.base, key)["planned_km"])

    def test_refusals_name_their_rule(self):
        race_day = D(2028, 2, 25)
        cases = [
            (pe.Override("easy", key="20401111-run"), "unknown_session"),
            (pe.Override("rest", date=race_day), "race_day"),
            (pe.Override("move", key=self.keys("long")[2], date=D(2025, 1, 1)), None),
            (pe.Override("cap_week_km", week_start=D(2030, 1, 7), km=10), "unknown_week"),
        ]
        for edit, rule in cases:
            with self.subTest(edit=edit.kind):
                plan = _with(edit)
                self.assertEqual(plan["overrides"]["applied"], [])
                self.assertEqual(len(plan["overrides"]["refused"]), 1)
                if rule:
                    self.assertEqual(plan["overrides"]["refused"][0]["rule"], rule)

    def test_move_within_the_week(self):
        key = self.keys("easy")[5]
        s = _session(self.base, key)
        w = _week_of(self.base, key)
        taken = {x["date"] for x in w["sessions"]}
        start = D.fromisoformat(w["start"])
        free = [start + dt.timedelta(days=i) for i in range(7)
                if (start + dt.timedelta(days=i)).isoformat() not in taken]
        if not free:
            self.skipTest("no free day in that week")
        plan = _with(pe.Override("move", key=key, date=free[0]))
        refused = plan["overrides"]["refused"]
        if refused:
            self.assertIn(refused[0]["rule"], ("rest_days", "no_adjacent_hard", "blocked_dates", "past_day"))
        else:
            self.assertEqual(_session(plan, key)["date"], free[0].isoformat())
            self.assertEqual(_session(plan, key)["km"], s["km"])

    def test_overrides_are_echoed_and_hashed(self):
        key = self.keys("threshold")[0]
        plan = _with(pe.Override("easy", key=key))
        self.assertEqual(plan["inputs"]["overrides"], [{"kind": "easy", "key": key}])
        self.assertNotEqual(plan["content_hash"], self.base["content_hash"])

    def test_inputs_are_not_changed(self):
        inputs = copy.deepcopy(GOLDEN["G1"]["inputs"])
        pe.build_plan(inputs, GOLDEN["G1"]["snapshot"], what_if_runs=False)
        self.assertEqual(inputs, GOLDEN["G1"]["inputs"])


# --- rules v2: planned all-sport ACWR (item 12) ---------------------------------------------------------------

class Acwr(unittest.TestCase):
    def test_ratio_is_seven_days_over_twenty_eight_by_four(self):
        day = D(2027, 12, 30)
        loads = {day - dt.timedelta(days=i): 100.0 for i in range(28)}
        self.assertAlmostEqual(pe.acwr_on(day, loads), 1.0)
        loads.update({day - dt.timedelta(days=i): 200.0 for i in range(7)})
        self.assertAlmostEqual(pe.acwr_on(day, loads), 1400 / ((7 * 200 + 21 * 100) / 4))
        self.assertIsNone(pe.acwr_on(day, {}))

    def test_g5_heavy_week_cuts_easy_runs_only_and_holds_the_invariant(self):
        plan = build("G5")
        acwr = plan["acwr"]
        self.assertTrue(acwr["enforced"])
        self.assertIsNone(acwr["reason"])
        # only the heavy TP week: week 4's one day over (Monday, all week-3 load) comes before its first easy
        # run, so no cut of week 4 can lower it and week 4 keeps its runs
        self.assertEqual([c["week"] for c in acwr["cuts"]], [3])
        w3 = next(w for w in plan["weeks"] if w["week"] == 3)
        self.assertGreaterEqual(w3["planned_km"], round(0.6 * w3["target_km"], 1))
        w4 = next(w for w in plan["weeks"] if w["week"] == 4)
        self.assertEqual(w4["planned_km"], 39.8)
        self.assertIn("acwr_cap", plan["invariants"]["checked"])
        self.assertTrue(plan["invariants"]["ok"], plan["invariants"]["failed"])
        ids = [w["id"] for w in plan["warnings"]]
        self.assertIn("acwr_cut", ids)
        self.assertIn("acwr_floor", ids)
        free = pe.build_plan(GOLDEN["G5"]["inputs"], dataclasses.replace(GOLDEN["G5"]["snapshot"], garmin_load=()),
                             what_if_runs=False)
        self.assertFalse(free["acwr"]["enforced"])
        for held, loose in zip(plan["weeks"], free["weeks"]):
            self.assertEqual(held["target_km"], loose["target_km"])
            self.assertLessEqual(held["planned_km"], loose["planned_km"])
            long_held = [s for s in held["sessions"] if s["role"] != "easy"]
            long_free = [s for s in loose["sessions"] if s["role"] != "easy"]
            self.assertEqual(long_held, long_free, held["start"])   # long runs and quality untouched

    def test_g1_respects_the_b_race_and_the_recovery_after_it(self):
        plan = build("G1")
        acwr = plan["acwr"]
        self.assertTrue(acwr["enforced"])
        self.assertTrue(plan["invariants"]["ok"], plan["invariants"]["failed"])
        # the B race's load: 330 minutes x the all-sport load per minute, on the race day
        self.assertEqual(acwr["b_races"], [{"date": "2028-01-28", "kind": "tri_half", "minutes": 330,
                                            "load": int(round(330 * acwr["load_per_min"]["all"]))}])
        # the race day itself: the cap acts on it too (its mini-taper runs are cut to their floor)
        self.assertEqual(acwr["planned_max"], {"ratio": 1.31, "date": "2028-01-28"})
        self.assertEqual(acwr["cap_max"], {"ratio": 1.31, "date": "2028-01-28"})
        # the mini-taper and recovery week is cut to its floor; the race day stays above 1.3
        # (held, and said so); the recovery week after it is cut too
        self.assertEqual([c["week"] for c in acwr["cuts"]], [9, 10])
        self.assertEqual([h["date"] for h in acwr["held"]], ["2028-01-28"])
        held = next(w for w in plan["warnings"] if w["id"] == "acwr_floor")
        self.assertEqual(held["dates"], ["2028-01-28"])
        w9 = next(w for w in plan["weeks"] if w["week"] == 9)
        self.assertGreaterEqual(w9["planned_km"], round(0.6 * w9["target_km"], 1))
        loads = pe.day_loads(plan["_check"]["weeks"], plan["_check"]["ctx"])
        for offset in range(2, 21):   # the rest of the recovery and the two weeks back to full volume
            day = D(2028, 1, 28) + dt.timedelta(days=offset)
            self.assertLessEqual(pe.acwr_on(day, loads), 1.3 + 1e-9, day)

    def test_not_enforced_when_the_model_does_not_track_garmin(self):
        plan = build("G4")
        self.assertFalse(plan["acwr"]["enforced"])
        self.assertIn("0.15", plan["acwr"]["reason"])
        self.assertTrue(all(c["gap"] > 0.15 for c in plan["acwr"]["checks"]))
        self.assertIn("acwr_not_enforced", [w["id"] for w in plan["warnings"]])
        self.assertEqual(plan["acwr"]["cuts"], [])

    def test_not_enforced_without_loads_or_enough_checks(self):
        plan = build("G2")
        self.assertIn("training_load", plan["missing"])
        self.assertIsNone(plan["acwr"]["planned_max"])
        snap = GOLDEN["G1"]["snapshot"]
        one = pe.build_plan(GOLDEN["G1"]["inputs"], dataclasses.replace(snap, garmin_load=snap.garmin_load[:1]),
                            what_if_runs=False)
        self.assertFalse(one["acwr"]["enforced"])
        self.assertIn("1 of the check days", one["acwr"]["reason"])
        none = pe.build_plan(GOLDEN["G1"]["inputs"], dataclasses.replace(snap, garmin_load=()), what_if_runs=False)
        self.assertIn("garmin_load", none["missing"])
        rides = tuple(a for a in snap.activity_load if a.sport != "run")
        no_run = pe.build_plan(GOLDEN["G1"]["inputs"], dataclasses.replace(snap, activity_load=rides),
                               what_if_runs=False)
        self.assertIn("no recorded run", no_run["acwr"]["reason"])

    def test_a_high_ratio_only_warns_when_not_enforced(self):
        snap = GOLDEN["G5"]["snapshot"]
        loose = dataclasses.replace(snap, garmin_load=tuple(dataclasses.replace(g, acute=g.acute * 2)
                                                            for g in snap.garmin_load))
        plan = pe.build_plan(GOLDEN["G5"]["inputs"], loose, what_if_runs=False)
        self.assertFalse(plan["acwr"]["enforced"])
        self.assertGreater(plan["acwr"]["days_over"], 0)
        high = next(w for w in plan["warnings"] if w["id"] == "acwr_high")
        self.assertIn("not enforced", high["detail"])
        self.assertTrue(plan["invariants"]["ok"])

    def test_fixed_sessions_without_minutes_use_the_sports_mean_session(self):
        snap = GOLDEN["G5"]["snapshot"]
        bare = dataclasses.replace(snap, fixed=tuple(dataclasses.replace(f, minutes=None) for f in snap.fixed))
        plan = pe.build_plan(GOLDEN["G5"]["inputs"], bare, what_if_runs=False)
        self.assertTrue(plan["invariants"]["ok"], plan["invariants"]["failed"])
        ctx = plan["_check"]["ctx"]
        ride = ctx.load.fixed[D(2027, 12, 14)]
        self.assertAlmostEqual(ride, 90 * 1.1, places=3)   # the history's ride: 90 min at 1.1 a minute

    def test_runs_without_lt_speed_use_the_recorded_run_speed(self):
        plan = build("G4")   # no LT speed: minutes from the recorded 3.0 m/s
        ctx = plan["_check"]["ctx"]
        s = next(s for w in plan["_check"]["weeks"] for s in w.sessions if s.role == "easy")
        self.assertAlmostEqual(pe.session_load(s, ctx), s.distance / 3.0 / 60.0 * ctx.load.lpm["run"], places=6)
        ctx.load.run_mps = None
        self.assertEqual(pe.session_load(s, ctx), 0.0)

    def test_the_invariant_catches_a_ratio_above_the_cap(self):
        plan = build("G5")
        weeks, ctx = plan["_check"]["weeks"], plan["_check"]["ctx"]
        w7 = next(w for w in weeks if w.number == 7)
        easy = next(s for s in w7.sessions if s.role == "easy" and s.key == "20280115-run")
        w7.sessions[w7.sessions.index(easy)] = pe.make_session(easy.key, easy.date, "easy", pe.Meters(14000.0),
                                                                ctx.paces, ctx.rules)
        failed = {i["id"]: i for i in pe.check_invariants(plan) if not i["ok"]}
        self.assertIn("acwr_cap", failed)
        self.assertRegex(failed["acwr_cap"]["detail"], r"2028-01-(09|1[0-8])")

    def test_overrides_cut_again_and_a_refused_edit_leaves_no_trace(self):
        base = build("G5")
        w2 = next(w for w in base["weeks"] if w["week"] == 2)
        plan = pe.build_plan(dataclasses.replace(GOLDEN["G5"]["inputs"], overrides=(
            pe.Override("cap_week_km", week_start=D.fromisoformat(w2["start"]), km=w2["planned_km"] - 12),)),
            GOLDEN["G5"]["snapshot"], what_if_runs=False)
        self.assertTrue(plan["invariants"]["ok"], plan["invariants"]["failed"])
        self.assertEqual(len(plan["overrides"]["applied"]) + len(plan["overrides"]["refused"]), 1)
        for a, b in zip(base["weeks"], plan["weeks"]):
            self.assertLessEqual(b["planned_km"], a["planned_km"] + 0.05)
        refused = pe.build_plan(dataclasses.replace(GOLDEN["G5"]["inputs"], overrides=(
            pe.Override("cap_week_km", week_start=D.fromisoformat(w2["start"]), km=w2["planned_km"] + 5),)),
            GOLDEN["G5"]["snapshot"], what_if_runs=False)
        self.assertEqual(refused["overrides"]["refused"][0]["rule"], "raises_load")
        self.assertEqual(refused["acwr"]["cuts"], base["acwr"]["cuts"])

    def test_echo_and_selfcheck(self):
        plan = build("G1")
        self.assertEqual(plan["acwr"]["rules"], pe.RULES["acwr"])
        self.assertNotIn("course_id", plan["inputs"])
        self.assertNotIn("race_pacing", plan)
        pe.selfcheck()

    def test_enforced_when_two_of_three_checks_agree(self):
        snap = GOLDEN["G5"]["snapshot"]
        g = list(snap.garmin_load)
        g[-1] = dataclasses.replace(g[-1], acute=g[-1].acute + 0.3 * g[-1].chronic)   # gaps 0.03, 0.03, 0.33
        plan = pe.build_plan(GOLDEN["G5"]["inputs"], dataclasses.replace(snap, garmin_load=tuple(g)),
                             what_if_runs=False)
        self.assertEqual([c["gap"] for c in plan["acwr"]["checks"]], [0.03, 0.03, 0.33])
        self.assertTrue(plan["acwr"]["enforced"])
        g[0] = dataclasses.replace(g[0], acute=g[0].acute + 0.3 * g[0].chronic)
        one = pe.build_plan(GOLDEN["G5"]["inputs"], dataclasses.replace(snap, garmin_load=tuple(g)),
                            what_if_runs=False)
        self.assertFalse(one["acwr"]["enforced"])
        self.assertIn("1 of 3 check days", one["acwr"]["reason"])

    def test_a_fixed_session_today_counts_until_something_is_recorded_today(self):
        snap = GOLDEN["G5"]["snapshot"]
        ride = pe.FixedSession(snap.today, "bike", False, True, 120.0)
        plan = pe.build_plan(GOLDEN["G5"]["inputs"], dataclasses.replace(snap, fixed=snap.fixed + (ride,)),
                             what_if_runs=False)
        self.assertAlmostEqual(plan["_check"]["ctx"].load.fixed[snap.today], 120 * 1.1, places=3)
        done = pe.ActivityLoad(snap.today, "bike", pe.Seconds(3600.0), 70.0)
        plan = pe.build_plan(GOLDEN["G5"]["inputs"], dataclasses.replace(
            snap, fixed=snap.fixed + (ride,), activity_load=snap.activity_load + (done,)), what_if_runs=False)
        self.assertNotIn(snap.today, plan["_check"]["ctx"].load.fixed)
        self.assertEqual(plan["_check"]["ctx"].load.actual[snap.today], 70.0)

    def test_b_race_minutes(self):
        model = pe.load_model(GOLDEN["G1"]["snapshot"], plan_ctx := build("G1")["_check"]["ctx"])
        self.assertEqual(pe.b_race_minutes(pe.BRace(D(2028, 1, 28), "tri_full"), model), 750.0)
        self.assertAlmostEqual(pe.b_race_minutes(pe.BRace(D(2028, 1, 28), "run"), model), 21097.5 / 3.0 / 60.0)
        model.run_mps = None
        self.assertEqual(pe.b_race_minutes(pe.BRace(D(2028, 1, 28), "run"), model), 120.0)
        self.assertIsNotNone(plan_ctx)

    def test_a_held_week_is_at_its_floors_and_enforcing_again_changes_nothing(self):
        plan = build("G5")
        weeks, ctx = plan["_check"]["weeks"], plan["_check"]["ctx"]
        w3 = next(w for w in weeks if w.number == 3)
        self.assertTrue(w3.acwr_held)
        self.assertGreaterEqual(pe._planned(w3), pe._acwr_floor(w3, ctx) - pe.EPS_M)
        for h in plan["acwr"]["held"]:   # a held day: no cut is left for it
            day = D.fromisoformat(h["date"])
            owner = next(o for d, _r, o in pe.acwr_cap_days(weeks, ctx) if d == day)
            self.assertEqual(pe._cut_options(weeks, ctx, day, owner), [])
        before = copy.deepcopy(w3.sessions)
        self.assertFalse(pe.enforce_acwr(w3, ctx))   # nothing more to cut: held again
        self.assertTrue(w3.acwr_held)
        self.assertEqual(w3.sessions, before)

    def test_the_invariant_says_not_enforced_instead_of_ok(self):
        plan = build("G4")
        self.assertNotIn("acwr_cap", plan["invariants"]["checked"])
        self.assertEqual([i["id"] for i in plan["invariants"]["not_enforced"]], ["acwr_cap"])
        self.assertIn("not enforced", plan["invariants"]["not_enforced"][0]["detail"])
        entry = next(i for i in pe.check_invariants(plan) if i["id"] == "acwr_cap")
        self.assertTrue(entry["not_enforced"])
        self.assertEqual(build("G1")["invariants"]["not_enforced"], [])


# --- a real-data-shaped test (fictional numbers): a travel gap, a B race, easy overrides ------------------------

OWNER_TODAY = D(2027, 11, 25)
OWNER_WEEKS_KM = (36, 40, 35, 39, 37, 39, 38, 38.4)   # 4-week mean 38.1 km


def owner_case(overrides=(), loads=None):
    """Fictional numbers: LT 4:15/km, longest run 22.4 km, marathon prediction 3:38:40, runs on Mon/Tue/Wed/Fri
    (long run Friday), a marathon on 2028-02-25 (A) with a half-distance triathlon on 2028-01-28, no goal, travel
    on 13-19 Dec 2027 and the load model tracking Garmin."""
    acts, garmin = pe.sample_loads(OWNER_TODAY, [k * 1000.0 for k in OWNER_WEEKS_KM], **(loads or {}))
    snap = pe.Snapshot(today=OWNER_TODAY, week_m=tuple(pe.Meters(k * 1000.0) for k in OWNER_WEEKS_KM),
                       week_runs=(4,) * 8, longest_run_8w_m=pe.Meters(22400.0),
                       lt_speed_mps=pe.MetersPerSecond(1000.0 / 255.0), prediction_s=(("marathon", 13120),),
                       activity_load=acts, garmin_load=garmin)
    inputs = pe.PlanInputs(race_date=D(2028, 2, 25), b_races=(pe.BRace(D(2028, 1, 28), "tri_half"),),
                           run_days=(0, 1, 2, 4), rest_days=(3, 5, 6), long_run_day=4,
                           blocked_dates=tuple(D(2027, 12, 13) + dt.timedelta(days=i) for i in range(7)),
                           overrides=tuple(overrides))
    return inputs, snap


def _easy_runs(week):
    return [s for s in week["sessions"] if s["role"] in ("easy", "strides")]


def _key_of(plan, week, role, nth=0):
    """The key of week `week`'s nth session of `role` ("quality": any quality role), by date (session keys are
    their days)."""
    w = next(w for w in plan["weeks"] if w["week"] == week)
    roles = pe.QUALITY_ROLES if role == "quality" else (role,)
    return [s["key"] for s in w["sessions"] if s["role"] in roles][nth]


class OwnerAcwr(unittest.TestCase):
    """The three findings of a real-data test, on fictional inputs of the same shape."""

    def setUp(self):
        self.inputs, self.snap = owner_case()
        self.plan = pe.build_plan(self.inputs, self.snap, what_if_runs=False)

    def test_the_invariant_measures_the_series_the_cuts_constrain(self):
        # finding 1: the planned ratio peaked at 1.42 on 12 days while acwr_cap said ok
        plan = self.plan
        self.assertTrue(plan["acwr"]["enforced"])
        self.assertTrue(plan["invariants"]["ok"], plan["invariants"]["failed"])
        weeks, ctx = plan["_check"]["weeks"], plan["_check"]["ctx"]
        loads = pe.day_loads(weeks, ctx)
        held = {h["date"] for h in plan["acwr"]["held"]}
        runs = {s.date for w in weeks for s in w.sessions if s.counts and s.date > OWNER_TODAY}
        day = OWNER_TODAY + dt.timedelta(days=1)
        while day < self.inputs.race_date:   # every day with a planned run in its 7 days: <= 1.3 unless held
            if any(day - dt.timedelta(days=i) in runs for i in range(7)) and day.isoformat() not in held:
                self.assertLessEqual(pe.acwr_on(day, loads), 1.3 + 1e-9, day)
            day += dt.timedelta(days=1)
        floor_days = {d for w in plan["warnings"] if w["id"] == "acwr_floor" for d in w["dates"]}
        self.assertTrue(held and held <= floor_days | {h["date"] for h in plan["acwr"]["held"][5:]}, held)
        for h in plan["acwr"]["held"]:   # held: nothing of the owner week is left to cut for it
            day = D.fromisoformat(h["date"])
            owner = next(o for d, _r, o in pe.acwr_cap_days(weeks, ctx) if d == day)
            self.assertEqual(pe._cut_options(weeks, ctx, day, owner), [], day)
        # a run lengthened in a cut week is caught
        w6 = next(w for w in weeks if w.number == 6)
        easy = next(s for s in w6.sessions if s.role == "easy")
        w6.sessions[w6.sessions.index(easy)] = pe.make_session(easy.key, easy.date, "easy", pe.Meters(12000.0),
                                                               ctx.paces, ctx.rules)
        failed = {i["id"]: i for i in pe.check_invariants(plan) if not i["ok"]}
        self.assertIn("acwr_cap", failed)

    def test_cuts_keep_an_easy_run_and_eighty_percent_easy(self):
        # finding 2: weeks 5-7 ended with threshold + MP/intervals + long run and no easy run
        free = pe.build_plan(self.inputs, dataclasses.replace(self.snap, garmin_load=()), what_if_runs=False)
        for week, loose in zip(self.plan["weeks"], free["weeks"]):
            if _easy_runs(loose):
                self.assertTrue(_easy_runs(week), week["start"])
        for n in (5, 6, 7):
            week = next(w for w in self.plan["weeks"] if w["week"] == n)
            roles = [s["role"] for s in week["sessions"]]
            self.assertIn("easy", roles, n)
            self.assertIn("long", roles, n)
            self.assertTrue(any(r in pe.QUALITY_ROLES for r in roles), n)
        cut = next(c for c in self.plan["acwr"]["cuts"] if c["week"] == 6)
        detail = next(w["detail"] for w in self.plan["warnings"] if w["id"] == "acwr_cut"
                      and w["dates"] == ["2028-01-03"])
        self.assertIn(f"from {cut['from_km']} to {cut['to_km']} km", detail)
        self.assertIn("threshold", detail)   # quality is cut before an easy run goes
        self.assertTrue(self.plan["invariants"]["ok"], self.plan["invariants"]["failed"])

    def test_easing_a_session_changes_nothing_else(self):
        # finding 3: easing one session in weeks 5 and 6 cut ~25 km elsewhere and dropped week 6's easy run
        base = {s["key"]: s for w in self.plan["weeks"] for s in w["sessions"]}
        eased = (_key_of(self.plan, 5, "quality"), _key_of(self.plan, 6, "quality", 1))
        inputs, snap = owner_case([pe.Override("easy", key=k) for k in eased])
        plan = pe.build_plan(inputs, snap, what_if_runs=False)
        self.assertEqual([o["key"] for o in plan["overrides"]["applied"]], list(eased))
        self.assertTrue(plan["invariants"]["ok"], plan["invariants"]["failed"])
        now = {s["key"]: s for w in plan["weeks"] for s in w["sessions"]}
        self.assertEqual(set(now), set(base))
        for key, s in base.items():
            if key not in eased:
                self.assertEqual(now[key], s, key)
        self.assertEqual(plan["acwr"]["cuts"], self.plan["acwr"]["cuts"])
        # every warning describes the plan as it is, once
        quality = {s["date"] for s in now.values() if s["role"] in pe.QUALITY_ROLES}
        for w in plan["warnings"]:
            if w["id"] == "quality_dropped_easy_share":
                self.assertFalse(set(w["dates"]) & quality, w)
        ids = [(w["id"], tuple(w["dates"])) for w in plan["warnings"]]
        self.assertEqual(len(ids), len(set(ids)), ids)

    def test_held_days_are_listed_and_each_says_why(self):
        # 1.1.1: invariants.ok stays true for a held day (nothing left to cut), but the invariant lists it, and
        # acwr_floor gives each day's reason: an earlier week's runs or a B race in its 7 days, and the owner
        # week's runs there with no cut left
        plan = self.plan
        held = plan["acwr"]["held"]
        self.assertTrue(held)
        self.assertTrue(plan["invariants"]["ok"])
        listed = plan["invariants"]["held"]
        self.assertEqual([(i["id"], i["days"]) for i in listed], [("acwr_cap", len(held))])
        self.assertIn(f"{held[0]['date']}: ratio {held[0]['ratio']}", listed[0]["detail"])
        floors = [w["detail"] for w in plan["warnings"] if w["id"] == "acwr_floor"]
        for h in held:
            self.assertTrue(any(f"{h['date']} ({h['ratio']})" in d for d in floors), h)
        week6 = next(d for d in floors if "nothing of week 6 " in d)
        first = week6.split(" | ")[0]   # its first held day: most of its load is week 5's runs
        self.assertIn("already planned in an earlier week", first)
        self.assertIn("long run 2027-12-31", first)
        self.assertIn("is the B race on 2028-01-28", next(d for d in floors if "nothing of week 9 " in d))
        self.assertNotIn("quality and the long run at their floors", " ".join(floors))
        # the long-run feasibility check fails and the cap shortened a long run: said in one warning
        check = next(c for c in plan["feasibility"]["checks"] if c["id"] == "long_run_km")
        self.assertFalse(check["ok"])
        warn = next(w for w in plan["warnings"] if w["id"] == "long_run_acwr_cut")
        self.assertEqual(warn["dates"], ["2028-01-07"])
        self.assertIn("24.4 -> 22.4 km", warn["detail"])

    def test_the_cut_warning_says_what_an_override_made_of_a_cut_session(self):
        # 1.1.1: acwr.cuts keeps the record as cut; the acwr_cut text adds what the session is now
        quality, easy = _key_of(self.plan, 6, "quality", 1), _key_of(self.plan, 6, "easy")
        inputs, snap = owner_case([pe.Override("easy", key=quality)])
        plan = pe.build_plan(inputs, snap, what_if_runs=False)
        self.assertEqual(plan["acwr"]["cuts"], self.plan["acwr"]["cuts"])
        cut = next(w["detail"] for w in plan["warnings"] if w["id"] == "acwr_cut" and w["dates"] == ["2028-01-03"])
        self.assertIn("intervals 2028-01-05 9.3 -> 8.0 km (now an easy run", cut)
        day = next(s["date"] for w in self.plan["weeks"] for s in w["sessions"] if s["key"] == easy)
        inputs, snap = owner_case([pe.Override("rest", date=D.fromisoformat(day))])
        plan = pe.build_plan(inputs, snap, what_if_runs=False)
        self.assertEqual(plan["overrides"]["refused"], [])
        cut = next(w["detail"] for w in plan["warnings"] if w["id"] == "acwr_cut" and w["dates"] == ["2028-01-03"])
        self.assertIn(f"easy run {day} 12.2 -> 4.0 km (now a rest day)", cut)
        before = next(w["detail"] for w in self.plan["warnings"] if w["id"] == "acwr_cut"
                      and w["dates"] == ["2028-01-03"])
        self.assertNotIn("(now", before)

    def test_a_refused_override_leaves_no_trace(self):
        # the last long run before the half-distance triathlon cut to 8 km: the first one after it would be over
        # 0.75 x it (rule 7 follows the plan as it is), so the edit is undone and refused
        inputs, snap = owner_case([pe.Override("cap_long", key=_key_of(self.plan, 8, "long"), km=8.0)])
        plan = pe.build_plan(inputs, snap, what_if_runs=False)
        self.assertEqual(plan["overrides"]["refused"][0]["rule"], "b_windows")
        self.assertEqual(plan["weeks"], self.plan["weeks"])
        self.assertEqual(plan["warnings"], self.plan["warnings"])
        self.assertEqual(plan["acwr"], self.plan["acwr"])


class AcwrMargin(unittest.TestCase):
    def test_signed_gaps_and_a_warning_near_the_tolerance(self):
        # 1.1.1, report only: each check carries model minus Garmin, and a deciding gap within 0.02 of the 0.15
        # tolerance warns that the switch is marginal; how enforcement switches on is unchanged
        g = GOLDEN["G1"]
        for gap, enforced, marginal in ((0.10, True, False), (0.14, True, True), (0.16, False, True),
                                        (0.30, False, False)):
            with self.subTest(gap=gap):
                acts, garmin = pe.sample_loads(TODAY, g["snapshot"].week_m, garmin_gap=gap)
                snap = dataclasses.replace(g["snapshot"], activity_load=acts, garmin_load=garmin)
                plan = pe.build_plan(g["inputs"], snap, what_if_runs=False)
                self.assertEqual(plan["acwr"]["enforced"], enforced)
                self.assertEqual({c["signed_gap"] for c in plan["acwr"]["checks"]}, {-gap})
                warns = [w["detail"] for w in plan["warnings"] if w["id"] == "acwr_marginal"]
                self.assertEqual(bool(warns), marginal)
                if warns:
                    self.assertIn(f"switch the cap {'off' if enforced else 'on'}", warns[0])


# --- rules v2: race pacing (item 13) --------------------------------------------------------------------------

def _course(km_total=42.2, climbs=((10.0, 12.0, 60.0), (30.0, 31.0, 40.0))):
    return pe.Course(pe.Meters(km_total * 1000.0),
                     tuple(pe.Climb(pe.Meters(a * 1000.0), pe.Meters(b * 1000.0), pe.Meters(g)) for a, b, g in climbs))


def _pace_s(text):
    m, s = text.split(":")
    return int(m) * 60 + int(s)


class RacePacing(unittest.TestCase):
    def plan(self, course, **inputs):
        case = GOLDEN["G1"]
        return pe.build_plan(dataclasses.replace(case["inputs"], course_id=4242, **inputs),
                             dataclasses.replace(case["snapshot"], course=course), what_if_runs=False)

    def test_climbs_slower_flats_faster_and_the_goal_time_held(self):
        plan = self.plan(_course())
        rp = plan["race_pacing"]
        self.assertEqual(plan["inputs"]["course_id"], 4242)
        self.assertEqual(rp["course_id"], 4242)
        self.assertEqual(rp["missing"], [])
        self.assertEqual(rp["rules"], pe.RULES["race_pacing"])
        kinds = [s["kind"] for s in rp["segments"]]
        self.assertEqual(kinds, ["flat", "climb", "flat", "climb", "flat"])
        self.assertEqual(rp["segments"][1]["grade_pct"], 3.0)
        self.assertEqual(rp["segments"][3]["gain_m"], 40)
        goal = _pace_s(rp["goal_pace"])
        flat = _pace_s(rp["flat_pace"])
        self.assertLess(flat, goal)
        for s in rp["segments"]:
            if s["kind"] == "climb":
                self.assertGreater(_pace_s(s["pace"]), goal)
        finish = sum(s["time_s"] for s in rp["segments"])
        target = (3 * 3600 + 52 * 60) * 42200 / 42195   # the goal pace over a 42.2 km course: 3:52:02
        self.assertEqual(finish, round(target))   # the segments add up to the finish exactly
        self.assertEqual(rp["finish"], "3:52:02")
        self.assertEqual(rp["segments"][-1]["at"], "3:52:02")
        self.assertNotIn("course_distance_mismatch", [w["id"] for w in plan["warnings"]])
        self.assertTrue(plan["invariants"]["ok"])

    def test_a_course_of_another_length_warns(self):
        plan = self.plan(_course(km_total=39.0))
        self.assertIn("course_distance_mismatch", [w["id"] for w in plan["warnings"]])
        self.assertEqual(plan["race_pacing"]["course_km"], 39.0)

    def test_overlapping_and_outside_climbs_are_clipped(self):
        rp = self.plan(_course(climbs=((5.0, 8.0, 90.0), (7.0, 9.0, 40.0), (41.0, 45.0, 50.0),
                                       (20.0, 20.0005, 5.0))))["race_pacing"]
        ends = [(s["from_km"], s["to_km"]) for s in rp["segments"]]
        self.assertEqual(ends[0], (0.0, 5.0))
        self.assertEqual(ends[-1][1], 42.2)
        for (_a, b), (c, _d) in zip(ends, ends[1:]):
            self.assertEqual(b, c)

    def test_missing_course_or_goal(self):
        plan = self.plan(None)
        self.assertEqual(plan["race_pacing"]["missing"], ["course"])
        self.assertEqual(plan["race_pacing"]["segments"], [])
        no_goal = pe.build_plan(dataclasses.replace(GOLDEN["G1"]["inputs"], goal_time_s=None, course_id=1),
                                dataclasses.replace(GOLDEN["G1"]["snapshot"], course=_course(), prediction_s=()),
                                what_if_runs=False)
        self.assertEqual(no_goal["race_pacing"]["missing"], ["goal_time"])
        by_prediction = pe.build_plan(dataclasses.replace(GOLDEN["G1"]["inputs"], goal_time_s=None, course_id=1),
                                      dataclasses.replace(GOLDEN["G1"]["snapshot"], course=_course()),
                                      what_if_runs=False)
        self.assertEqual(by_prediction["race_pacing"]["goal_time"], "3:57:00")

    def test_course_id_is_hashed(self):
        a = self.plan(_course())
        self.assertNotEqual(a["content_hash"], build("G1")["content_hash"])


# --- rule 7: the long runs and race-week runs around a B race -------------------------------------------------

# Fictional numbers: about 40 km a week, longest run 22.4 km, LT 3.92 m/s, runs on Monday, Tuesday, Wednesday
# and Friday with the long run on Friday, no goal time.
SHAPE_WEEKS = (38.0, 41.0, 39.0, 42.0, 40.0, 41.0, 39.0, 42.0)
A_RACE = D(2028, 2, 25)
# rule 7's B-race limits switched off: the plan the engine made before them
NO_B_LIMITS = copy.deepcopy(pe.RULES)
NO_B_LIMITS["b_race"].update(long_before_of_peak=9.0, mini_taper_max_km=99.0,
                             long_after_of_peak={k: 9.0 for k in pe.B_RACE_KINDS},
                             long_after_min_days={k: 0 for k in pe.B_RACE_KINDS},
                             long_after_of_last={"tri_half": 9.0})


def _shape_snap(loads=None):
    return _snap(TODAY, SHAPE_WEEKS, 22.4, lt=3.92, loads=loads)


def _shape_inputs(*b_races, **kw):
    base = dict(race_date=A_RACE, b_races=tuple(b_races), run_days=(0, 1, 2, 4), rest_days=(3, 5, 6),
                long_run_day=4)
    base.update(kw)
    return pe.PlanInputs(**base)


def _runs(plan):
    """{date: session} of the plan's runs (not the races)."""
    return {s["date"]: s for w in plan["weeks"] for s in w["sessions"] if s["role"] not in ("race", "b_race")}


def _ref(plan, day):
    """Rule 7's reference from the answer: the longest long run of a normal week 21+ days before the A race
    dated before `day`; before one, the longest of the 8 weeks' longest run and the long runs before `day`."""
    a_race = D.fromisoformat(plan["race"]["date"])
    longs = [(D.fromisoformat(s["date"]), s["km"], w["kind"]) for w in plan["weeks"] for s in w["sessions"]
             if s["role"] == "long" and D.fromisoformat(s["date"]) < day]
    peak = [k for d, k, kind in longs if kind == "normal" and (a_race - d).days >= 21]
    return max(peak) if peak else max([plan["start"]["longest_run_8w_km"]] + [k for _d, k, _kind in longs])


def _capped(plan):
    return [d for w in plan["warnings"] if w["id"] == "b_race_capped" for d in w["dates"]]


class BRaceWindow(unittest.TestCase):
    def build(self, inputs, snap=None, rules=pe.RULES):
        plan = pe.build_plan(inputs, snap or _shape_snap(), rules, what_if_runs=False)
        self.assertTrue(plan["invariants"]["ok"], plan["invariants"]["failed"])
        return plan

    def test_rules_are_in_rules(self):
        self.assertEqual(pe.RULES["b_race"], {
            "long_before_days": [5, 13], "long_before_of_peak": 0.70, "mini_taper_max_km": 8.0,
            "long_after_of_peak": {"tri_full": 0.60, "tri_half": 0.70, "run": 0.70},
            "long_after_min_days": {"tri_full": 10, "tri_half": 0, "run": 0},
            "long_after_of_last": {"tri_half": 0.75}, "long_after_fallback_of_cap": [0.65, 0.75]})
        self.assertEqual(pe.RULES_VERSION, 3)

    def test_long_run_before_a_b_race_is_held_and_not_made_up(self):
        b_day, long_day = D(2028, 1, 28), "2028-01-21"   # a Friday B race: the long run 7 days before it
        for kind in pe.B_RACE_KINDS:
            with self.subTest(kind=kind):
                inputs = _shape_inputs(pe.BRace(b_day, kind))
                before, plan = self.build(inputs, rules=NO_B_LIMITS), self.build(inputs)
                old, new = _runs(before)[long_day], _runs(plan)[long_day]
                limit = 0.70 * _ref(plan, D.fromisoformat(long_day))
                self.assertGreater(old["km"], limit)                      # the limit binds here
                self.assertEqual(new["role"], "long")
                self.assertLessEqual(new["km"], limit + 0.05)
                self.assertGreaterEqual(new["km"], limit - 0.15)          # held at it, 100 m steps
                self.assertIn(long_day, _capped(plan))
                # no make-up: the other runs of the week are no longer, so the week drops by the cut
                wk_old = next(w for w in before["weeks"] if w["start"] <= long_day <= w["end"])
                wk_new = next(w for w in plan["weeks"] if w["start"] <= long_day <= w["end"])
                for s in wk_new["sessions"]:
                    if s["date"] != long_day:
                        self.assertLessEqual(s["km"], _runs(before)[s["date"]]["km"] + 0.05, s)
                self.assertLessEqual(wk_new["planned_km"], wk_old["planned_km"] - (old["km"] - new["km"]) + 0.15)
                self.assertEqual(wk_new["target_km"], wk_old["target_km"])

    def test_a_long_run_14_days_before_is_not_held(self):
        plan = self.build(_shape_inputs(pe.BRace(D(2028, 1, 28), "tri_half")))
        self.assertNotIn("2028-01-14", _capped(plan))
        self.assertEqual(_runs(plan)["2028-01-14"]["km"],
                         _runs(self.build(_shape_inputs(pe.BRace(D(2028, 1, 28), "tri_half")),
                                          rules=NO_B_LIMITS))["2028-01-14"]["km"])

    def test_race_week_runs_are_at_most_8_km(self):
        for kind in pe.B_RACE_KINDS:
            with self.subTest(kind=kind):
                inputs = _shape_inputs(pe.BRace(D(2028, 1, 28), kind))
                before, plan = _runs(self.build(inputs, rules=NO_B_LIMITS)), self.build(inputs)
                mini = [f"2028-01-{d}" for d in (24, 25, 26)]   # Monday to Wednesday (Thursday is a rest day)
                self.assertTrue(any(before[d]["km"] > 8.0 for d in mini))
                for d in mini:
                    s = _runs(plan)[d]
                    self.assertEqual(s["role"], "strides")   # strides stay
                    self.assertAlmostEqual(s["km"], min(before[d]["km"], 8.0), delta=0.05)
                self.assertTrue(set(d for d in mini if before[d]["km"] > 8.0) <= set(_capped(plan)))

    def test_first_long_run_after_a_b_race(self):
        for kind, share in (("tri_half", 0.70), ("run", 0.70)):
            with self.subTest(kind=kind):
                plan = self.build(_shape_inputs(pe.BRace(D(2028, 1, 28), kind)))
                after = _runs(plan)["2028-02-04"]   # the first long run, 7 days after
                self.assertEqual(after["role"], "long")
                self.assertLessEqual(after["km"], share * _ref(plan, D(2028, 2, 4)) + 0.05)
                self.assertIn("2028-02-04", _capped(plan))
                # only the first: the next long run is not held by rule 7
                self.assertNotIn("2028-02-11", _capped(plan))

    def test_full_distance_triathlon_long_run_waits_10_days_at_0_60(self):
        # a Wednesday full-distance triathlon: 7 recovery days, then the Friday long run 9 days after it
        b_day = D(2028, 1, 5)
        inputs = _shape_inputs(pe.BRace(b_day, "tri_full"))
        before, plan = self.build(inputs, rules=NO_B_LIMITS), self.build(inputs)
        self.assertEqual(_runs(before)["2028-01-14"]["role"], "long")
        early = _runs(plan)["2028-01-14"]
        self.assertEqual(early["role"], "easy")
        self.assertFalse(early["hard_or_long"])
        self.assertLessEqual(early["km"], 14.0)
        self.assertIn("2028-01-14", _capped(plan))
        first = min((s for s in _runs(plan).values() if s["role"] == "long" and s["date"] > b_day.isoformat()),
                    key=lambda s: s["date"])
        self.assertGreaterEqual((D.fromisoformat(first["date"]) - b_day).days, 10)
        self.assertLessEqual(first["km"], 0.60 * _ref(plan, D.fromisoformat(first["date"])) + 0.05)

    def test_b_race_in_week1_caps_week0_and_the_long_run_after_it(self):
        plan = self.build(_shape_inputs(pe.BRace(D(2027, 11, 29), "run")))   # mini-taper from Thursday of week 0
        self.assertEqual(plan["b_races"][0]["mini_taper"], ["2027-11-25", "2027-11-28"])
        self.assertLessEqual(_runs(plan)["2027-11-26"]["km"], 8.0)
        self.assertIn("2027-11-26", _capped(plan))
        first = _runs(plan)["2027-12-10"]   # the Friday 4 days after is a recovery day; the next is the first
        self.assertEqual(first["role"], "long")
        self.assertLessEqual(first["km"], 0.70 * 22.4 + 0.05)   # before any peak: the 8 weeks' longest run

    def test_the_check_catches_each_part(self):
        plan = self.build(_shape_inputs(pe.BRace(D(2028, 1, 28), "tri_half")))
        weeks, ctx = plan["_check"]["weeks"], plan["_check"]["ctx"]
        for day, role, km in (("2028-01-21", "long", 25.0), ("2028-01-25", "strides", 9.0),
                              ("2028-02-04", "long", 25.0)):
            with self.subTest(day=day):
                w = next(w for w in weeks if any(s.date.isoformat() == day for s in w.sessions))
                i, s = next((i, s) for i, s in enumerate(w.sessions) if s.date.isoformat() == day)
                saved = w.sessions[i]
                w.sessions[i] = pe.make_session(s.key, s.date, role, pe.Meters(km * 1000.0), ctx.paces, ctx.rules)
                failed = {x["id"]: x["detail"] for x in pe.check_invariants(plan) if not x["ok"]}
                w.sessions[i] = saved
                self.assertIn("b_windows", failed)
                self.assertIn(day, failed["b_windows"])
        # a full-distance triathlon's first long run under 10 days after it
        b_day = D(2028, 1, 5)
        plan = self.build(_shape_inputs(pe.BRace(b_day, "tri_full")))
        weeks, ctx = plan["_check"]["weeks"], plan["_check"]["ctx"]
        w = next(w for w in weeks if any(s.date == D(2028, 1, 14) for s in w.sessions))
        i, s = next((i, s) for i, s in enumerate(w.sessions) if s.date == D(2028, 1, 14))
        w.sessions[i] = pe.make_session(s.key, s.date, "long", pe.Meters(8000.0), ctx.paces, ctx.rules)
        failed = {x["id"]: x["detail"] for x in pe.check_invariants(plan) if not x["ok"]}
        self.assertIn("9 days after a tri_full", failed.get("b_windows", ""))

    def test_overrides_cannot_break_it(self):
        default_days = dict(run_days=(1, 2, 3, 5, 6), rest_days=(0, 4))
        # long_run_day: a Saturday long run 14 days before a Saturday B race moved to Sunday (13 days)
        inputs = _shape_inputs(pe.BRace(D(2028, 1, 29), "tri_half"), long_run_day=5, **default_days)
        base = self.build(inputs)
        self.assertNotIn("2028-01-15", _capped(base))
        o = pe.Override("long_run_day", week_start=D(2028, 1, 10), weekday=6)
        plan = self.build(dataclasses.replace(inputs, overrides=(o,)))
        self.assertEqual([r["rule"] for r in plan["overrides"]["refused"]], ["b_window"])
        self.assertEqual(plan["weeks"], base["weeks"])
        # long_run_day: a full-distance triathlon on Thursday; its first long run moved from 10 to 9 days after
        inputs = _shape_inputs(pe.BRace(D(2028, 1, 6), "tri_full"), long_run_day=6, **default_days)
        base = self.build(inputs)
        self.assertEqual(_runs(base)["2028-01-16"]["role"], "long")
        o = pe.Override("long_run_day", week_start=D(2028, 1, 10), weekday=5)
        plan = self.build(dataclasses.replace(inputs, overrides=(o,)))
        self.assertEqual([r["rule"] for r in plan["overrides"]["refused"]], ["b_window"])
        # swap: a run over 8 km onto a B race-week day
        inputs = _shape_inputs(pe.BRace(D(2028, 1, 30), "run"), long_run_day=6, **default_days)
        base = self.build(inputs)
        runs = _runs(base)
        self.assertGreater(runs["2028-01-25"]["km"], 8.0)                 # Tuesday, before the mini-taper
        self.assertEqual(base["b_races"][0]["mini_taper"][0], "2028-01-26")
        o = pe.Override("swap", date=D(2028, 1, 25), date_b=D(2028, 1, 26))
        plan = self.build(dataclasses.replace(inputs, overrides=(o,)))
        self.assertEqual([r["rule"] for r in plan["overrides"]["refused"]], ["b_window"])
        # an edit that only shortens is still applied: cap_km on the long run before the B race
        o = pe.Override("cap_km", date=D(2028, 1, 23), km=12.0)
        plan = self.build(dataclasses.replace(inputs, overrides=(o,)))
        self.assertEqual(plan["overrides"]["refused"], [])

    def test_regression_long_friday_half_ironman_before_a_marathon(self):
        """The shape that made the rule: a Friday long run, a half-distance triathlon B race on a Friday four
        weeks before the A marathon, about 40 km a week. Without rule 7's limits the plan had the peak long
        run 7 days before the B race, three strides runs over 8 km in its race week and a 21+ km long run 7
        days after it."""
        snap = _shape_snap(loads={})
        inputs = _shape_inputs(pe.BRace(D(2028, 1, 28), "tri_half"))
        before, plan = _runs(self.build(inputs, snap, NO_B_LIMITS)), self.build(inputs, snap)
        peak_before = max(s["km"] for s in before.values() if s["role"] == "long")
        self.assertEqual(before["2028-01-21"]["km"], peak_before)
        self.assertTrue(all(before[d]["km"] > 8.0 for d in ("2028-01-24", "2028-01-25", "2028-01-26")))
        self.assertGreater(before["2028-02-04"]["km"], 21.0)
        runs = _runs(plan)
        peak = max(s["km"] for s in runs.values() if s["role"] == "long")
        for day in ("2028-01-21", "2028-02-04"):
            self.assertLessEqual(runs[day]["km"], 0.70 * peak + 0.05)
        for day in ("2028-01-24", "2028-01-25", "2028-01-26"):
            self.assertLessEqual(runs[day]["km"], 8.0)
        self.assertEqual(sorted(set(_capped(plan))), ["2028-01-21", "2028-01-24", "2028-01-25", "2028-01-26",
                                                      "2028-02-04"])
        self.assertTrue(plan["acwr"]["enforced"])


# --- a real-data shape: a half-distance triathlon B race, a travel week, cap_long --------------------------

# The shape of a real case (fictional numbers, as above): a marathon on Friday 2028-02-25, a half-distance
# triathlon (tri_half) on Friday 2028-01-28, runs Mon/Tue/Wed/Fri with the long run on Friday, rest
# Thu/Sat/Sun, no goal time, and a travel week 2027-12-13..19 with every date blocked.
TRAVEL = tuple(D(2027, 12, 13) + dt.timedelta(days=i) for i in range(7))
B_RACE_DAY = D(2028, 1, 28)


def owner_inputs(*overrides, **kw):
    kw.setdefault("blocked_dates", TRAVEL)
    return _shape_inputs(pe.BRace(B_RACE_DAY, "tri_half"), overrides=tuple(overrides), **kw)


def owner_plan(*overrides, **kw):
    return pe.build_plan(owner_inputs(*overrides, **kw), _shape_snap(loads={}), what_if_runs=False)


# The real-data shape (fictional numbers): 8 weeks of 35-40 km, the longest run 22.4 km, LT 4:15/km, Garmin's
# marathon prediction 3:38:40, loads that track Garmin.
REAL_TODAY = D(2027, 11, 25)
REAL_WEEKS = (36.0, 40.0, 35.0, 39.0, 37.0, 39.0, 38.0, 38.4)


def real_snap():
    acts, garmin = pe.sample_loads(REAL_TODAY, [k * 1000.0 for k in REAL_WEEKS])
    return pe.Snapshot(today=REAL_TODAY, week_m=tuple(pe.Meters(k * 1000.0) for k in REAL_WEEKS),
                       week_runs=(4,) * 8, longest_run_8w_m=pe.Meters(22400.0),
                       lt_speed_mps=pe.MetersPerSecond(1000 / 255.0), prediction_s=(("marathon", 13120),),
                       activity_load=acts, garmin_load=garmin)


def long_step_problems(plan, step_km=2.0):
    """The long-run step recomputed from the answer alone (rule 3 with the gap rule): a normal week's long run
    is at most the step reference + 2 km. The reference is the last long run of a normal week 21+ days before
    the A race that rule 7 does not hold (from the 8 weeks' longest run). After a gap (a normal or deload week
    with no long run and a blocked run day, or no run at all) the first normal week's long run gets no step:
    at most the reference itself; deload and B-race long runs in between change nothing."""
    a_race = D.fromisoformat(plan["race"]["date"])
    blocked = set(plan["inputs"]["blocked_dates"])
    run_days = {pe.WEEKDAYS.index(d) for d in plan["inputs"]["run_days"]} - \
        {pe.WEEKDAYS.index(d) for d in plan["inputs"]["rest_days"]}
    ref = plan["start"]["longest_run_8w_km"] or 0.0
    gap = False
    out = []
    # a week a reduce-only override took under the rules (override_relaxed) keeps its long run as built for the
    # step, which the answer does not show: the next normal week's long run is not checked, the step resumes from it
    relaxed = {x["dates"][0] for x in plan["warnings"] if x["id"] == "override_relaxed" and x["dates"]}
    unknown = False
    for w in plan["weeks"]:
        if w["kind"] == "week0":
            continue
        if w["start"] in relaxed:
            unknown = True
            continue
        longs = [s for s in w["sessions"] if s["role"] == "long"]
        runs = [s for s in w["sessions"] if s["role"] not in ("race", "b_race")]
        if w["kind"] in ("normal", "deload") and not longs:
            start = D.fromisoformat(w["start"])
            days = [start + dt.timedelta(days=i) for i in range(7)]
            if not runs or any(d.isoformat() in blocked and d.weekday() in run_days for d in days):
                gap = True
            continue
        if not longs or w["kind"] != "normal":
            continue
        lmax = max(s["km"] for s in longs)
        allowed = ref + (0.0 if gap else step_km)
        if lmax > allowed + 0.05 and not unknown:
            out.append(f"{w['start']}: long {lmax} km > {ref} km" + (" (after a gap)" if gap else f" + {step_km} km"))
        gap = False
        if (a_race - D.fromisoformat(longs[0]["date"])).days >= 21 and not _b_held(plan, longs[0]["date"]):
            ref = lmax
            unknown = False
    return out


def _b_held(plan, day):
    """True when rule 7 holds a long run on `day`: 5-13 days before a B race, or the first long run after one."""
    d = D.fromisoformat(day)
    longs = sorted(s["date"] for w in plan["weeks"] for s in w["sessions"] if s["role"] == "long")
    for b in plan["b_races"]:
        bd = D.fromisoformat(b["date"])
        if 5 <= (bd - d).days <= 13:
            return True
        if d > bd and not any(b["date"] < x < day for x in longs):
            return True
    return False


def easy_run_due(plan, start):
    """Rule 4's easy-run rule for the week starting `start` (ISO), recomputed from the finished plan: a week with
    a quality session or a long run must hold an easy (or strides) run when it has no calendar item, a free run
    day outside a B window, and room in its target beside the quality and long km (as built, before the ratio
    cuts) for an easy run of the easy minimum. False: the week is exempt (or has no quality and no long run)."""
    ctx = plan["_check"]["ctx"]
    w = next(x for x in plan["_check"]["weeks"] if x.start.isoformat() == start)
    fixed = sum(float(w.acwr_ops[s.key][1]) if s.key in w.acwr_ops else float(s.distance)
                for s in w.sessions if s.role in pe.QUALITY_ROLES + ("long",))
    used = {s.date for s in w.sessions}
    free = any(ctx.runnable(d) and d not in used and ctx.window_factor(d) is None for d in w.days())
    room = float(w.target) - w.b_cut - fixed >= float(ctx.rules["easy_min_km"]) * 1000.0
    return fixed > 0 and free and room and not pe.calendar_week(w, ctx)


def ci_owner_like_case():
    """The travel-gap property's case CI found (2026-11-26): a marathon 2028-02-18, runs Mon-Fri with the long
    run on Monday, travel 10-14 Dec, a flat 30 km history with one 26.25 km week, the load model tracking."""
    week_m = tuple(pe.Meters(k) for k in (30000.0,) * 6 + (26250.0, 30000.0))
    acts, garmin = pe.sample_loads(TODAY, week_m, garmin_gap=0.0, run_lpm=2.0, bike_min=0, swim_min=45)
    snap = pe.Snapshot(today=TODAY, week_m=week_m, week_runs=(4,) * 8, longest_run_8w_m=pe.Meters(25400.0),
                       lt_speed_mps=pe.MetersPerSecond(4.0), prediction_s=(("marathon", 12600),),
                       activity_load=acts, garmin_load=garmin)
    inputs = pe.PlanInputs(race_date=D(2028, 2, 18), run_days=(0, 1, 2, 3, 4), rest_days=(5, 6), long_run_day=0,
                           blocked_dates=tuple(D(2027, 12, 10) + dt.timedelta(days=i) for i in range(5)))
    return inputs, snap


class BlockedLongDay(unittest.TestCase):
    def test_blocking_the_days_long_run_does_not_raise_the_other_days(self):
        # The blocked-date property's case (hypothesis 6.170.15, run after the mutation suite): the week after a
        # full-distance triathlon has its short long run (easy, 8.9 km) on Tuesday, the other days split what is
        # left (8.5 km each). Blocking that Tuesday took the long run's spot, so the week was an even split of
        # all its km and the other days rose to 8.6. Rule 11: the blocked day's share drops, the others stay.
        week_m = tuple(pe.Meters(k) for k in (8000.0,) * 6 + (22030.0, 90000.0))
        snap = pe.Snapshot(today=D(2027, 11, 26), week_m=week_m, week_runs=(4,) * 8,
                           longest_run_8w_m=pe.Meters(7200.0), prediction_s=(("marathon", 13200), ("half", 6300)))
        inputs = pe.PlanInputs(race_date=D(2028, 1, 3), b_races=(pe.BRace(D(2027, 12, 20), "tri_full"),),
                               run_days=(0, 1, 2, 3, 4), rest_days=(5, 6), long_run_day=0, quality_per_week=1,
                               max_long_km=24.0)
        def week_of(p):
            return {s["date"]: s["km"] for w in pe.plan_answer(p)["weeks"] if w["start"] == "2027-12-27"
                    for s in w["sessions"]}

        base = week_of(pe.build_plan(inputs, snap, what_if_runs=False))
        self.assertEqual(base, {"2027-12-28": 8.9, "2027-12-29": 8.5, "2027-12-30": 8.5, "2027-12-31": 8.5})
        edited = pe.build_plan(dataclasses.replace(inputs, blocked_dates=(D(2027, 12, 28),)), snap,
                               what_if_runs=False)
        self.assertTrue(edited["invariants"]["ok"], edited["invariants"]["failed"])
        self.assertEqual(week_of(edited), {"2027-12-29": 8.5, "2027-12-30": 8.5, "2027-12-31": 8.5})


class EasyRunRule(unittest.TestCase):
    def test_a_peak_week_without_room_for_an_easy_run_is_exempt(self):
        # CI's travel-gap case: in the week of 31 Jan the long run and the two quality sessions left less than the
        # 4 km easy minimum of the target, so no easy run fitted and easy_share rightly passed. Since 1.1.1 the
        # ratio cap never cuts a long run below the last normal week's, the weeks after the gap are bigger and
        # this week has room and an easy run. The exemption is checked on that week with its easy run taken
        # out: under 4 km of room it is exempt (in the property's helper and the invariant alike); with 10 km
        # more target the invariant fails without one.
        inputs, snap = ci_owner_like_case()
        plan = pe.build_plan(inputs, snap, what_if_runs=False)
        self.assertTrue(plan["acwr"]["enforced"])
        self.assertTrue(plan["invariants"]["ok"], plan["invariants"]["failed"])
        week = next(w for w in plan["weeks"] if w["start"] == "2028-01-31")
        roles = [s["role"] for s in week["sessions"]]
        self.assertIn("long", roles)
        self.assertTrue({"easy", "strides"} & set(roles))
        self.assertTrue(easy_run_due(plan, "2028-01-31"))
        w = next(x for x in plan["_check"]["weeks"] if x.start == D(2028, 1, 31))
        w.sessions = [s for s in w.sessions if s.role not in pe.EASY_ROLES]
        fixed = sum(pe._built_m(w, s) for s in w.sessions if s.role in pe.QUALITY_ROLES + ("long",))
        w.target = pe.Meters(fixed + 3700.0 + w.b_cut)
        self.assertFalse(easy_run_due(plan, "2028-01-31"))
        failed = {i["id"]: i["detail"] for i in pe.check_invariants(plan) if not i["ok"]}
        self.assertNotIn(f"week {w.number}: no easy run", failed.get("easy_share", ""))
        # the rule bites when there is room: give the week 10 km more target and the invariant fails without one
        w.target = pe.Meters(w.target + 10000.0)
        self.assertTrue(easy_run_due(plan, "2028-01-31"))
        failed = {i["id"]: i["detail"] for i in pe.check_invariants(plan) if not i["ok"]}
        self.assertIn(f"week {w.number}: no easy run", failed.get("easy_share", ""))


class OwnerCase(unittest.TestCase):
    def setUp(self):
        self.plan = owner_plan()
        self.assertTrue(self.plan["invariants"]["ok"], self.plan["invariants"]["failed"])

    def test_tri_half_recovery_is_easy_through_race_day_plus_6(self):
        self.assertEqual(self.plan["b_races"][0]["recovery"], ["2028-01-29", "2028-02-03"])
        for day, s in _runs(self.plan).items():
            if "2028-01-29" <= day <= "2028-02-03":
                self.assertEqual(s["role"], "easy", s)
        self.assertEqual(pe.RULES["b_race"]["long_after_of_last"], {"tri_half": 0.75})
        self.assertEqual(pe.RULES["b_race"]["long_after_fallback_of_cap"], [0.65, 0.75])

    def test_first_long_run_after_tri_half_is_at_most_075_of_the_last_before(self):
        runs = _runs(self.plan)
        before = max((s for s in runs.values() if s["role"] == "long" and s["date"] < B_RACE_DAY.isoformat()),
                     key=lambda s: s["date"])
        after = min((s for s in runs.values() if s["role"] == "long" and s["date"] > B_RACE_DAY.isoformat()),
                    key=lambda s: s["date"])
        self.assertEqual(after["date"], "2028-02-04")
        self.assertLessEqual(after["km"], 0.75 * before["km"] + 0.05)
        self.assertGreaterEqual(after["km"], 0.75 * before["km"] - 0.15)   # a ceiling it is held at (100 m)
        self.assertIn("2028-02-04", _capped(self.plan))

    def test_notes_after_tri_half(self):
        runs = _runs(self.plan)
        after = runs["2028-02-04"]
        cap = after["km"]
        lo, hi = int(0.65 * cap + 0.5), int(0.75 * cap + 0.5)
        self.assertEqual(after["note"], f"Run up to {cap} km only if your legs feel normal and your HRV is back "
                                        f"in its usual range; otherwise run {lo}-{hi} km easy.")
        first = min((s for s in runs.values() if s["date"] > B_RACE_DAY.isoformat()), key=lambda s: s["date"])
        self.assertEqual(first["role"], "easy")
        self.assertEqual(first["note"], "Skip it if you're still sore.")
        noted = sorted(s["date"] for s in runs.values() if "note" in s)
        self.assertEqual(noted, sorted([first["date"], "2028-02-04"]))

    def test_tri_full_and_run_get_no_notes(self):
        for kind in ("tri_full", "run"):
            with self.subTest(kind=kind):
                plan = pe.build_plan(_shape_inputs(pe.BRace(B_RACE_DAY, kind)), _shape_snap(), what_if_runs=False)
                self.assertFalse([s for s in _runs(plan).values() if "note" in s])

    def test_after_the_travel_week_the_long_run_returns_to_the_one_before_it(self):
        # the first normal week after the gap returns to the last normal week's long run (23.6 km), no +2 km;
        # the deload in between (18.4 km) does not reset it. These loads track Garmin, so the ratio cap (rule 12)
        # cuts the return week, but never its long run below the last normal week's (1.1.1): the other runs
        # take the cut, the long run stays at 23.6 km and the next week steps from it
        runs = _runs(self.plan)
        self.assertEqual(long_step_problems(self.plan), [])
        self.assertEqual([runs[d]["km"] for d in ("2027-12-10", "2027-12-24")], [23.6, 18.4])
        w5 = next(w for w in self.plan["_check"]["weeks"] if w.start == D(2027, 12, 27))
        self.assertTrue(w5.acwr_ops)
        self.assertNotIn("long", {op[0] for op in w5.acwr_ops.values()})
        self.assertEqual(runs["2027-12-31"]["km"], 23.6)
        self.assertGreaterEqual(runs["2028-01-07"]["km"], runs["2027-12-31"]["km"])
        self.assertLessEqual(runs["2028-01-07"]["km"], runs["2027-12-31"]["km"] + 2.0 + 0.05)

    def test_real_data_shape_returns_to_the_long_run_before_the_gap_then_steps_2_km(self):
        plan = pe.build_plan(owner_inputs(), real_snap(), what_if_runs=False)
        self.assertTrue(plan["invariants"]["ok"], plan["invariants"]["failed"])
        self.assertEqual(long_step_problems(plan), [])
        longs = [(w["kind"], w["long_km"]) for w in plan["weeks"] if w["kind"] != "week0"][:7]
        # With the ratio cap enforced on these loads, the return week's long run is planned at 22.4 km (the
        # one before the gap, no step); the cap cuts the week's other runs but never takes a normal week's long
        # run below the last normal week's (1.1.1): 22.4, then 24.4 cut back to 22.4, then 24.4.
        self.assertEqual(longs, [("normal", 22.4), ("normal", 22.4), ("normal", None), ("deload", 17.4),
                                 ("normal", 22.4), ("normal", 22.4), ("normal", 24.4)])
        built = {w.number: {op[3].isoformat(): (op[0], op[1], op[2]) for op in w.acwr_ops.values()}
                 for w in plan["_check"]["weeks"]}
        self.assertTrue(built[5])
        self.assertNotIn("2027-12-31", built[5])   # the return week's long run is not cut
        self.assertEqual(built[6]["2028-01-07"], ("long", 24400.0, 22400.0))   # 22.4 + 2 km, cut back to 22.4
        self.assertEqual([x["id"] for x in plan["warnings"] if "2027-12-13" in x["dates"]], ["no_runs_blocked"])
        runs = _runs(plan)
        self.assertEqual(runs["2028-01-31"]["note"], "Skip it if you're still sore.")
        before, after = runs["2028-01-21"]["km"], runs["2028-02-04"]["km"]
        self.assertLessEqual(after, 0.75 * before + 0.05)

    def test_the_invariant_catches_growth_in_the_return_week(self):
        weeks, ctx = self.plan["_check"]["weeks"], self.plan["_check"]["ctx"]
        w = next(w for w in weeks if w.start == D(2027, 12, 27))
        i, s = next((i, s) for i, s in enumerate(w.sessions) if s.role == "long")
        w.sessions[i] = pe.make_session(s.key, s.date, "long", pe.Meters(24100.0), ctx.paces, ctx.rules)   # 23.6 + 0.5
        failed = {x["id"]: x["detail"] for x in pe.check_invariants(self.plan) if not x["ok"]}
        self.assertIn("long_step", failed)
        self.assertIn("week 5: long 24.1 km > 23.6 km (the first normal week after a gap: no step)",
                      failed["long_step"])
        answer = dict(pe.plan_answer(self.plan), weeks=[pe.week_json(x, ctx.paces) for x in weeks])
        self.assertEqual(long_step_problems(answer), ["2027-12-27: long 24.1 km > 23.6 km (after a gap)"])

    def test_the_step_counts_from_the_last_long_run_not_the_longest(self):
        # the mutation test's known gap: G4's week 10 long run cut to 23 km, week 11 back at 32 km (+39%)
        plan = build("G4")
        weeks, ctx = plan["_check"]["weeks"], plan["_check"]["ctx"]
        w10, w11 = (next(w for w in weeks if w.number == n) for n in (10, 11))
        self.assertEqual((w10.kind, w11.kind), ("normal", "normal"))
        i, s = next((i, s) for i, s in enumerate(w10.sessions) if s.role == "long")
        self.assertEqual(max(x.distance for x in w11.sessions if x.role == "long"), 32000.0)
        w10.sessions[i] = pe.make_session(s.key, s.date, "long", pe.Meters(23000.0), ctx.paces, ctx.rules)
        failed = {x["id"]: x["detail"] for x in pe.check_invariants(plan) if not x["ok"]}
        self.assertIn("week 11: long 32.0 km > 23.0 km + 2 km", failed.get("long_step", ""))
        self.assertTrue(long_step_problems(pe.plan_answer(build("G4"))) == [])

    def test_a_week_with_every_run_day_blocked_gets_one_warning(self):
        week = [x for x in self.plan["warnings"] if "2027-12-13" in x["dates"] or "2027-12-19" in x["dates"]]
        self.assertEqual([x["id"] for x in week], ["no_runs_blocked"])
        self.assertEqual(week[0]["dates"], ["2027-12-13", "2027-12-19"])
        self.assertIn("blocked", week[0]["detail"])

    def test_a_partly_blocked_week_keeps_its_fallback_warnings(self):
        plan = owner_plan(blocked_dates=(D(2027, 12, 17),))   # only the Friday: the long run moves or drops
        ids = {x["id"] for x in plan["warnings"] if "2027-12-13" in x["dates"]}
        self.assertNotIn("no_runs_blocked", ids)

    # cap_long{date or key, km}: lowers one long run, nothing else in its week

    def test_cap_long_lowers_one_long_run_only(self):
        day = D(2028, 1, 14)
        base = _runs(self.plan)[day.isoformat()]
        self.assertEqual(base["role"], "long")
        plan = owner_plan(pe.Override("cap_long", date=day, km=base["km"] - 4.0))
        self.assertEqual(plan["overrides"]["refused"], [])
        self.assertEqual(plan["overrides"]["applied"], [{"kind": "cap_long", "date": "2028-01-14",
                                                         "km": base["km"] - 4.0}])
        self.assertAlmostEqual(_runs(plan)[day.isoformat()]["km"], base["km"] - 4.0, places=1)
        wk_old = next(w for w in self.plan["weeks"] if w["start"] == "2028-01-10")
        wk_new = next(w for w in plan["weeks"] if w["start"] == "2028-01-10")
        others = [(s["date"], s["role"], s["km"]) for s in wk_new["sessions"] if s["date"] != day.isoformat()]
        self.assertEqual(others, [(s["date"], s["role"], s["km"]) for s in wk_old["sessions"]
                                  if s["date"] != day.isoformat()])
        self.assertTrue(plan["invariants"]["ok"], plan["invariants"]["failed"])
        self.assertEqual(plan["inputs"]["overrides"], [{"kind": "cap_long", "date": "2028-01-14",
                                                        "km": base["km"] - 4.0}])
        self.assertNotEqual(plan["content_hash"], self.plan["content_hash"])
        # by key as well
        by_key = owner_plan(pe.Override("cap_long", key=base["key"], km=base["km"] - 4.0))
        self.assertEqual(by_key["weeks"], plan["weeks"])

    def test_cap_long_refusals(self):
        runs = _runs(self.plan)
        long_day, easy_day = D(2028, 1, 14), next(D.fromisoformat(d) for d, s in runs.items() if s["role"] == "easy")
        planned = runs[long_day.isoformat()]["km"]
        cases = [
            (pe.Override("cap_long", date=long_day, km=planned + 1.0), "raises_load", planned + 1.0, planned),
            (pe.Override("cap_long", date=long_day, km=3.0), "easy_min_km", 3.0, 4.0),
            (pe.Override("cap_long", date=easy_day, km=4.0), "not_long_run", "easy", "long"),
            (pe.Override("cap_long", date=D(2027, 12, 16), km=10.0), "unknown_session", "2027-12-16", None),
            (pe.Override("cap_long", date=long_day), "km_required", None, None),
        ]
        for edit, rule, value, limit in cases:
            with self.subTest(rule=rule):
                plan = owner_plan(edit)
                self.assertEqual(plan["overrides"]["applied"], [])
                refused = plan["overrides"]["refused"]
                self.assertEqual([r["rule"] for r in refused], [rule])
                self.assertEqual(refused[0]["value"], value)
                if limit is not None:
                    self.assertEqual(refused[0]["limit"], limit)
                self.assertEqual(plan["weeks"], self.plan["weeks"])


if __name__ == "__main__":
    unittest.main()
