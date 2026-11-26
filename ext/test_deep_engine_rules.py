"""Deep test phase: plan_engine's overrides (every kind applied and refused, conflicts, past dates), the planned
ACWR (rules v2 item 12: the 2-of-3 tracking rule, B-race load, the floor, the days a cut can lower),
feasibility verdicts and corrected_goal_s at their boundaries, what_if and assess.

    cd ext && python3 -m unittest test_deep_engine_rules -v

Standard library only. Every plan built here is also checked by test_deep_engine_matrix's oracle.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import math
import unittest
from typing import Any, Dict, List, Optional, Tuple

import plan_engine as pe
from test_deep_engine_matrix import PROFILES, TODAY, Oracle, build, check_plan, snapshot

D = dt.date
RACE = D(2027, 3, 21)   # a Sunday, 16 plan weeks from TODAY (Wednesday 2026-11-25)


def _day(text: str) -> dt.date:
    return D.fromisoformat(text)


class OverrideBase(Oracle):
    def setUp(self) -> None:
        self.snap = snapshot(TODAY, PROFILES["owner40"])
        # six run days, Monday off: a build week then has a free run day (Saturday, before the long run)
        self.inputs = pe.PlanInputs(race_date=RACE, goal_time_s=12600, run_days=(1, 2, 3, 4, 5, 6), rest_days=(0,))
        self.base = pe.plan_answer(build(self.inputs, self.snap))
        self.sessions = {s["key"]: s for w in self.base["weeks"] for s in w["sessions"]}

    def edit(self, *overrides: pe.Override) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        inputs = dataclasses.replace(self.inputs, overrides=tuple(overrides))
        plan = build(inputs, self.snap)
        self.assertTrue(plan["invariants"]["ok"], plan["invariants"]["failed"])
        self.assertEqual(check_plan(inputs, self.snap, plan), [])
        a = pe.plan_answer(plan)
        self.assertEqual(len(a["overrides"]["applied"]) + len(a["overrides"]["refused"]), len(overrides))
        for wa, wb in zip(self.base["weeks"], a["weeks"]):
            self.assertLessEqual(wb["planned_km"], wa["planned_km"] + 0.05, wb["start"])
            self.assertLessEqual(wb["long_km"] or 0.0, wa["long_km"] or 0.0, wb["start"])
            self.assertLessEqual(sum(1 for s in wb["sessions"] if s["role"] in pe.QUALITY_ROLES),
                                 sum(1 for s in wa["sessions"] if s["role"] in pe.QUALITY_ROLES))
        return a, {s["key"]: s for w in a["weeks"] for s in w["sessions"]}

    def refused(self, o: pe.Override, rule: str) -> Dict[str, Any]:
        a, now = self.edit(o)
        self.assertEqual(a["overrides"]["applied"], [], o)
        self.assertEqual([r["rule"] for r in a["overrides"]["refused"]], [rule], a["overrides"]["refused"])
        self.assertEqual(a["weeks"], self.base["weeks"])   # a refused edit leaves no trace
        self.assertEqual(a["overrides"]["refused"][0]["edit"], o.echo())
        return a

    def applied(self, *o: pe.Override) -> Dict[str, Dict[str, Any]]:
        a, now = self.edit(*o)
        self.assertEqual(a["overrides"]["refused"], [], a["overrides"]["refused"])
        self.assertEqual(a["overrides"]["applied"], [x.echo() for x in o])
        return now

    def week_of(self, key: str) -> Dict[str, Any]:
        return next(w for w in self.base["weeks"] if any(s["key"] == key for s in w["sessions"]))

    def a_normal_week(self) -> Dict[str, Any]:
        """The first normal build week (threshold, easy runs, a second quality session, the long run and a
        free run day)."""
        return next(w for w in self.base["weeks"] if w["kind"] == "normal" and w["phase"] == "build")

    def key(self, week: Dict[str, Any], role: str) -> str:
        return next(s["key"] for s in week["sessions"] if s["role"] == role)


class OverridesApplied(OverrideBase):
    def test_move_an_easy_run_to_a_free_run_day(self):
        w = self.a_normal_week()
        taken = {s["date"] for s in w["sessions"]}
        e = self.key(w, "easy")
        free = [d for d in (_day(w["start"]) + dt.timedelta(days=i) for i in range(7))
                if d.isoformat() not in taken and d.weekday() not in self.inputs.rest_days]
        self.assertTrue(free)
        now = self.applied(pe.Override("move", key=e, date=free[0]))
        self.assertEqual(now[e]["date"], free[0].isoformat())
        self.assertEqual(now[e]["km"], self.sessions[e]["km"])

    def test_swap_two_easy_days_and_swap_back(self):
        w = self.a_normal_week()
        easy = [s for s in w["sessions"] if s["role"] in ("easy", "strides")]
        a, b = _day(easy[0]["date"]), _day(easy[1]["date"])
        now = self.applied(pe.Override("swap", date=a, date_b=b))
        self.assertEqual(now[easy[0]["key"]]["date"], b.isoformat())
        back, _ = self.edit(pe.Override("swap", date=a, date_b=b), pe.Override("swap", date=b, date_b=a))
        self.assertEqual(back["weeks"], self.base["weeks"])

    def test_swap_with_an_empty_day(self):
        w = self.a_normal_week()
        taken = {s["date"] for s in w["sessions"]}
        e = next(s for s in w["sessions"] if s["role"] == "easy")
        empty = next(d for d in (_day(w["start"]) + dt.timedelta(days=i) for i in range(7))
                     if d.isoformat() not in taken and d.weekday() not in self.inputs.rest_days)
        now = self.applied(pe.Override("swap", date=_day(e["date"]), date_b=empty))
        self.assertEqual(now[e["key"]]["date"], empty.isoformat())

    # Each edit below only lowers load; one that breaks a rule on the finished plan (80% easy, the long-run
    # cap and step that follow the longest long run so far) is undone and refused with that rule.
    SIDE_EFFECTS = ("easy_share", "long_cap", "long_step")

    def try_each(self, make, weeks):
        """Apply make(week) to each week alone: [(week, answer, sessions)] of those applied; every refusal is
        one of SIDE_EFFECTS."""
        done = []
        for w in weeks:
            o = make(w)
            a, now = self.edit(o)
            if a["overrides"]["refused"]:
                self.assertIn(a["overrides"]["refused"][0]["rule"], self.SIDE_EFFECTS, (o.echo(), a["overrides"]))
                self.assertEqual(a["weeks"], self.base["weeks"])
            else:
                self.assertEqual(a["overrides"]["applied"], [o.echo()])
                done.append((w, a, now))
        self.assertTrue(done)
        return done

    def normal_weeks(self):
        return [w for w in self.base["weeks"] if w["kind"] == "normal"]

    def test_rest(self):
        w = self.a_normal_week()
        e = self.key(w, "easy")
        # in a week with two quality sessions, resting an easy run breaks 80% easy: undone and refused
        self.refused(pe.Override("rest", date=_day(self.sessions[e]["date"])), "easy_share")
        for w, _a, now in self.try_each(lambda w: pe.Override("rest", date=_day(self.sessions[self.key(w, "easy")]["date"])),
                                        self.normal_weeks()):
            self.assertNotIn(self.key(w, "easy"), now)

    def test_easy(self):
        q = self.key(self.a_normal_week(), "threshold")
        now = self.applied(pe.Override("easy", key=q))
        self.assertEqual(now[q]["role"], "easy")
        self.assertLessEqual(now[q]["km"], self.sessions[q]["km"])
        for w, _a, now in self.try_each(lambda w: pe.Override("easy", date=_day(self.sessions[self.key(w, "long")]["date"])),
                                        self.normal_weeks()):
            lg = self.key(w, "long")
            self.assertEqual(now[lg]["role"], "easy")
            self.assertLessEqual(now[lg]["km"], min(14.0, self.sessions[lg]["km"]))

    def test_cap_km(self):
        for w, _a, now in self.try_each(lambda w: pe.Override("cap_km", key=self.key(w, "easy"), km=4.0),
                                        self.normal_weeks()):
            self.assertEqual(now[self.key(w, "easy")]["km"], 4.0)
        lg = self.key(self.a_normal_week(), "long")
        now = self.applied(pe.Override("cap_km", date=_day(self.sessions[lg]["date"]), km=self.sessions[lg]["km"]))
        self.assertEqual(now[lg]["km"], self.sessions[lg]["km"])

    def test_cap_week_km(self):
        for w, a, _now in self.try_each(lambda w: pe.Override("cap_week_km", week_start=_day(w["start"]),
                                                              km=w["planned_km"] - 10), self.normal_weeks()):
            self.assertLessEqual(next(x for x in a["weeks"] if x["start"] == w["start"])["planned_km"],
                                 w["planned_km"] - 10 + 0.05)
        w = self.a_normal_week()
        a, _ = self.edit(pe.Override("cap_week_km", week_start=_day(w["start"]), km=w["planned_km"]))
        self.assertEqual(a["weeks"], self.base["weeks"])
        a, _ = self.edit(pe.Override("cap_week_km", week_start=_day(w["start"]), km=0.0))
        self.assertEqual(next(x for x in a["weeks"] if x["start"] == w["start"])["planned_km"], 0.0)

    def test_long_run_day_to_saturday_swaps_with_the_easy_run(self):
        w = self.a_normal_week()
        lg = self.key(w, "long")
        sat = _day(w["start"]) + dt.timedelta(days=5)
        before = [s for s in w["sessions"] if s["date"] == sat.isoformat()]
        a, now = self.edit(pe.Override("long_run_day", week_start=_day(w["start"]), weekday=5))
        if a["overrides"]["refused"]:   # the Friday or Sunday next to it may be hard
            self.assertEqual(a["overrides"]["refused"][0]["rule"], "no_adjacent_hard")
            return
        self.assertEqual(now[lg]["date"], sat.isoformat())
        for s in before:
            self.assertEqual(now[s["key"]]["date"], self.sessions[lg]["date"])

    def test_week0_can_be_capped_and_rested(self):
        w0 = self.base["weeks"][0]
        self.assertEqual(w0["kind"], "week0")
        a, _ = self.edit(pe.Override("cap_week_km", week_start=_day(w0["start"]), km=w0["planned_km"] / 2))
        self.assertEqual(a["overrides"]["refused"], [])
        first = w0["sessions"][0]
        now = self.applied(pe.Override("rest", date=_day(first["date"])))
        self.assertNotIn(first["key"], now)


class OverridesRefused(OverrideBase):
    def test_move_refusals(self):
        w = self.a_normal_week()
        e, q, lg = self.key(w, "easy"), self.key(w, "threshold"), self.key(w, "long")
        start = _day(w["start"])
        rest_day = start + dt.timedelta(days=self.inputs.rest_days[0])
        other_run = next(s for s in w["sessions"] if s["key"] not in (e,) and s["role"] != "long")
        cases = [
            (pe.Override("move", key=e, date=start + dt.timedelta(days=7)), "move_within_week"),
            (pe.Override("move", key=e, date=start - dt.timedelta(days=1)), "move_within_week"),
            (pe.Override("move", key=e, date=rest_day), "rest_days"),
            (pe.Override("move", key=e, date=_day(other_run["date"])), "one_run_per_day"),
            (pe.Override("move", key=e), "date_required"),
            (pe.Override("move", key="20401105-run", date=start), "unknown_session"),
            (pe.Override("move", key="", date=start), "unknown_session"),
            (pe.Override("move", key=self.key(self.base["weeks"][-1], "race"), date=RACE - dt.timedelta(days=1)),
             "unknown_session"),
            (pe.Override("move", key=q, date=_day(self.sessions[lg]["date"]) - dt.timedelta(days=1)),
             "no_adjacent_hard"),
        ]
        for o, rule in cases:
            with self.subTest(o=o.echo()):
                self.refused(o, rule)

    def test_move_to_a_blocked_date_a_past_day_and_a_fixed_run(self):
        w = self.a_normal_week()
        e = self.key(w, "easy")
        free = next(d for d in (_day(w["start"]) + dt.timedelta(days=i) for i in range(7))
                    if d.isoformat() not in {s["date"] for s in w["sessions"]}
                    and d.weekday() not in self.inputs.rest_days)
        # blocked: the date is blocked in the inputs (the plan is rebuilt with it, the edit refused)
        self.inputs = dataclasses.replace(self.inputs, blocked_dates=(free,))
        self.base = pe.plan_answer(build(self.inputs, self.snap))
        self.refused(pe.Override("move", key=e, date=free), "blocked_dates")
        # a fixed run on the calendar that day
        self.inputs = dataclasses.replace(self.inputs, blocked_dates=())
        self.snap = snapshot(TODAY, PROFILES["owner40"], fixed=(pe.FixedSession(free, "run", False, False, 40.0),))
        self.base = pe.plan_answer(build(self.inputs, self.snap))
        self.refused(pe.Override("move", key=e, date=free), "one_run_per_day")
        # week 0: today and before are past
        self.snap = snapshot(TODAY, PROFILES["owner40"])
        self.base = pe.plan_answer(build(self.inputs, self.snap))
        w0 = self.base["weeks"][0]
        k0 = w0["sessions"][0]["key"]
        for day in (TODAY, TODAY - dt.timedelta(days=1)):
            self.refused(pe.Override("move", key=k0, date=day), "past_day")

    def test_hard_sessions_into_a_b_window_and_the_last_21_days(self):
        b_day = D(2027, 1, 16)
        self.inputs = dataclasses.replace(self.inputs, b_races=(pe.BRace(b_day, "tri_half"),))
        self.base = pe.plan_answer(build(self.inputs, self.snap))
        self.sessions = {s["key"]: s for w in self.base["weeks"] for s in w["sessions"]}
        mini = _day(self.base["b_races"][0]["mini_taper"][0])
        w = next(x for x in self.base["weeks"] if x["start"] <= mini.isoformat() <= x["end"])
        hard = [s for s in w["sessions"] if s["hard_or_long"] and s["role"] != "b_race"
                and s["date"] < mini.isoformat()]
        for s in hard:
            with self.subTest(key=s["key"]):
                self.refused(pe.Override("move", key=s["key"], date=mini), "b_window")
        # the long run of the last normal week before the taper, moved later into the last 21 days
        late_week = next(x for x in self.base["weeks"] if x["kind"] == "normal"
                         and (RACE - _day(x["end"])).days < 21 + 7 and any(s["role"] == "long" for s in x["sessions"]))
        lg = self.key(late_week, "long")
        later = _day(late_week["end"])
        if (RACE - _day(self.sessions[lg]["date"])).days >= 21 > (RACE - later).days \
                and self.sessions[lg]["date"] != later.isoformat():
            a, _ = self.edit(pe.Override("move", key=lg, date=later))
            self.assertIn(a["overrides"]["refused"][0]["rule"] if a["overrides"]["refused"] else "applied",
                          ("peak_long_timing", "no_adjacent_hard", "one_run_per_day", "applied"))

    def test_swap_refusals(self):
        w = self.a_normal_week()
        lg = self.sessions[self.key(w, "long")]
        q = self.sessions[self.key(w, "threshold")]
        start = _day(w["start"])
        rest_day = start + dt.timedelta(days=self.inputs.rest_days[0])
        cases = [
            (pe.Override("swap", date=_day(lg["date"]), date_b=_day(lg["date"])), "two_dates_required"),
            (pe.Override("swap", date=_day(lg["date"])), "two_dates_required"),
            (pe.Override("swap", date=rest_day, date_b=TODAY - dt.timedelta(days=1)), "nothing_to_swap"),
            (pe.Override("swap", date=_day(lg["date"]), date_b=rest_day), "rest_days"),
            (pe.Override("swap", date=_day(q["date"]), date_b=_day(q["date"]) + dt.timedelta(days=7)),
             "move_within_week"),
            (pe.Override("swap", date=RACE, date_b=RACE - dt.timedelta(days=1)), "race_day"),
        ]
        for o, rule in cases:
            with self.subTest(o=o.echo()):
                self.refused(o, rule)

    def test_rest_easy_cap_refusals(self):
        w = self.a_normal_week()
        e, q, lg = self.key(w, "easy"), self.key(w, "threshold"), self.key(w, "long")
        empty = next(d for d in (_day(w["start"]) + dt.timedelta(days=i) for i in range(7))
                     if d.isoformat() not in {s["date"] for s in w["sessions"]})
        cases = [
            (pe.Override("rest", date=empty), "no_run_that_day"),
            (pe.Override("rest", date=TODAY - dt.timedelta(days=3)), "no_run_that_day"),
            (pe.Override("rest", date=RACE), "race_day"),
            (pe.Override("rest"), "date_required"),
            (pe.Override("easy", key=e), "already_easy"),
            (pe.Override("easy", date=empty), "unknown_session"),
            (pe.Override("easy", date=RACE), "unknown_session"),
            (pe.Override("cap_km", key=e, km=self.sessions[e]["km"] + 0.2), "raises_load"),
            (pe.Override("cap_km", key=e, km=3.9), "easy_min_km"),
            (pe.Override("cap_km", key=e), "km_required"),
            (pe.Override("cap_km", key=q, km=4.0), "cap_km_role"),
            (pe.Override("cap_km", key=lg, km=40.0), "raises_load"),
            (pe.Override("cap_week_km", week_start=_day(w["start"]), km=w["planned_km"] + 0.1), "raises_load"),
            (pe.Override("cap_week_km", week_start=_day(w["start"]) + dt.timedelta(days=1), km=10.0), "unknown_week"),
            (pe.Override("cap_week_km", week_start=_day(w["start"])), "unknown_week"),
            (pe.Override("cap_week_km", week_start=D(2030, 1, 7), km=10.0), "unknown_week"),
            (pe.Override("long_run_day", week_start=_day(w["start"])), "unknown_week"),
            (pe.Override("long_run_day", week_start=_day(self.base["weeks"][-1]["start"]), weekday=2), "no_long_run"),
            (pe.Override("long_run_day", week_start=_day(w["start"]), weekday=self.inputs.rest_days[0]), "rest_days"),
        ]
        for o, rule in cases:
            with self.subTest(o=o.echo()):
                self.refused(o, rule)

    def test_conflicting_overrides(self):
        w = self.a_normal_week()
        e = self.sessions[self.key(w, "easy")]
        free = next(d for d in (_day(w["start"]) + dt.timedelta(days=i) for i in range(7))
                    if d.isoformat() not in {s["date"] for s in w["sessions"]}
                    and d.weekday() not in self.inputs.rest_days)
        # move, then rest the old day (nothing left there) and the new day (refused: 80% easy in this week)
        a, now = self.edit(pe.Override("move", key=e["key"], date=free), pe.Override("rest", date=_day(e["date"])),
                           pe.Override("rest", date=free))
        self.assertEqual([x["kind"] for x in a["overrides"]["applied"]], ["move"])
        self.assertEqual([r["rule"] for r in a["overrides"]["refused"]], ["no_run_that_day", "easy_share"])
        self.assertEqual(now[e["key"]]["date"], free.isoformat())
        # rest, then move the removed session
        base_week = next(x for x in self.base["weeks"] if x["kind"] == "normal" and x["phase"] == "base")
        e0 = self.sessions[self.key(base_week, "easy")]
        a, now = self.edit(pe.Override("rest", date=_day(e0["date"])),
                           pe.Override("move", key=e0["key"], date=_day(e0["date"]) + dt.timedelta(days=1)))
        self.assertEqual([r["rule"] for r in a["overrides"]["refused"]], ["unknown_session"])
        # a lower week cap, then a higher one: the higher one would raise the capped week
        bw = base_week
        start = _day(bw["start"])
        a, _ = self.edit(pe.Override("cap_week_km", week_start=start, km=bw["planned_km"] - 15),
                         pe.Override("cap_week_km", week_start=start, km=bw["planned_km"] - 5))
        self.assertEqual([r["rule"] for r in a["overrides"]["refused"]], ["raises_load"])
        # easy then cap_km on the eased session
        q = self.key(w, "threshold")
        a, now = self.edit(pe.Override("easy", key=q), pe.Override("cap_km", key=q, km=4.0))
        self.assertEqual(a["overrides"]["refused"], [])
        self.assertEqual(now[q]["km"], 4.0)
        # the same edit twice
        a, _ = self.edit(pe.Override("easy", key=q), pe.Override("easy", key=q))
        self.assertEqual([r["rule"] for r in a["overrides"]["refused"]], ["already_easy"])

    def test_rest_on_the_long_run_day_is_refused_when_it_breaks_a_rule(self):
        # a dropped long run lowers the longest run so far (the next long run then breaks the 2 km step and
        # the long-run cap) and the week's easy time (80% easy): the edit is undone and refused with that
        # invariant, or applied when every rule still holds
        for w in [x for x in self.base["weeks"] if x["kind"] == "normal"][:6]:
            lg = self.sessions[self.key(w, "long")]
            with self.subTest(week=w["start"]):
                a, now = self.edit(pe.Override("rest", date=_day(lg["date"])))
                if a["overrides"]["refused"]:
                    self.assertIn(a["overrides"]["refused"][0]["rule"], ("long_cap", "long_step", "easy_share"))
                    self.assertEqual(a["weeks"], self.base["weeks"])
                else:
                    self.assertNotIn(lg["key"], now)

    def test_no_override_puts_a_run_after_the_a_race(self):
        race = D(2027, 3, 17)   # a Wednesday: the race week has runs on Tuesday only
        self.inputs = dataclasses.replace(self.inputs, race_date=race)
        self.base = pe.plan_answer(build(self.inputs, self.snap))
        last = self.base["weeks"][-1]
        e = next(s for s in last["sessions"] if s["role"] not in ("race",))
        for o in (pe.Override("move", key=e["key"], date=race + dt.timedelta(days=1)),
                  pe.Override("swap", date=_day(e["date"]), date_b=race + dt.timedelta(days=3))):
            with self.subTest(o=o.echo()):
                plan = pe.plan_answer(build(dataclasses.replace(self.inputs, overrides=(o,)), self.snap))
                after = [s for w in plan["weeks"] for s in w["sessions"] if s["date"] > race.isoformat()]
                self.assertEqual(after, [])


# --- the planned ACWR (rules v2 item 12) ---------------------------------------------------------------------------

class AcwrModel(Oracle):
    def plan(self, loads: Optional[Dict[str, Any]], garmin: Optional[Tuple[pe.GarminLoad, ...]] = None,
             fixed: Tuple[pe.FixedSession, ...] = (), b_races: Tuple[pe.BRace, ...] = (),
             prof: str = "owner40") -> Tuple[pe.PlanInputs, pe.Snapshot, Dict[str, Any]]:
        snap = snapshot(TODAY, PROFILES[prof], loads=loads, fixed=fixed)
        if garmin is not None:
            snap = dataclasses.replace(snap, garmin_load=garmin)
        inputs = pe.PlanInputs(race_date=RACE, goal_time_s=12600, b_races=b_races)
        return inputs, snap, self.assertSound(inputs, snap)

    def model_ratios(self) -> List[Tuple[dt.date, float, float, float]]:
        """(day, model ratio, garmin chronic) on the three check days, from the tracked sample loads."""
        snap = snapshot(TODAY, PROFILES["owner40"], loads={"garmin_gap": 0.0})
        actual: Dict[dt.date, float] = {}
        for act in snap.activity_load:
            actual[act.date] = actual.get(act.date, 0.0) + act.load
        out = []
        for g in snap.garmin_load:
            out.append((g.date, pe.acwr_on(g.date, actual) or 0.0, g.chronic, g.acute))
        return out

    def garmin_with_gaps(self, gaps: Tuple[float, float, float]) -> Tuple[pe.GarminLoad, ...]:
        out = []
        for (day, model, chronic, _acute), gap in zip(self.model_ratios(), gaps):
            out.append(pe.GarminLoad(day, (model + gap) * chronic, chronic))
        return tuple(out)

    def test_enforced_tracks_and_not_enforced_reports(self):
        _i, _s, plan = self.plan({"garmin_gap": 0.0})
        self.assertTrue(plan["acwr"]["enforced"])
        self.assertIsNone(plan["acwr"]["reason"])
        _i, _s, plan = self.plan({"garmin_gap": 0.3})
        self.assertFalse(plan["acwr"]["enforced"])
        self.assertIn("within 0.15", plan["acwr"]["reason"])
        self.assertIn("acwr_not_enforced", [w["id"] for w in plan["warnings"]])
        self.assertEqual(plan["acwr"]["cuts"], [])

    def test_two_of_three_rule_at_the_0_15_boundary(self):
        cases = {(0.0, 0.0, 0.0): True, (0.14, 0.14, 0.14): True, (0.149, -0.149, 0.5): True,
                 (0.0, 0.5, 0.0): True, (0.5, 0.0, 0.0): True, (0.0, 0.0, 0.5): True,
                 (0.5, 0.5, 0.0): False, (0.0, 0.5, -0.5): False, (0.16, 0.16, 0.16): False,
                 (0.16, -0.16, 0.0): False, (0.3, 0.3, 0.3): False}
        for gaps, enforced in cases.items():
            with self.subTest(gaps=gaps):
                _i, _s, plan = self.plan({}, garmin=self.garmin_with_gaps(gaps))
                self.assertEqual(plan["acwr"]["enforced"], enforced, plan["acwr"]["checks"])
                self.assertEqual(len(plan["acwr"]["checks"]), 3)

    def test_a_gap_just_over_0_15_is_not_within_0_15(self):
        _i, _s, plan = self.plan({"garmin_gap": 0.152})
        self.assertFalse(plan["acwr"]["enforced"], plan["acwr"]["checks"])

    def test_too_few_checks_no_load_no_run(self):
        garmin = self.garmin_with_gaps((0.0, 0.0, 0.0))
        _i, _s, plan = self.plan({}, garmin=garmin[:1])
        self.assertFalse(plan["acwr"]["enforced"])
        self.assertIn("read on 1 of the check days", plan["acwr"]["reason"])
        _i, _s, plan = self.plan({}, garmin=())
        self.assertFalse(plan["acwr"]["enforced"])
        self.assertIn("garmin_load", plan["missing"])
        # a Garmin day with no chronic load is not a check
        zero = tuple(dataclasses.replace(g, chronic=0.0) for g in garmin[:2]) + garmin[2:]
        _i, _s, plan = self.plan({}, garmin=zero)
        self.assertEqual(len(plan["acwr"]["checks"]), 1)
        _i, _s, plan = self.plan(None)
        self.assertFalse(plan["acwr"]["enforced"])
        self.assertIn("training_load", plan["missing"])
        snap = snapshot(TODAY, PROFILES["owner40"], loads={})
        rides = tuple(a for a in snap.activity_load if a.sport != "run")
        snap = dataclasses.replace(snap, activity_load=rides)
        inputs = pe.PlanInputs(race_date=RACE)
        plan = self.assertSound(inputs, snap)
        self.assertFalse(plan["acwr"]["enforced"])
        self.assertIn("no recorded run", plan["acwr"]["reason"])

    def test_b_race_load(self):
        b = (pe.BRace(D(2027, 1, 9), "tri_full"), pe.BRace(D(2027, 2, 6), "run"))
        _i, snap, plan = self.plan({}, b_races=b)
        lpm = plan["acwr"]["load_per_min"]
        runs = [a for a in snap.activity_load if a.sport == "run"]
        run_mps = sum(a.meters for a in runs) / sum(a.seconds for a in runs)
        got = {x["kind"]: x for x in plan["acwr"]["b_races"]}
        self.assertEqual(got["tri_full"]["minutes"], 750)
        self.assertEqual(got["run"]["minutes"], round(21097.5 / run_mps / 60))
        for x in got.values():
            self.assertAlmostEqual(x["load"], x["minutes"] * lpm["all"], delta=x["minutes"] * 0.001 + 1)
        _i, _s, plan = self.plan(None, b_races=(pe.BRace(D(2027, 1, 9), "run"), pe.BRace(D(2027, 2, 6), "tri_half")))
        got = {x["kind"]: x for x in plan["acwr"]["b_races"]}
        self.assertEqual((got["run"]["minutes"], got["run"]["load"]), (120, None))
        self.assertEqual(got["tri_half"]["minutes"], 330)

    def test_heavy_fixed_rides_cut_easy_runs_only_down_to_the_floor(self):
        first = pe.week1_start(TODAY)
        rides = tuple(pe.FixedSession(first + dt.timedelta(days=7 * k + d), "bike", False, True, 300.0)
                      for k in (1, 2, 5) for d in (0, 2, 4))
        inputs, snap, held = self.plan({}, fixed=rides)
        _i, _s2, free = self.plan({}, fixed=rides, garmin=())
        self.assertTrue(held["acwr"]["enforced"])
        self.assertTrue(held["acwr"]["cuts"])
        weeks, ctx = held["_check"]["weeks"], held["_check"]["ctx"]
        cut = {c["week"] for c in held["acwr"]["cuts"]}
        a, b = pe.plan_answer(held), pe.plan_answer(free)
        for w, wa, wb in zip(weeks, a["weeks"], b["weeks"]):
            self.assertEqual(wa["target_km"], wb["target_km"])
            self.assertLessEqual(wa["planned_km"], wb["planned_km"] + 0.05)
            if w.number in cut:
                self.assertGreaterEqual(pe._planned(w), 0.6 * float(w.target) - pe.EPS_M)
            hard_a = sorted((s["date"], s["role"], s["km"]) for s in wa["sessions"] if s["role"] == "long")
            hard_b = sorted((s["date"], s["role"], s["km"]) for s in wb["sessions"] if s["role"] == "long")
            self.assertEqual(hard_a, hard_b, wa["start"])   # the long run stays
        # on every day the cap acts on: at or under 1.3, unless the week is at its floor or held
        loads = pe.day_loads(weeks, ctx)
        for w in weeks:
            if w.acwr_held or pe._planned(w) <= pe._acwr_floor(w, ctx) + pe.EPS_M:
                continue
            for d in pe._acwr_days(w, ctx):
                self.assertLessEqual(pe.acwr_on(d, loads) or 0.0, 1.3 + 1e-9, d)
        ids = [x["id"] for x in held["warnings"]]
        self.assertIn("acwr_cut", ids)

    def test_the_cap_acts_only_from_the_first_easy_run(self):
        first = pe.week1_start(TODAY)
        # a huge ride on week 3's Monday: its own day is before any easy run of the week, so it is not cut for
        ride = (pe.FixedSession(first + dt.timedelta(days=14), "bike", False, True, 600.0),)
        _i, _s, plan = self.plan({}, fixed=ride)
        weeks, ctx = plan["_check"]["weeks"], plan["_check"]["ctx"]
        w3 = next(w for w in weeks if w.start == first + dt.timedelta(days=14))
        days = pe._acwr_days(w3, ctx)
        easy = [s.date for s in w3.sessions if s.role in ("easy", "strides")]
        if easy:
            self.assertEqual(days[0], min(easy))
            self.assertNotIn(w3.start, days)
        b = (pe.BRace(D(2027, 1, 9), "tri_full"),)
        _i, _s, plan = self.plan({}, b_races=b)
        weeks, ctx = plan["_check"]["weeks"], plan["_check"]["ctx"]
        for w in weeks:
            self.assertNotIn(D(2027, 1, 9), pe._acwr_days(w, ctx))

    def test_heavy_load_across_profiles_holds_every_rule(self):
        first = pe.week1_start(TODAY)
        rides = tuple(pe.FixedSession(first + dt.timedelta(days=d), "bike", d % 3 == 0, d % 3 == 1, 200.0)
                      for d in range(0, 70, 2))
        for name in ("beginner15", "owner40", "runner60", "runner90", "very_slow", "no_lt"):
            for gap in (0.0, 0.1, 0.3):
                with self.subTest(profile=name, gap=gap):
                    _i, _s, plan = self.plan({"garmin_gap": gap, "bike_min": 300}, fixed=rides, prof=name)
                    if plan["acwr"]["enforced"]:
                        self.assertTrue(all(c["to_km"] < c["from_km"] for c in plan["acwr"]["cuts"]))


# --- feasibility, what_if, assess ---------------------------------------------------------------------------------

class Feasibility(unittest.TestCase):
    def feas(self, dist: str = "marathon", goal: Optional[int] = None, pred: Optional[int] = None,
             lt: Optional[float] = None, weeks: Optional[int] = 16, peak: Optional[float] = 60.0,
             long: Optional[float] = 30.0, km0: Optional[float] = 40.0) -> Dict[str, Any]:
        snap = pe.Snapshot(today=TODAY, lt_speed_mps=pe.MetersPerSecond(lt) if lt else None,
                           prediction_s=((dist, pred),) if pred else ())
        return pe.feasibility(pe.PlanInputs(race_date=RACE, race_distance=dist, goal_time_s=goal), snap, weeks,
                              peak, long, km0)

    def check(self, f: Dict[str, Any], cid: str) -> Dict[str, Any]:
        return next(c for c in f["checks"] if c["id"] == cid)

    def test_prediction_boundaries_3_and_7_percent(self):
        pred = 14000
        for pct, verdict in ((0.0, "feasible"), (2.9, "feasible"), (3.0, "feasible"), (3.1, "ambitious"),
                             (6.9, "ambitious"), (7.0, "ambitious"), (7.1, "unrealistic"), (-5.0, "feasible")):
            goal = round(pred * (1 - pct / 100))
            with self.subTest(pct=pct):
                f = self.feas(goal=goal, pred=pred)
                self.assertEqual(f["verdict"], verdict, f["checks"])
                self.assertEqual(self.check(f, "goal_vs_prediction")["value"], pct)

    def test_goal_vs_threshold_0_95(self):
        lt = 4.0
        for ratio, ok in ((0.90, True), (0.949, True), (0.95, True), (0.951, False), (1.0, False)):
            goal = int(round(42195 / (ratio * lt)))
            with self.subTest(ratio=ratio):
                f = self.feas(goal=goal, lt=lt)
                c = self.check(f, "goal_vs_threshold")
                self.assertEqual(c["ok"], ok, c)
                self.assertEqual(f["verdict"], "feasible" if ok else "unrealistic")
        h = self.feas(dist="half", goal=int(round(21097.5 / (1.02 * lt))), lt=lt)
        self.assertTrue(self.check(h, "goal_vs_threshold")["ok"])
        h = self.feas(dist="half", goal=int(round(21097.5 / (1.03 * lt))), lt=lt)
        self.assertFalse(self.check(h, "goal_vs_threshold")["ok"])

    def test_corrected_goal(self):
        f = self.feas(goal=12600, pred=13000, lt=3.85)
        by_lt = 42195 / (0.93 * 3.85)
        self.assertEqual(f["corrected_goal_s"], int(math.ceil(max(13000, by_lt) / 60) * 60))
        self.assertEqual(f["corrected_goal"], pe.hms(f["corrected_goal_s"]))
        self.assertEqual(self.feas(pred=12001)["corrected_goal_s"], 12060)
        self.assertEqual(self.feas(pred=12000)["corrected_goal_s"], 12000)
        self.assertEqual(self.feas(dist="half", lt=4.0)["corrected_goal_s"],
                         int(math.ceil(21097.5 / (0.97 * 4.0) / 60) * 60))
        self.assertIsNone(self.feas()["corrected_goal_s"])
        f = self.feas(goal=12600)
        self.assertEqual(sorted(f["missing"]), ["goal_vs_prediction", "goal_vs_threshold"])

    def test_volume_checks_warn(self):
        f = self.feas(km0=24.9, peak=49.9, long=27.9)
        self.assertEqual(f["verdict"], "ambitious")
        self.assertEqual({c["id"] for c in f["checks"] if not c["ok"]}, {"base_km", "peak_km", "long_run_km"})
        f = self.feas(km0=25.0, peak=50.0, long=28.0)
        self.assertEqual(f["verdict"], "feasible")
        h = self.feas(dist="half", km0=15.0, peak=30.0, long=16.0, weeks=6)
        self.assertEqual(h["verdict"], "feasible")
        h = self.feas(dist="half", km0=14.9, peak=30.0, long=16.0, weeks=5)
        self.assertEqual(h["verdict"], "unrealistic")
        f = self.feas(weeks=None, peak=None, long=None, km0=None)
        self.assertEqual((f["verdict"], f["checks"]), ("feasible", []))

    def test_the_plan_never_grows_to_meet_a_goal(self):
        snap = snapshot(TODAY, PROFILES["owner40"])
        slow = pe.plan_answer(build(pe.PlanInputs(race_date=RACE, goal_time_s=5 * 3600), snap))
        fast = pe.plan_answer(build(pe.PlanInputs(race_date=RACE, goal_time_s=2 * 3600 + 30 * 60), snap))
        self.assertEqual([w["target_km"] for w in slow["weeks"]], [w["target_km"] for w in fast["weeks"]])
        self.assertEqual(fast["feasibility"]["verdict"], "unrealistic")
        self.assertIn("mp_clamped", [w["id"] for w in fast["warnings"]])


class WhatIfAndAssess(Oracle):
    def test_what_if_variants_and_their_invariants(self):
        for name in ("beginner15", "owner40", "runner90", "no_lt", "very_slow"):
            snap = snapshot(TODAY, PROFILES[name], loads={})
            for goal, expect in ((12600, ["goal_minus_5min", "goal_plus_5min", "long_share_0.35", "long_share_0.45"]),
                                 (None, ["long_share_0.35", "long_share_0.45"]),
                                 (3700, ["goal_plus_5min", "long_share_0.35", "long_share_0.45"]),
                                 (8 * 3600 - 100, ["goal_minus_5min", "long_share_0.35", "long_share_0.45"])):
                with self.subTest(profile=name, goal=goal):
                    plan = build(pe.PlanInputs(race_date=RACE, goal_time_s=goal), snap, what_if=True)
                    self.assertEqual([v["variant"] for v in plan["what_if"]], expect)
                    for v in plan["what_if"]:
                        self.assertTrue(v["invariants_ok"], v)
                        self.assertEqual(v["delta_peak_km"],
                                         round((v["peak_km"] or 0) - (plan["summary"]["peak_km"] or 0), 1))
                    shares = {v["variant"]: v for v in plan["what_if"]}
                    self.assertLessEqual(shares["long_share_0.35"]["peak_long_km"] or 0,
                                         shares["long_share_0.45"]["peak_long_km"] or 0)

    def test_assess(self):
        snap = snapshot(TODAY, PROFILES["owner40"])
        out = pe.assess(pe.PlanInputs(race_date=RACE, goal_time_s=12600), snap)
        self.assertEqual(out["weeks_to_race"], 16)
        self.assertEqual(out["avg_km_4w"], 40.0)
        self.assertEqual(len(out["history"]), 8)
        self.assertIsNotNone(out["projected_peak_km"])
        early = pe.assess(pe.PlanInputs(race_date=D(2026, 11, 28)), snap)
        self.assertIsNone(early["weeks_to_race"])
        self.assertTrue(any(m.startswith("plan: race_date") for m in early["missing"]))
        empty = pe.assess(pe.PlanInputs(race_date=RACE), dataclasses.replace(snap, week_m=()))
        self.assertIsNone(empty["avg_km_4w"])
        self.assertEqual(empty["weeks_to_race"], 16)
        with self.assertRaises(pe.PlanError):
            pe.assess(pe.PlanInputs(race_date=RACE, max_long_km=50), snap)


if __name__ == "__main__":
    unittest.main()
