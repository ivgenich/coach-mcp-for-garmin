"""Security review repros for the Garmin write path (each test asserts the documented guarantee and FAILS today).

Run from the repo root:  PYTHONPATH=ext ~/.cache/coach-mcp-ci/venv-full/bin/python -m unittest rv/test_rv_writes.py -v
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "ext"))

import plan_writes as pw         # noqa: E402
import test_plan_writes as tpw   # noqa: E402
from test_plan_writes import TODAY, session   # noqa: E402


class DayBase(tpw.Base):
    """tpw.Base with a movable "today" for the WriteService (the store clock moves on its own)."""

    def setUp(self):
        self.day = TODAY
        super().setUp()

    def service(self, **options):
        async def confirm(ctx, **summary):
            self.confirm_binds.append(summary.pop("bind", None))
            self.confirm_calls.append(summary)
            return self.confirm_answer

        opts = {"sleep": lambda s: None, "join_s": 0.3, "clock": lambda: self.mono[0], **options}
        return pw.WriteService(lambda: self.garmin, lambda: self.store, lambda: self.day,
                               may_write=lambda: self.may_write, confirm=confirm, executor_options=opts)

    def deletes(self):
        return [c[1] for c in self.garmin.writes() if c[0] == "DELETE"]


class R1ResumeNextDayTouchesToday(DayBase):
    """F1: a partial commit token resumed after midnight deletes the workout that is now TODAY's, which
    commit_ops (8d5df21) would never put in a token issued that day."""

    def test_resumed_token_deletes_todays_workout(self):
        d1, d2, d3 = (TODAY + dt.timedelta(days=n) for n in (1, 2, 3))
        v1 = [session("20261202-run", d1), session("20261203-run", d2)]
        pid, c1 = self.preview(sessions=v1, content="a")
        self.assertEqual(self.commit(pid, c1["token"])["status"], "done")
        wid_d1 = self.rows(pid)["20261202-run"]["garmin_workout_id"]
        # v2: the D+1 session leaves the plan (removed delete), a new D+3 session is added (create first).
        v2 = [session("20261203-run", d2), session("20261204-run", d3)]
        _, c2 = self.preview(sessions=v2, plan_id=pid, content="b")
        self.assertEqual([o["op"] for o in c2["ops"]], ["create", "delete"])
        self.garmin.faults["create"] = [tpw.GarminConnectTooManyRequestsError("Too many")]
        first = self.commit(pid, c2["token"])
        self.assertEqual(first["status"], "partial")               # nothing deleted yet
        self.assertIn(wid_d1, self.garmin.library)
        # Next day, 20 h later (inside TOKEN_RESUME 24 h): D+1 is now today.
        self.clock.advance(hours=20)
        self.day = d1
        # What a token issued today would do: commit_ops never deletes today's workout.
        fresh = pw.commit_ops(tpw.plan_answer(v2, "b"), self.store.write_rows(pid), today=d1, days=14,
                              tag=self.store.install_tag(), plan_id=pid, fixed_hard=[], lt_speed=None)
        self.assertNotIn("20261202-run", [o["key"] for o in fresh["ops"] if o["op"] == "delete"])
        again = self.commit(pid, c2["token"])
        print("\n[R1] resumed answer:", json.dumps({k: again.get(k) for k in ("status", "written", "remaining")}),
              "\n[R1] deleted today's workout:", any(p.endswith(f"/{wid_d1}") for p in self.deletes()))
        self.assertIn(wid_d1, self.garmin.library, "today's workout was deleted by a token resumed past midnight")


class R2CancelDeletesToday(DayBase):
    """F2: plan_cancel deletes today's unlinked workout (cancel_ops takes date_local >= today)."""

    def test_cancel_deletes_todays_workout(self):
        pid, c = self.preview()
        self.assertEqual(self.commit(pid, c["token"])["status"], "done")
        wid = self.rows(pid)["20261202-run"]["garmin_workout_id"]
        self.day = TODAY + dt.timedelta(days=1)      # 2026-12-02: that workout is today's now (not linked)
        prev = self.cancel(pid)
        print("\n[R2] cancel preview ops:", [(o["date"], o["key"]) for o in prev["cancel"]["ops"]],
              "kept:", prev["kept"])
        out = self.cancel(pid, prev["cancel"]["token"])
        print("[R2] cancel answer:", out["status"], "deleted:", out["deleted"])
        self.assertIn(wid, self.garmin.library, "plan_cancel deleted today's workout")


