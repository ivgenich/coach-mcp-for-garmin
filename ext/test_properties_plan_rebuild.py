"""Property-based tests (Hypothesis) for a stored plan rebuilt on a later day (plan_engine.build_plan(born=...,
history=...), rule 1):

    python3 -m unittest discover -s ext -p "test_properties_plan_rebuild.py" -v

For any plan the engine builds, rebuilt with the same inputs on any later day before the race (the stored
version with its plan_basis, as plan_tools stores it; the history weeks moved on as the athlete ran the plan):
the merged plan passes every invariant with nothing put down to history, no newly planned day has more km than
the original plan had for it, every session key is unique, and rule 7's cap on the first long run after a
half-distance triathlon is the original's: 0.75 x the last long run before the race. DEEP_EXAMPLES raises the
examples. Skipped ("needs hypothesis") where hypothesis is not installed.
"""

import dataclasses
import datetime as dt
import os
import unittest

import plan_engine as pe
from test_properties_plan_engine import (HAVE_HYPOTHESIS, NEEDS_HYPOTHESIS, PROPS, _first_long_after_problems,
                                         gap_case, given, settings, st)

REBUILD_PROPS = dict(PROPS, max_examples=max(100, int(os.environ.get("DEEP_EXAMPLES") or 0)))


def stored(plan):
    """A version body as plan_tools stores it: the answer and its plan_basis."""
    return dict(pe.plan_answer(plan), basis=pe.plan_basis(plan))


def born_of(body):
    if body.get("basis", {}).get("born"):
        return dt.date.fromisoformat(body["basis"]["born"])
    """plan_tools.plan_born, standard library only: the Sunday before week 1, or its Monday without a week 0
    and with a session that Monday."""
    first = next(dt.date.fromisoformat(w["start"]) for w in body["weeks"] if w["week"] == 1)
    early = any(w["week"] == 0 for w in body["weeks"]) or any(
        s["date"] == first.isoformat() for w in body["weeks"] for s in w["sessions"])
    return first - dt.timedelta(days=1) if early else first


def run_days(body):
    return {s["date"]: s for w in body["weeks"] for s in w["sessions"] if s["role"] not in ("race", "b_race")}


@unittest.skipUnless(HAVE_HYPOTHESIS, NEEDS_HYPOTHESIS)
class RebuildProperties(unittest.TestCase):
    @settings(**REBUILD_PROPS)
    @given(gap_case(), st.integers(1, 140), st.booleans())
    def test_a_rebuild_with_the_same_inputs_keeps_every_rule_and_never_plans_more(self, case, later, moved_on):
        inputs, snap = case
        # no recorded load: the planned-ratio cap reads the recorded days up to today, which a later day changes
        snap = dataclasses.replace(snap, activity_load=(), garmin_load=())
        first = pe.build_plan(inputs, snap, what_if_runs=False)
        body = stored(first)
        today = snap.today + dt.timedelta(days=later)
        if today >= inputs.race_date or pe.week1_start(today) > pe.monday(inputs.race_date):
            today = inputs.race_date - dt.timedelta(days=1 + (later % 3))
        if today <= snap.today:
            return
        week_m = snap.week_m
        if moved_on:   # the athlete ran the plan: its weeks are in Garmin's history now (they never raise it)
            ran = [pe.Meters(w["planned_km"] * 1000.0) for w in body["weeks"]
                   if dt.date.fromisoformat(w["end"]) < pe.monday(today)]
            week_m = tuple((list(snap.week_m) + ran)[-8:])
        again = pe.build_plan(inputs, dataclasses.replace(snap, today=today, week_m=week_m), what_if_runs=False,
                              born=born_of(body), history=body)
        inv = again["invariants"]
        self.assertTrue(inv["ok"], inv["failed"])
        self.assertNotIn("history", inv)
        answer = pe.plan_answer(again)
        keys = [s["key"] for w in answer["weeks"] for s in w["sessions"]]
        self.assertEqual(len(keys), len(set(keys)), "two sessions with one key")
        old, new = run_days(body), run_days(answer)
        for day, s in new.items():
            if day > today.isoformat():
                self.assertLessEqual(s["km"], old[day]["km"] + 0.05 if day in old else 0.0, day)
            else:
                self.assertEqual(s, old[day], f"{day} is history: as stored")
        self.assertEqual(_first_long_after_problems(answer), [])
        # rule 7's cap on the first long run after a B race is the original's (0.75 x the last long run before a
        # half-distance triathlon), however far the history goes
        ctx0, ctx1 = first["_check"]["ctx"], again["_check"]["ctx"]
        for b in inputs.b_races:
            after = sorted(d for d, s in old.items() if s["role"] == "long" and d > b.date.isoformat())
            if not after or after[0] <= today.isoformat():
                continue
            day = dt.date.fromisoformat(after[0])
            self.assertEqual(pe.b_long_limit(ctx1, day, again["_check"]["weeks"])[0],
                             pe.b_long_limit(ctx0, day, first["_check"]["weeks"])[0], day)


    @settings(**REBUILD_PROPS)
    @given(gap_case(), st.integers(1, 140), st.data())
    def test_a_rebuild_with_new_inputs_plans_the_rest_around_the_kept_days(self, case, later, data):
        """Run days, the long-run day, the quality count or an override changed on a later day (recorded loads
        kept): the days up to today stay as stored and the days after them keep every rule against them (a
        rule only the kept days break is history, never a failure); keys stay unique."""
        inputs, snap = case
        body = stored(pe.build_plan(inputs, snap, what_if_runs=False))
        today = snap.today + dt.timedelta(days=later)
        if today >= inputs.race_date:
            today = inputs.race_date - dt.timedelta(days=1 + (later % 3))
        if today <= snap.today:
            return
        days = tuple(sorted(data.draw(st.sets(st.integers(0, 6), min_size=3, max_size=7))))
        new = dataclasses.replace(
            inputs, run_days=days, rest_days=tuple(sorted(set(range(7)) - set(days))),
            long_run_day=data.draw(st.sampled_from(days)), quality_per_week=data.draw(st.integers(0, 2)))
        sessions = [s for w in body["weeks"] for s in w["sessions"] if s["role"] not in ("race", "b_race")]
        if sessions and data.draw(st.booleans()):   # an edit of a stored session, often one now in the past
            s = data.draw(st.sampled_from(sessions))
            edit = data.draw(st.sampled_from((
                pe.Override("easy", key=s["key"]), pe.Override("rest", date=dt.date.fromisoformat(s["date"])),
                pe.Override("move", key=s["key"], date=dt.date.fromisoformat(s["date"]) + dt.timedelta(days=1)))))
            new = dataclasses.replace(new, overrides=(edit,))
        again = pe.build_plan(new, dataclasses.replace(snap, today=today), what_if_runs=False,
                              born=born_of(body), history=body)
        self.assertTrue(again["invariants"]["ok"], again["invariants"]["failed"])
        answer = pe.plan_answer(again)
        keys = [s["key"] for w in answer["weeks"] for s in w["sessions"]]
        self.assertEqual(len(keys), len(set(keys)), "two sessions with one key")
        old, now = run_days(body), run_days(answer)
        for day in set(old) | set(now):
            if day <= today.isoformat():
                self.assertEqual(now.get(day), old.get(day), f"{day} is history: as stored")
        for r in again["overrides"]["refused"]:
            self.assertTrue(r["rule"])


if __name__ == "__main__":
    unittest.main()
