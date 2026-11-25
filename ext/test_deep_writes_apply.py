"""Deep write tests for plan_apply_changes (the weekly review's apply token, at1.): faults mid-apply, resume,
change-id binding, token windows, refusals that must store nothing, and the per-plan lock.

The setup is test_plan_review_tools.Flow's (a plan built and committed 14 days, a week later two changes
proposed), borrowed by function so its tests do not run twice. Standard library only.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import unittest

import plan_store as ps
import plan_writes as pw
import test_plan_review_tools as prt_tests
import test_plan_writes as tpw


def run(coro):
    return asyncio.run(coro)


class ApplyDeep(unittest.TestCase):
    COMMIT_DAYS = 14
    setUp = prt_tests.Flow.setUp
    _level = prt_tests.Flow._level
    _release = prt_tests.Flow._release
    apply = prt_tests.Flow.apply
    review = prt_tests.Flow.review

    def once(self, token, ids):
        return json.loads(run(self.writes.apply(self.pid, token, ids)))

    def fresh_apply(self):
        out = self.review(week_start="2026-11-30")
        apply = out["apply"]
        self.assertTrue(apply["token"], apply)
        self.assertTrue(apply["token"].startswith("at1."))
        return apply, ",".join(apply["change_ids"])

    def statuses(self, ids):
        return {r["status"] for r in self.store.changes_of(self.pid, ids)}

    def version(self):
        return self.store.get_plan(self.store.account_fp("123456"), self.pid)["current_version"]

    def assert_one_per_day(self):
        live = [r for r in self.store.write_rows(self.pid) if r["state"] in ("uploaded", "scheduled")]
        days = [r["date_local"] for r in live if "~" not in r["workout_key"]]
        self.assertEqual(len(days), len(set(days)), "two live workouts of the plan on one day")
        ids = {}
        for e in self.garmin.calendar:
            ids[e["workoutId"]] = ids.get(e["workoutId"], 0) + 1
        self.assertTrue(all(n == 1 for n in ids.values()), ids)
        marks = [w["description"] for w in self.garmin.library.values()]
        self.assertEqual(len(marks), len(set(marks)), "two workouts with one marker")
        on_calendar = {e["workoutId"] for e in self.garmin.calendar}
        self.assertEqual(on_calendar, set(self.garmin.library), "every library workout is on the calendar once")

    def test_a_429_mid_apply_resumes_and_applies_once(self):
        apply, ids = self.fresh_apply()
        self.garmin.faults["create"] = [tpw.GarminConnectTooManyRequestsError("x")]
        out = self.once(apply["token"], ids)
        self.assertEqual((out["status"], out["retry_after_s"]), ("partial", 900), out)
        self.assertEqual(out["version"], 2)
        self.assertEqual(self.version(), 2)
        self.assertEqual(self.statuses(apply["change_ids"]), {"approved"}, "applied only once every write is done")
        done = self.apply(apply["token"], ids)
        self.assertEqual(done["status"], "done", done)
        self.assertEqual(self.version(), 2, "the resumed token stores no third version")
        self.assertEqual(self.statuses(apply["change_ids"]), {"applied"})
        self.assert_one_per_day()

    def test_a_rate_limited_delete_of_the_replaced_workout(self):
        apply, ids = self.fresh_apply()
        self.assertIn("delete", [o["op"] for o in apply["ops"]])
        self.garmin.faults["delete"] = [tpw.GarminConnectTooManyRequestsError("x")]
        out = self.once(apply["token"], ids)
        self.assertEqual((out["status"], out["retry_after_s"]), ("partial", 900), out)
        self.assertEqual(self.statuses(apply["change_ids"]), {"approved"})
        done = self.apply(apply["token"], ids)
        self.assertEqual(done["status"], "done", done)
        self.assertEqual(self.statuses(apply["change_ids"]), {"applied"})
        self.assert_one_per_day()

    def test_an_unknown_upload_mid_apply_is_adopted(self):
        apply, ids = self.fresh_apply()
        self.garmin.faults["create"] = ["block"]
        out = self.once(apply["token"], ids)
        self.assertEqual(out["status"], "unknown", out)
        self.garmin.blocked.set()
        self.assertTrue(pw.join_write_threads(10))
        self.clock.advance(minutes=3)
        done = self.apply(apply["token"], ids)
        self.assertEqual(done["status"], "done", done)
        self.assert_one_per_day()

    def test_change_ids_binding(self):
        apply, ids = self.fresh_apply()
        listed = apply["change_ids"]
        self.assertGreaterEqual(len(listed), 2, listed)
        mismatch = [listed[:1], listed + ["c" + "f" * 16]]
        for value in mismatch:
            with self.subTest(ids=value):
                self.assertEqual(self.once(apply["token"], ",".join(value))["reason"], "changes_mismatch")
        self.assertEqual(self.garmin.writes()[-1:], self.garmin.writes()[-1:])
        n = len(self.garmin.writes())
        self.assertEqual(self.version(), 1)
        # Reordered, with blanks, semicolons and a repeat: the same set.
        same = " ; ".join(reversed(listed)) + "," + listed[0]
        done = self.apply(apply["token"], same)
        self.assertEqual(done["status"], "done", done)
        self.assertGreater(len(self.garmin.writes()), n)

    def test_refusals_store_nothing(self):
        apply, ids = self.fresh_apply()
        n = len(self.garmin.writes())
        self.may_write = False
        self.assertEqual(self.once(apply["token"], ids)["reason"], "no_write_permission")
        self.may_write = True
        self.confirm_answer = "declined"
        self.assertEqual(self.once(apply["token"], ids)["status"], "declined")
        self.confirm_answer = "unsupported"
        other = "0" * 32
        self.assertEqual(json.loads(run(self.writes.apply(other, apply["token"], ids)))["reason"], "unknown_token")
        self.assertTrue(run(self.writes.commit(plan_id=self.pid, commit_token=apply["token"])).startswith(
            "Error: Invalid input: commit_token"))
        self.assertTrue(run(self.writes.cancel(plan_id=self.pid, cancel_token=apply["token"])).startswith(
            "Error: Invalid input: cancel_token"))
        self.assertEqual(len(self.garmin.writes()), n)
        self.assertEqual(self.version(), 1)
        self.assertEqual(self.statuses(apply["change_ids"]), {"proposed"})
        self.assertEqual(self.store.tokens[ps.token_hash(apply["token"])]["status"], "issued")

    def test_first_use_boundary(self):
        apply, ids = self.fresh_apply()
        self.clock.advance(minutes=30)
        out = self.apply(apply["token"], ids)
        self.assertEqual(out["status"], "done", out)

    def test_first_use_just_late(self):
        apply, ids = self.fresh_apply()
        self.clock.advance(minutes=30, microseconds=1)
        self.assertEqual(self.once(apply["token"], ids)["reason"], "expired")
        self.assertEqual(self.version(), 1)

    def test_busy_while_another_write_of_the_plan_runs(self):
        apply, ids = self.fresh_apply()
        lock = self.writes._lock(self.pid)
        lock.acquire()
        try:
            self.assertEqual(self.once(apply["token"], ids)["reason"], "busy")
        finally:
            lock.release()
        self.assertEqual(self.statuses(apply["change_ids"]), {"proposed"})
        self.assertEqual(self.store.tokens[ps.token_hash(apply["token"])]["status"], "issued")

    def test_a_calendar_read_failure_stores_nothing(self):
        apply, ids = self.fresh_apply()
        self.garmin.read_faults["calendar"] = ConnectionError("API Error 503")
        out = self.once(apply["token"], ids)
        self.assertEqual(out["reason"], "calendar_unreadable")
        self.assertEqual(self.version(), 1)
        self.assertEqual(self.statuses(apply["change_ids"]), {"proposed"})
        del self.garmin.read_faults["calendar"]
        self.assertEqual(self.apply(apply["token"], ids)["status"], "done")

    def test_replay_after_done_calls_nothing(self):
        apply, ids = self.fresh_apply()
        self.assertEqual(self.apply(apply["token"], ids)["status"], "done")
        calls = len(self.garmin.calls)
        self.clock.advance(days=3)
        again = self.once(apply["token"], ids)
        self.assertTrue(again["replayed"])
        self.assertEqual(len(self.garmin.calls), calls)
        self.assertEqual(self.once(apply["token"], apply["change_ids"][0])["reason"], "changes_mismatch",
                         "the change ids are checked before the replay")

    def test_the_confirmation_lists_adds_and_removes(self):
        apply, ids = self.fresh_apply()
        self.confirm_answer = "accepted"
        mark = len(self.confirm_calls)
        self.assertEqual(self.apply(apply["token"], ids)["status"], "done")
        summary = self.confirm_calls[mark]
        self.assertEqual(summary["action"], "apply")
        adds = [o for o in apply["ops"] if o["op"] == "create"]
        drops = [o for o in apply["ops"] if o["op"] == "delete"]
        self.assertEqual(summary["count"], len(adds))
        self.assertEqual(summary["dates"], sorted({o["date"] for o in adds}))
        self.assertEqual(summary.get("removes", 0), len(drops))
        self.assertEqual(len(self.confirm_calls), mark + 1)
        self.assertTrue(all(d > dt.date(2026, 12, 7).isoformat() for d in summary["dates"]), "never today")


if __name__ == "__main__":
    unittest.main()
