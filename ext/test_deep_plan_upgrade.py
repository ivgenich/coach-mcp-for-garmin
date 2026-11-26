"""Multi-day plan simulations (test_deep_plan_days.Sim) for three defects an independent review proved:

1. plans stored with the old week-numbered session keys ("w01-e1", before the date keys): migration 004
   (MemoryPlanStore.upgrade_keys here) turns them into date keys, after which tracking, log_feedback,
   plan_preview, plan_commit, plan_cancel and --plan-cleanup work on them; and without the conversion
   plan_cancel and --plan-cleanup never crash on an unexpected key (counted as failed, the rest goes on).
2. a swap override and then a rebuild without it: a commit never deletes or replaces today's workout, and a
   version with one session key twice fails one_run_per_day (no commit token).
3. the plan read before the user's confirmation is read again under the plan's lock: a plan stopped, or a new
   version stored, while the confirmation waited writes nothing.

    cd ext && REQUIRE_UPSTREAM=1 python -B -m unittest test_deep_plan_upgrade -v
"""

from __future__ import annotations

import asyncio
import collections
import datetime as dt
import json
import unittest

import plan_activate as pa
import test_deep_plan_days as T

INPUTS = {"race_date": "2027-02-26", "goal_time": "3:30:00"}


def run(coro):
    return asyncio.run(coro)


def old_keys(store, pid):
    """Rewrite the stored plan as the old key scheme had it: "wNN-long", "wNN-qN", "wNN-eN", "wNN-race",
    "wNN-b" in every version body, the rows (a retired row keeps its suffix) and the feedback."""
    mapping = {}
    for (p, _ver), v in sorted(store.versions.items()):
        if p != pid:
            continue
        for w in v["body"]["weeks"]:
            n = {"e": 0, "q": 0}
            for s in w["sessions"]:
                if s["key"] in mapping:
                    s["key"] = mapping[s["key"]]
                    continue
                role = s["role"]
                if role in ("race", "long"):
                    tag = role
                elif role == "b_race":
                    tag = "b"
                else:
                    kind = "q" if s.get("hard_or_long") else "e"
                    n[kind] += 1
                    tag = f"{kind}{n[kind]}"
                mapping[s["key"]] = f"w{w['week']:02d}-{tag}"
                s["key"] = mapping[s["key"]]
    for (p, k) in [x for x in store.workout_rows if x[0] == pid]:
        base, _, suffix = k.partition("~")
        if base not in mapping:
            continue
        row = store.workout_rows.pop((p, k))
        new = mapping[base] + (f"~{suffix}" if suffix else "")
        store.workout_rows[(p, new)] = {**row, "workout_key": new}
    for (p, k) in [x for x in store.feedback_rows if x[0] == pid]:
        if k in mapping:
            row = store.feedback_rows.pop((p, k))
            store.feedback_rows[(p, mapping[k])] = {**row, "workout_key": mapping[k]}
    return mapping


class Days(unittest.TestCase):
    """test_deep_plan_days.PlanDays' setup without its scenarios (they run in their own module)."""
    setUp = T.PlanDays.setUp
    sim = T.PlanDays.sim