class R3LibraryPageDoubleUpload(DayBase):
    """F3: an `unknown` create is reconciled against the first LIBRARY_LIMIT (100) workouts only; on an account
    with more workouts and a listing that does not put the new one first, a landed POST is uploaded again."""

    def test_late_landed_create_is_uploaded_twice(self):
        with self.garmin.lock:
            for n in range(100):                       # 100 older workouts of the user's own
                self.garmin.library[100 + n] = {"workoutId": 100 + n, "workoutName": f"Mine {n}",
                                                "description": "the user's own"}
        orig = self.garmin.get_workouts

        def paged(start, limit):                       # Garmin answers one page: oldest first here
            return sorted(orig(start, limit), key=lambda w: w["workoutId"])[start:start + limit]
        self.garmin.get_workouts = paged
        pid, c = self.preview(sessions=[session("20261202-run", TODAY + dt.timedelta(days=1))])
        self.garmin.faults["create"] = ["block"]       # the POST outlives the call (read timeout), lands later
        out = self.commit(pid, c["token"])
        self.assertEqual(out["status"], "unknown")
        self.garmin.blocked.set()
        self.assertTrue(pw.join_write_threads(5))      # it landed
        self.clock.advance(minutes=5)                  # quarantine over
        again = self.commit(pid, c["token"])
        tag = self.store.install_tag()
        copies = [w for w in self.garmin.library.values() if str(w.get("description", "")).startswith(
            f"coach-mcp {tag} {pid} 20261202-run")]
        print("\n[R3] second call:", again["status"], "| copies of the workout in the library:", len(copies),
              "| creates sent:", sum(1 for c_ in self.garmin.writes() if c_ == ("POST", "workout-service/workout")))
        self.assertEqual(len(copies), 1, "one token wrote the same workout twice")


class R4DeleteOutsideTokenOps(DayBase):
    """F4: Executor.run's retired-row sweep deletes a workout the token's ops (and the confirmation) never named."""

    def test_sweep_deletes_unlisted_workout(self):
        d1 = TODAY + dt.timedelta(days=1)
        pid, c1 = self.preview(sessions=[session("20261202-run", d1)], content="a")
        self.garmin.faults["schedule"] = ["block"]     # the schedule POST outlives the call: row unknown, has wid
        self.assertEqual(self.commit(pid, c1["token"])["status"], "unknown")
        self.garmin.blocked.set()
        self.assertTrue(pw.join_write_threads(5))
        old = self.rows(pid)["20261202-run"]["garmin_workout_id"]
        _, c2 = self.preview(sessions=[session("20261202-run", d1, km=10.0)], plan_id=pid, content="b")
        print("\n[R4] token ops:", [(o["op"], o["key"]) for o in c2["ops"]])
        self.assertEqual([o["op"] for o in c2["ops"]], ["create"])    # no delete is listed
        self.clock.advance(minutes=5)
        self.confirm_answer = "accepted"
        out = self.commit(pid, c2["token"])
        print("[R4] confirmation summary shown:", self.confirm_calls[-1])
        print("[R4] answer:", out["status"], "| DELETEs sent:", self.deletes())
        self.assertFalse(any(p.endswith(f"/{old}") for p in self.deletes()),
                         "a workout outside the token's ops was deleted")


class R5AdoptsRetiredTwin(DayBase):
    """F5: reconcile adopts the FIRST library workout whose marker matches. A day replaced back to an earlier
    content while that content's old workout is still on Garmin (retired row K~h) has two workouts with the same
    marker; the unknown create adopts the old one, the same call then deletes it as the retired row, and the
    plan's row says `scheduled` for a workout that no longer exists (the landed one stays as an orphan)."""

    def test_unknown_create_adopts_retired_workout_then_deletes_it(self):
        d1 = TODAY + dt.timedelta(days=1)
        key = "20261202-run"
        pid, c1 = self.preview(sessions=[session(key, d1, km=8.0)], content="a")
        self.assertEqual(self.commit(pid, c1["token"])["status"], "done")
        # v2: content B; Garmin refuses unscheduling the old workout (4xx): the retired row stays on Garmin.
        _, c2 = self.preview(sessions=[session(key, d1, km=10.0)], plan_id=pid, content="b")
        self.garmin.faults["delete"] = [ConnectionError("API Error 400 - Bad Request")]
        self.commit(pid, c2["token"])
        # v3: back to content A (same DTO hash as v1); its upload times out and lands late.
        _, c3 = self.preview(sessions=[session(key, d1, km=8.0)], plan_id=pid, content="c")
        self.garmin.faults["create"] = ["block"]
        self.assertEqual(self.commit(pid, c3["token"])["status"], "unknown")
        self.garmin.blocked.set()
        self.assertTrue(pw.join_write_threads(5))
        self.clock.advance(minutes=5)
        out = self.commit(pid, c3["token"])
        row = self.rows(pid)[key]
        print("\n[R5] answer:", out["status"], "| row:", row["state"], row["garmin_workout_id"],
              "| library ids:", sorted(self.garmin.library),
              "| calendar ids:", sorted(e["workoutId"] for e in self.garmin.calendar))
        self.assertIn(row["garmin_workout_id"], self.garmin.library,
                      "the plan's row points at a workout this same call deleted")


if __name__ == "__main__":
    unittest.main()
