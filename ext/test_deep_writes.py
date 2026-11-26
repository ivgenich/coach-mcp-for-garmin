"""Deep write tests: late landings, concurrency, a restart, the token windows and bindings, the replace flow, the
write budget and --plan-cleanup, on the fake Garmin of test_deep_writes_base and MemoryPlanStore.

Standard library only. Threads a test starts are joined in the test; blocked writes are released and the helper's
threads joined in the cleanup.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import io
import json
import threading
import time
import unittest

import garmin_paths
import plan_engine as pe
import plan_store as ps
import plan_writes as pw
import test_deep_writes_base as b
from test_deep_writes_base import BLOCK, after, before, http

KEYS = ["20261202-run", "20261203-run", "20261205-run"]


def days_sessions(n):
    """Easy runs on the n days after b.TODAY, each keyed by its day."""
    days = [b.TODAY + dt.timedelta(days=i) for i in range(1, n + 1)]
    return [b.session(pe.session_key(d), d) for d in days]


def wait_for(cond, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


class LateLanding(b.DeepBase):
    def test_a_middle_upload_that_lands_late_is_adopted_once(self):
        pid, c = self.preview()
        self.garmin.faults["upload"] = {2: BLOCK}
        out = self.commit(pid, c["token"])
        self.assertEqual(out["status"], "unknown", out)
        self.assertEqual([w["key"] for w in out["written"]], ["20261202-run"])
        # The thread still runs: even after the quarantine nothing is decided and nothing re-sent for that row,
        # but the other rows are written.
        self.clock.advance(minutes=5)
        again = self.commit(pid, c["token"])
        self.assertEqual((again["status"], again["retry_after_s"]), ("partial", pw.RETRY_AFTER_UNKNOWN_S), again)
        self.assertEqual([w["key"] for w in again["written"]], ["20261205-run"])
        self.assertEqual(self.rows(pid)["20261203-run"]["state"], "unknown")
        self.assertEqual(len(self.garmin.uploads()), 3)
        self.garmin.blocked.set()
        self.assertTrue(pw.join_write_threads(10))
        # Thread ended at "now": the row was last changed 5 minutes ago, so it is settled at once.
        final = self.commit(pid, c["token"])
        self.assertEqual(final["status"], "done", final)
        self.assertEqual(len(self.garmin.uploads()), 3, "the late upload is adopted, not sent again")
        self.assert_no_double_writes()

    def test_a_late_schedule_is_settled_from_the_calendar_once(self):
        pid, c = self.preview()
        self.garmin.faults["schedule"] = {3: BLOCK}
        self.assertEqual(self.commit(pid, c["token"])["status"], "unknown")
        self.garmin.blocked.set()
        self.assertTrue(pw.join_write_threads(10))
        self.assertEqual(self.commit(pid, c["token"])["status"], "partial", "quarantine not passed")
        self.clock.advance(minutes=2, seconds=1)
        out = self.commit(pid, c["token"])
        self.assertEqual(out["status"], "done", out)
        self.assertEqual(len([w for w in self.garmin.write_log if w[0] == "schedule"]), 3)
        self.assert_no_double_writes()
        self.assertTrue(self.rows(pid)["20261205-run"]["garmin_schedule_id"])

    def test_quarantine_boundary(self):
        pid, c = self.preview(sessions=[b.session("20261202-run", b.TODAY + dt.timedelta(days=1))])
        self.garmin.faults["upload"] = {1: after(b.ReadTimeout("r"))}
        self.assertEqual(self.commit(pid, c["token"])["status"], "unknown")
        self.clock.advance(minutes=2, microseconds=-1)
        self.assertEqual(self.commit(pid, c["token"])["status"], "partial")
        self.assertEqual(self.rows(pid)["20261202-run"]["state"], "unknown")
        self.clock.advance(microseconds=1)
        self.assertEqual(self.commit(pid, c["token"])["status"], "done")
        self.assert_no_double_writes(1)

    def test_another_installs_marker_is_never_adopted(self):
        pid, c = self.preview(sessions=[b.session("20261202-run", b.TODAY + dt.timedelta(days=1))])
        self.garmin.faults["upload"] = {1: before(b.ReadTimeout("lost"))}
        self.assertEqual(self.commit(pid, c["token"])["status"], "unknown")
        row = self.rows(pid)["20261202-run"]
        tag = self.store.install_tag()
        other_tag = "0badc0de" if tag != "0badc0de" else "1badc0de"
        foreign = pw.marker(other_tag, pid, "20261202-run", row["dto_hash"])
        older = pw.marker(tag, pid, "20261202-run", "f" * 64)     # ours, but another version of the session
        with self.garmin.lock:
            self.garmin.library[700] = {"workoutId": 700, "workoutName": "Coach Easy", "description": foreign}
            self.garmin.library[701] = {"workoutId": 701, "workoutName": "Coach Easy", "description": older}
            self.garmin.library[702] = {"workoutId": 702, "workoutName": "Coach Easy",
                                        "description": pw.marker(tag, "0" * 32, "20261202-run", row["dto_hash"])}
        self.clock.advance(minutes=3)
        out = self.commit(pid, c["token"])
        self.assertEqual(out["status"], "done", out)
        wid = self.rows(pid)["20261202-run"]["garmin_workout_id"]
        self.assertNotIn(wid, (700, 701, 702))
        self.assertEqual({700, 701, 702} - set(self.garmin.library), set(), "nobody else's workout is touched")
        self.assertFalse([w for w in self.garmin.write_log if w[1].endswith(("/700", "/701", "/702"))])

    def test_adoption_by_the_exact_marker(self):
        pid, c = self.preview(sessions=[b.session("20261202-run", b.TODAY + dt.timedelta(days=1))])
        self.garmin.faults["upload"] = {1: after(b.ReadTimeout("landed"))}
        self.assertEqual(self.commit(pid, c["token"])["status"], "unknown")
        landed = next(iter(self.garmin.library))
        self.clock.advance(minutes=3)
        self.assertEqual(self.commit(pid, c["token"])["status"], "done")
        self.assertEqual(self.rows(pid)["20261202-run"]["garmin_workout_id"], landed)
        self.assertEqual(self.garmin.calendar[0]["workoutId"], landed)

    def test_a_crash_after_the_post_is_settled_by_the_next_call(self):
        """The process dies between a POST and storing its answer (here: storing fails): the row stays
        `uploading`; the next call turns it unknown and adopts the workout by its marker after the quarantine."""
        pid, c = self.preview(sessions=[b.session("20261202-run", b.TODAY + dt.timedelta(days=1))])
        real = self.store.put_workout

        def crash(plan_id, row):
            if row["state"] == "uploaded":
                raise RuntimeError("TEST-ONLY crash")
            return real(plan_id, row)

        self.store.put_workout = crash
        with self.assertLogs("garmin_ext.plans", "ERROR"):
            out = self.commit(pid, c["token"])
        self.assertEqual(out["status"], "unknown", out)
        self.assertEqual(self.rows(pid)["20261202-run"]["state"], "uploading")
        self.store.put_workout = real
        restarted = self.service()       # a new process: no lock, no thread registry entry for this row
        self.assertEqual(self.commit(pid, c["token"], svc=restarted)["status"], "partial")
        self.clock.advance(minutes=3)
        out = self.commit(pid, c["token"], svc=restarted)
        self.assertEqual(out["status"], "done", out)
        self.assertEqual(len(self.garmin.uploads()), 1)
        self.assert_no_double_writes(1)


class Concurrency(b.DeepBase):
    def _background(self, svc, pid, token, box):
        def target():
            box["out"] = json.loads(asyncio.run(svc.commit(plan_id=pid, commit_token=token)))

        t = threading.Thread(target=target, name="deep-writes-call")
        t.start()
        self.addCleanup(t.join, 10)
        return t

    def test_two_calls_with_the_same_token(self):
        pid, c = self.preview()
        svc = self.service(join_s=10)
        self.garmin.faults["upload"] = {1: BLOCK}
        box = {}
        t = self._background(svc, pid, c["token"], box)
        self.assertTrue(wait_for(lambda: len(self.garmin.uploads()) == 1))
        second = self.commit(pid, c["token"], svc=svc)
        self.assertEqual((second["status"], second["reason"]), ("refused", "busy"))
        self.garmin.blocked.set()
        t.join(10)
        self.assertEqual(box["out"]["status"], "done", box)
        self.assertTrue(self.commit(pid, c["token"], svc=svc)["replayed"])
        self.assert_no_double_writes()

    def test_two_tokens_for_the_same_plan(self):
        pid, c1 = self.preview()
        _p, c2 = self.preview(plan_id=pid)          # the same version again: a second token, the same ops
        self.assertNotEqual(c1["token"], c2["token"])
        self.assertEqual(c1["ops"], c2["ops"])
        svc = self.service(join_s=10)
        self.garmin.faults["upload"] = {2: BLOCK}
        box = {}
        t = self._background(svc, pid, c1["token"], box)
        self.assertTrue(wait_for(lambda: len(self.garmin.uploads()) == 2))
        self.assertEqual(self.commit(pid, c2["token"], svc=svc)["reason"], "busy")
        self.garmin.blocked.set()
        t.join(10)
        self.assertEqual(box["out"]["status"], "done")
        writes = len(self.garmin.writes())
        out = self.commit(pid, c2["token"], svc=svc)
        self.assertEqual(out["status"], "done", out)
        self.assertEqual(out["written"], [])
        self.assertEqual(len(self.garmin.writes()), writes, "the second token writes nothing again")
        self.assert_no_double_writes()

    def test_a_commit_and_a_cancel_of_the_same_plan(self):
        pid, c = self.committed()
        _p, c2 = self.preview(sessions=b.default_sessions()[:2], plan_id=pid, content="b")
        prev = self.cancel(pid)             # the same version as c2: both tokens are valid
        svc = self.service(join_s=10)
        self.garmin.faults["unschedule"] = {1: BLOCK}
        box = {}
        t = self._background(svc, pid, c2["token"], box)
        self.assertTrue(wait_for(lambda: any(w[0] == "unschedule" for w in self.garmin.write_log)))
        out = self.cancel(pid, prev["cancel"]["token"], svc=svc)
        self.assertEqual(out["reason"], "busy")
        self.garmin.blocked.set()
        t.join(10)
        self.assertEqual(box["out"]["status"], "done")
        # The cancel token listed the dropped day too: a token listing more than the plan has is still valid.
        out = self.cancel(pid, prev["cancel"]["token"], svc=svc)
        self.assertEqual(out["status"], "done", out)
        self.assertEqual(self.garmin.library, {})

    def test_a_cancelled_call_finishes_its_writes_and_stores_the_answer(self):
        pid, c = self.preview()
        svc = self.service(join_s=10)
        self.garmin.faults["schedule"] = {2: BLOCK}

        async def main():
            task = asyncio.create_task(svc.commit(plan_id=pid, commit_token=c["token"]))
            for _ in range(500):
                await asyncio.sleep(0.01)
                if len([w for w in self.garmin.write_log if w[0] == "schedule"]) >= 2:
                    break
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            busy = json.loads(await svc.commit(plan_id=pid, commit_token=c["token"]))
            self.garmin.blocked.set()
            return busy

        busy = run(main())
        self.assertEqual(busy["reason"], "busy")
        self.assertTrue(pw.join_write_threads(10))
        self.assertTrue(wait_for(lambda: self.token_status(c["token"]) == "done"))
        self.assertFalse(svc._lock(pid).locked(), "the worker released the lock")
        out = self.commit(pid, c["token"], svc=svc)
        self.assertTrue(out.pop("replayed"))
        self.assertEqual(out["status"], "done")
        self.assertEqual([w["key"] for w in out["written"]], KEYS)
        self.assert_no_double_writes()

    def test_a_restart_resumes_a_partial_token(self):
        pid, c = self.preview()
        self.confirm_answer = "accepted"
        self.garmin.faults["upload"] = {2: before(b.GarminConnectTooManyRequestsError("x"))}
        self.assertEqual(self.commit(pid, c["token"])["status"], "partial")
        restarted = self.service()
        self.confirm_answer = "declined"
        out = self.commit(pid, c["token"], svc=restarted)
        self.assertEqual((out["status"], out["confirmed_by"]), ("done", "elicitation"), out)
        self.assertEqual(len(self.confirm_calls), 1, "a resumed token is not asked again")
        self.assert_no_double_writes()


def run(coro):
    return asyncio.run(coro)


class Tokens(b.DeepBase):
    def test_first_use_window_boundary(self):
        pid, c = self.preview()
        self.clock.advance(minutes=30)
        self.assertEqual(self.commit(pid, c["token"])["status"], "done", "exactly 30 minutes is still valid")
        pid2, c2 = self.preview(sessions=[b.session("20300502-run", b.TODAY + dt.timedelta(days=9))], content="d")
        self.store.move_plan(pid, "cancelled")
        self.clock.advance(minutes=30, microseconds=1)
        calls = len(self.garmin.calls)
        out = self.commit(pid2, c2["token"])
        self.assertEqual((out["status"], out["reason"]), ("refused", "expired"))
        self.assertEqual(len(self.garmin.calls), calls)

    def test_resume_window_boundary(self):
        pid, c = self.preview()
        self.clock.advance(minutes=10)
        self.garmin.faults["upload"] = {1: before(b.GarminConnectTooManyRequestsError("x"))}
        self.assertEqual(self.commit(pid, c["token"])["status"], "partial")
        first = self.store.tokens[ps.token_hash(c["token"])]["first_used_at"]
        self.assertEqual(self.store.tokens[ps.token_hash(c["token"])]["expires_at"], first + ps.TOKEN_RESUME)
        self.clock.advance(hours=12)
        self.garmin.faults["upload"] = {2: before(b.GarminConnectTooManyRequestsError("x"))}
        self.assertEqual(self.commit(pid, c["token"])["status"], "partial")
        self.assertEqual(self.store.tokens[ps.token_hash(c["token"])]["expires_at"], first + ps.TOKEN_RESUME,
                         "a resume does not extend the window")
        self.clock.now = first + ps.TOKEN_RESUME
        self.garmin.faults["upload"] = {3: before(b.GarminConnectTooManyRequestsError("x"))}
        self.assertEqual(self.commit(pid, c["token"])["status"], "partial", "exactly 24 h is still valid")
        self.clock.advance(microseconds=1)
        calls = len(self.garmin.calls)
        self.assertEqual(self.commit(pid, c["token"])["reason"], "expired")
        self.assertEqual(len(self.garmin.calls), calls)

    def test_a_done_token_replays_without_garmin_long_after(self):
        self.confirm_answer = "accepted"
        pid, c = self.committed()
        calls, confirms = len(self.garmin.calls), len(self.confirm_calls)
        self.store.move_plan(pid, "cancelled")
        self.clock.advance(days=6)
        self.garmin.read_faults["stats"] = http(503)
        out = self.commit(pid, c["token"])
        self.assertTrue(out["replayed"])
        self.assertEqual(out["status"], "done")
        self.assertEqual((len(self.garmin.calls), len(self.confirm_calls)), (calls, confirms))
        self.may_write = False
        self.assertEqual(self.commit(pid, c["token"])["reason"], "no_write_permission",
                         "a replay still needs the write scope")

    def test_wrong_prefix_and_tampered_strings(self):
        pid, c = self.preview()
        token = c["token"]
        body = token[4:]
        bad_inputs = {"commit": ["xt1." + body, "at1." + body, "CT1." + body, token[:-1], token + "A",
                                 token[:-1] + "é", "ct1-" + body, "", "ct1." + body + "/..", None, 42]}
        for value in bad_inputs["commit"]:
            with self.subTest(value=repr(value)):
                out = run(self.svc.commit(plan_id=pid, commit_token=value))
                self.assertTrue(out.startswith("Error: Invalid input: commit_token"), out)
        for value in ("ct1." + body, "at1." + body):
            with self.subTest(cancel=value[:4]):
                out = run(self.svc.cancel(plan_id=pid, cancel_token=value))
                self.assertTrue(out.startswith("Error: Invalid input: cancel_token"), out)
        flipped = token[:-1] + ("A" if token[-1] != "A" else "B")
        self.assertEqual(self.commit(pid, flipped)["reason"], "unknown_token")
        self.assertEqual(self.cancel(pid, "xt1." + body)["reason"], "unknown_token", "a commit body as cancel")
        self.assertEqual(self.garmin.calls, [])
        self.assertEqual(self.token_status(token), "issued")
        self.assertEqual(self.commit(pid, "  " + token + "\n")["status"], "done", "surrounding blanks are trimmed")

    def test_another_plan_and_another_account(self):
        pid, c = self.preview()
        other, _c = self.preview(sessions=[b.session("20300721-run", b.TODAY + dt.timedelta(days=10))], content="e")
        self.assertEqual(self.commit(other, c["token"])["reason"], "unknown_token")
        self.set_history_account(self.store.account_fp(777))
        self.assertEqual(self.commit(pid, c["token"])["reason"], "unknown_plan")
        self.set_history_account(self.fp)
        self.garmin.profile = 777
        self.assertEqual(self.commit(pid, c["token"])["reason"], "other_account")
        self.assertEqual(self.garmin.writes(), [])
        self.assertEqual(self.token_status(c["token"]), "issued")

    def test_superseded_tokens(self):
        pid, c = self.preview()
        prev = self.cancel(pid)
        self.preview(plan_id=pid, content="b")
        self.assertEqual(self.commit(pid, c["token"])["reason"], "superseded")
        self.assertEqual(self.cancel(pid, prev["cancel"]["token"])["reason"], "superseded")
        self.assertEqual(self.garmin.calls, [])

    def test_ops_run_in_token_order_creates_before_deletes(self):
        pid, _c = self.committed()
        changed = [b.session("20261202-run", b.TODAY + dt.timedelta(days=1), km=9.0),
                   b.session("20261203-run", b.TODAY + dt.timedelta(days=2), role="threshold", km=12.0, hard=True),
                   b.session("20261205-run", b.TODAY + dt.timedelta(days=4), km=7.0)]
        _p, c2 = self.preview(sessions=changed, plan_id=pid, content="b")
        self.assertEqual([o["op"] for o in c2["ops"]], ["create"] * 3 + ["delete"] * 3)
        mark = len(self.garmin.write_log)
        self.assertEqual(self.commit(pid, c2["token"])["status"], "partial", "10 writes: over the budget")
        steps = [w[0] for w in self.garmin.write_log[mark:]]
        self.assertEqual(steps, ["upload", "schedule"] * 3 + ["unschedule", "delete"])
        out = self.commit(pid, c2["token"])
        self.assertEqual(out["status"], "done", out)
        self.assertEqual(len(out["replaced"]), 2)
        self.assert_no_double_writes()

    def test_the_token_writes_its_stored_ops_not_the_current_plan(self):
        """A token is bound to the ops it was issued with: a later change of the stored rows by another token of
        the same version does not add writes to it."""
        pid, c1 = self.preview(days=2)
        self.assertEqual([o["key"] for o in c1["ops"]], ["20261202-run"])
        _p, c14 = self.preview(plan_id=pid, days=14)
        self.assertEqual(self.commit(pid, c1["token"])["status"], "done")
        self.assertEqual(len(self.garmin.library), 1)
        self.assertEqual(self.commit(pid, c14["token"])["status"], "done")
        self.assert_no_double_writes()


class ReplaceFlow(b.DeepBase):
    def test_a_moved_day_is_written_on_the_new_date_and_removed_from_the_old(self):
        pid, _c = self.committed()
        old = self.rows(pid)["20261205-run"]
        moved = b.default_sessions()
        moved[2] = b.session("20261205-run", b.TODAY + dt.timedelta(days=6))
        _p, c2 = self.preview(sessions=moved, plan_id=pid, content="b")
        out = self.commit(pid, c2["token"])
        self.assertEqual(out["status"], "done", out)
        days = sorted(e["scheduleDate"] for e in self.garmin.calendar)
        self.assertIn((b.TODAY + dt.timedelta(days=6)).isoformat(), days)
        self.assertNotIn(old["date_local"].isoformat(), days)
        self.assertNotIn(old["garmin_workout_id"], self.garmin.library)
        self.assert_no_double_writes()

    def test_a_replaced_day_linked_to_an_activity_or_in_the_past_is_kept(self):
        pid, _c = self.committed()
        self.store.workout_rows[(pid, "20261202-run")]["linked_activity_id"] = 5
        changed = b.default_sessions()
        changed[0] = b.session("20261202-run", b.TODAY + dt.timedelta(days=1), km=12.0)
        _p, c2 = self.preview(sessions=changed, plan_id=pid, content="b")
        self.assertEqual([o["op"] for o in c2["ops"]], ["create"], "no delete of a linked workout")
        out = self.commit(pid, c2["token"])
        self.assertEqual(out["status"], "done", out)
        self.assertEqual(len(self.garmin.library), 4)
        self.assertEqual(len(self.garmin.calendar), 4)

    def test_a_deleted_session_readded_with_new_content(self):
        pid, _c = self.committed()
        _p, c2 = self.preview(sessions=b.default_sessions()[:2], plan_id=pid, content="b")
        self.assertEqual(self.commit(pid, c2["token"])["status"], "done")
        self.assertEqual(self.rows(pid)["20261205-run"]["state"], "deleted")
        back = b.default_sessions()
        back[2] = b.session("20261205-run", b.TODAY + dt.timedelta(days=4), km=10.0)
        _p, c3 = self.preview(sessions=back, plan_id=pid, content="c")
        self.assertEqual([(o["op"], o["key"]) for o in c3["ops"]], [("create", "20261205-run")])
        out = self.commit(pid, c3["token"])
        self.assertEqual(out["status"], "done", out)
        self.assertEqual(out["replaced"], [])
        self.assert_no_double_writes()

    def test_a_cancel_preview_then_a_replacement_supersedes_the_cancel(self):
        pid, _c = self.committed()
        prev = self.cancel(pid)
        changed = b.default_sessions()
        changed[1] = b.session("20261203-run", b.TODAY + dt.timedelta(days=2), role="threshold", km=12.0, hard=True)
        _p, c2 = self.preview(sessions=changed, plan_id=pid, content="b")
        self.assertEqual(self.commit(pid, c2["token"])["status"], "done")
        out = self.cancel(pid, prev["cancel"]["token"])
        self.assertEqual((out["status"], out["reason"]), ("refused", "superseded"))
        self.assertEqual(len(self.garmin.calendar), 3)
        prev2 = self.cancel(pid)
        self.assertEqual(self.cancel(pid, prev2["cancel"]["token"])["status"], "done")
        self.assertEqual(self.garmin.library, {})

    def test_a_cancel_token_issued_while_an_upload_is_unknown_resumes(self):
        pid, c = self.preview()
        self.garmin.faults["upload"] = {3: after(b.ReadTimeout("landed"))}
        self.assertEqual(self.commit(pid, c["token"])["status"], "unknown")
        prev = self.cancel(pid)
        self.assertEqual([o["key"] for o in prev["cancel"]["ops"]], KEYS)
        first = self.cancel(pid, prev["cancel"]["token"])
        self.assertEqual((first["status"], first["retry_after_s"]), ("partial", pw.RETRY_AFTER_UNKNOWN_S))
        self.assertIn("same token", first["note"])
        self.clock.advance(minutes=3)
        out = self.cancel(pid, prev["cancel"]["token"])
        self.assertEqual(out["status"], "done", out)
        self.assertEqual(self.garmin.library, {})
        self.assertEqual(self.store.get_plan(self.fp, pid)["status"], "cancelled")

    def test_a_cancel_token_with_a_lost_unknown_upload_finishes(self):
        pid, c = self.preview()
        self.garmin.faults["upload"] = {3: before(b.ReadTimeout("lost"))}
        self.assertEqual(self.commit(pid, c["token"])["status"], "unknown")
        prev = self.cancel(pid)
        self.clock.advance(minutes=3)
        out = self.cancel(pid, prev["cancel"]["token"])
        self.assertEqual(out["status"], "done", out)
        self.assertEqual(self.garmin.library, {})
        self.assertEqual(self.rows(pid)["20261205-run"]["state"], "deleted")

    def test_cancel_with_a_rejected_delete_then_the_retry(self):
        pid, _c = self.committed()
        wid = self.rows(pid)["20261203-run"]["garmin_workout_id"]
        self.garmin.reject_delete_ids.add(wid)
        prev = self.cancel(pid)
        out = self.cancel(pid, prev["cancel"]["token"])
        self.assertEqual(out["status"], "partial", out)
        self.assertEqual([f["key"] for f in out["failed"]], ["20261203-run"])
        self.assertEqual(self.store.get_plan(self.fp, pid)["status"], "active")
        self.assertEqual(self.token_status(prev["cancel"]["token"]), "partial")
        again = self.cancel(pid, prev["cancel"]["token"])
        self.assertEqual(again["status"], "partial", "Garmin still refuses")
        self.garmin.reject_delete_ids.clear()
        final = self.cancel(pid, prev["cancel"]["token"])
        self.assertEqual(final["status"], "done", final)
        self.assertEqual(self.store.get_plan(self.fp, pid)["status"], "cancelled")
        self.assertEqual(self.garmin.library, {})

    def test_a_cancel_never_touches_other_workouts_on_the_same_days(self):
        pid, _c = self.committed()
        with self.garmin.lock:
            for wid, name, desc in ((800, "TP easy", "from TrainingPeaks"), (801, "Coach Easy", None),
                                    (802, "Coach Easy", "coach-mcp 0badc0de " + pid + " 20261202-run h00000000")):
                self.garmin.library[wid] = {"workoutId": wid, "workoutName": name, "description": desc}
                self.garmin.calendar.append({"workoutId": wid, "scheduleDate": (b.TODAY + dt.timedelta(
                    days=1)).isoformat(), "workoutName": name, "workoutType": "cycling", "id": wid + 1000})
        prev = self.cancel(pid)
        self.assertEqual(self.cancel(pid, prev["cancel"]["token"])["status"], "done")
        self.assertEqual(set(self.garmin.library), {800, 801, 802})
        self.assertFalse([w for w in self.garmin.write_log if w[1].rsplit("/", 1)[1] in
                          ("800", "801", "802", "1800", "1801", "1802")])

    def test_a_rejected_schedule_is_offered_again_by_the_next_preview(self):
        pid, c = self.preview()
        self.garmin.faults["schedule"] = {2: before(http(400))}
        out = self.commit(pid, c["token"])
        self.assertEqual((out["status"], len(out["failed"])), ("partial", 1))
        self.assertEqual(self.rows(pid)["20261203-run"]["state"], "uploaded")
        _p, again = self.preview(plan_id=pid)
        self.assertIsNotNone(again["token"], again)
        self.assertIn("20261203-run", [o["key"] for o in again["ops"]])

    def test_an_upload_left_unscheduled_by_an_expired_token_is_offered_again(self):
        pid, c = self.preview()
        self.garmin.faults["schedule"] = {1: before(b.GarminConnectTooManyRequestsError("x"))}
        self.assertEqual(self.commit(pid, c["token"])["status"], "partial")
        self.clock.advance(hours=25)
        self.assertEqual(self.commit(pid, c["token"])["reason"], "expired")
        _p, again = self.preview(plan_id=pid)
        self.assertIsNotNone(again["token"], again)
        self.assertIn("20261202-run", [o["key"] for o in again["ops"]])

    def test_a_twice_retired_hash_keeps_tracking_the_old_workout(self):
        day = b.TODAY + dt.timedelta(days=1)
        a = [b.session("20261202-run", day, km=8.0)]
        bb = [b.session("20261202-run", day, km=10.0)]
        pid, _c = self.committed(sessions=a)
        wid1 = self.rows(pid)["20261202-run"]["garmin_workout_id"]
        self.garmin.reject_delete_ids.add(wid1)           # Garmin refuses unscheduling and deleting it
        for content, sessions in (("b", bb), ("c", a), ("d", bb)):
            _p, cx = self.preview(sessions=sessions, plan_id=pid, content=content)
            self.assertEqual(self.commit(pid, cx["token"])["status"], "done")
        self.assertIn(wid1, self.garmin.library)
        tracked = {r["garmin_workout_id"] for r in self.rows(pid).values() if r["state"] != "deleted"}
        self.assertIn(wid1, tracked, "the refused workout is no longer tracked by any row")


class RefusedWrites(b.DeepBase):
    """Garmin refusing (4xx) a create or a schedule: the answer never says every write is done."""

    def test_a_refused_upload_answers_partial_and_the_same_token_retries(self):
        pid, c = self.preview()
        self.garmin.faults["upload"] = {2: before(http(400))}
        out = self.commit(pid, c["token"])
        self.assertEqual(out["status"], "partial", out)
        self.assertEqual(out["failed"], [{"key": "20261203-run", "date": (b.TODAY + dt.timedelta(days=2)).isoformat()}])
        self.assertIsNone(out["retry_after_s"])
        self.assertNotIn("Every write", out["note"])
        self.assertIn("failed[]", out["note"])
        self.assertEqual(self.token_status(c["token"]), "partial")
        again = self.commit(pid, c["token"])
        self.assertEqual(again["status"], "done", again)
        self.assertIn("Every write", again["note"])
        self.assert_no_double_writes()

    def test_a_refused_delete_of_a_replaced_workout_is_done_with_its_own_note(self):
        pid, _c = self.committed()
        old = self.rows(pid)["20261202-run"]["garmin_workout_id"]
        self.garmin.reject_delete_ids.add(old)
        changed = b.default_sessions()
        changed[0] = b.session("20261202-run", b.TODAY + dt.timedelta(days=1), km=12.0)
        _p, c2 = self.preview(sessions=changed, plan_id=pid, content="b")
        out = self.commit(pid, c2["token"])
        self.assertEqual(out["status"], "done", out)
        self.assertTrue(out["failed"])
        self.assertNotIn("Every write", out["note"])
        self.assertIn("refused deleting", out["note"])


class FinishedPlans(b.DeepBase):
    def test_a_plan_whose_race_passed_never_blocks_the_next_commit(self):
        old, _v, _ch = self.store.save_preview(self.fp, None, b.TODAY - dt.timedelta(days=1), 1, {}, {}, "e" * 64)
        self.store.move_plan(old, "active")
        pid, c = self.preview()
        out = self.commit(pid, c["token"])
        self.assertEqual(out["status"], "done", out)
        self.assertEqual(self.store.get_plan(self.fp, old)["status"], "finished")
        self.assertEqual(self.store.get_plan(self.fp, pid)["status"], "active")

    def test_a_plan_whose_race_is_today_still_blocks(self):
        old, _v, _ch = self.store.save_preview(self.fp, None, b.TODAY, 1, {}, {}, "e" * 64)
        self.store.move_plan(old, "active")
        pid, c = self.preview()
        out = self.commit(pid, c["token"])
        self.assertEqual((out["status"], out["reason"]), ("refused", "another_plan_active"))
        self.assertEqual(self.garmin.writes(), [])


class DoneSessions(b.DeepBase):
    """A workout the calendar links to an activity (associatedActivityId) is done, also with PLAN_TRACKING off
    (no row carries linked_activity_id): never deleted, never written again."""

    def sessions(self, km=8.0):
        return [b.session("20261202-run", b.TODAY, km=km), b.session("20261205-run", b.TODAY + dt.timedelta(days=2))]

    def done_today(self):
        pid, _c = self.committed(sessions=self.sessions())
        wid = self.rows(pid)["20261202-run"]["garmin_workout_id"]
        with self.garmin.lock:
            for e in self.garmin.calendar:
                if e["workoutId"] == wid:
                    e["associatedActivityId"] = 777
        self.assertIsNone(self.rows(pid)["20261202-run"]["linked_activity_id"])
        return pid, wid

    def test_cancel_keeps_a_done_workout(self):
        pid, wid = self.done_today()
        prev = self.cancel(pid)
        out = self.cancel(pid, prev["cancel"]["token"])
        self.assertEqual(out["status"], "done", out)
        self.assertEqual([k["key"] for k in out["kept"]], ["20261202-run"])
        self.assertEqual(set(self.garmin.library), {wid})
        self.assertEqual(self.rows(pid)["20261205-run"]["state"], "deleted")
        self.assertEqual(self.rows(pid)["20261202-run"]["state"], "scheduled")

    def test_commit_never_writes_a_done_session_again(self):
        pid, wid = self.done_today()
        _p, c2 = self.preview(sessions=self.sessions(km=12.0), plan_id=pid, content="b")
        uploads = len(self.garmin.uploads())
        out = self.commit(pid, c2["token"])
        self.assertEqual(out["status"], "done", out)
        self.assertEqual([k["key"] for k in out["kept"]], ["20261202-run"])
        self.assertEqual(len(self.garmin.uploads()), uploads, "no second workout for today's done session")
        self.assertIn(wid, self.garmin.library)
        self.assertEqual(out["replaced"], [])

    def test_cleanup_keeps_a_done_workout(self):
        pid, wid = self.done_today()
        counts = pw.cleanup(self.store, self.garmin, b.TODAY, dry_run=False, sleep=lambda s: None, join_s=2)
        self.assertEqual((counts["rows"], counts["deleted"], counts["failed"]), (1, 1, 0), counts)
        self.assertEqual(set(self.garmin.library), {wid})


class Budget(b.DeepBase):
    def test_exactly_eight_writes_finish_in_one_call(self):
        sessions = days_sessions(4)
        pid, c = self.preview(sessions=sessions)
        out = self.commit(pid, c["token"])
        self.assertEqual(out["status"], "done", out)
        self.assertEqual(len(self.garmin.writes()), 8)

    def test_nine_writes_stop_at_eight(self):
        sessions = days_sessions(5)
        pid, c = self.preview(sessions=sessions)
        out = self.commit(pid, c["token"])
        self.assertEqual((out["status"], out["retry_after_s"], out["remaining"]), ("partial", 0, 1), out)
        self.assertEqual(len(self.garmin.writes()), 8)
        self.assertEqual(self.rows(pid)["20261206-run"]["state"], "pending")
        out = self.commit(pid, c["token"])
        self.assertEqual(out["status"], "done", out)
        self.assertEqual(len(self.garmin.writes()), 10)
        self.assert_no_double_writes(5)

    def test_a_budget_cut_between_upload_and_schedule(self):
        sessions = days_sessions(4)
        pid, c = self.preview(sessions=sessions)
        svc = self.service(max_writes=7)
        out = self.commit(pid, c["token"], svc=svc)
        self.assertEqual(out["status"], "partial", out)
        self.assertEqual(self.rows(pid)["20261205-run"]["state"], "uploaded")
        out = self.commit(pid, c["token"], svc=svc)
        self.assertEqual(out["status"], "done", out)
        self.assertEqual(len(self.garmin.uploads()), 4)
        self.assert_no_double_writes(4)

    def test_the_cancel_budget(self):
        sessions = days_sessions(5)
        pid, c = self.preview(sessions=sessions)
        while self.commit(pid, c["token"])["status"] != "done":
            pass
        prev = self.cancel(pid)
        mark = len(self.garmin.writes())
        out = self.cancel(pid, prev["cancel"]["token"])
        self.assertEqual((out["status"], out["retry_after_s"]), ("partial", 0), out)
        self.assertEqual(len(self.garmin.writes()) - mark, 8)
        self.assertEqual(self.store.get_plan(self.fp, pid)["status"], "active")
        self.assertEqual(self.cancel(pid, prev["cancel"]["token"])["status"], "done")
        self.assertEqual(self.garmin.library, {})

    def _timed(self, step_s):
        """Each request takes step_s (the clock moves after it starts); writes are then spaced by a 1 s sleep, so
        the budget is checked before write k at (k-1)*step_s + (k-2) s."""
        pid, c = self.preview()
        real_log = self.garmin.client._log

        def log(step, path):
            real_log(step, path)
            self.mono[0] += step_s

        self.garmin.client._log = log
        out = self.commit(pid, c["token"])
        return out, [w[2] for w in self.garmin.write_log]

    def test_the_fifteen_second_cut_off(self):
        out, starts = self._timed(7.0)
        self.assertEqual(starts, [0.0, 8.0, 16.0], "the third write is checked at exactly 15 s: still in")
        self.assertEqual((out["status"], out["retry_after_s"]), ("partial", 0))
        self.reset()
        out, starts = self._timed(7.001)
        self.assertEqual(len(starts), 2, starts)
        self.assertEqual((out["status"], out["retry_after_s"]), ("partial", 0))

    def test_writes_are_at_least_a_second_apart(self):
        pid, c = self.preview()

        def slow(method, path):
            self.mono[0] += 0.4

        self.garmin.on_write = slow
        starts = []
        real_log = self.garmin.client._log

        def log(step, path):
            starts.append(self.mono[0])
            real_log(step, path)

        self.garmin.client._log = log
        self.assertEqual(self.commit(pid, c["token"])["status"], "done")
        ends = [s for s in starts]
        self.assertEqual(len(ends), 6)
        # A request takes 0.4 s (before it is logged); from the end of one to the start of the next >= 1 s.
        for prev_start, start in zip(starts, starts[1:]):
            self.assertGreaterEqual(round(start - prev_start, 6), 1.0 + 0.4 - 1e-9)
        self.assertEqual([round(s, 6) for s in self.sleeps], [1.0] * 5)


class Cleanup(b.DeepBase):
    def orphan(self, wid, desc, *days, linked=None):
        with self.garmin.lock:
            self.garmin.library[wid] = {"workoutId": wid, "workoutName": "Coach Easy", "description": desc}
            for i, day in enumerate(days):
                self.garmin.calendar.append({"workoutId": wid, "scheduleDate": day.isoformat(), "id": wid * 10 + i,
                                             "workoutName": "Coach Easy", "workoutType": "running",
                                             "associatedActivityId": linked})

    def snapshot(self):
        return ([dict(r) for r in self.store.all_write_rows()], dict(self.garmin.library), list(self.garmin.calendar))

    def test_dry_run_writes_nothing_anywhere(self):
        pid, _c = self.committed()
        tag = self.store.install_tag()
        self.orphan(610, f"coach-mcp {tag} {pid} 20301228-run h12345678", b.TODAY + dt.timedelta(days=3))
        before_ = self.snapshot()
        out = io.StringIO()
        seen = []

        def client():
            seen.append(garmin_paths.allowed_write_kinds())
            return self.garmin

        garmin_paths.allow_plan_writes(())
        env = {"DATABASE_URL": "postgresql://ci@localhost/x", "GARMIN_AUTH": "full"}
        code = pw.cleanup_cli(env, ["--dry-run", "--orphans"], build_client=client, build_store=lambda d: self.store,
                              out=out, today=b.TODAY)
        self.assertEqual(code, 0, out.getvalue())
        self.assertEqual(seen, [frozenset()], "a dry run allows no write")
        self.assertEqual(self.snapshot(), before_)
        self.assertEqual(self.garmin.writes()[6:], [])
        self.assertIn("would delete 4 workout(s) (3 from the plan store, 1 library orphans", out.getvalue())
        self.assertNotIn(pid, out.getvalue())
        self.assertNotIn("610", out.getvalue())

    def test_a_real_run_allows_only_the_two_deletes(self):
        pid, _c = self.committed()
        kinds = []
        self.garmin.on_write = lambda m, p: kinds.append(garmin_paths.allowed_write_kinds())
        garmin_paths.allow_plan_writes(())
        env = {"DATABASE_URL": "postgresql://ci@localhost/x", "GARMIN_AUTH": "full"}
        out = io.StringIO()
        code = pw.cleanup_cli(env, [], build_client=lambda: self.garmin, build_store=lambda d: self.store, out=out,
                              today=b.TODAY, sleep=lambda s: None)
        self.assertEqual(code, 0, out.getvalue())
        self.assertTrue(kinds)
        self.assertEqual(set(kinds), {frozenset({"unschedule", "delete"})})
        self.assertEqual(garmin_paths.allowed_write_kinds(), frozenset())
        with self.assertRaises(garmin_paths.GarminMethodRefused):
            garmin_paths.check_garmin_request("POST", "workout-service/workout", {"json": {}})

    def test_only_this_account_future_unlinked(self):
        pid, _c = self.committed()
        rows = self.rows(pid)
        self.store.workout_rows[(pid, "20261202-run")]["linked_activity_id"] = 9            # linked: kept
        self.store.workout_rows[(pid, "20261203-run")]["date_local"] = b.TODAY - dt.timedelta(days=1)   # past: kept
        self.store.workout_rows[(pid, "20261205-run")]["date_local"] = b.TODAY                # today: deleted
        # Another account's plan with a row on Garmin.
        other_fp = self.store.account_fp(4242)
        opid, _v, _ch = self.store.save_preview(other_fp, None, dt.date(2027, 3, 1), 1, {}, {}, "e" * 64)
        self.store.put_workout(opid, {"workout_key": "20261202-run", "version": 1, "date_local": b.TODAY + dt.timedelta(
            days=2), "role": "easy", "dto_hash": "d" * 64, "state": "scheduled", "garmin_workout_id": 950,
            "garmin_schedule_id": 951})
        counts = pw.cleanup(self.store, self.garmin, b.TODAY, dry_run=False, sleep=lambda s: None, join_s=2)
        self.assertEqual(counts, {"rows": 1, "orphans": 0, "other_account": 1, "deleted": 1, "failed": 0})
        self.assertEqual(set(self.garmin.library), {rows["20261202-run"]["garmin_workout_id"],
                                                     rows["20261203-run"]["garmin_workout_id"]})
        self.assertEqual(self.rows(pid)["20261205-run"]["state"], "deleted")
        self.assertEqual(self.rows(opid)["20261202-run"]["state"], "scheduled")
        self.assertFalse([w for w in self.garmin.write_log if w[1].endswith(("/950", "/951"))])

    def test_orphan_rules(self):
        pid, _c = self.committed()
        tag = self.store.install_tag()
        later, earlier = b.TODAY + dt.timedelta(days=5), b.TODAY - dt.timedelta(days=2)
        mine = f"coach-mcp {tag} {pid} 20301228-run h12345678"
        self.orphan(620, mine, later)                         # deleted
        self.orphan(621, mine, b.TODAY)                       # today: deleted
        self.orphan(622, mine, earlier, later)                # also on an earlier day: kept
        self.orphan(623, mine, later, linked=55)              # linked to an activity: kept
        self.orphan(624, mine)                                # not on the calendar: kept
        self.orphan(625, f"coach-mcp 0badc0de {pid} 20301228-run h12345678", later)    # another install: kept
        self.orphan(626, "Coach Easy coach-mcp " + tag, later)                  # marker not at the start: kept
        self.orphan(627, f"coach-mcp {tag}x {pid} 20301228-run h12345678", later)     # another tag sharing a prefix
        counts = pw.cleanup(self.store, self.garmin, b.TODAY, dry_run=True, orphans=True, sleep=lambda s: None)
        self.assertEqual(counts["orphans"], 2, counts)
        counts = pw.cleanup(self.store, self.garmin, b.TODAY, dry_run=False, orphans=True, sleep=lambda s: None,
                            join_s=2)
        self.assertEqual(counts["deleted"], 5, counts)       # 3 rows + 2 orphans
        self.assertEqual({620, 621} & set(self.garmin.library), set())
        self.assertEqual({622, 623, 624, 625, 626, 627} - set(self.garmin.library), set())

    def test_an_unknown_upload_is_left_to_the_orphan_pass(self):
        pid, c = self.preview(sessions=[b.session("20261202-run", b.TODAY + dt.timedelta(days=1))])
        self.garmin.faults["schedule"] = {1: after(b.ReadTimeout("r"))}
        self.assertEqual(self.commit(pid, c["token"])["status"], "unknown")
        counts = pw.cleanup(self.store, self.garmin, b.TODAY, dry_run=False, sleep=lambda s: None, join_s=2)
        self.assertEqual((counts["rows"], counts["deleted"]), (1, 1), "an unknown row with a workout id goes")
        self.assertEqual(self.garmin.library, {})
        self.assertEqual(self.garmin.calendar, [])

    def test_a_rate_limit_stops_and_counts(self):
        sessions = days_sessions(3)
        self.committed(sessions=sessions)
        self.garmin.faults["delete"] = {2: before(b.GarminConnectTooManyRequestsError("x"))}
        counts = pw.cleanup(self.store, self.garmin, b.TODAY, dry_run=False, sleep=lambda s: None, join_s=2)
        self.assertEqual((counts["deleted"], counts["failed"]), (1, 1), counts)
        self.assertEqual(len(self.garmin.library), 2)
        out = io.StringIO()
        env = {"DATABASE_URL": "postgresql://ci@localhost/x", "GARMIN_AUTH": "full"}
        self.garmin.faults["delete"] = {4: before(http(400))}
        code = pw.cleanup_cli(env, [], build_client=lambda: self.garmin, build_store=lambda d: self.store, out=out,
                              today=b.TODAY, sleep=lambda s: None)
        self.assertEqual(code, 1, "a failed delete is exit 1")
        self.assertIn("1 failed", out.getvalue())

    def test_a_second_run_finds_nothing(self):
        self.committed()
        pw.cleanup(self.store, self.garmin, b.TODAY, dry_run=False, sleep=lambda s: None, join_s=2)
        mark = len(self.garmin.writes())
        counts = pw.cleanup(self.store, self.garmin, b.TODAY, dry_run=False, orphans=True, sleep=lambda s: None,
                            join_s=2)
        self.assertEqual(counts, {"rows": 0, "orphans": 0, "other_account": 0, "deleted": 0, "failed": 0})
        self.assertEqual(len(self.garmin.writes()), mark)


if __name__ == "__main__":
    unittest.main()
