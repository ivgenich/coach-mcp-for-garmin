"""plan_engine pace bands, marathon pace, feasibility projection by distance, and the VO2max plan_assess reads,
on fictional numbers shaped like a real-data test. Standard library only.

The athlete: Garmin's lactate threshold 4:40/km, race predictions 5K 23:00 (4:36/km) and marathon 4:00:00,
running VO2max 47, a marathon on 2028-02-25 without a goal.
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
TODAY = D(2027, 11, 25)
RACE = D(2028, 2, 25)
LT = 1000.0 / 280.0                         # 4:40/km
PRED_5K, PRED_10K, PRED_HALF, PRED_MARATHON = 1380, 2880, 6360, 4 * 3600
FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def athlete_snapshot(predictions=None, weeks_km=(45, 50, 47, 52, 48, 50, 46, 51), longest_km=24.0):
    preds = ((("5k", PRED_5K), ("10k", PRED_10K), ("half", PRED_HALF), ("marathon", PRED_MARATHON))
             if predictions is None else predictions)
    week_m = [k * 1000.0 for k in weeks_km]
    acts, garmin = pe.sample_loads(TODAY, week_m)
    return pe.Snapshot(today=TODAY, week_m=tuple(pe.Meters(m) for m in week_m), week_runs=(5,) * len(week_m),
                       longest_run_8w_m=pe.Meters(longest_km * 1000.0), lt_speed_mps=pe.MetersPerSecond(LT),
                       vo2max=47.0, prediction_s=preds, activity_load=acts, garmin_load=garmin)


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
        snap = athlete_snapshot()
        inputs = pe.PlanInputs(race_date=RACE)
        plan = pe.build_plan(inputs, snap, what_if_runs=False)
        corrected = plan["feasibility"]["corrected_goal_s"]
        self.assertEqual(corrected, 4 * 3600)                    # the slower of the prediction and 0.93 x LT
        mp = pe.marathon_pace(inputs, snap, pe.RULES, [])
        self.assertIsNotNone(mp)
        # 42.195 km in 4:00:00: 5:41/km, not 0.85 x LT (5:29/km, a 3:52 finish feasibility calls ambitious)
        self.assertAlmostEqual(mp, 42195.0 / corrected, places=6)
        self.assertEqual(plan["race"]["marathon_pace"], "5:41")
        self.assertLessEqual(mp, pe.accepted_speed(inputs, snap, pe.RULES) + 1e-9)
        ids = [w["id"] for w in plan["warnings"]]
        self.assertIn("mp_below_band", ids)                       # 82% of LT: said, not sped up
        self.assertNotIn("mp_clamped", ids)

    def test_no_goal_mp_is_never_faster_than_a_goal_feasibility_rates_feasible(self):
        snap = athlete_snapshot()
        mp = pe.marathon_pace(pe.PlanInputs(race_date=RACE), snap, pe.RULES, [])
        finish = round(42195.0 / mp)
        f = pe.feasibility(pe.PlanInputs(race_date=RACE, goal_time_s=finish), snap, 13, 60.0, 30.0, 48.0)
        self.assertEqual(f["verdict"], "feasible", f["checks"])

    def test_goal_keeps_its_pace_and_says_when_beyond_feasibility(self):
        snap = athlete_snapshot()
        goal = 3 * 3600 + 45 * 60                                  # 6% faster than the prediction
        warnings = []
        mp = pe.marathon_pace(pe.PlanInputs(race_date=RACE, goal_time_s=goal), snap, pe.RULES, warnings)
        self.assertAlmostEqual(mp, 42195.0 / goal, places=6)
        self.assertIn("goal_beyond_feasible", [w["id"] for w in warnings])
        ok = []
        pe.marathon_pace(pe.PlanInputs(race_date=RACE, goal_time_s=PRED_MARATHON), snap, pe.RULES, ok)
        self.assertNotIn("goal_beyond_feasible", [w["id"] for w in ok])

    def test_a_slow_goal_is_raised_no_further_than_feasibility_accepts(self):
        snap = athlete_snapshot()
        inputs = pe.PlanInputs(race_date=RACE, goal_time_s=4 * 3600)
        warnings = []
        mp = pe.marathon_pace(inputs, snap, pe.RULES, warnings)
        self.assertLessEqual(mp, pe.accepted_speed(inputs, snap, pe.RULES) + 1e-9)
        self.assertGreater(mp, 42195.0 / (4 * 3600))
        self.assertIn("mp_clamped", [w["id"] for w in warnings])


class AnchoredBands(unittest.TestCase):
    def test_athlete_intervals_are_no_faster_than_the_3k_equivalent_of_the_5k(self):
        plan = pe.build_plan(pe.PlanInputs(race_date=RACE), athlete_snapshot(), what_if_runs=False)
        b = bands(plan)
        v5k = 5000.0 / PRED_5K
        lo, hi = b["intervals"]
        self.assertAlmostEqual(lo, round(v5k, 3), places=3)        # 4:36/km
        self.assertLessEqual(hi, v5k * (5 / 3) ** 0.06 + 0.001)    # 4:28/km, not 4:15 (1.10 x LT)
        self.assertEqual(pe.pace_text(hi), "4:28")
        self.assertTrue(plan["invariants"]["ok"], plan["invariants"]["failed"])

    def test_athlete_threshold_band_is_between_half_and_10k_pace(self):
        plan = pe.build_plan(pe.PlanInputs(race_date=RACE), athlete_snapshot(), what_if_runs=False)
        lo, hi = bands(plan)["threshold"]
        self.assertLessEqual(hi, 10000.0 / PRED_10K + 0.001)       # not faster than the 10K pace
        self.assertAlmostEqual(lo, round(21097.5 / PRED_HALF, 3), places=3)
        self.assertGreater((lo + hi) / 2, 0.90 * LT)               # still hard for get_daily_status

    def test_only_a_5k_prediction_converts_to_10k_and_half(self):
        snap = athlete_snapshot(predictions=(("5k", PRED_5K),))
        a = pe.anchored_bands(pe.MetersPerSecond(LT), snap, pe.RULES)
        v10k = 5000.0 / PRED_5K * 0.5 ** 0.06
        self.assertAlmostEqual(a["threshold"][1], v10k, places=6)
        self.assertAlmostEqual(a["intervals"][0], 5000.0 / PRED_5K, places=6)

    def test_without_5k_or_10k_predictions_the_lt_multiples_stay(self):
        snap = athlete_snapshot(predictions=(("marathon", PRED_MARATHON), ("half", PRED_HALF)))
        self.assertEqual(set(pe.anchored_bands(pe.MetersPerSecond(LT), snap, pe.RULES)), {"easy", "long"})
        b = bands(pe.build_plan(pe.PlanInputs(race_date=RACE), snap, what_if_runs=False))
        self.assertEqual(b["intervals"], (round(LT * 1.05, 3), round(LT * 1.10, 3)))
        self.assertEqual(b["threshold"], (round(LT * 0.97, 3), round(LT * 1.03, 3)))

    def test_a_threshold_band_slower_than_10k_pace_is_kept(self):
        slow_lt = pe.MetersPerSecond(3.2)                           # 5:13/km, well under the 10K pace
        a = pe.anchored_bands(slow_lt, athlete_snapshot(), pe.RULES)
        self.assertNotIn("threshold", a)
        self.assertAlmostEqual(a["intervals"][0], 5000.0 / PRED_5K, places=6)

    def test_a_10k_pace_far_under_lt_wins_over_the_0_91_lt_floor(self):
        snap = athlete_snapshot(predictions=(("10k", 3400),))         # 5:40/km: far under LT 4:40
        warnings = []
        a = pe.anchored_bands(pe.MetersPerSecond(LT), snap, pe.RULES, warnings)
        lo, hi = a["threshold"]
        self.assertAlmostEqual(hi, 10000.0 / 3400, places=6)          # never faster than the 10K pace (rule 5)
        self.assertLess(lo, hi)
        self.assertLess(lo, 0.91 * LT)                                 # under the floor: said in a warning
        self.assertEqual([w["id"] for w in warnings], ["threshold_anchor_conflict"])
        self.assertGreaterEqual(a["intervals"][0], hi)               # intervals never under the threshold band


class EasyBands(unittest.TestCase):
    """Easy and long runs from the marathon prediction, never near marathon pace (easy 6:19 vs MP 5:41)."""

    def test_athlete_easy_and_long_follow_the_marathon_prediction(self):
        plan = pe.build_plan(pe.PlanInputs(race_date=RACE), athlete_snapshot(), what_if_runs=False)
        b = bands(plan)
        v = 42195.0 / (4 * 3600)                                    # the corrected goal 4:00:00
        self.assertEqual(b["easy"], (round(v * 0.82, 3), round(v * 0.90, 3)))
        self.assertEqual(pe.band_text(b["easy"]), "6:19-6:56")       # not 5:50-6:40 (0.70-0.80 x LT)
        self.assertEqual(pe.band_text(b["long"]), "6:11-6:56")
        mp_s = 1000.0 / plan["_check"]["ctx"].paces.mp
        self.assertGreaterEqual(1000.0 / b["easy"][1], mp_s + 30 - 0.5)
        self.assertGreaterEqual(1000.0 / b["long"][1], mp_s + 20 - 0.5)
        self.assertTrue(plan["invariants"]["ok"], plan["invariants"]["failed"])

    def test_a_half_prediction_alone_converts_to_the_marathon(self):
        snap = athlete_snapshot(predictions=(("half", PRED_HALF),))
        a = pe.anchored_bands(pe.MetersPerSecond(LT), snap, pe.RULES)
        v_m = 21097.5 / PRED_HALF * 0.5 ** 0.06
        t = max(42195.0 / v_m, 42195.0 / (0.93 * LT))
        v = 42195.0 / (-(-t // 60) * 60)
        self.assertAlmostEqual(a["easy"][1], v * 0.90, places=6)

    def test_a_fast_runners_easy_fast_end_stays_30_s_slower_than_mp(self):
        # a 2:40 marathoner: 0.90 x MP speed would be MP + 25 s/km, so the fast end is held at MP + 30 s
        snap = dataclasses.replace(athlete_snapshot(predictions=(("marathon", 9600),)),
                                   lt_speed_mps=pe.MetersPerSecond(4.9))
        plan = pe.build_plan(pe.PlanInputs(race_date=RACE), snap, what_if_runs=False)
        b = bands(plan)
        mp_s = 1000.0 / plan["_check"]["ctx"].paces.mp
        self.assertAlmostEqual(mp_s, 9600 / 42.195, places=6)
        self.assertAlmostEqual(1000.0 / b["easy"][1], mp_s + 30, delta=0.5)
        self.assertAlmostEqual(1000.0 / b["long"][1], mp_s + 20, delta=0.5)
        self.assertLess(b["easy"][0], b["easy"][1])
        self.assertTrue(plan["invariants"]["ok"], plan["invariants"]["failed"])

    def test_without_predictions_the_easy_band_stays_on_lt(self):
        snap = athlete_snapshot(predictions=())
        free = bands(pe.build_plan(pe.PlanInputs(race_date=RACE), snap, what_if_runs=False))
        self.assertEqual(free["easy"], (round(LT * 0.70, 3), round(LT * 0.80, 3)))
        # a 3:05 goal (MP 0.94 x LT) is 47 s/km faster than 0.80 x LT: unchanged too (golden G5's MP at 0.85 x
        # LT is only 20 s/km faster, so its easy fast end is held at MP + 30 s)
        goal = bands(pe.build_plan(pe.PlanInputs(race_date=RACE, goal_time_s=3 * 3600 + 5 * 60), snap,
                                   what_if_runs=False))
        self.assertEqual(goal["easy"], free["easy"])

    def test_bands_match_roles_catches_an_easy_band_near_mp(self):
        plan = pe.build_plan(pe.PlanInputs(race_date=RACE), athlete_snapshot(), what_if_runs=False)
        paces = plan["_check"]["ctx"].paces
        paces.anchored["easy"] = (paces.mp * 0.95, paces.mp * 0.99)   # an easy band at MP + 3 s
        paces.rules = dict(paces.rules, pace_anchors=dict(paces.rules["pace_anchors"],
                                                          easy_min_s_slower_than_mp=0))   # Paces' own cap off
        check = {i["id"]: i for i in pe.check_invariants(plan)}["bands_match_roles"]
        self.assertFalse(check["ok"])
        self.assertIn("of marathon pace", check["detail"])


class SpeedLimits(unittest.TestCase):
    """A band outside the speed limits loses its pace target only with it listed as missing and a warning;
    otherwise bands_match_roles fails (it skipped every such band before)."""

    def failed(self, plan):
        return {i["id"]: i for i in pe.check_invariants(plan)}

    def test_bands_shifted_out_of_the_limits_fail_bands_match_roles(self):
        plan = pe.build_plan(pe.PlanInputs(race_date=RACE), athlete_snapshot(), what_if_runs=False)
        self.assertTrue(self.failed(plan)["bands_match_roles"]["ok"])
        plan["_check"]["ctx"].paces.lt = pe.MetersPerSecond(9.0)   # every LT band above 7 m/s
        check = self.failed(plan)["bands_match_roles"]
        self.assertFalse(check["ok"])
        self.assertIn("outside the speed limits", check["detail"])

    def test_a_slow_runners_easy_band_is_reported_missing(self):
        snap = athlete_snapshot(predictions=())
        slow = pe.build_plan(pe.PlanInputs(race_date=RACE), dataclasses.replace(
            snap, lt_speed_mps=pe.MetersPerSecond(2.0)), what_if_runs=False)   # 0.70 x 2.0 = 1.4 m/s < 1.5
        self.assertTrue(slow["invariants"]["ok"], slow["invariants"]["failed"])
        self.assertIn("pace_targets.easy", slow["missing"])
        self.assertIn("band_out_of_limits", [w["id"] for w in slow["warnings"]])
        ok = pe.build_plan(pe.PlanInputs(race_date=RACE), snap, what_if_runs=False)
        self.assertFalse([m for m in ok["missing"] if m.startswith("pace_targets.")])


class HalfProjection(unittest.TestCase):
    def test_half_projects_below_the_marathon(self):
        snap = athlete_snapshot()
        m = pe.assess(pe.PlanInputs(race_date=RACE), snap)
        h = pe.assess(pe.PlanInputs(race_date=RACE, race_distance="half"), snap)
        self.assertLess(h["projected_peak_km"], m["projected_peak_km"])
        self.assertLess(h["projected_long_km"], m["projected_long_km"])
        self.assertLessEqual(h["projected_long_km"], 24.0)
        self.assertEqual(h["feasibility"]["verdict"], "feasible")

    def test_half_peak_ceiling_and_long_cap_hold_in_the_invariants(self):
        snap = athlete_snapshot()
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
        rec = {"calendarDate": "2027-11-25", "time5K": PRED_5K, "time10K": PRED_10K,
               "timeHalfMarathon": PRED_HALF, "timeMarathon": PRED_MARATHON}
        self.assertEqual(dict(pt.predictions(rec)),
                         {"5k": PRED_5K, "10k": PRED_10K, "half": PRED_HALF, "marathon": PRED_MARATHON})
        self.assertEqual(pt.predictions({"time5K": 30}), ())


if __name__ == "__main__":
    unittest.main()
