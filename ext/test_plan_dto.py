"""plan_dto: the Garmin workout JSON of the plan's sessions, checked by upstream's validators and by
daily_status' classifier. The upstream checks are skipped without upstream garmin_mcp."""

from __future__ import annotations

import copy
import datetime as dt
import importlib.util
import unittest
from unittest import mock

import mcp2_compat
import plan_dto
import plan_engine as pe

mcp2_compat.register_finder()
HAVE_UPSTREAM = importlib.util.find_spec("garmin_mcp") is not None and mcp2_compat._mcp_major() == "2"
UPSTREAM_SKIP = "needs upstream garmin_mcp on mcp 2.x"


def sample_plan(lt=3.55):
    snap = pe.sample_snapshot()
    if lt is None:
        import dataclasses

        snap = dataclasses.replace(snap, lt_speed_mps=None)
    inputs = pe.PlanInputs(race_date=dt.date(2028, 2, 25), goal_time_s=13920,
                           b_races=(pe.BRace(dt.date(2028, 1, 28), "tri_half"),))
    return pe.plan_answer(pe.build_plan(inputs, snap, what_if_runs=False))


def first(plan, role):
    return next(s for w in plan["weeks"] for s in w["sessions"] if s["role"] == role)


class Dto(unittest.TestCase):
    def setUp(self):
        self.plan = sample_plan()

    def test_ids_are_upstreams(self):
        dto = plan_dto.workout_dto(first(self.plan, "threshold"))
        self.assertEqual(dto["sportType"], {"sportTypeId": 1, "sportTypeKey": "running"})
        steps = dto["workoutSegments"][0]["workoutSteps"]
        self.assertEqual(steps[0]["stepType"], {"stepTypeId": 1, "stepTypeKey": "warmup"})
        self.assertEqual(steps[-1]["stepType"], {"stepTypeId": 2, "stepTypeKey": "cooldown"})
        self.assertEqual(steps[0]["endCondition"], {"conditionTypeId": 3, "conditionTypeKey": "distance"})
        repeat = next(s for s in steps if s["type"] == "RepeatGroupDTO")
        self.assertEqual(repeat["endCondition"]["conditionTypeId"], 7)
        work = repeat["workoutSteps"][0]
        self.assertEqual(work["targetType"]["workoutTargetTypeId"], 6)
        self.assertLess(work["targetValueOne"], work["targetValueTwo"])
        orders = []

        def walk(items):
            for s in items:
                orders.append(s["stepOrder"])
                walk(s.get("workoutSteps") or [])
        walk(steps)
        self.assertEqual(orders, list(range(1, len(orders) + 1)))

    def test_names_carry_no_free_text(self):
        for _s, dto in plan_dto.session_dtos(self.plan):
            self.assertRegex(dto["workoutName"], r"^Coach [A-Za-z +]+$")
            self.assertNotIn("description", dto)
        self.assertEqual(plan_dto.workout_dto(first(self.plan, "easy"), marker="tag")["description"], "tag")

    def test_races_are_not_workouts(self):
        roles = {s["role"] for s, _ in plan_dto.session_dtos(self.plan)}
        self.assertFalse(roles & {"race", "b_race"})

    def test_no_lt_means_no_target(self):
        plan = sample_plan(lt=None)
        def leaves(items):
            for step in items:
                if step["type"] == "RepeatGroupDTO":
                    yield from leaves(step["workoutSteps"])
                else:
                    yield step
        for _s, dto in plan_dto.session_dtos(plan):
            for step in leaves(dto["workoutSegments"][0]["workoutSteps"]):
                self.assertEqual(step["targetType"]["workoutTargetTypeId"], 1)
                self.assertNotIn("targetValueOne", step)

    def test_render_reads_the_dto(self):
        text = plan_dto.render(plan_dto.workout_dto(first(self.plan, "strides")))
        self.assertTrue(text.startswith("Coach Easy + strides: "))
        self.assertIn("6 x (100 m run at ", text)
        self.assertIn("60 s recovery", text)

    def test_hash_is_stable_and_does_not_change_the_dto(self):
        dto = plan_dto.workout_dto(first(self.plan, "long"))
        before = copy.deepcopy(dto)
        self.assertRegex(plan_dto.dto_hash(dto), r"^[0-9a-f]{64}$")
        self.assertEqual(plan_dto.dto_hash(dto), plan_dto.dto_hash(before))
        self.assertEqual(dto, before)


