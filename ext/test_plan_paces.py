"""plan_engine pace bands, marathon pace, feasibility projection by distance, and the VO2max plan_assess reads,
on owner-like numbers (the real-data test of 2026-11-26). Standard library only.

The owner: Garmin's lactate threshold 4:08/km, race predictions 5K 20:20 (4:04/km) and marathon 3:32:42,
running VO2max 60, Tel Aviv Marathon on 2027-02-26 without a goal.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import os
import unittest

import plan_engine as pe
import plan_tools as pt

D = dt.date
TODAY = D(2026, 11, 26)
RACE = D(2027, 2, 26)
LT = 1000.0 / 248.0                         # 4:08/km
PRED_5K, PRED_10K, PRED_HALF, PRED_MARATHON = 1220, 2550, 5640, 3 * 3600 + 32 * 60 + 42
FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def owner_snapshot(predictions=None, weeks_km=(45, 50, 47, 52, 48, 50, 46, 51), longest_km=24.0):
    preds = ((("5k", PRED_5K), ("10k", PRED_10K), ("half", PRED_HALF), ("marathon", PRED_MARATHON))
             if predictions is None else predictions)
    week_m = [k * 1000.0 for k in weeks_km]
    acts, garmin = pe.sample_loads(TODAY, week_m)
    return pe.Snapshot(today=TODAY, week_m=tuple(pe.Meters(m) for m in week_m), week_runs=(5,) * len(week_m),
                       longest_run_8w_m=pe.Meters(longest_km * 1000.0), lt_speed_mps=pe.MetersPerSecond(LT),
                       vo2max=60.0, prediction_s=preds, activity_load=acts, garmin_load=garmin)


def bands(plan):
    """{role: (low, high) m/s}: the work steps' band of each quality role, the easy and long runs' band."""
    out = {}
    for w in plan["weeks"]:
        for s in w["sessions"]:
            for st in _leaves(s["steps"]):
                work = st["type"] == "interval" if s["role"] in pe.QUALITY_ROLES else s["role"] in ("easy", "long")
                if "speed_mps" in st and work:
                    out.setdefault(s["role"], tuple(st["speed_mps"]))
    return out


def _leaves(steps):
    for st in steps:
        if st.get("type") == "repeat":
            yield from _leaves(st["steps"])
        else:
            yield st


class MarathonPace(unittest.TestCase):
    def test_no_goal_uses_the_prediction_feasibility_suggests(self):
        snap = owner_snapshot()
        inputs = pe.PlanInputs(race_date=RACE)
        plan = pe.build_plan(inputs, snap, what_if_runs=False)
        corrected = plan["feasibility"]["corrected_goal_s"]
        self.assertEqual(corrected, 3 * 3600 + 33 * 60)          # the slower of the prediction and 0.93 x LT
        mp = pe.marathon_pace(inputs, snap, pe.RULES, [])
        self.assertIsNotNone(mp)
        # 42.195 km in 3:33:00: 5:03/km, not 0.85 x LT (4:52/km, a 3:25 finish feasibility calls ambitious)
        self.assertAlmostEqual(mp, 42195.0 / corrected, places=6)
        self.assertEqual(plan["race"]["marathon_pace"], "5:03")
        self.assertLessEqual(mp, pe.accepted_speed(inputs, snap, pe.RULES) + 1e-9)
        ids = [w["id"] for w in plan["warnings"]]
        self.assertIn("mp_below_band", ids)                       # 82% of LT: said, not sped up
        self.assertNotIn("mp_clamped", ids)

    def test_no_goal_mp_is_never_faster_than_a_goal_feasibility_rates_feasible(self):
        snap = owner_snapshot()
        mp = pe.marathon_pace(pe.PlanInputs(race_date=RACE), snap, pe.RULES, [])
        finish = round(42195.0 / mp)
        f = pe.feasibility(pe.PlanInputs(race_date=RACE, goal_time_s=finish), snap, 13, 60.0, 30.0, 48.0)
        self.assertEqual(f["verdict"], "feasible", f["checks"])

    def test_goal_keeps_its_pace_and_says_when_beyond_feasibility(self):
        snap = owner_snapshot()
        goal = 3 * 3600 + 20 * 60                                  # 6% faster than the prediction
        warnings = []
        mp = pe.marathon_pace(pe.PlanInputs(race_date=RACE, goal_time_s=goal), snap, pe.RULES, warnings)
        self.assertAlmostEqual(mp, 42195.0 / goal, places=6)
        self.assertIn("goal_beyond_feasible", [w["id"] for w in warnings])
        ok = []
        pe.marathon_pace(pe.PlanInputs(race_date=RACE, goal_time_s=PRED_MARATHON), snap, pe.RULES, ok)
        self.assertNotIn("goal_beyond_feasible", [w["id"] for w in ok])

    def test_a_slow_goal_is_raised_no_further_than_feasibility_accepts(self):
        snap = owner_snapshot()
        inputs = pe.PlanInputs(race_date=RACE, goal_time_s=4 * 3600)
        warnings = []
        mp = pe.marathon_pace(inputs, snap, pe.RULES, warnings)
        self.assertLessEqual(mp, pe.accepted_speed(inputs, snap, pe.RULES) + 1e-9)
        self.assertGreater(mp, 42195.0 / (4 * 3600))
        self.assertIn("mp_clamped", [w["id"] for w in warnings])


class AnchoredBands(unittest.TestCase):
    def test_owner_intervals_are_no_faster_than_the_3k_equivalent_of_the_5k(self):
        plan = pe.build_plan(pe.PlanInputs(race_date=RACE), owner_snapshot(), what_if_runs=False)
        b = bands(plan)
        v5k = 5000.0 / PRED_5K
        lo, hi = b["intervals"]
        self.assertAlmostEqual(lo, round(v5k, 3), places=3)        # 4:04/km
        self.assertLessEqual(hi, v5k * (5 / 3) ** 0.06 + 0.001)    # 3:57/km, not 3:46 (1.10 x LT)
        self.assertEqual(pe.pace_text(hi), "3:57")
        self.assertTrue(plan["invariants"]["ok"], plan["invariants"]["failed"])

    def test_owner_threshold_band_is_between_half_and_10k_pace(self):
        plan = pe.build_plan(pe.PlanInputs(race_date=RACE), owner_snapshot(), what_if_runs=False)
        lo, hi = bands(plan)["threshold"]
        self.assertLessEqual(hi, 10000.0 / PRED_10K + 0.001)       # not faster than the 10K pace
        self.assertAlmostEqual(lo, round(21097.5 / PRED_HALF, 3), places=3)
        self.assertGreater((lo + hi) / 2, 0.90 * LT)               # still hard for get_daily_status

    def test_only_a_5k_prediction_converts_to_10k_and_half(self):
        snap = owner_snapshot(predictions=(("5k", PRED_5K),))
        a = pe.anchored_bands(pe.MetersPerSecond(LT), snap, pe.RULES)
        v10k = 5000.0 / PRED_5K * 0.5 ** 0.06
        self.assertAlmostEqual(a["threshold"][1], v10k, places=6)
        self.assertAlmostEqual(a["intervals"][0], 5000.0 / PRED_5K, places=6)

    def test_without_5k_or_10k_predictions_the_lt_multiples_stay(self):
        snap = owner_snapshot(predictions=(("marathon", PRED_MARATHON), ("half", PRED_HALF)))
        self.assertEqual(pe.anchored_bands(pe.MetersPerSecond(LT), snap, pe.RULES), {})
        b = bands(pe.build_plan(pe.PlanInputs(race_date=RACE), snap, what_if_runs=False))
        self.assertEqual(b["intervals"], (round(LT * 1.05, 3), round(LT * 1.10, 3)))
        self.assertEqual(b["threshold"], (round(LT * 0.97, 3), round(LT * 1.03, 3)))

    def test_a_threshold_band_slower_than_10k_pace_is_kept(self):
        slow_lt = pe.MetersPerSecond(3.6)                           # 4:38/km, well under the 10K pace
        a = pe.anchored_bands(slow_lt, owner_snapshot(), pe.RULES)
        self.assertNotIn("threshold", a)
        self.assertAlmostEqual(a["intervals"][0], 5000.0 / PRED_5K, places=6)

    def test_the_threshold_band_moves_down_at_most_to_0_91_lt(self):
        snap = owner_snapshot(predictions=(("10k", 3000),))         # 5:00/km: far under LT 4:08
        lo, hi = pe.anchored_bands(pe.MetersPerSecond(LT), snap, pe.RULES)["threshold"]
        self.assertAlmostEqual(lo, 0.91 * LT, places=6)
        a = pe.anchored_bands(pe.MetersPerSecond(LT), snap, pe.RULES)
        self.assertGreaterEqual(a["intervals"][0], hi)               # intervals never under the threshold band


class SpeedLimits(unittest.TestCase):
    """A band outside the speed limits loses its pace target only with it listed as missing and a warning;
    otherwise bands_match_roles fails (it skipped every such band before)."""

    def failed(self, plan):
        return {i["id"]: i for i in pe.check_invariants(plan)}

    def test_bands_shifted_out_of_the_limits_fail_bands_match_roles(self):
        plan = pe.build_plan(pe.PlanInputs(race_date=RACE), owner_snapshot(), what_if_runs=False)
        self.assertTrue(self.failed(plan)["bands_match_roles"]["ok"])
        plan["_check"]["ctx"].paces.lt = pe.MetersPerSecond(9.0)   # every LT band above 7 m/s
        check = self.failed(plan)["bands_match_roles"]
        self.assertFalse(check["ok"])
        self.assertIn("outside the speed limits", check["detail"])

    def test_a_slow_runners_easy_band_is_reported_missing(self):
        snap = owner_snapshot(predictions=())
        slow = pe.build_plan(pe.PlanInputs(race_date=RACE), dataclasses.replace(
            snap, lt_speed_mps=pe.MetersPerSecond(2.0)), what_if_runs=False)   # 0.70 x 2.0 = 1.4 m/s < 1.5
        self.assertTrue(slow["invariants"]["ok"], slow["invariants"]["failed"])
        self.assertIn("pace_targets.easy", slow["missing"])
        self.assertIn("band_out_of_limits", [w["id"] for w in slow["warnings"]])
        ok = pe.build_plan(pe.PlanInputs(race_date=RACE), snap, what_if_runs=False)
        self.assertFalse([m for m in ok["missing"] if m.startswith("pace_targets.")])


class HalfProjection(unittest.TestCase):
    def test_half_projects_below_the_marathon(self):
        snap = owner_snapshot()
        m = pe.assess(pe.PlanInputs(race_date=RACE), snap)
        h = pe.assess(pe.PlanInputs(race_date=RACE, race_distance="half"), snap)
        self.assertLess(h["projected_peak_km"], m["projected_peak_km"])
        self.assertLess(h["projected_long_km"], m["projected_long_km"])
        self.assertLessEqual(h["projected_long_km"], 24.0)
        self.assertEqual(h["feasibility"]["verdict"], "feasible")

    def test_half_peak_ceiling_and_long_cap_hold_in_the_invariants(self):
        snap = owner_snapshot()
        plan = pe.build_plan(pe.PlanInputs(race_date=RACE, race_distance="half"), snap, what_if_runs=False)
        self.assertTrue(plan["invariants"]["ok"], plan["invariants"]["failed"])
        ceiling, _b = pe.peak_ceiling(snap, pe.Meters(sum(snap.week_m[-4:]) / 4), pe.RULES, "half")
        self.assertLessEqual(plan["summary"]["peak_km"], pe.km(ceiling) + 0.1)
        marathon_ceiling, _b = pe.peak_ceiling(snap, pe.Meters(sum(snap.week_m[-4:]) / 4), pe.RULES)
        self.assertLess(ceiling, marathon_ceiling)


class AssessReadsGarmin(unittest.TestCase):
    def training_status(self):
        with open(os.path.join(FIXTURES, "daily_status", "training_status.json"), encoding="utf-8") as fh:
            return json.load(fh)

    def test_vo2max_from_training_status_when_max_metrics_has_no_reading_today(self):
        # get_max_metrics(today) is a one-day range: [] on a day without a new reading
        self.assertEqual(pt.vo2max([], self.training_status()), 55.4)
        self.assertEqual(pt.vo2max([{"userId": 1, "generic": None, "cycling": {"vo2MaxPreciseValue": 50.1}}],
                                   self.training_status()), 55.4)
        self.assertEqual(pt.vo2max([{"generic": {"vo2MaxPreciseValue": 60.2}}], self.training_status()), 60.2)
        self.assertIsNone(pt.vo2max([], {}))

    def test_predictions_read_5k_and_10k(self):
        rec = {"calendarDate": "2026-11-26", "time5K": PRED_5K, "time10K": PRED_10K,
               "timeHalfMarathon": PRED_HALF, "timeMarathon": PRED_MARATHON}
        self.assertEqual(dict(pt.predictions(rec)),
                         {"5k": PRED_5K, "10k": PRED_10K, "half": PRED_HALF, "marathon": PRED_MARATHON})
        self.assertEqual(pt.predictions({"time5K": 30}), ())


if __name__ == "__main__":
    unittest.main()