@unittest.skipUnless(T.HAVE_UPSTREAM, T.NEEDS)
class OldKeys(Days):
    def build(self, name):
        sim = self.sim(name, inputs=dict(INPUTS), history_until=dt.date(2027, 1, 17))
        sim.morning(dt.date(2027, 1, 18))
        sim.run_session(dt.date(2027, 1, 18)) if any(s["date"] == "2027-01-18" for s in sim.sessions()) else None
        return sim

    def test_old_keys_after_migration_work_like_date_keys(self):
        sim = self.build("old keys upgraded")
        before = {s["date"]: s["key"] for s in sim.sessions()}
        rows_before = {r["workout_key"]: (r["date_local"], r["garmin_workout_id"]) for r in sim.store.links(sim.pid)}
        mapping = old_keys(sim.store, sim.pid)
        self.assertTrue(mapping and all(k.startswith("w") for k in mapping.values()))
        self.assertTrue(any(k.startswith("w") for k in sim.links()), "the rows carry old keys")
        self.assertGreater(sim.store.upgrade_keys(), 0)
        self.assertEqual(sim.store.upgrade_keys(), 0, "a second run changes nothing")
        # Every key is the date key of its session (and of its row) again: nothing else moved.
        self.assertEqual({s["date"]: s["key"] for s in sim.sessions()}, before)
        self.assertEqual({r["workout_key"]: (r["date_local"], r["garmin_workout_id"])
                          for r in sim.store.links(sim.pid)}, rows_before)
        # Tracking, feedback, the next previews and commits, a cancel and the cleanup: no violation.
        for day in T.days(dt.date(2027, 1, 19), dt.date(2027, 1, 21)):
            sim.morning(day)
            if sim.run_session(day):
                sim.feedback(day, rpe=4)
        sim.at(dt.date(2027, 1, 21), 21)
        sim.cancel()
        sim.cleanup()
        sim.report()

    def test_without_migration_cancel_and_cleanup_do_not_crash(self):
        sim = self.build("old keys left")
        old_keys(sim.store, sim.pid)
        sim.at(dt.date(2027, 1, 19), 9)
        out = json.loads(run(sim.writes.cancel(plan_id=sim.pid)))
        token = out["cancel"]["token"]
        with self.assertLogs("garmin_ext.plans", "WARNING") as logs:
            res = json.loads(run(sim.writes.cancel(plan_id=sim.pid, cancel_token=token)))
        self.assertEqual(res["status"], "partial", res)
        self.assertTrue(res.get("failed"), res)
        self.assertNotIn("w0", " ".join(logs.output), "the key never reaches the log")
        self.assertEqual(sim.store.get_plan(sim.fp, sim.pid)["status"], "active", "not cancelled with workouts left")
        with self.assertLogs("garmin_ext.plans", "WARNING"):
            counts = T.pw.cleanup(sim.store, sim.garmin, sim.today(), dry_run=False, sleep=sim._sleep, join_s=10.0)
        self.assertGreater(counts["failed"], 0)


@unittest.skipUnless(T.HAVE_UPSTREAM, T.NEEDS)
class SwapThenRebuild(Days):
    def test_todays_workout_survives_a_rebuild_after_a_swap(self):
        sim = self.sim("swap then rebuild", inputs=dict(INPUTS), history_until=dt.date(2027, 1, 17))
        sim.at(dt.date(2027, 1, 18), 7)
        sim.preview()
        week = [s for s in sim.sessions() if "2027-01-19" <= s["date"] <= "2027-01-24"]
        a, b = week[0], week[-1]
        self.assertEqual(a["date"], "2027-01-19")
        sim.inputs["overrides"] = json.dumps([{"kind": "swap", "date": a["date"], "date_b": b["date"]}])
        out = sim.preview()
        self.assertTrue(out["overrides"]["applied"], out["overrides"])
        sim.at(dt.date(2027, 1, 18), 7, 10)
        sim.commit(out)
        # The next morning the override is gone: the rebuilt plan keeps today's stored (moved) session.
        sim.at(dt.date(2027, 1, 19), 7)
        sim.inputs.pop("overrides")
        today_before = [e["workoutId"] for e in sim.garmin.ours_entries() if e["scheduleDate"] == "2027-01-19"]
        self.assertEqual(len(today_before), 1)
        text = run(sim.plans.preview(plan_id=sim.pid, commit_days=7, **sim.inputs))
        out = json.loads(text)
        keys = collections.Counter(s["key"] for s in sim.sessions())
        dups = [k for k, n in keys.items() if n > 1]
        ops = (out.get("commit") or {}).get("ops") or []
        self.assertEqual([o for o in ops if o["date"] <= "2027-01-19"], [], "nothing on today or earlier")
        if dups:
            self.assertFalse(out["invariants"]["ok"])
            failed = {i["id"]: i["detail"] for i in out["invariants"]["failed"]}
            self.assertIn("one_run_per_day", failed)
            self.assertIn(f"key {dups[0]}", failed["one_run_per_day"])
            self.assertIsNone(out["commit"].get("token"), "no commit token with a key twice in the version")
        elif out["commit"].get("token"):
            sim.at(dt.date(2027, 1, 19), 7, 10)
            run(sim.writes.commit(plan_id=sim.pid, commit_token=out["commit"]["token"]))
        today_after = [e["workoutId"] for e in sim.garmin.ours_entries() if e["scheduleDate"] == "2027-01-19"]
        self.assertEqual(today_after, today_before, "today's workout stays")

    def test_commit_ops_never_touch_a_row_of_today_whose_key_moved(self):
        import plan_writes as pw
        import test_plan_writes as tpw

        today = tpw.TODAY
        key = T.pt.pe.session_key(today + dt.timedelta(days=5))
        later = today + dt.timedelta(days=5)
        plan = {"weeks": [{"week": 1, "start": today.isoformat(), "sessions": [tpw.session(key, later)]}]}
        row = {"workout_key": key, "date_local": today, "role": "long", "dto_hash": "0" * 64, "state": "scheduled",
               "garmin_workout_id": 11, "garmin_schedule_id": 12, "linked_activity_id": None}
        ops = pw.commit_ops(plan, [row], today=today, days=7, tag="t", plan_id="0" * 32, fixed_hard=[],
                            lt_speed=None)["ops"]
        self.assertEqual([o for o in ops if o["date"] <= today.isoformat()], [])
        self.assertEqual(ops, [], "the session cannot take the key from today's workout")


