"""Deep test phase: every invariant the plan engine reports can fail.

The property tests only assert that the invariants pass, so a check that cannot fail (an exemption too wide,
an early return, a dead branch) passes them too. Here each invariant gets a valid golden plan (G1: ACWR
enforced with a B race; G4: ACWR not enforced; G5: enforced with fixed calendar sessions), one minimal edit of
the finished plan that breaks exactly that rule, and check_invariants run again on the edited plan the way
build_plan calls it (nothing is rebuilt). The invariant must report a failure under its own id.

A mutation the invariant does not catch today is an expected failure marked "# KNOWN GAP: <why>": the branch
stays green and the gaps are listed. Remove the marker once the check is fixed (an unexpected success fails
the CI run, so a fixed check cannot go unnoticed).

    cd ext && python3 -m unittest test_deep_invariants_mutation -v

The engine checks are standard library only. dto_valid and cross_engine (plan_dto: upstream garmin_mcp's step
validators and daily_status' classifier) are skipped ("needs upstream ...") without upstream.
"""

from __future__ import annotations

import copy
import dataclasses
import datetime as dt
import importlib.util
import unittest
from unittest import mock
from typing import Any, Dict, Optional

import mcp2_compat
import plan_dto
import plan_engine as pe
from test_plan_engine import GOLDEN

mcp2_compat.register_finder()
HAVE_UPSTREAM = importlib.util.find_spec("garmin_mcp") is not None and mcp2_compat._mcp_major() == "2"
UPSTREAM_SKIP = "needs upstream garmin_mcp on mcp 2.x"
D = dt.date
_BUILT: Dict[str, Dict[str, Any]] = {}
WORK = (pe.Meters(6000.0), pe.Meters(2000.0))   # a quality session's work and rep length


def _blocked_g1() -> pe.PlanInputs:
    return dataclasses.replace(GOLDEN["G1"]["inputs"], blocked_dates=(D(2028, 1, 5),))


CASES = {
    "G1": lambda: (GOLDEN["G1"]["inputs"], GOLDEN["G1"]["snapshot"]),
    "G4": lambda: (GOLDEN["G4"]["inputs"], GOLDEN["G4"]["snapshot"]),
    "G5": lambda: (GOLDEN["G5"]["inputs"], GOLDEN["G5"]["snapshot"]),
    "G1-blocked": lambda: (_blocked_g1(), GOLDEN["G1"]["snapshot"]),
}


def fresh(name: str) -> Dict[str, Any]:
    """A deep copy of the case's plan (built once): each test edits its own."""
    if name not in _BUILT:
        inputs, snap = CASES[name]()
        _BUILT[name] = pe.build_plan(inputs, snap, what_if_runs=False)
    return copy.deepcopy(_BUILT[name])


