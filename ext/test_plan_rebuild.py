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
from unittest import mock

import plan_engine as pe
import plan_review as pr
import plan_store as ps
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


def moved_on(snap, body, today):
    """The snapshot's 8 weeks once the athlete ran the stored weeks that ended before this week as planned."""
    ran = [pe.Meters(w["planned_km"] * 1000.0) for w in body["weeks"] if D.fromisoformat(w["end"]) < pe.monday(today)]
    return tuple((list(snap.week_m) + ran)[-8:])


def changed_weeks(a, b):
    """(start, target km before, after) of every week that differs in any way."""
    A, B = {w["start"]: w for w in a["weeks"]}, {w["start"]: w for w in b["weeks"]}
    return [(k, (A.get(k) or {}).get("target_km"), (B.get(k) or {}).get("target_km"))
            for k in sorted(set(A) | set(B)) if A.get(k) != B.get(k)]


def review_cap(body, week_start, km, today):
    patched, _accepted, _refused, applied = pr.apply_changes(body, [{"change_id": "c1", "kind": "repeat_volume", "edits": [
        {"op": "cap_week", "week_start": week_start, "km": km}]}], today, None)
    assert applied == ["c1"], applied
    return patched


class ReviewCut(RebuildCase):
    """Repro F3: plan_review_week's accepted cap of G4's week of 28 Dec (65.1 -> 42.6 km on Sunday 27 Dec) was
    stored with the old plan_basis, so a rebuild with the same inputs on 5 Jan grew week 2027-01-04 from the
    pre-cut base (70.5 km; rule 2 allows 1.08 x 42.6 = 46.0), and one in the capped week put the cut easy runs
    back (42.6 -> 57.8 km)."""

    CAPPED = "2026-12-28"

    def setUp(self):
        self.inputs, self.snap = g("G4")
        self.body = stored(pe.build_plan(self.inputs, self.snap, what_if_runs=False))
        self.cap_km = round(week(self.body, self.CAPPED)["planned_km"] * 0.7, 1)
        self.patched = review_cap(self.body, self.CAPPED, self.cap_km, D(2026, 12, 27))
        self.cut = week(self.patched, self.CAPPED)

    def test_the_patched_basis_counts_the_week_at_its_cut_size(self):
        entry = self.patched["basis"]["weeks"][self.CAPPED]
        self.assertLessEqual(entry["base_m"], self.cut["planned_km"] * 1000.0 + 1)
        self.assertEqual((entry["cap_m"], entry["cap_from"]), (self.cap_km * 1000.0, "2026-12-27"))
        self.assertNotIn("cap_m", self.body["basis"]["weeks"][self.CAPPED])   # the reviewed version is unchanged

    def test_the_week_after_the_cut_grows_from_it(self):
        today = D(2027, 1, 5)
        snap = dataclasses.replace(self.snap, today=today, week_m=moved_on(self.snap, self.patched, today))
        plan = pt.build_rebuilt(self.inputs, snap, self.patched)
        answer = self.assert_merged_ok(plan)
        self.assertEqual(week(answer, self.CAPPED), self.cut)
        self.assertLessEqual(week(answer, "2027-01-04")["planned_km"], round(self.cut["planned_km"] * 1.08, 1) + 0.05)
        # the next rebuild goes on from the same chain (the basis this one stores keeps the cut base)
        again = pt.build_rebuilt(self.inputs, dataclasses.replace(snap, today=today + dt.timedelta(days=1)), stored(plan))
        self.assertEqual(changed_weeks(answer, pe.plan_answer(again)), [])

    def test_a_rebuild_before_or_in_the_capped_week_keeps_the_cut(self):
        for day in (D(2026, 12, 27), D(2026, 12, 28), D(2026, 12, 30), D(2027, 1, 2)):
            with self.subTest(today=day):
                plan = pt.build_rebuilt(self.inputs, dataclasses.replace(self.snap, today=day), self.patched)
                answer = self.assert_merged_ok(plan)
                self.assertEqual(week(answer, self.CAPPED)["sessions"], self.cut["sessions"])
                # the weeks before it as stored; the ones after grow from it (rule 2), not from the km before the cut
                self.assertEqual([w for w in answer["weeks"] if w["start"] <= self.CAPPED],
                                 [w for w in self.patched["weeks"] if w["start"] <= self.CAPPED])
                self.assertLessEqual(week(answer, "2027-01-04")["planned_km"],
                                     round(self.cut["planned_km"] * 1.08, 1) + 0.05)
                self.assertEqual(pe.plan_basis(plan)["weeks"][self.CAPPED]["cap_m"],
                                 self.patched["basis"]["weeks"][self.CAPPED]["cap_m"])

    def test_a_plan_stored_before_the_basis_counts_the_cut_too(self):
        old = {k: v for k, v in self.body.items() if k != "basis"}
        patched = review_cap(old, self.CAPPED, self.cut["planned_km"], D(2026, 12, 27))
        today = D(2027, 1, 5)
        plan = pt.build_rebuilt(self.inputs, dataclasses.replace(self.snap, today=today,
                                                                 week_m=moved_on(self.snap, patched, today)), patched)
        answer = self.assert_merged_ok(plan)
        self.assertLessEqual(week(answer, "2027-01-04")["planned_km"], round(self.cut["planned_km"] * 1.08, 1) + 0.05)