@unittest.skipUnless(T.HAVE_UPSTREAM, T.NEEDS)
class ConfirmRace(Days):
    def test_a_plan_stopped_during_the_confirmation_writes_nothing(self):
        sim = self.sim("stop during confirm", inputs=dict(INPUTS), history_until=dt.date(2027, 1, 17))
        sim.at(dt.date(2027, 1, 18), 7)
        out = sim.preview()
        svc = pa.ActivateService(lambda: sim.store, sim.today)
        self.assertEqual(json.loads(svc.activate(sim.pid, out["stored"]["version"], "start"))["status"], "active")
        orig = sim._confirm

        async def confirm(ctx, **summary):
            self.assertEqual(json.loads(svc.activate(sim.pid, 0, "stop"))["status"], "draft")
            return await orig(ctx, **summary)

        sim.writes._confirm = confirm
        n = len(sim.garmin.log)
        res = json.loads(run(sim.writes.commit(plan_id=sim.pid, commit_token=out["commit"]["token"])))
        self.assertEqual((res["status"], res["reason"]), ("refused", "plan_changed"), res)
        self.assertEqual(sim.garmin.log[n:], [], "no Garmin write")
        self.assertEqual(sim.store.get_plan(sim.fp, sim.pid)["status"], "draft")
        self.assertEqual(sim.store.links(sim.pid), [])

    def test_a_new_version_stored_during_the_confirmation_writes_nothing(self):
        sim = self.sim("version during confirm", inputs=dict(INPUTS), history_until=dt.date(2027, 1, 17))
        sim.at(dt.date(2027, 1, 18), 7)
        out = sim.preview()
        orig = sim._confirm
        stored = {}

        async def confirm(ctx, **summary):
            text = await sim.plans.preview(plan_id=sim.pid, commit_days=7, blocked_dates="2027-01-20,2027-01-23",
                                           **INPUTS)
            stored.update(json.loads(text)["stored"])
            return await orig(ctx, **summary)

        sim.writes._confirm = confirm
        n = len(sim.garmin.log)
        res = json.loads(run(sim.writes.commit(plan_id=sim.pid, commit_token=out["commit"]["token"])))
        self.assertEqual(stored.get("version"), 2)
        self.assertEqual((res["status"], res["reason"]), ("refused", "plan_changed"), res)
        self.assertEqual(sim.garmin.log[n:], [])
        self.assertEqual(sim.garmin.ours_entries(), [])

    def test_a_new_version_stored_during_a_cancel_confirmation_deletes_nothing(self):
        sim = self.sim("version during cancel", inputs=dict(INPUTS), history_until=dt.date(2027, 1, 17))
        sim.morning(dt.date(2027, 1, 18))
        sim.at(dt.date(2027, 1, 18), 9)
        token = json.loads(run(sim.writes.cancel(plan_id=sim.pid)))["cancel"]["token"]
        orig = sim._confirm

        async def confirm(ctx, **summary):
            await sim.plans.preview(plan_id=sim.pid, commit_days=7, blocked_dates="2027-01-22", **INPUTS)
            return await orig(ctx, **summary)

        sim.writes._confirm = confirm
        entries = sim.garmin.ours_entries()
        n = len(sim.garmin.log)
        res = json.loads(run(sim.writes.cancel(plan_id=sim.pid, cancel_token=token)))
        self.assertEqual((res["status"], res["reason"]), ("refused", "plan_changed"), res)
        self.assertEqual(sim.garmin.log[n:], [])
        self.assertEqual(sim.garmin.ours_entries(), entries)
        self.assertEqual(sim.store.get_plan(sim.fp, sim.pid)["status"], "active")


if __name__ == "__main__":
    unittest.main()