def results(plan: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """check_invariants on the plan's own objects, as build_plan calls it: {id: {id, ok, detail}}."""
    return {i["id"]: i for i in pe.check_invariants(plan, plan["_check"]["ctx"].rules)}


def week(plan: Dict[str, Any], number: int) -> pe.Week:
    return next(w for w in plan["_check"]["weeks"] if w.number == number)


def session(plan: Dict[str, Any], key: str) -> pe.Session:
    return next(s for w in plan["_check"]["weeks"] for s in w.sessions if s.key == key)


def remake(plan: Dict[str, Any], key: str, *, role: Optional[str] = None, km: Optional[float] = None,
           date: Optional[dt.date] = None, add_km: float = 0.0) -> pe.Session:
    """Replace session `key` with one of the given role, distance and date (the rest kept), built by the
    engine's own make_session so its steps are what the engine would write."""
    ctx = plan["_check"]["ctx"]
    for w in plan["_check"]["weeks"]:
        for i, s in enumerate(w.sessions):
            if s.key != key:
                continue
            r = role or s.role
            dist = pe.Meters(km * 1000.0 if km is not None else s.distance + add_km * 1000.0)
            new = pe.make_session(s.key, date or s.date, r, dist, ctx.paces, ctx.rules,
                                  work=WORK if r in pe.QUALITY_ROLES else None)
            w.sessions[i] = new
            w.sessions.sort(key=lambda x: (x.date, x.key))
            return new
    raise KeyError(key)


def add(plan: Dict[str, Any], number: int, key: str, day: dt.date, role: str, km: float) -> pe.Session:
    ctx = plan["_check"]["ctx"]
    s = pe.make_session(key, day, role, pe.Meters(km * 1000.0), ctx.paces, ctx.rules,
                        work=WORK if role in pe.QUALITY_ROLES else None)
    w = week(plan, number)
    w.sessions.append(s)
    w.sessions.sort(key=lambda x: (x.date, x.key))
    return s


class Mutation(unittest.TestCase):
    """Each test: the case's plan passes `rule`, one edit, then `rule` fails and names itself."""

    def start(self, name: str, rule: str) -> Dict[str, Any]:
        plan = fresh(name)
        before = results(plan)
        self.assertIn(rule, before, f"{rule} is not reported at all")
        self.assertTrue(before[rule]["ok"], f"{name} fails {rule} before the edit: {before[rule]}")
        return plan

    def caught(self, plan: Dict[str, Any], rule: str) -> None:
        after = results(plan)
        self.assertIn(rule, after)
        self.assertFalse(after[rule]["ok"], f"{rule} does not catch the edit; failed: "
                                            f"{sorted(k for k, v in after.items() if not v['ok'])}")
        self.assertTrue(after[rule]["detail"], f"{rule} fails without saying where")


class EveryInvariantIsCovered(unittest.TestCase):
    def test_the_list_below_is_what_check_invariants_reports(self):
        for name in ("G1", "G4", "G5"):
            self.assertEqual(sorted(results(fresh(name))), sorted(ENGINE_INVARIANTS), name)
        self.assertTrue(all(results(fresh(n))[r]["ok"] for n in ("G1", "G4", "G5") for r in ENGINE_INVARIANTS))

    def test_each_invariant_has_a_mutation(self):
        names = {n for cls in (Volume, LongRuns, Intensity, Calendar, BRaceAndTaper, Acwr) for n in dir(cls)
                 if n.startswith("test_")}
        for rule in ENGINE_INVARIANTS:
            self.assertTrue(any(rule in n for n in names), f"no mutation test names {rule}")


ENGINE_INVARIANTS = ("growth_cap", "no_makeup", "deload_cadence", "deload_depth", "long_cap", "long_step",
                     "peak_long_timing", "easy_share", "peak_ceiling", "no_adjacent_hard", "bands_match_roles",
                     "b_windows", "a_taper", "last_quality", "rest_days", "one_run_per_day", "acwr_cap")


class Volume(Mutation):
    def test_growth_cap_g1_easy_run_pushes_the_week_over_8_percent(self):
        plan = self.start("G1", "growth_cap")
        remake(plan, "20280105-run", add_km=10)   # week 6 follows normal week 5
        self.caught(plan, "growth_cap")

    def test_growth_cap_g4_easy_run_pushes_the_week_over_8_percent(self):
        plan = self.start("G4", "growth_cap")
        remake(plan, "20271208-run", add_km=8)
        self.caught(plan, "growth_cap")

    def test_no_makeup_g1_week_after_the_deload_makes_up_its_km(self):
        plan = self.start("G1", "no_makeup")
        remake(plan, "20271229-run", add_km=12)   # week 5 follows deload week 4
        self.caught(plan, "no_makeup")

    def test_no_makeup_g4_week_after_the_deload_makes_up_its_km(self):
        plan = self.start("G4", "no_makeup")
        remake(plan, "20280126-run", add_km=12)   # week 9 follows deload week 8
        self.caught(plan, "no_makeup")

    def test_deload_cadence_g4_deload_relabelled_normal(self):
        plan = self.start("G4", "deload_cadence")
        week(plan, 4).kind = "normal"   # weeks 1-7 now all normal
        self.caught(plan, "deload_cadence")

    def test_deload_cadence_g5_deload_relabelled_normal(self):
        plan = self.start("G5", "deload_cadence")
        week(plan, 8).kind = "normal"   # weeks 5-11 now all normal
        self.caught(plan, "deload_cadence")

    def test_deload_depth_g1_deload_week_at_full_volume(self):
        plan = self.start("G1", "deload_depth")
        remake(plan, "20271222-run", add_km=10)
        self.caught(plan, "deload_depth")

    def test_deload_depth_g4_deload_week_at_full_volume(self):
        plan = self.start("G4", "deload_depth")
        remake(plan, "20280119-run", add_km=12)
        self.caught(plan, "deload_depth")

    def test_peak_ceiling_g4_peak_week_over_the_ceiling(self):
        plan = self.start("G4", "peak_ceiling")
        remake(plan, "20280223-run", add_km=10)   # week 13 is at the 73 km ceiling
        self.caught(plan, "peak_ceiling")

    def test_peak_ceiling_g1_peak_week_over_the_ceiling(self):
        plan = self.start("G1", "peak_ceiling")
        remake(plan, "20280209-run", add_km=10)   # week 11 is at the 65 km ceiling
        self.caught(plan, "peak_ceiling")


class LongRuns(Mutation):
    def test_long_cap_g1_long_run_5_km_over_its_cap(self):
        plan = self.start("G1", "long_cap")
        remake(plan, "20280109-run", add_km=5)
        self.caught(plan, "long_cap")

    def test_long_cap_g4_long_run_over_max_long_km_only(self):
        plan = self.start("G4", "long_cap")
        remake(plan, "20280130-run", km=33.5)   # 32 km max_long_km; the 2 km step (34 km) still allows it
        self.caught(plan, "long_cap")

    def test_long_step_g1_long_run_3_km_over_the_longest_so_far(self):
        plan = self.start("G1", "long_step")
        remake(plan, "20271212-run", km=21.6 + 3)   # week 1's 21.6 km is the longest before it
        self.caught(plan, "long_step")

    def test_long_step_g4_deload_long_run_over_the_peak_long_run(self):
        plan = self.start("G4", "long_step")
        remake(plan, "20280123-run", km=34.0)   # a reduced week's long run above every normal week's
        self.caught(plan, "long_step")

    # Was a known gap (long_step compared with the longest long run so far): the step now counts from the
    # last normal week's long run as placed, so a 39% jump after a short week is caught.
    def test_long_step_g4_39_percent_jump_after_a_travel_gap_week(self):
        plan = self.start("G4", "long_step")
        remake(plan, "20280206-run", km=23.0)   # a short week (travel): long run 23 km
        self.assertAlmostEqual(session(plan, "20280213-run").distance / 23000.0, 32.0 / 23.0)   # +39% next week
        self.caught(plan, "long_step")

    def test_peak_long_timing_g1_long_run_in_the_last_21_days(self):
        plan = self.start("G1", "peak_long_timing")
        remake(plan, "20280220-run", km=30.0)   # 5 days before the race
        self.caught(plan, "peak_long_timing")

    def test_peak_long_timing_g4_long_run_in_the_last_21_days(self):
        plan = self.start("G4", "peak_long_timing")
        remake(plan, "20280305-run", km=32.0)   # 12 days before the race
        self.caught(plan, "peak_long_timing")

    def test_peak_long_timing_g1_long_run_inside_the_b_window(self):
        plan = self.start("G1", "peak_long_timing")
        remake(plan, "20280130-run", role="long", km=16.0)   # the day after the recovery starts
        self.caught(plan, "peak_long_timing")


class Intensity(Mutation):
    def test_easy_share_g1_easy_runs_turned_threshold(self):
        plan = self.start("G1", "easy_share")
        for key in ("20280105-run", "20280108-run"):
            remake(plan, key, role="threshold")
        self.caught(plan, "easy_share")

    def test_easy_share_g4_easy_runs_turned_threshold(self):
        plan = self.start("G4", "easy_share")
        for key in ("20280202-run", "20280205-run"):
            remake(plan, key, role="threshold")
        self.caught(plan, "easy_share")

    # Was a known gap (the long run counted as easy time): easy_share now also needs an easy run in a week with
    # quality or a long run when there is a day and room for one.
    def test_easy_share_g1_week_with_no_easy_run(self):
        plan = self.start("G1", "easy_share")
        w = week(plan, 6)
        w.sessions = [s for s in w.sessions if s.role not in ("easy", "strides")]
        self.caught(plan, "easy_share")

    def test_easy_share_g4_week_with_no_easy_run(self):
        plan = self.start("G4", "easy_share")
        w = week(plan, 10)
        w.sessions = [s for s in w.sessions if s.role not in ("easy", "strides")]
        self.caught(plan, "easy_share")

    def test_no_adjacent_hard_g1_threshold_the_day_before_the_long_run(self):
        plan = self.start("G1", "no_adjacent_hard")
        remake(plan, "20280108-run", role="threshold")   # Saturday, long run on Sunday
        self.caught(plan, "no_adjacent_hard")

    def test_no_adjacent_hard_g4_threshold_the_day_after_intervals(self):
        plan = self.start("G4", "no_adjacent_hard")
        remake(plan, "20280112-run", role="threshold")   # Wednesday between Tuesday's and Thursday's quality
        self.caught(plan, "no_adjacent_hard")

    def test_no_adjacent_hard_g5_threshold_next_to_a_fixed_hard_session(self):
        plan = self.start("G5", "no_adjacent_hard")
        remake(plan, "20271216-run", role="threshold")   # Thu 16 Dec, the fixed hard run is on Wed 15 Dec
        self.caught(plan, "no_adjacent_hard")

    def test_no_adjacent_hard_g1_threshold_the_day_after_the_b_race(self):
        plan = self.start("G1", "no_adjacent_hard")
        remake(plan, "20280129-run", role="threshold")   # Sat 29 Jan, the B race on Fri 28 Jan
        self.caught(plan, "no_adjacent_hard")

    def test_no_adjacent_hard_g1_threshold_the_day_before_the_a_race(self):
        plan = self.start("G1", "no_adjacent_hard")
        remake(plan, "20280224-run", role="threshold")
        self.caught(plan, "no_adjacent_hard")

    def test_bands_match_roles_g1_threshold_step_in_an_easy_run(self):
        plan = self.start("G1", "bands_match_roles")
        s = session(plan, "20280105-run")
        s.steps = (dataclasses.replace(s.steps[0], band="threshold"),)
        self.caught(plan, "bands_match_roles")

    def test_bands_match_roles_g4_easy_work_in_a_threshold_session(self):
        plan = self.start("G4", "bands_match_roles")
        s = session(plan, "20271228-run")
        s.steps = tuple(_rebanded(st, "easy") for st in s.steps)
        self.caught(plan, "bands_match_roles")

    def test_bands_match_roles_g1_mp_step_in_a_long_run(self):
        plan = self.start("G1", "bands_match_roles")
        s = session(plan, "20280109-run")
        s.steps = (dataclasses.replace(s.steps[0], band="mp"),)
        self.caught(plan, "bands_match_roles")

    # Was a known gap (Paces.band() hid a band outside speed_limits_mps): bands_match_roles reads raw_band and
    # fails a band out of the limits unless it is listed as missing (pace_targets.<band>).
    def test_bands_match_roles_g1_bands_shifted_out_of_the_speed_limits(self):
        plan = self.start("G1", "bands_match_roles")
        plan["_check"]["ctx"].paces.lt = pe.MetersPerSecond(9.0)   # every band now above 7 m/s
        self.caught(plan, "bands_match_roles")

    def test_last_quality_g1_threshold_two_days_before_the_race(self):
        plan = self.start("G1", "last_quality")
        remake(plan, "20280223-run", role="threshold")   # Wed 23 Feb, race Fri 25 Feb
        self.caught(plan, "last_quality")

    def test_last_quality_g4_threshold_two_days_before_the_race(self):
        plan = self.start("G4", "last_quality")
        remake(plan, "20280315-run", role="threshold")   # Wed 15 Mar, race Fri 17 Mar
        self.caught(plan, "last_quality")


class Calendar(Mutation):
    def test_rest_days_g1_run_moved_to_a_monday(self):
        plan = self.start("G1", "rest_days")
        remake(plan, "20280105-run", date=D(2028, 1, 3))
        self.caught(plan, "rest_days")

    def test_rest_days_g4_run_moved_to_a_friday(self):
        plan = self.start("G4", "rest_days")
        remake(plan, "20280108-run", date=D(2028, 1, 7))
        self.caught(plan, "rest_days")

    def test_rest_days_g1_run_on_a_blocked_date(self):
        plan = self.start("G1-blocked", "rest_days")
        self.assertFalse([s for w in plan["_check"]["weeks"] for s in w.sessions if s.date == D(2028, 1, 5)])
        add(plan, 6, pe.session_key(D(2028, 1, 5)), D(2028, 1, 5), "easy", 5.0)
        self.caught(plan, "rest_days")

    def test_one_run_per_day_g1_second_run_on_a_day(self):
        plan = self.start("G1", "one_run_per_day")
        add(plan, 6, pe.session_key(D(2028, 1, 5)), D(2028, 1, 5), "easy", 5.0)   # an easy run is already on Wed 5 Jan
        self.caught(plan, "one_run_per_day")

    def test_one_run_per_day_g4_second_run_on_a_day(self):
        plan = self.start("G4", "one_run_per_day")
        remake(plan, "20280108-run", date=D(2028, 1, 5))   # onto the Wednesday easy run's day
        self.caught(plan, "one_run_per_day")

    def test_one_run_per_day_g5_run_on_the_fixed_run_day(self):
        plan = self.start("G5", "one_run_per_day")
        remake(plan, "20271214-run", date=D(2027, 12, 15))   # the fixed TrainingPeaks run
        self.caught(plan, "one_run_per_day")


class BRaceAndTaper(Mutation):
    def test_b_windows_g1_long_run_inside_the_b_window(self):
        plan = self.start("G1", "b_windows")
        remake(plan, "20280130-run", role="long", km=16.0)
        self.caught(plan, "b_windows")

    def test_b_windows_g1_quality_in_the_mini_taper(self):
        plan = self.start("G1", "b_windows")
        remake(plan, "20280125-run", role="threshold")
        self.caught(plan, "b_windows")

    def test_b_windows_g1_easy_run_over_the_mini_taper_cap(self):
        plan = self.start("G1", "b_windows")
        remake(plan, "20280125-run", role="easy", km=12.0)   # Tue 25 Jan, in the mini-taper before the B race
        self.caught(plan, "b_windows")

    def test_b_windows_g1_long_run_too_soon_after_the_b_race(self):
        plan = self.start("G1", "b_windows")
        remake(plan, "20280201-run", date=D(2028, 2, 6))   # the long run's Sunday and Tuesday change places:
        remake(plan, "20280206-run", date=D(2028, 2, 1))   # 4 days after the B race
        self.caught(plan, "b_windows")

    def test_a_taper_g1_taper_week_at_full_volume(self):
        plan = self.start("G1", "a_taper")
        remake(plan, "20280216-run", add_km=15)
        self.caught(plan, "a_taper")

    def test_a_taper_g4_race_week_at_full_volume(self):
        plan = self.start("G4", "a_taper")
        remake(plan, "20280315-run", add_km=15)
        self.caught(plan, "a_taper")


class Acwr(Mutation):
    def test_acwr_cap_g5_easy_run_raises_a_free_weeks_ratio(self):
        plan = self.start("G5", "acwr_cap")
        remake(plan, "20280115-run", km=14.0)
        self.caught(plan, "acwr_cap")

    def test_acwr_cap_g1_easy_run_raises_a_free_weeks_ratio(self):
        plan = self.start("G1", "acwr_cap")
        remake(plan, "20280209-run", add_km=25)   # week 11 is neither held nor at its floor
        self.caught(plan, "acwr_cap")

    # Was a known gap (check_acwr skipped held weeks): a held day is exempt only while nothing is left to cut.
    def test_acwr_cap_g1_ratio_raising_day_in_a_held_week(self):
        plan = self.start("G1", "acwr_cap")
        self.assertTrue(week(plan, 9).acwr_held)
        remake(plan, "20280125-run", add_km=6)   # Tue 25 Jan, the mini-taper before the B race
        self.caught(plan, "acwr_cap")

    # As above, G5's held week 3 (the heavy TrainingPeaks week).
    def test_acwr_cap_g5_ratio_raising_day_in_a_held_week(self):
        plan = self.start("G5", "acwr_cap")
        self.assertTrue(week(plan, 3).acwr_held)
        remake(plan, "20271219-run", add_km=10)
        self.caught(plan, "acwr_cap")

    # Was a known gap (a plain ok while not enforced): the entry now says not_enforced.
    def test_acwr_cap_g4_not_enforced_ratio_far_over_the_cap(self):
        plan = self.start("G4", "acwr_cap")
        remake(plan, "20280202-run", add_km=25)
        ctx = plan["_check"]["ctx"]
        loads = pe.day_loads(plan["_check"]["weeks"], ctx)
        top = max(pe.acwr_on(D(2028, 2, 2) + dt.timedelta(days=i), loads, ctx.rules) or 0.0 for i in range(5))
        self.assertGreater(top, float(ctx.rules["acwr"]["max"]))
        entry = results(plan)["acwr_cap"]
        self.assertTrue(entry["ok"] is not True or _says_not_enforced(entry), entry)

    # Was a known gap: the not-enforced acwr_cap entry carries not_enforced and its reason.
    def test_acwr_cap_g4_not_enforced_is_distinguishable_from_ok(self):
        plan = fresh("G4")
        self.assertFalse(plan["acwr"]["enforced"])
        not_enforced = results(plan)["acwr_cap"]
        ok = results(fresh("G1"))["acwr_cap"]
        self.assertTrue(ok["ok"])
        self.assertTrue(_says_not_enforced(not_enforced) and not _says_not_enforced(ok), (not_enforced, ok))


def _rebanded(st: pe.Step, band: str) -> pe.Step:
    """The step with its interval steps (also inside repeats) on `band`."""
    if st.kind == "repeat":
        return dataclasses.replace(st, children=tuple(_rebanded(c, band) for c in st.children))
    return dataclasses.replace(st, band=band) if st.kind == "interval" else st


def _says_not_enforced(entry: Dict[str, Any]) -> bool:
    status = str(entry.get("status", ""))
    return (entry.get("enforced") is False or status.replace("-", "_") == "not_enforced"
            or "not enforced" in str(entry.get("detail", "")).lower())


def answer(name: str) -> Dict[str, Any]:
    return pe.plan_answer(fresh(name))


def lt_of(name: str) -> Optional[float]:
    return CASES[name]()[1].lt_speed_mps


def dto_results(plan: Dict[str, Any], name: str) -> Dict[str, Dict[str, Any]]:
    return {c["id"]: c for c in plan_dto.check_plan(plan, lt_of(name))}


def first_of(plan: Dict[str, Any], role: str) -> Dict[str, Any]:
    return next(s for w in plan["weeks"] for s in w["sessions"] if s["role"] == role)


@unittest.skipUnless(HAVE_UPSTREAM, UPSTREAM_SKIP)
class UpstreamChecks(unittest.TestCase):
    """dto_valid and cross_engine (plan_dto.check_plan, as plan_tools.with_dto_checks calls it on the answer)."""

    def assert_ok_then(self, name: str, rule: str, edit: Any) -> None:
        plan = answer(name)
        before = dto_results(plan, name)
        self.assertTrue(before[rule]["ok"], before[rule])
        self.assertFalse(str(before[rule]["detail"]).startswith("skipped"), before[rule])
        edit(plan)
        after = dto_results(plan, name)
        self.assertFalse(after[rule]["ok"], f"{rule} does not catch the edit")
        self.assertTrue(after[rule]["detail"])

    def assert_drift_caught(self, name: str, table: Dict[str, Any], change: Dict[str, Any]) -> None:
        """A wrong id in the DTO writer's tables (the DTOs check_plan builds from the answer)."""
        plan = answer(name)
        self.assertTrue(dto_results(plan, name)["dto_valid"]["ok"])
        with mock.patch.dict(table, change):
            after = dto_results(plan, name)["dto_valid"]
        self.assertFalse(after["ok"], "dto_valid does not catch the edit")
        self.assertTrue(after["detail"])

    def test_dto_valid_g1_pace_target_with_the_wrong_type_id(self):
        self.assert_drift_caught("G1", plan_dto.PACE_TARGET, {"workoutTargetTypeId": 4})

    def test_dto_valid_g4_distance_end_with_the_time_id(self):
        self.assert_drift_caught("G4", plan_dto.END_CONDITIONS, {"distance": 2})

    # Was a known gap (only upstream's id/key checks ran): dto_valid also checks every step's values
    # (plan_dto.step_value_problems), so a 0 m distance step is caught.
    def test_dto_valid_g1_distance_step_with_no_distance(self):
        def edit(plan: Dict[str, Any]) -> None:
            first_of(plan, "easy")["steps"][0]["distance_m"] = 0
        self.assert_ok_then("G1", "dto_valid", edit)

    def test_cross_engine_g1_threshold_labelled_easy(self):
        def edit(plan: Dict[str, Any]) -> None:
            for w in plan["weeks"]:
                for s in w["sessions"]:
                    if s["role"] == "threshold":
                        s["hard_or_long"] = False
        self.assert_ok_then("G1", "cross_engine", edit)

    def test_cross_engine_g5_long_run_labelled_easy(self):
        def edit(plan: Dict[str, Any]) -> None:
            for w in plan["weeks"]:
                for s in w["sessions"]:
                    if s["role"] == "long":
                        s["hard_or_long"] = False
        self.assert_ok_then("G5", "cross_engine", edit)


if __name__ == "__main__":
    unittest.main()