class DistanceChange(RebuildCase):
    """Repro F2: a stored marathon plan rebuilt as a half marathon kept the marathon's peak ceiling (1.3 x the
    biggest week, km0 + 25 km) from its basis: G4's half rebuild peaked at 73.0 km, the half's ceiling from the
    same km0 and biggest week is 66.0 km."""

    def test_the_new_distance_gets_its_own_ceiling(self):
        for name in ("G1", "G4", "G5"):
            with self.subTest(name):
                inputs, snap = g(name)
                body = stored(pe.build_plan(inputs, snap, what_if_runs=False))
                today = snap.today + dt.timedelta(days=13)
                b = body["basis"]
                half = dataclasses.replace(inputs, race_distance="half", goal_time_s=None,
                                           b_races=tuple(x for x in inputs.b_races if x.date > today))
                plan = pt.build_rebuilt(half, dataclasses.replace(snap, today=today), body)
                self.assert_merged_ok(plan)
                ceiling = max(1.2 * b["biggest_m"], b["km0_m"] + 18000.0)
                self.assertAlmostEqual(pe.plan_ceiling(plan["_check"]["ctx"])[0], ceiling)
                self.assertLessEqual(plan["summary"]["peak_km"], pe.km(ceiling))
                # back to the marathon: the plan's own marathon ceiling again
                back = pt.build_rebuilt(dataclasses.replace(half, race_distance="marathon"),
                                        dataclasses.replace(snap, today=today + dt.timedelta(days=1)), stored(plan))
                self.assertAlmostEqual(pe.plan_ceiling(back["_check"]["ctx"])[0], b["ceiling_m"])


