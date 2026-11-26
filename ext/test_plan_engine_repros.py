"""plan_engine: the defects an independent review proved (2026-11-26), each kept as a regression test.

Standard library only. One class per defect:

1. A long-run day that is a rest day (run Monday to Friday, rest at the weekend, the long run left on Sunday)
   planned 15 weeks without a long run, every invariant passing and feasibility "feasible".
2. A half marathon's race pace was clamped by the marathon's sanity band (0.85-0.96 x LT) while its feasibility
   accepts goals up to 1.02 x LT: a feasible 1:30 goal raced and trained at 4:20-4:28/km instead of 4:16.
3. The anchored bands broke rule 5: a threshold fast end faster than the 10K (even the 5K) pace where the
   0.91 x LT floor stopped the shift, intervals faster than the 3K equivalent behind it.
4. Reduce-only overrides on long-run days were refused (long_cap, long_step, easy_share): a sick athlete could
   not rest.
5. MP sessions without a pace target counted metres as seconds in the 80%-easy check.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import unittest

import plan_engine as pe
from test_plan_engine import GOLDEN, TODAY, _snap

D = dt.date


def _weeks(plan):
    return [w for w in plan["weeks"] if w["kind"] != "week0"]


def _sessions(plan):
    return {s["key"]: s for w in plan["weeks"] for s in w["sessions"]}


def _week_of(plan, day):
    return next(w for w in plan["weeks"] if w["start"] <= day.isoformat() <= w["end"])


def _band_of(plan, role):
    """(low, high) m/s of the first work step of the first session of `role`."""
    for w in plan["weeks"]:
        for s in w["sessions"]:
            if s["role"] != role:
                continue
            for st in _leaves(s["steps"]):
                if st["type"] == "interval" and "speed_mps" in st:
                    return tuple(st["speed_mps"])
    return None


def _leaves(steps):
    for st in steps:
        if st.get("type") == "repeat":
            yield from _leaves(st["steps"])
        else:
            yield st


# --- 1. the long-run day on a rest day -------------------------------------------------------------------------

WEEKDAY_RUNNER = _snap(TODAY, (38, 42, 36, 44, 40, 41, 39, 43), 22.0, lt=3.7)
WEEKDAY_INPUTS = pe.PlanInputs(race_date=D(2027, 3, 12), run_days=(0, 1, 2, 3, 4), rest_days=(5, 6),
                               long_run_day=6)


class LongRunDayOnARestDay(unittest.TestCase):
    def setUp(self):
        self.plan = pe.build_plan(WEEKDAY_INPUTS, WEEKDAY_RUNNER, what_if_runs=False)

    def test_every_normal_week_has_a_long_run_on_the_nearest_run_day(self):
        self.assertTrue(self.plan["invariants"]["ok"], self.plan["invariants"]["failed"])
        late = WEEKDAY_INPUTS.race_date - dt.timedelta(days=21)
        normal = [w for w in _weeks(self.plan) if w["kind"] == "normal" and w["start"] < late.isoformat()]
        self.assertTrue(normal)
        for w in normal:
            longs = [s for s in w["sessions"] if s["role"] == "long"]
            self.assertEqual(len(longs), 1, w)
            # Friday: the run day nearest to Sunday (Monday is as near only across the week's end)
            self.assertEqual(longs[0]["weekday"], "fri", w)
        self.assertIsNotNone(self.plan["summary"]["peak_long_km"])
        self.assertGreaterEqual(self.plan["summary"]["peak_long_km"], 26.0)

    def test_one_warning_names_the_day_the_long_runs_go_on(self):
        ids = [w["id"] for w in self.plan["warnings"]]
        self.assertEqual(ids.count("long_run_day_not_a_run_day"), 1, ids)
        self.assertNotIn("long_dropped", ids)
        note = next(w for w in self.plan["warnings"] if w["id"] == "long_run_day_not_a_run_day")
        self.assertIn("sun", note["detail"])
        self.assertIn("fri", note["detail"])

    def test_no_long_run_on_a_rest_day_and_no_hard_days_touch(self):
        for w in self.plan["weeks"]:
            for s in w["sessions"]:
                if s["role"] not in ("race", "b_race"):
                    self.assertIn(s["weekday"], ("mon", "tue", "wed", "thu", "fri"), s)
        failed = {i["id"] for i in self.plan["invariants"]["failed"]}
        self.assertNotIn("no_adjacent_hard", failed)

    def test_a_plan_without_long_runs_in_its_normal_weeks_fails_an_invariant(self):
        weeks = self.plan["_check"]["weeks"]
        for w in weeks:
            if w.kind == "normal":
                w.sessions = [s for s in w.sessions if s.role != "long"]
        failed = {i["id"]: i["detail"] for i in pe.check_invariants(self.plan) if not i["ok"]}
        self.assertIn("long_cap", failed)
        self.assertIn("no long run in any normal week", failed["long_cap"])

    def test_a_week_whose_long_run_day_is_today_does_not_count_as_one_that_needed_a_long_run(self):
        # Repro (DEEP_EXAMPLES=2000, a half never plans more than a marathon): a half built on Monday 30 Nov with
        # the long run on Monday; the engine plans nothing on today, so week 1 has no long run, and it was the
        # only normal week 21+ days out: "no long run in any normal week" failed a plan that could not have one
        inputs = pe.PlanInputs(race_date=D(2026, 12, 21), race_distance="half", run_days=(0, 2, 3),
                               rest_days=(1, 4, 5, 6), long_run_day=0, quality_per_week=0)
        snap = _snap(D(2026, 11, 30), [8.0] * 8, 5.0, predictions=(("marathon", 13200), ("half", 6300)))
        plan = pe.build_plan(inputs, snap, what_if_runs=False)
        self.assertTrue(plan["invariants"]["ok"], plan["invariants"]["failed"])
        first = _week_of(plan, D(2026, 11, 30))
        self.assertFalse([s for s in first["sessions"] if s["role"] == "long"])

    def test_feasibility_fails_a_plan_with_no_long_run(self):
        f = pe.feasibility(WEEKDAY_INPUTS, WEEKDAY_RUNNER, 15, 59.9, 0.0, 40.8)
        check = next(c for c in f["checks"] if c["id"] == "long_run_km")
        self.assertFalse(check["ok"])
        self.assertEqual(check["level"], "fail")
        self.assertEqual(f["verdict"], "unrealistic")
        # unknown (no plan could be built) is not "none planned": the check is left out
        f = pe.feasibility(WEEKDAY_INPUTS, WEEKDAY_RUNNER, 15, 59.9, None, 40.8)
        self.assertNotIn("long_run_km", [c["id"] for c in f["checks"]])

    def test_a_long_run_day_that_is_a_run_day_keeps_its_day(self):
        inputs = dataclasses.replace(WEEKDAY_INPUTS, long_run_day=4)
        plan = pe.build_plan(inputs, WEEKDAY_RUNNER, what_if_runs=False)
        self.assertNotIn("long_run_day_not_a_run_day", [w["id"] for w in plan["warnings"]])
        days = {s["weekday"] for w in plan["weeks"] for s in w["sessions"] if s["role"] == "long"}
        self.assertEqual(days, {"fri"})

    def test_ties_go_to_the_later_run_day(self):
        # run Tuesday, Thursday and Saturday, long run on Friday: Thursday and Saturday are as near
        inputs = pe.PlanInputs(race_date=D(2027, 3, 12), run_days=(1, 3, 5), rest_days=(0, 2, 4, 6),
                               long_run_day=4)
        plan = pe.build_plan(inputs, WEEKDAY_RUNNER, what_if_runs=False)
        self.assertTrue(plan["invariants"]["ok"], plan["invariants"]["failed"])
        late = inputs.race_date - dt.timedelta(days=21)
        days = [s["weekday"] for w in _weeks(plan) for s in w["sessions"]
                if s["role"] == "long" and w["kind"] == "normal" and s["date"] < late.isoformat()]
        self.assertTrue(days)
        self.assertEqual(set(days), {"sat"})


# --- 2. the half marathon's race pace ----------------------------------------------------------------------------

HALF_RUNNER = _snap(TODAY, (40, 42, 38, 44, 41, 43, 40, 44), 18.0, lt=3.95,
                    predictions=(("half", 3600 + 31 * 60), ("10k", 41 * 60 + 30)))
HALF_INPUTS = pe.PlanInputs(race_date=D(2027, 3, 21), race_distance="half", goal_time_s=3600 + 30 * 60)


class HalfMarathonRacePace(unittest.TestCase):
    def test_a_feasible_goal_is_raced_and_trained_at_its_pace(self):
        plan = pe.build_plan(HALF_INPUTS, HALF_RUNNER, what_if_runs=False)
        self.assertEqual(plan["feasibility"]["verdict"], "feasible")
        self.assertTrue(plan["invariants"]["ok"], plan["invariants"]["failed"])
        goal = 21097.5 / HALF_INPUTS.goal_time_s   # 4:16/km
        for role in ("race", "mp"):
            band = _band_of(plan, role)
            self.assertIsNotNone(band, role)
            self.assertLessEqual(band[0], goal + 0.001, (role, band))
            self.assertGreaterEqual(band[1], goal - 0.001, (role, band))
        self.assertEqual(plan["race"]["marathon_pace"], "4:16")
        self.assertNotIn("mp_clamped", [w["id"] for w in plan["warnings"]])

    def test_the_half_sanity_band_matches_its_feasibility(self):
        half = pe.RULES["by_distance"]["half"]["mp_of_lt_sanity"]
        f = pe.RULES["feasibility"]["half"]
        self.assertEqual(float(half[1]), float(f["max_goal_of_lt"]))
        self.assertLess(float(half[0]), float(f["corrected_of_lt"]))
        self.assertEqual(pe.by_distance(pe.RULES, "marathon", "mp_of_lt_sanity"), pe.RULES["mp_of_lt_sanity"])

    def test_a_half_goal_past_its_band_is_still_clamped(self):
        warnings = []
        fast = dataclasses.replace(HALF_INPUTS, goal_time_s=3600 + 15 * 60)   # 1.12 x LT
        mp = pe.marathon_pace(fast, HALF_RUNNER, pe.RULES, warnings)
        self.assertAlmostEqual(mp, 1.02 * 3.95, places=6)
        self.assertIn("mp_clamped", [w["id"] for w in warnings])


# --- 3. the anchored bands against rule 5 -------------------------------------------------------------------------

def _anchor_case(lt, t5):
    snap = _snap(TODAY, (40, 42, 38, 44, 41, 43, 40, 44), 20.0, lt=lt, predictions=(("5k", t5),))
    return snap, pe.build_plan(pe.PlanInputs(race_date=D(2027, 3, 12)), snap, what_if_runs=False)


class AnchoredBandsKeepRule5(unittest.TestCase):
    CASES = ((4.17, 25 * 60), (4.0, 22 * 60), (3.9, 22 * 60 + 30))   # LT 4:00, 4:10, 4:16; 5K 5:00, 4:24, 4:30

    def test_threshold_never_faster_than_10k_and_intervals_never_faster_than_3k(self):
        for lt, t5 in self.CASES:
            with self.subTest(lt=lt, t5=t5):
                snap, plan = _anchor_case(lt, t5)
                self.assertTrue(plan["invariants"]["ok"], plan["invariants"]["failed"])
                paces = plan["_check"]["ctx"].paces
                v5 = 5000.0 / t5
                v10 = pe.riegel_speed(v5, 5000.0, 10000.0, 1.06)
                v3 = pe.riegel_speed(v5, 5000.0, 3000.0, 1.06)
                thr, itv = paces.band("threshold"), paces.band("intervals")
                self.assertLessEqual(thr[1], v10 + 0.0005, pe.band_text(thr))
                self.assertLess(thr[0], thr[1])
                self.assertLessEqual(itv[1], v3 + 0.0005, pe.band_text(itv))
                self.assertGreaterEqual(itv[0], v5 - 0.0005, pe.band_text(itv))
                self.assertGreaterEqual(itv[0], thr[1] - 0.0005)

    def test_the_conflict_with_the_floor_warns(self):
        # LT 4:10/km, 5K 4:24/km: the 10K pace (4:35) is under 0.91 x LT, the floor that keeps the band hard
        _snap_, plan = _anchor_case(4.0, 22 * 60)
        warns = [w for w in plan["warnings"] if w["id"] == "threshold_anchor_conflict"]
        self.assertEqual(len(warns), 1, plan["warnings"])
        self.assertIn("10K", warns[0]["detail"])
        self.assertEqual(_band_of(plan, "threshold"), plan["_check"]["ctx"].paces.band("threshold"))

    def test_a_10k_pace_just_under_the_shifted_band_narrows_it_to_the_floor(self):
        lt = pe.MetersPerSecond(4.0)
        v10 = 0.95 * lt   # the 0.97-1.03 band shifted to the 10K pace would end at 0.895 x LT, under 0.91
        snap = _snap(TODAY, (40,) * 8, 20.0, lt=4.0, predictions=(("10k", int(round(10000 / v10))),))
        warnings = []
        lo, hi = pe.anchored_bands(lt, snap, pe.RULES, warnings)["threshold"]
        self.assertLessEqual(hi, 10000 / int(round(10000 / v10)) + 1e-9)
        self.assertAlmostEqual(lo, 0.91 * lt, places=6)
        self.assertEqual([w["id"] for w in warnings], ["threshold_anchor_conflict"])

    def test_no_conflict_no_warning(self):
        lt = pe.MetersPerSecond(1000.0 / 248.0)   # the owner: LT 4:08, 5K 20:20, 10K 42:30
        snap = _snap(TODAY, (40,) * 8, 20.0, lt=lt, predictions=(("5k", 1220), ("10k", 2550)))
        warnings = []
        pe.anchored_bands(lt, snap, pe.RULES, warnings)
        self.assertEqual(warnings, [])


# --- 4. reduce-only overrides on long-run days ---------------------------------------------------------------------

def _g(name, *overrides):
    case = GOLDEN[name]
    return pe.build_plan(dataclasses.replace(case["inputs"], overrides=tuple(overrides)), case["snapshot"],
                         what_if_runs=False)


class ReduceOnlyOverridesOnLongRuns(unittest.TestCase):
    def assert_applied(self, plan, n=1):
        self.assertEqual(plan["overrides"]["refused"], [])
        self.assertEqual(len(plan["overrides"]["applied"]), n)
        self.assertTrue(plan["invariants"]["ok"], plan["invariants"]["failed"])

    def test_g4_long_run_can_be_rested_eased_or_capped(self):
        day = D(2026, 12, 13)   # G4 week 2's long run, 27.9 km
        base = _sessions(_g("G4"))
        key = next(k for k, s in base.items() if s["date"] == day.isoformat())
        for o in (pe.Override("easy", date=day), pe.Override("cap_long", date=day, km=20.0),
                  pe.Override("cap_km", date=day, km=20.0), pe.Override("rest", date=day),
                  pe.Override("cap_week_km", week_start=D(2026, 12, 7), km=0.0)):
            with self.subTest(edit=o.echo()):
                plan = _g("G4", o)
                self.assert_applied(plan)
                now = _sessions(plan)
                self.assertTrue(key not in now or now[key]["km"] <= base[key]["km"])
                # the later weeks are as planned: the long run reduced by an override is not the step reference
                for k, s in base.items():
                    if s["date"] > "2026-12-13":
                        self.assertEqual(now[k], s, k)
                if o.kind in ("rest", "cap_week_km", "easy", "cap_long", "cap_km"):
                    relaxed = [w for w in plan["warnings"] if w["id"] == "override_relaxed"]
                    self.assertTrue(relaxed, plan["warnings"])
                    self.assertEqual(relaxed[0]["dates"], ["2026-12-07", "2026-12-13"])
                    self.assertIn("long_step", relaxed[0]["detail"])

    def test_g1_rest_and_cap_long_on_a_long_run_day_apply_with_a_warning(self):
        for o in (pe.Override("rest", date=D(2027, 1, 24)), pe.Override("cap_long", date=D(2027, 2, 14), km=16.0)):
            with self.subTest(edit=o.echo()):
                plan = _g("G1", o)
                self.assert_applied(plan)
                w = _week_of(plan, o.date)
                relaxed = [x for x in plan["warnings"] if x["id"] == "override_relaxed"]
                self.assertEqual([x["dates"] for x in relaxed], [[w["start"], w["end"]]])
                self.assertIn("easy_share", relaxed[0]["detail"])

    def test_still_refused_when_the_b_race_limit_follows_the_plan_as_it_is(self):
        # rule 7: the first long run after a half-distance triathlon is at most 0.75 x the last one before it,
        # so shortening the one before is refused until that one is capped first
        plan = _g("G1", pe.Override("cap_long", date=D(2027, 1, 24), km=15.0))
        self.assertEqual([r["rule"] for r in plan["overrides"]["refused"]], ["b_windows"])
        plan = _g("G1", pe.Override("cap_long", date=D(2027, 2, 7), km=11.2),
                  pe.Override("cap_long", date=D(2027, 1, 24), km=15.0))
        self.assert_applied(plan, 2)

    def test_still_refused_when_it_raises_load(self):
        plan = _g("G4", pe.Override("cap_long", date=D(2026, 12, 13), km=29.0))
        self.assertEqual([r["rule"] for r in plan["overrides"]["refused"]], ["raises_load"])

    def test_the_week_after_steps_from_the_long_run_as_built(self):
        plan = _g("G4", pe.Override("cap_long", date=D(2026, 12, 13), km=12.0))
        self.assert_applied(plan)
        longs = [(w["start"], w["long_km"]) for w in _weeks(plan)][:3]
        self.assertEqual(longs, [("2026-11-30", 25.9), ("2026-12-07", 12.0), ("2026-12-14", 29.9)])
        # the invariant recounts it: the reduced long run counts as built for the step and the cap
        failed = [i for i in pe.check_invariants(plan) if not i["ok"]]
        self.assertEqual(failed, [])

    def test_a_move_that_breaks_hard_day_spacing_is_still_refused(self):
        base = _g("G4")
        w = next(x for x in _weeks(base) if any(s["role"] in pe.QUALITY_ROLES for s in x["sessions"])
                 and any(s["role"] == "long" for s in x["sessions"]))
        q = next(s for s in w["sessions"] if s["role"] in pe.QUALITY_ROLES)
        long_day = D.fromisoformat(next(s["date"] for s in w["sessions"] if s["role"] == "long"))
        # the quality session swapped with the easy run the day before the long run
        plan = _g("G4", pe.Override("swap", date=D.fromisoformat(q["date"]), date_b=long_day - dt.timedelta(days=1)))
        self.assertEqual([r["rule"] for r in plan["overrides"]["refused"]], ["no_adjacent_hard"])


# --- 5. MP sessions without a pace target --------------------------------------------------------------------------

class MarathonPaceSessionsWithoutATarget(unittest.TestCase):
    def setUp(self):
        self.plan = pe.build_plan(pe.PlanInputs(race_date=D(2027, 3, 12)), WEEKDAY_RUNNER, what_if_runs=False)
        self.weeks, self.ctx = self.plan["_check"]["weeks"], self.plan["_check"]["ctx"]

    def test_their_work_counts_in_seconds(self):
        paces = self.ctx.paces
        self.assertIsNone(paces.mp)
        self.assertIn("marathon_pace", self.plan["missing"])
        mps = [s for w in self.weeks for s in w.sessions if s.role == "mp"]
        self.assertTrue(mps)
        # the estimate is feasibility's corrected goal: 0.93 x LT for a marathon
        speed = 0.93 * 3.7
        for s in mps:
            work = sum(st.value for st in pe._leaves(s.steps) if st.kind == "interval")
            hard, total = pe.hard_seconds(s, paces)
            self.assertAlmostEqual(hard, work / speed, delta=work / speed * 0.02)
            self.assertLess(hard, total)

    def test_they_are_not_held_at_the_minimum_by_a_unit_mix_up(self):
        works = [sum(st.value for st in pe._leaves(s.steps) if st.kind == "interval")
                 for w in self.weeks if w.kind == "normal" for s in w.sessions if s.role == "mp"]
        self.assertTrue(works)
        self.assertGreater(max(works), 3000.0)
        self.assertTrue(self.plan["invariants"]["ok"], self.plan["invariants"]["failed"])


if __name__ == "__main__":
    unittest.main()
