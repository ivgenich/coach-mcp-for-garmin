"""A stored plan rebuilt on a later day (plan_preview with a plan_id: plan_tools.build_rebuilt, plan_engine
build_plan(born=..., history=...), rule 1). Standard library only.

Each scenario was a reviewer's repro against the old merge (plan_tools.carry_history pasted the stored days up to
today over a plan built without them): the merged week was never checked (two hard days in a row, two marathon-
pace sessions, more km than the target, invariants "ok"); after a B race the rebuild lost rule 7's reference (the
first long run after a half-distance triathlon at 0.75 x the 8 weeks' longest run instead of the last long run
before the race); volume restarted from this snapshot's km0 and the peak ceiling counted the plan's own weeks;
after a swap a stored session's key belonged to another day and the engine added a second session with it.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import unittest

import plan_engine as pe
import plan_tools as pt
from test_plan_engine import GOLDEN

D = dt.date


def stored(plan):
    """A version body as plan_tools stores it: the answer and its plan_basis."""
    return dict(pe.plan_answer(plan), basis=pe.plan_basis(plan))


def g(name, **snap):
    case = GOLDEN[name]
    return case["inputs"], dataclasses.replace(case["snapshot"], activity_load=(), garmin_load=(), **snap)


def week(answer, start):
    return next(w for w in answer["weeks"] if w["start"] == start)


def runs(answer):
    return [s for w in answer["weeks"] for s in w["sessions"] if s["role"] not in ("race", "b_race")]


class RebuildCase(unittest.TestCase):
    def assert_merged_ok(self, plan):
        self.assertTrue(plan["invariants"]["ok"], plan["invariants"]["failed"])
        answer = pe.plan_answer(plan)
        keys = [s["key"] for w in answer["weeks"] for s in w["sessions"]]
        self.assertEqual(len(keys), len(set(keys)), "two sessions with one key")
        days = [s["date"] for s in runs(answer)]
        self.assertEqual(len(days), len(set(days)), "two runs on one day")
        return answer


class MergedWeek(RebuildCase):
    """Repro rebuild2: G1 rebuilt on Thursday 30 Dec with Tuesday to Sunday as run days and the long run on
    Friday; the stored week had threshold Tue, easy Wed, MP Thu (today) and the long run on Sunday."""

    def setUp(self):
        inputs, snap = g("G1")
        self.first = pe.build_plan(inputs, snap, what_if_runs=False)
        self.body = stored(self.first)
        self.today = D(2027, 12, 30)
        self.snap = dataclasses.replace(snap, today=self.today)
        self.new = dataclasses.replace(inputs, run_days=(1, 2, 3, 4, 5, 6), rest_days=(0,), long_run_day=4)

    def test_the_merged_week_keeps_every_rule(self):
        plan = pt.build_rebuilt(self.new, self.snap, self.body)
        answer = self.assert_merged_ok(plan)
        w = week(answer, "2027-12-27")
        self.assertEqual(w["sessions"][:3], week(self.body, "2027-12-27")["sessions"][:3], "the stored days stay")
        roles = [s["role"] for s in w["sessions"]]
        self.assertEqual(roles.count("mp"), 1, roles)
        self.assertEqual(roles.count("long"), 1, roles)
        hard = sorted(D.fromisoformat(s["date"]) for s in w["sessions"] if s["hard_or_long"])
        self.assertFalse([a for a, b in zip(hard, hard[1:]) if (b - a).days == 1], hard)
        self.assertLessEqual(w["planned_km"], w["target_km"] + 0.05)
        self.assertNotIn("history", plan["invariants"])

    def test_a_new_day_that_breaks_a_rule_against_the_kept_ones_fails(self):
        plan = pt.build_rebuilt(self.new, self.snap, self.body)
        weeks, ctx = plan["_check"]["weeks"], plan["_check"]["ctx"]
        w = next(x for x in weeks if x.start == D(2027, 12, 27))
        new = next(s for s in w.sessions if s.date > self.today and s.role == "long")
        new.date = self.today + dt.timedelta(days=1)   # the long run the day after today's MP session
        self.assertEqual(ctx.snapshot.today, self.today)
        inv, kept = pe.merged_invariants(plan)
        failed = {i["id"] for i in inv if not i["ok"]}
        self.assertIn("no_adjacent_hard", failed)
        self.assertEqual(kept, [])

    def test_a_rule_only_the_kept_days_break_is_a_warning(self):
        # Tuesday is a rest day now: the threshold run stored on Tuesday 28 Dec is history
        new = dataclasses.replace(self.new, run_days=(2, 3, 4, 5, 6), rest_days=(0, 1))
        plan = pt.build_rebuilt(new, self.snap, self.body)
        self.assert_merged_ok(plan)
        self.assertEqual([k["id"] for k in plan["invariants"]["history"]], ["rest_days"])
        self.assertIn("2027-12-28", plan["invariants"]["history"][0]["detail"])
        self.assertIn("history_rule", [w["id"] for w in plan["warnings"]])

    def test_an_edit_of_a_kept_day_is_refused(self):
        new = dataclasses.replace(self.new, overrides=(pe.Override("easy", key="20271228-run"),
                                                       pe.Override("rest", date=self.today)))
        plan = pt.build_rebuilt(new, self.snap, self.body)
        self.assertEqual([r["rule"] for r in plan["overrides"]["refused"]], ["past_day", "past_day"])
        self.assertEqual(week(pe.plan_answer(plan), "2027-12-27")["sessions"][:3],
                         week(self.body, "2027-12-27")["sessions"][:3])


class PastDaysAreNoRunDays(RebuildCase):
    def test_rule_4_never_asks_for_an_easy_run_on_a_day_up_to_today(self):
        # a property example: built on a Monday (week 1 from Tuesday), rebuilt on Tuesday with Tuesday as the
        # long-run day; Wednesday is the only day left, so the week has no easy run, and that keeps rule 4
        inputs = pe.PlanInputs(race_date=D(2027, 12, 6), run_days=(0, 2, 3), rest_days=(1, 4, 5, 6), long_run_day=0,
                               quality_per_week=0, max_long_km=24.0)
        snap = pe.Snapshot(today=D(2027, 11, 29), week_m=(8000.0,) * 8, week_runs=(4,) * 8,
                           longest_run_8w_m=pe.Meters(5000.0), prediction_s=(("marathon", 13200), ("half", 6300)))
        body = stored(pe.build_plan(inputs, snap, what_if_runs=False))
        new = dataclasses.replace(inputs, run_days=(0, 1, 2), rest_days=(3, 4, 5, 6), long_run_day=1)
        plan = pt.build_rebuilt(new, dataclasses.replace(snap, today=D(2027, 11, 30)), body)
        self.assert_merged_ok(plan)


class SameInputs(RebuildCase):
    def test_a_rebuild_on_any_later_day_gives_the_stored_plan(self):
        for name in ("G1", "G4", "G5"):
            inputs, snap = g(name)
            first = pe.build_plan(inputs, snap, what_if_runs=False)
            body = stored(first)
            for offset in (1, 2, 5, 6, 7, 13, 20, 33, 47):
                today = snap.today + dt.timedelta(days=offset)
                if today >= inputs.race_date:
                    continue
                with self.subTest(plan=name, today=today):
                    again = pt.build_rebuilt(inputs, dataclasses.replace(snap, today=today), body)
                    answer = self.assert_merged_ok(again)
                    self.assertEqual(answer["weeks"], body["weeks"])
                    self.assertEqual(answer["content_hash"], body["content_hash"])


class BRaceReference(RebuildCase):
    """Repro rebuild_b: G1 rebuilt the day after its half-distance triathlon (28 Jan 2028); the long run before
    it was 18.7 km, so the first one after it is at most 0.75 x 18.7 = 14.0 km."""

    def test_the_first_long_run_after_the_race_keeps_its_reference(self):
        inputs, snap = g("G1")
        body = stored(pe.build_plan(inputs, snap, what_if_runs=False))
        before = [s for s in runs(body) if s["role"] == "long" and s["date"] < "2028-01-28"][-1]
        self.assertEqual(before["km"], 18.7)
        # plan_preview takes only B races after today: the stored plan's B race still counts
        for b_races in (inputs.b_races, ()):
            with self.subTest(b_races=b_races):
                plan = pt.build_rebuilt(dataclasses.replace(inputs, b_races=b_races),
                                        dataclasses.replace(snap, today=D(2028, 1, 29)), body)
                answer = self.assert_merged_ok(plan)
                after = next(s for s in runs(answer) if s["role"] == "long" and s["date"] > "2028-01-28")
                self.assertLessEqual(after["km"], round(0.75 * 18.7, 1))
                self.assertEqual(after["km"], 14.0)
                self.assertIn("up to 14.0 km", after["note"])
                self.assertEqual([b["date"] for b in answer["b_races"]], ["2028-01-28"])
                self.assertEqual(answer["inputs"]["b_races"], [{"date": b.date.isoformat(), "kind": b.kind}
                                                               for b in b_races])


class Volume(RebuildCase):
    """Repro rebuild3: G4 rebuilt on Tuesday 28 Dec after the athlete ran weeks 0-4 as planned. The new
    snapshot's km0 and 8 weeks hold the plan's own weeks: the targets dropped (65.3 -> 58.0 km in week 5) and
    the peak rose past the original ceiling (73.0 -> 78.7 km)."""

    def setUp(self):
        self.inputs, self.snap = g("G4")
        self.first = pe.build_plan(self.inputs, self.snap, what_if_runs=False)
        self.body = stored(self.first)
        done = [w["planned_km"] for w in self.body["weeks"] if w["start"] < "2027-12-27"]
        done[0] = 47.0
        self.week_m = tuple(pe.Meters(k * 1000.0) for k in ([45, 48, 40, 50, 46, 52, 44, 50] + done)[-8:])
        longest = max(w["long_km"] or 0 for w in self.body["weeks"] if w["start"] < "2027-12-27")
        self.later = dataclasses.replace(self.snap, today=D(2027, 12, 28), week_m=self.week_m,
                                         longest_run_8w_m=pe.Meters(longest * 1000.0))

    def test_the_volume_goes_on_from_the_stored_weeks(self):
        for body in (self.body, {k: v for k, v in self.body.items() if k != "basis"}):   # a pre-basis plan too
            with self.subTest(basis="basis" in body):
                plan = pt.build_rebuilt(self.inputs, self.later, body)
                answer = self.assert_merged_ok(plan)
                self.assertEqual([(w["start"], w["target_km"], w["long_km"]) for w in answer["weeks"]],
                                 [(w["start"], w["target_km"], w["long_km"]) for w in self.body["weeks"]])
                self.assertEqual(answer["summary"], self.body["summary"])

    def test_the_peak_ceiling_never_counts_the_plans_own_weeks(self):
        # the athlete ran far more than planned: the ceiling stays the one the plan was built with
        big = tuple(pe.Meters(m * 1.4) for m in self.week_m)
        plan = pt.build_rebuilt(self.inputs, dataclasses.replace(self.later, week_m=big), self.body)
        self.assert_merged_ok(plan)
        ceiling = pe.plan_ceiling(self.first["_check"]["ctx"])[0]
        self.assertEqual(pe.plan_ceiling(plan["_check"]["ctx"])[0], ceiling)
        self.assertLessEqual(plan["summary"]["peak_km"], pe.km(ceiling))


class SwapThenRebuild(RebuildCase):
    """Repro test_swap_then_rebuild: the stored plan swapped Tuesday's run with Sunday's long run (each keeps the
    key of the day it was planned on); rebuilt on that Tuesday without the edit, the kept long run holds key
    20280123-run, and the engine planned a second long run with that key on Sunday."""

    def test_a_carried_key_wins_and_the_week_keeps_one_long_run(self):
        inputs, snap = g("G1")
        body = stored(pe.build_plan(inputs, snap, what_if_runs=False))
        monday = dataclasses.replace(snap, today=D(2028, 1, 17))
        tue, sun = D(2028, 1, 18), D(2028, 1, 23)
        swapped = pt.build_rebuilt(dataclasses.replace(inputs, overrides=(pe.Override("swap", date=tue, date_b=sun),)),
                                   monday, body)
        self.assertEqual(len(swapped["overrides"]["applied"]), 1, swapped["overrides"])
        body2 = stored(swapped)
        on_tue = next(s for s in runs(body2) if s["date"] == tue.isoformat())
        self.assertEqual((on_tue["key"], on_tue["role"]), ("20280123-run", "long"))
        plan = pt.build_rebuilt(inputs, dataclasses.replace(snap, today=tue), body2)
        answer = self.assert_merged_ok(plan)
        w = week(answer, "2028-01-17")
        self.assertEqual([s["role"] for s in w["sessions"]].count("long"), 1)
        sunday = next(s for s in w["sessions"] if s["date"] == sun.isoformat())
        self.assertEqual(sunday["key"], "20280118-run", "the stored version's key of that day")


class PlanTools(unittest.TestCase):
    def test_the_basis_is_stored_but_never_answered(self):
        inputs, snap = g("G1")
        plan = pe.build_plan(inputs, snap, what_if_runs=False)
        answer = dict(pe.plan_answer(plan), basis=pe.plan_basis(plan))
        self.assertNotIn("basis", pt._slim(answer))
        self.assertEqual(answer["basis"]["born"], snap.today.isoformat())

    def test_the_stored_born_day_wins(self):
        # built on a Sunday with no session on week 1's Monday (a blocked date): not a plan built that Monday
        inputs = pe.PlanInputs(race_date=D(2028, 3, 19), blocked_dates=(D(2027, 11, 29),), run_days=(0, 1, 2, 3, 5, 6),
                               rest_days=(4,))
        _i, snap = g("G1", today=D(2027, 11, 28))
        plan = pe.build_plan(inputs, snap, what_if_runs=False)
        body = stored(plan)
        self.assertFalse([s for s in runs(body) if s["date"] == "2027-11-29"])
        self.assertEqual(pt.plan_born(body), D(2027, 11, 28))
        self.assertEqual(pt.plan_born({k: v for k, v in body.items() if k != "basis"}), D(2027, 11, 29))
        again = pt.build_rebuilt(inputs, dataclasses.replace(snap, today=D(2027, 11, 30)), body)
        self.assertEqual(pe.plan_answer(again)["weeks"], body["weeks"])


if __name__ == "__main__":
    unittest.main()