class OverrideOnRebuild(RebuildCase):
    """Repro F1: apply_overrides refused an edit only when a rule id that passed before failed after it; on a
    rebuild the kept days already broke easy_share (week 11: 78% easy), so a cap_week_km that left week 14 with
    no easy run was applied and the version had invariants.ok false. (Since eng-rules2 a reduce-only edit may
    relax easy_share, long_cap and long_step: that cap is now applied with the warning override_relaxed.)"""

    def test_an_applied_edit_never_leaves_the_rebuilt_plan_failing(self):
        inputs, snap0 = g("G4")
        snap = dataclasses.replace(snap0, today=snap0.today + dt.timedelta(days=3))
        body = stored(pe.build_plan(inputs, snap, what_if_runs=False))
        new = dataclasses.replace(inputs, run_days=(0, 1, 2, 4, 6), rest_days=(3, 5), long_run_day=4,
                                  quality_per_week=1, blocked_dates=(D(2027, 2, 15), D(2027, 2, 16), D(2027, 2, 21)),
                                  overrides=(pe.Override("cap_week_km", week_start=D(2027, 3, 1), km=38.0),))
        plan = pt.build_rebuilt(new, dataclasses.replace(snap, today=D(2027, 2, 13)), body)
        self.assertIn("easy_share", [h["id"] for h in plan["invariants"]["history"]])
        self.assertTrue(plan["invariants"]["ok"], plan["invariants"]["failed"])
        if plan["overrides"]["applied"]:
            self.assertIn("override_relaxed", [w["id"] for w in plan["warnings"]])

    def test_a_rule_another_week_breaks_never_hides_a_new_break_of_it(self):
        # apply_overrides itself: week A already has no easy run; an edit (not reduce-only, so nothing counts as
        # built) that takes week B's easy runs away adds "week B: no easy run" and is refused and undone
        inputs, snap = g("G1")
        first = pe.build_plan(inputs, snap, what_if_runs=False)
        normal = [w.start for w in first["_check"]["weeks"] if w.kind == "normal"
                  and any(s.role in ("easy", "strides") for s in w.sessions) and any(s.role == "long" for s in w.sessions)]
        swap = pe.Override("swap", date=normal[0], date_b=normal[0] + dt.timedelta(days=1))
        plan = pe.build_plan(dataclasses.replace(inputs, overrides=(swap,)), snap, what_if_runs=False)
        weeks, ctx = plan["_check"]["weeks"], plan["_check"]["ctx"]
        by_start = {w.start: w for w in weeks}
        a, b = by_start[normal[0]], by_start[normal[-1]]

        def no_easy(w):
            w.sessions = [s for s in w.sessions if s.role not in ("easy", "strides")]

        no_easy(a)
        before = [i["detail"] for i in pe.check_invariants(plan) if i["id"] == "easy_share" and not i["ok"]]
        self.assertTrue(before and f"week {a.number}: no easy run" in before[0], before)

        def edit(_weeks, _ctx, o):
            no_easy(b)
            return [{"edit": o.echo()}], []

        with mock.patch.object(pe, "_apply_one", edit):
            applied, refused = pe.apply_overrides(weeks, ctx)
        self.assertEqual(applied, [])
        self.assertEqual([r["rule"] for r in refused], ["easy_share"])
        self.assertIn(f"week {b.number}: no easy run", refused[0]["value"])
        self.assertTrue(any(s.role in ("easy", "strides") for s in b.sessions), "the refused edit is undone")


class PlanStoredBeforeTheBasis(RebuildCase):
    """Repro F4: a plan stored before plan_basis rebuilt with the same inputs changed days (km0 from the rounded
    start block: G5 on Monday 30 Nov, every target +0.1 km; a past week's base recomputed without its rule-7
    cut: G1 on 17 Jan, week 2027-01-18 65.0 -> 59.0 km), and the store kept the old body while the content hash
    matched, so the basis was never stored."""

    def old_body(self, name):
        inputs, snap = g(name)
        body = {k: v for k, v in stored(pe.build_plan(inputs, snap, what_if_runs=False)).items() if k != "basis"}
        return inputs, snap, body

    def test_a_same_input_rebuild_plans_no_day_differently(self):
        for name, today in (("G5", D(2026, 11, 30)), ("G1", D(2027, 1, 17)), ("G5", D(2026, 12, 28)),
                            ("G4", D(2027, 1, 9)), ("G2", D(2026, 12, 20))):
            with self.subTest(name, today=today):
                inputs, snap, body = self.old_body(name)
                b_races = tuple(x for x in inputs.b_races if x.date > today)
                plan = pt.build_rebuilt(dataclasses.replace(inputs, b_races=b_races),
                                        dataclasses.replace(snap, today=today, week_m=moved_on(snap, body, today)), body)
                self.assertEqual(changed_weeks(body, pe.plan_answer(plan)), [])

    def test_the_first_rebuild_stores_the_basis(self):
        inputs, snap, body = self.old_body("G1")
        store = ps.MemoryPlanStore()
        fp = store.account_fp("12345")
        pid, _v, _c = store.save_preview(fp, None, inputs.race_date, pe.RULES_VERSION, inputs.echo(), body,
                                         body["content_hash"])
        plan = pt.build_rebuilt(inputs, dataclasses.replace(snap, today=snap.today + dt.timedelta(days=1)), body)
        answer = dict(pe.plan_answer(plan), basis=pe.plan_basis(plan))
        self.assertEqual(store.save_preview(fp, pid, inputs.race_date, pe.RULES_VERSION, inputs.echo(), answer,
                                            answer["content_hash"]), (pid, 1, False))
        self.assertEqual(store.get_plan(fp, pid)["version_row"]["body"]["basis"], answer["basis"])


if __name__ == "__main__":
    unittest.main()