class StepValues(unittest.TestCase):
    """dto_valid's own value checks (upstream checks ids and keys only)."""

    def steps(self, dto):
        return dto["workoutSegments"][0]["workoutSteps"]

    def test_every_sample_dto_has_sane_values(self):
        for _s, dto in plan_dto.session_dtos(sample_plan()):
            self.assertEqual(plan_dto.step_value_problems(dto), [])

    def test_bad_values_are_reported(self):
        plan = sample_plan()
        easy = plan_dto.workout_dto(first(plan, "easy"))
        strides = plan_dto.workout_dto(first(plan, "strides"))
        edits = {
            "zero distance": (easy, lambda d: self.steps(d)[0].update(endConditionValue=0.0)),
            "51 km": (easy, lambda d: self.steps(d)[0].update(endConditionValue=51_000.0)),
            "pace min = max": (easy, lambda d: self.steps(d)[0].update(targetValueTwo=self.steps(d)[0]["targetValueOne"])),
            "pace over 7 m/s": (easy, lambda d: self.steps(d)[0].update(targetValueTwo=7.5)),
            "no steps": (easy, lambda d: d["workoutSegments"][0].update(workoutSteps=[])),
            "zero repeats": (strides, lambda d: next(s for s in self.steps(d) if s["type"] == "RepeatGroupDTO")
                             .update(numberOfIterations=0)),
            "zero-second jog": (strides, lambda d: next(s for s in self.steps(d) if s["type"] == "RepeatGroupDTO")
                                ["workoutSteps"][1].update(endConditionValue=0.0)),
        }
        for name, (dto, edit) in edits.items():
            with self.subTest(edit=name):
                bad = copy.deepcopy(dto)
                edit(bad)
                self.assertTrue(plan_dto.step_value_problems(bad))


class WithoutUpstream(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(plan_dto, "_upstream", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_checks_say_skipped(self):
        checks = plan_dto.check_plan(sample_plan(), 3.55)
        self.assertEqual([c["id"] for c in checks], ["dto_valid", "cross_engine"])
        self.assertTrue(all(c["ok"] and c["detail"] == plan_dto.SKIPPED for c in checks))
        self.assertIsNone(plan_dto.validate({}))
        self.assertIsNone(plan_dto.classify({}, {}))


@unittest.skipUnless(HAVE_UPSTREAM, UPSTREAM_SKIP)
class WithUpstream(unittest.TestCase):
    def test_every_sample_dto_passes_upstreams_checks_unchanged(self):
        plan = sample_plan()
        for s, dto in plan_dto.session_dtos(plan):
            with self.subTest(key=s["key"]):
                self.assertEqual(plan_dto.validate(dto), [])

    def test_the_daily_classifier_agrees_with_the_plan(self):
        plan = sample_plan()
        athlete = {"lt_speed_mps": 3.55, "lt_hr": None, "ftp_w": None}
        seen = set()
        for s, dto in plan_dto.session_dtos(plan):
            c = plan_dto.classify(dto, athlete)
            if s["role"] in ("threshold", "intervals"):
                self.assertTrue(c["hard"], s["key"])
            if s["role"] in ("easy", "strides"):
                self.assertFalse(c["hard"] or c["long"], s["key"])
            seen.add(s["role"])
        self.assertTrue({"easy", "strides", "threshold", "long"} <= seen)
        self.assertTrue(all(c["ok"] for c in plan_dto.check_plan(plan, 3.55)))

    def test_a_broken_dto_is_reported(self):
        dto = plan_dto.workout_dto(first(sample_plan(), "easy"))
        dto["workoutSegments"][0]["workoutSteps"][0]["endCondition"] = {"conditionTypeId": 3,
                                                                        "conditionTypeKey": "time"}
        self.assertTrue(plan_dto.validate(dto))

    def test_cross_engine_catches_an_easy_label_on_a_hard_session(self):
        plan = sample_plan()
        for w in plan["weeks"]:
            for s in w["sessions"]:
                if s["role"] == "threshold":
                    s["hard_or_long"] = False
        check = {c["id"]: c for c in plan_dto.check_plan(plan, 3.55)}
        self.assertFalse(check["cross_engine"]["ok"])

    def test_dto_valid_catches_a_distance_step_of_0_m(self):
        plan = sample_plan()
        first(plan, "easy")["steps"][0]["distance_m"] = 0
        check = {c["id"]: c for c in plan_dto.check_plan(plan, 3.55)}
        self.assertFalse(check["dto_valid"]["ok"])
        self.assertIn("distance step", check["dto_valid"]["detail"])

    def test_selfcheck(self):
        plan_dto.selfcheck()


if __name__ == "__main__":
    unittest.main()
