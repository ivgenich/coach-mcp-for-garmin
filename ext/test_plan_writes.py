"""plan_writes: plan_commit, plan_cancel, the tokens and the write helper on a fake Garmin and MemoryPlanStore.

Standard library only. A fake write that "hangs" blocks on an Event; every test releases it and joins the
helper's threads in its cleanup, so no thread outlives the test (ci-unittest fails on leaked threads).
"""

from __future__ import annotations

import asyncio
import copy
import datetime as dt
import io
import json
import re
import threading
import types
import unittest

import garmin_paths
import plan_dto
import plan_engine as pe
import plan_store as ps
import plan_writes as pw

TODAY = dt.date(2026, 12, 1)
PROFILE = 123456
T0 = dt.datetime(2026, 12, 1, 7, 0, tzinfo=dt.timezone.utc)


class GarminConnectTooManyRequestsError(Exception):
    __module__ = "garminconnect.exceptions"


class ConnectTimeout(Exception):
    """Same class name as requests' (classify reads the names in the MRO)."""


class ReadTimeout(Exception):
    pass


class Resp:
    def __init__(self, payload):
        self.status_code = 200
        self._payload = payload

    def json(self):
        return copy.deepcopy(self._payload)


def session(key, day, role="easy", km=8.0, hard=False):
    steps = [{"type": "interval", "distance_m": km * 1000, "speed_mps": [2.7, 3.1]}]
    if role == "threshold":
        steps = [{"type": "warmup", "distance_m": 2000, "speed_mps": [2.7, 3.1]},
                 {"type": "interval", "distance_m": 6000, "speed_mps": [3.9, 4.1]},
                 {"type": "cooldown", "distance_m": 2000, "speed_mps": [2.7, 3.1]}]
    return {"key": key, "date": day.isoformat(), "role": role, "km": km, "minutes": int(km * 6),
            "hard_or_long": hard, "steps": steps}


def plan_answer(sessions, content="a"):
    return {"weeks": [{"sessions": sessions}], "invariants": {"ok": True}, "content_hash": (content * 64)[:64]}


def default_sessions():
    d = TODAY
    return [session("w01-e1", d + dt.timedelta(days=1)),
            session("w01-q1", d + dt.timedelta(days=2), role="threshold", km=10.0, hard=True),
            session("w01-e2", d + dt.timedelta(days=4))]


class FakeRaw:
    """garminconnect's Client: post/delete with scripted faults."""

    def __init__(self, garmin):
        self.g = garmin

    def _fault(self, kind):
        queue = self.g.faults.get(kind) or []
        if queue:
            fault = queue.pop(0)
            if isinstance(fault, BaseException):
                raise fault
            if fault == "block":
                self.g.blocked.wait(10)
                return "late"
        return None

    def post(self, domain, path, json=None, timeout=None, **kwargs):
        assert domain == "connectapi" and timeout is not None
        garmin_paths.check_garmin_request("POST", path, {"json": json})
        self.g.calls.append(("POST", path))
        if path == "workout-service/workout":
            self._fault("create")
            with self.g.lock:
                wid = self.g.next_id()
                self.g.library[wid] = {"workoutId": wid, "workoutName": json["workoutName"],
                                       "description": json.get("description"), "dto": json}
            return Resp({"workoutId": wid, "workoutName": json["workoutName"]})
        m = re.fullmatch(r"workout-service/schedule/(\d+)", path)
        assert m, path
        self._fault("schedule")
        wid = int(m.group(1))
        with self.g.lock:
            sid = self.g.next_id()
            w = self.g.library[wid]
            self.g.calendar.append({"workoutId": wid, "scheduleDate": json["date"], "workoutName": w["workoutName"],
                                    "workoutType": "running", "id": sid})
        return Resp({"workoutScheduleId": sid, "workout": {"workoutId": wid}})

    def delete(self, domain, path, timeout=None, **kwargs):
        garmin_paths.check_garmin_request("DELETE", path, {})
        self.g.calls.append(("DELETE", path))
        m = re.fullmatch(r"workout-service/(schedule|workout)/(\d+)", path)
        assert m, path
        self._fault("delete")
        n = int(m.group(2))
        with self.g.lock:
            if m.group(1) == "schedule":
                before = len(self.g.calendar)
                self.g.calendar = [e for e in self.g.calendar if e.get("id") != n]
                if len(self.g.calendar) == before:
                    raise ConnectionError("API Error 404 - Not Found")
            else:
                if n not in self.g.library:
                    raise ConnectionError("API Error 404 - Not Found")
                del self.g.library[n]
                self.g.calendar = [e for e in self.g.calendar if e.get("workoutId") != n]
        return Resp({})


class FakeGarmin:
    """upstream's Garmin client as plan_writes sees it: reads on the object, raw writes on .client."""

    def __init__(self):
        self.lock = threading.Lock()
        self.calls = []
        self.faults = {}
        self.read_faults = {}
        self.blocked = threading.Event()
        self.library = {}
        self.calendar = []
        self._id = 9000
        self.profile = PROFILE
        self.client = FakeRaw(self)

    def next_id(self):
        self._id += 1
        return self._id

    def _read(self, name):
        self.calls.append(("READ", name))
        fault = self.read_faults.get(name)
        if fault is not None:
            raise fault

    def get_stats(self, day):
        self._read("stats")
        return {"userProfileId": self.profile}

    def query_garmin_graphql(self, query):
        self._read("calendar")
        assert query["query"].startswith("query{workoutScheduleSummariesScalar")
        with self.lock:
            return {"data": {"workoutScheduleSummariesScalar": copy.deepcopy(self.calendar)}}

    def get_workouts(self, start, limit):
        self._read("library")
        with self.lock:
            return [{k: v for k, v in w.items() if k != "dto"} for w in self.library.values()]

    def writes(self):
        return [c for c in self.calls if c[0] != "READ"]


class Clock:
    def __init__(self, start=T0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, **kw):
        self.now += dt.timedelta(**kw)


def run(coro):
    return asyncio.run(coro)


class Base(unittest.TestCase):
    def setUp(self):
        garmin_paths.allow_plan_writes(set(garmin_paths.WRITE_PATHS))
        self.addCleanup(garmin_paths.allow_plan_writes, ())
        self.garmin = FakeGarmin()
        self.addCleanup(self._release)
        self.clock = Clock()
        self.store = ps.MemoryPlanStore(clock=self.clock)
        self.fp = self.store.account_fp(PROFILE)
        self.store.meta[ps.META_ACCOUNT] = self.fp
        self.confirm_answer = "unsupported"
        self.confirm_calls = []
        self.may_write = True
        self.mono = [0.0]
        self.svc = self.service()

    def _release(self):
        self.garmin.blocked.set()
        self.assertTrue(pw.join_write_threads(10))

    def service(self, **options):
        async def confirm(ctx, **summary):
            self.confirm_calls.append(summary)
            if isinstance(self.confirm_answer, BaseException):
                raise self.confirm_answer
            return self.confirm_answer

        opts = {"sleep": lambda s: None, "join_s": 0.3, "clock": lambda: self.mono[0], **options}
        return pw.WriteService(lambda: self.garmin, lambda: self.store, lambda: TODAY,
                               may_write=lambda: self.may_write, confirm=confirm, executor_options=opts)

    def preview(self, sessions=None, plan_id=None, content="a", days=14, fixed=(), lt=None):
        answer = plan_answer(sessions if sessions is not None else default_sessions(), content)
        pid, version, changed = self.store.save_preview(self.fp, plan_id, dt.date(2027, 2, 26), 1, {"x": 1}, answer,
                                                        answer["content_hash"])
        snap = types.SimpleNamespace(fixed=fixed, lt_speed_mps=lt)
        commit = pw.commit_section(self.store, answer, snap, {"plan_id": pid, "version": version}, TODAY, days,
                                   PROFILE)
        return pid, commit

    def commit(self, pid, token):
        return json.loads(run(self.svc.commit(plan_id=pid, commit_token=token)))

    def cancel(self, pid, token=""):
        return json.loads(run(self.svc.cancel(plan_id=pid, cancel_token=token)))

    def rows(self, pid):
        return {r["workout_key"]: r for r in self.store.write_rows(pid)}


class Commit(Base):
    def test_commit_writes_every_session_once(self):
        pid, commit = self.preview()
        self.assertTrue(commit["token"].startswith("ct1."))
        self.assertEqual([o["op"] for o in commit["ops"]], ["create"] * 3)
        self.assertEqual(commit["dates"], ["2026-12-02", "2026-12-03", "2026-12-05"])
        out = self.commit(pid, commit["token"])
        self.assertEqual(out["status"], "done", out)
        self.assertEqual(out["confirmed_by"], "token")
        self.assertEqual([w["key"] for w in out["written"]], ["w01-e1", "w01-q1", "w01-e2"])
        self.assertEqual(len(self.garmin.library), 3)
        self.assertEqual(len(self.garmin.calendar), 3)
        self.assertEqual(len(self.garmin.writes()), 6)
        rows = self.rows(pid)
        self.assertEqual({r["state"] for r in rows.values()}, {"scheduled"})
        self.assertTrue(all(r["garmin_workout_id"] and r["garmin_schedule_id"] for r in rows.values()))
        self.assertEqual(self.store.get_plan(self.fp, pid)["status"], "active")
        tag = self.store.install_tag()
        for w in self.garmin.library.values():
            self.assertTrue(w["description"].startswith(f"coach-mcp {tag} {pid} w01-"))
            self.assertTrue(w["workoutName"].startswith("Coach "))

    def test_same_token_twice_calls_garmin_no_more(self):
        pid, commit = self.preview()
        first = self.commit(pid, commit["token"])
        calls = len(self.garmin.calls)
        again = self.commit(pid, commit["token"])
        self.assertEqual(len(self.garmin.calls), calls)
        self.assertTrue(again.pop("replayed"))
        self.assertEqual(again, first)
        self.assertEqual(len(self.garmin.library), 3)

    def test_a_second_preview_with_nothing_new_has_no_token(self):
        pid, commit = self.preview()
        self.commit(pid, commit["token"])
        _pid, again = self.preview(plan_id=pid)
        self.assertIsNone(again["token"])
        self.assertEqual(again["ops"], [])

    def test_no_token_when_a_rule_check_fails_or_not_stored(self):
        answer = plan_answer(default_sessions())
        answer["invariants"] = {"ok": False}
        snap = types.SimpleNamespace(fixed=(), lt_speed_mps=None)
        self.assertIsNone(pw.commit_section(self.store, answer, snap, {"plan_id": None}, TODAY, 3, PROFILE)["token"])
        pid, _ = self.preview()
        out = pw.commit_section(self.store, answer, snap, {"plan_id": pid, "version": 1}, TODAY, 3, PROFILE)
        self.assertIsNone(out["token"])
        self.assertIn("invariants", out["why"])

    def test_commit_days_limit_the_window(self):
        pid, commit = self.preview(days=2)
        self.assertEqual([o["key"] for o in commit["ops"]], ["w01-e1"])
        for bad in (0, 22, "x", True):
            with self.assertRaises(pw.BadInput):
                pw.parse_commit_days(bad)
        self.assertEqual(pw.parse_commit_days(None), 7)

    def test_429_on_upload_is_partial_then_resumes(self):
        pid, commit = self.preview()
        self.garmin.faults["create"] = [None, GarminConnectTooManyRequestsError("Too many")]
        out = self.commit(pid, commit["token"])
        self.assertEqual(out["status"], "partial", out)
        self.assertEqual(out["retry_after_s"], 900)
        self.assertIn("15 minutes", out["note"])
        self.assertEqual(out["remaining"], 2)
        rows = self.rows(pid)
        self.assertEqual(rows["w01-e1"]["state"], "scheduled")
        self.assertEqual(rows["w01-q1"]["state"], "pending")
        self.assertEqual(self.store.tokens[ps.token_hash(commit["token"])]["status"], "partial")
        again = self.commit(pid, commit["token"])
        self.assertEqual(again["status"], "done", again)
        self.assertEqual(len(self.garmin.library), 3)
        self.assertEqual(len(self.garmin.calendar), 3)

    def test_429_on_schedule_keeps_the_workout_id(self):
        pid, commit = self.preview()
        self.garmin.faults["schedule"] = [ConnectionError("API Error 429 - Too Many Requests")]
        out = self.commit(pid, commit["token"])
        self.assertEqual((out["status"], out["retry_after_s"]), ("partial", 900))
        row = self.rows(pid)["w01-e1"]
        self.assertEqual(row["state"], "uploaded")
        wid = row["garmin_workout_id"]
        again = self.commit(pid, commit["token"])
        self.assertEqual(again["status"], "done")
        self.assertEqual(self.rows(pid)["w01-e1"]["garmin_workout_id"], wid)
        self.assertEqual(len(self.garmin.library), 3)
        self.assertEqual(len(self.garmin.calendar), 3)

    def test_connect_timeout_is_never_sent_and_read_timeout_is_unknown(self):
        pid, commit = self.preview()
        self.garmin.faults["create"] = [ConnectTimeout("connect")]
        out = self.commit(pid, commit["token"])
        self.assertEqual(out["status"], "partial", out)
        self.assertEqual(self.rows(pid)["w01-e1"]["state"], "pending")
        self.garmin.faults["create"] = [ReadTimeout("read")]
        out = self.commit(pid, commit["token"])
        self.assertEqual(out["status"], "unknown", out)
        self.assertEqual(out["retry_after_s"], pw.RETRY_AFTER_UNKNOWN_S)
        self.assertEqual(self.rows(pid)["w01-e1"]["state"], "unknown")
        self.assertEqual(len(self.garmin.library), 0)

    def test_a_late_post_after_the_timeout_leaves_one_workout(self):
        """The first upload hangs past the helper's wait and lands afterwards; the retry adopts it by its marker
        instead of uploading again: one workout and one calendar entry."""
        pid, commit = self.preview(sessions=[session("w01-e1", TODAY + dt.timedelta(days=1))])
        self.garmin.faults["create"] = ["block"]
        out = self.commit(pid, commit["token"])
        self.assertEqual(out["status"], "unknown", out)
        self.assertEqual(self.rows(pid)["w01-e1"]["state"], "unknown")
        # Called again while the thread still runs: nothing is written, the row waits.
        again = self.commit(pid, commit["token"])
        self.assertEqual(again["status"], "partial", again)
        self.assertEqual(again["retry_after_s"], pw.RETRY_AFTER_UNKNOWN_S)
        self.garmin.blocked.set()
        self.assertTrue(pw.join_write_threads(10))
        self.assertEqual(len(self.garmin.library), 1)
        # The thread ended, but the quarantine has not passed yet.
        self.assertEqual(self.commit(pid, commit["token"])["status"], "partial")
        self.clock.advance(minutes=3)
        final = self.commit(pid, commit["token"])
        self.assertEqual(final["status"], "done", final)
        self.assertEqual(len(self.garmin.library), 1)
        self.assertEqual(len(self.garmin.calendar), 1)
        self.assertEqual([c for c in self.garmin.writes() if c[1] == "workout-service/workout"],
                         [("POST", "workout-service/workout")])

    def test_an_unknown_upload_that_never_landed_is_sent_again(self):
        pid, commit = self.preview(sessions=[session("w01-e1", TODAY + dt.timedelta(days=1))])
        self.garmin.faults["create"] = [ReadTimeout("read")]
        self.assertEqual(self.commit(pid, commit["token"])["status"], "unknown")
        self.clock.advance(minutes=3)
        self.assertEqual(self.commit(pid, commit["token"])["status"], "done")
        self.assertEqual(len(self.garmin.library), 1)

    def test_an_unknown_schedule_is_settled_from_the_calendar(self):
        pid, commit = self.preview(sessions=[session("w01-e1", TODAY + dt.timedelta(days=1))])
        self.garmin.faults["schedule"] = ["block"]
        self.assertEqual(self.commit(pid, commit["token"])["status"], "unknown")
        self.garmin.blocked.set()
        self.assertTrue(pw.join_write_threads(10))
        self.clock.advance(minutes=3)
        out = self.commit(pid, commit["token"])
        self.assertEqual(out["status"], "done", out)
        self.assertEqual(len(self.garmin.calendar), 1)
        self.assertEqual(self.rows(pid)["w01-e1"]["state"], "scheduled")

    def test_a_5xx_is_unknown_and_a_4xx_is_failed(self):
        pid, commit = self.preview()
        self.garmin.faults["create"] = [ConnectionError("API Error 400 - bad"), ConnectionError("API Error 502")]
        out = self.commit(pid, commit["token"])
        self.assertEqual(out["status"], "unknown", out)
        self.assertEqual(out["written"], [])
        rows = self.rows(pid)
        self.assertEqual((rows["w01-e1"]["state"], rows["w01-q1"]["state"]), ("failed", "unknown"))

    def test_calendar_read_fails_nothing_is_scheduled(self):
        pid, commit = self.preview()
        self.garmin.read_faults["calendar"] = ConnectionError("API Error 503")
        out = self.commit(pid, commit["token"])
        self.assertEqual((out["status"], out["reason"]), ("refused", "calendar_unreadable"))
        self.assertEqual(self.garmin.writes(), [])
        self.assertEqual(self.store.tokens[ps.token_hash(commit["token"])]["status"], "issued")
        del self.garmin.read_faults["calendar"]
        self.assertEqual(self.commit(pid, commit["token"])["status"], "done")

    def test_a_graphql_error_answer_is_unreadable_too(self):
        pid, commit = self.preview()
        self.garmin.query_garmin_graphql = lambda q: {"data": None, "errors": [{}]}
        self.assertEqual(self.commit(pid, commit["token"])["reason"], "calendar_unreadable")
        self.assertEqual(self.garmin.writes(), [])

    def test_a_rate_limited_read_says_when(self):
        pid, commit = self.preview()
        self.garmin.read_faults["stats"] = GarminConnectTooManyRequestsError("Too many")
        out = self.commit(pid, commit["token"])
        self.assertEqual((out["reason"], out["retry_after_s"]), ("garmin_unreadable", 900))

    def test_another_account_is_refused(self):
        pid, commit = self.preview()
        self.garmin.profile = 999
        self.assertEqual(self.commit(pid, commit["token"])["reason"], "other_account")
        self.assertEqual(self.garmin.writes(), [])

    def test_superseded_version_on_first_use_and_on_resume(self):
        pid, commit = self.preview()
        self.garmin.faults["create"] = [GarminConnectTooManyRequestsError("x")]
        self.assertEqual(self.commit(pid, commit["token"])["status"], "partial")
        self.preview(plan_id=pid, content="b")
        calls = len(self.garmin.calls)
        out = self.commit(pid, commit["token"])
        self.assertEqual((out["status"], out["reason"]), ("refused", "superseded"))
        self.assertEqual(len(self.garmin.calls), calls)

    def test_conflict_is_skipped(self):
        day = (TODAY + dt.timedelta(days=4)).isoformat()
        self.garmin.calendar.append({"workoutId": 777, "scheduleDate": day, "workoutName": "TP easy run",
                                     "workoutType": "running", "id": 778})
        pid, commit = self.preview()
        out = self.commit(pid, commit["token"])
        self.assertEqual(out["status"], "done", out)
        self.assertEqual(out["conflicts"], [{"key": "w01-e2", "date": day}])
        self.assertEqual(self.rows(pid)["w01-e2"]["state"], "conflict")
        self.assertEqual(len(self.garmin.library), 2)

    def test_calendar_changed_refuses(self):
        pid, commit = self.preview(lt=3.9)
        day = (TODAY + dt.timedelta(days=3)).isoformat()
        self.garmin.calendar.append({"workoutId": 555, "scheduleDate": day, "workoutName": "Long ride",
                                     "workoutType": "cycling", "estimatedDurationInSecs": 4 * 3600, "id": 556})
        out = self.commit(pid, commit["token"])
        self.assertEqual(out["reason"], "calendar_changed")
        self.assertEqual(self.garmin.writes(), [])

    def test_a_long_session_seen_at_preview_is_no_change(self):
        day = TODAY + dt.timedelta(days=3)
        self.garmin.calendar.append({"workoutId": 555, "scheduleDate": day.isoformat(), "workoutName": "Long ride",
                                     "workoutType": "cycling", "estimatedDurationInSecs": 4 * 3600, "id": 556})
        pid, commit = self.preview(fixed=(pe.FixedSession(date=day, sport="bike", long=True),), lt=3.9)
        self.assertEqual(self.commit(pid, commit["token"])["status"], "done")

    def test_budget_stops_at_eight_writes_then_continues(self):
        days = [TODAY + dt.timedelta(days=i) for i in range(1, 6)]
        pid, commit = self.preview(sessions=[session(f"w01-e{i}", d) for i, d in enumerate(days, 1)])
        out = self.commit(pid, commit["token"])
        self.assertEqual(out["status"], "partial", out)
        self.assertEqual(out["retry_after_s"], 0)
        self.assertIn("call this tool again", out["note"].lower())
        self.assertEqual(len(self.garmin.writes()), 8)
        again = self.commit(pid, commit["token"])
        self.assertEqual(again["status"], "done", again)
        self.assertEqual(len(self.garmin.writes()), 10)
        self.assertEqual(len(self.garmin.calendar), 5)

    def test_time_budget_stops_before_the_next_write(self):
        svc = self.service(clock=lambda: self.mono[0], write_start_s=15.0)
        pid, commit = self.preview()
        orig = self.garmin.client.post

        def slow(*a, **k):
            self.mono[0] += 10.0
            return orig(*a, **k)

        self.garmin.client.post = slow
        out = json.loads(run(svc.commit(plan_id=pid, commit_token=commit["token"])))
        self.assertEqual(out["status"], "partial")
        self.assertEqual(len(self.garmin.writes()), 2)

    def test_expired_and_unknown_tokens_are_refused_before_garmin(self):
        pid, commit = self.preview()
        self.clock.advance(minutes=31)
        self.assertEqual(self.commit(pid, commit["token"])["reason"], "expired")
        other = "ct1." + "A" * 32
        self.assertEqual(self.commit(pid, other)["reason"], "unknown_token")
        self.assertEqual(self.garmin.calls, [])

    def test_a_started_token_resumes_for_a_day(self):
        pid, commit = self.preview()
        self.garmin.faults["create"] = [GarminConnectTooManyRequestsError("x")]
        self.commit(pid, commit["token"])
        self.clock.advance(hours=23)
        self.assertEqual(self.commit(pid, commit["token"])["status"], "done")
        pid2, commit2 = self.preview(sessions=[session("w02-e1", TODAY + dt.timedelta(days=8))], content="c")
        self.garmin.faults["create"] = [GarminConnectTooManyRequestsError("x")]
        self.store.move_plan(pid, "cancelled")
        self.commit(pid2, commit2["token"])
        self.clock.advance(hours=25)
        self.assertEqual(self.commit(pid2, commit2["token"])["reason"], "expired")

    def test_no_write_permission_refuses_before_anything(self):
        self.may_write = False
        pid, commit = self.preview()
        out = self.commit(pid, commit["token"])
        self.assertEqual((out["status"], out["reason"]), ("refused", "no_write_permission"))
        self.assertEqual(self.cancel(pid)["reason"], "no_write_permission")
        self.assertEqual(self.garmin.calls, [])
        self.assertEqual(self.confirm_calls, [])
        self.assertEqual(self.store.tokens[ps.token_hash(commit["token"])]["status"], "issued")

    def test_bad_arguments(self):
        out = run(self.svc.commit(plan_id="x", commit_token="ct1." + "A" * 32))
        self.assertTrue(out.startswith("Error: Invalid input: plan_id"))
        pid, _ = self.preview()
        out = run(self.svc.commit(plan_id=pid, commit_token="xt1." + "A" * 32))
        self.assertTrue(out.startswith("Error: Invalid input: commit_token"))
        out = run(self.svc.cancel(plan_id=pid, cancel_token="nope"))
        self.assertTrue(out.startswith("Error: Invalid input: cancel_token"))

    def test_another_active_plan_is_refused(self):
        pid, commit = self.preview()
        self.commit(pid, commit["token"])
        pid2, commit2 = self.preview(sessions=[session("w02-e1", TODAY + dt.timedelta(days=8))], content="c")
        out = self.commit(pid2, commit2["token"])
        self.assertEqual(out["reason"], "another_plan_active")
        self.assertEqual(len(self.garmin.library), 3)

    def test_a_store_failure_answers_a_fixed_text(self):
        pid, commit = self.preview()

        class Broken:
            def token_row(self, *args):
                raise ps.StoreError("plans: database error")

        svc = pw.WriteService(lambda: self.garmin, lambda: Broken(), lambda: TODAY, may_write=lambda: True,
                              confirm=None)
        with self.assertLogs("garmin_ext.plans", "WARNING"):
            self.assertEqual(run(svc.commit(plan_id=pid, commit_token=commit["token"])), pw.TEXT_DB)

    def test_an_unexpected_failure_is_unknown_never_raised(self):
        pid, commit = self.preview()
        self.garmin.client.post = lambda *a, **k: (_ for _ in ()).throw(KeyError("bug"))
        out = self.commit(pid, commit["token"])
        self.assertEqual(out["status"], "unknown", out)

        def boom(*a, **k):
            raise RuntimeError("TEST-ONLY")

        self.store.claim_token = boom
        pid2 = self.preview(sessions=[session("w02-e1", TODAY + dt.timedelta(days=8))], content="c")[0]
        del pid2
        with self.assertLogs("garmin_ext.plans", "ERROR") as logs:
            out = self.commit(pid, commit["token"])
        self.assertEqual(out["status"], "unknown")
        self.assertNotIn("TEST-ONLY", "\n".join(logs.output))

    def test_a_concurrent_call_for_the_same_plan_is_busy(self):
        pid, commit = self.preview()
        lock = self.svc._lock(pid)
        lock.acquire()
        try:
            self.assertEqual(self.commit(pid, commit["token"])["reason"], "busy")
        finally:
            lock.release()


class Replace(Base):
    def test_a_changed_day_is_replaced_new_first(self):
        pid, commit = self.preview()
        self.commit(pid, commit["token"])
        old = self.rows(pid)["w01-e2"]
        changed = default_sessions()
        changed[2] = session("w01-e2", TODAY + dt.timedelta(days=5), km=9.0)
        _pid, commit2 = self.preview(sessions=changed, plan_id=pid, content="b")
        self.assertEqual([(o["op"], o["key"]) for o in commit2["ops"]],
                         [("create", "w01-e2"), ("delete", f"w01-e2~{old['dto_hash'][:8]}")])
        mark = len(self.garmin.calls)
        out = self.commit(pid, commit2["token"])
        self.assertEqual(out["status"], "done", out)
        self.assertEqual(out["replaced"], [{"key": "w01-e2", "date": old["date_local"].isoformat()}])
        writes = [c for c in self.garmin.calls[mark:] if c[0] != "READ"]
        self.assertEqual([c[0] for c in writes], ["POST", "POST", "DELETE", "DELETE"])
        self.assertNotIn(old["garmin_workout_id"], self.garmin.library)
        self.assertEqual(len(self.garmin.calendar), 3)
        rows = self.rows(pid)
        self.assertEqual(rows[f"w01-e2~{old['dto_hash'][:8]}"]["state"], "deleted")
        self.assertEqual(rows["w01-e2"]["state"], "scheduled")

    def test_a_day_that_left_the_plan_is_deleted_and_a_linked_one_kept(self):
        pid, commit = self.preview()
        self.commit(pid, commit["token"])
        self.store.workout_rows[(pid, "w01-e1")]["linked_activity_id"] = 42
        _pid, commit2 = self.preview(sessions=default_sessions()[1:2], plan_id=pid, content="b")
        self.assertEqual([(o["op"], o["key"]) for o in commit2["ops"]], [("delete", "w01-e2")])
        self.assertEqual(self.commit(pid, commit2["token"])["status"], "done")
        self.assertEqual(len(self.garmin.calendar), 2)


class Cancel(Base):
    def other_workouts(self):
        with self.garmin.lock:
            for wid, name, desc, days, sport in (
                    (501, "Coach Easy", "coach-mcp ffffffff " + "0" * 32 + " w01-e1 h00000000", 6, "running"),
                    (502, "TP Ride", "from TrainingPeaks", 4, "cycling")):
                self.garmin.library[wid] = {"workoutId": wid, "workoutName": name, "description": desc}
                self.garmin.calendar.append({"workoutId": wid, "workoutName": name, "workoutType": sport,
                                             "scheduleDate": (TODAY + dt.timedelta(days=days)).isoformat(),
                                             "id": wid + 1000})

    def test_cancel_previews_then_deletes_only_ours(self):
        self.other_workouts()
        pid, commit = self.preview()
        self.assertEqual(self.commit(pid, commit["token"])["status"], "done")
        writes_before = len(self.garmin.writes())
        prev = self.cancel(pid)
        self.assertEqual(prev["status"], "preview")
        self.assertEqual(len(self.garmin.writes()), writes_before)
        self.assertTrue(prev["cancel"]["token"].startswith("xt1."))
        self.assertEqual(len(prev["cancel"]["ops"]), 3)
        out = self.cancel(pid, prev["cancel"]["token"])
        self.assertEqual(out["status"], "done", out)
        self.assertEqual(len(out["deleted"]), 3)
        self.assertEqual(set(self.garmin.library), {501, 502})
        self.assertEqual({e["workoutId"] for e in self.garmin.calendar}, {501, 502})
        self.assertEqual(self.store.get_plan(self.fp, pid)["status"], "cancelled")
        deleted = {c[1] for c in self.garmin.writes() if c[0] == "DELETE"}
        self.assertFalse({p for p in deleted if p.endswith(("/501", "/502", "/1501", "/1502"))})

    def test_cancel_keeps_linked_and_past_workouts(self):
        pid, commit = self.preview()
        self.commit(pid, commit["token"])
        self.store.workout_rows[(pid, "w01-e1")]["linked_activity_id"] = 42
        self.store.workout_rows[(pid, "w01-q1")]["date_local"] = TODAY - dt.timedelta(days=1)
        prev = self.cancel(pid)
        self.assertEqual({k["reason"] for k in prev["kept"]}, {"linked to an activity", "in the past"})
        self.assertEqual([o["key"] for o in prev["cancel"]["ops"]], ["w01-e2"])
        self.assertEqual(self.cancel(pid, prev["cancel"]["token"])["status"], "done")
        self.assertEqual(len(self.garmin.library), 2)

    def test_a_gone_workout_counts_as_deleted(self):
        pid, commit = self.preview()
        self.commit(pid, commit["token"])
        with self.garmin.lock:
            self.garmin.library.clear()
            self.garmin.calendar.clear()
        prev = self.cancel(pid)
        out = self.cancel(pid, prev["cancel"]["token"])
        self.assertEqual(out["status"], "done", out)
        self.assertEqual({r["state"] for r in self.rows(pid).values()}, {"deleted"})

    def test_a_429_on_delete_is_partial(self):
        pid, commit = self.preview()
        self.commit(pid, commit["token"])
        prev = self.cancel(pid)
        self.garmin.faults["delete"] = [GarminConnectTooManyRequestsError("x")]
        out = self.cancel(pid, prev["cancel"]["token"])
        self.assertEqual((out["status"], out["retry_after_s"]), ("partial", 900))
        self.assertEqual(self.store.get_plan(self.fp, pid)["status"], "active")
        self.assertEqual(self.cancel(pid, prev["cancel"]["token"])["status"], "done")
        self.assertEqual(self.garmin.library, {})

    def test_cancel_of_an_unknown_plan(self):
        self.assertEqual(self.cancel("0" * 32)["reason"], "unknown_plan")


class Confirmation(Base):
    def test_accepted_is_confirmed_by_elicitation(self):
        self.confirm_answer = "accepted"
        pid, commit = self.preview()
        out = self.commit(pid, commit["token"])
        self.assertEqual((out["status"], out["confirmed_by"]), ("done", "elicitation"))
        self.assertEqual(self.confirm_calls, [{"action": "commit", "count": 3, "dates": [
            "2026-12-02", "2026-12-03", "2026-12-05"], "roles": ["easy", "easy", "threshold"]}])

    def test_a_replacement_asks_for_the_adds_and_the_removes_apart(self):
        pid, commit = self.preview()
        self.commit(pid, commit["token"])
        changed = default_sessions()[:2]
        changed[1] = session("w01-q1", TODAY + dt.timedelta(days=2), role="threshold", km=12.0, hard=True)
        _p, commit2 = self.preview(sessions=changed, plan_id=pid, content="b")
        self.confirm_answer = "declined"
        self.assertEqual(self.commit(pid, commit2["token"])["status"], "declined")
        self.assertEqual(self.confirm_calls[-1], {
            "action": "commit", "count": 1, "dates": ["2026-12-03"], "roles": ["threshold"],
            "removes": 2, "remove_dates": ["2026-12-03", "2026-12-05"], "remove_roles": ["easy", "threshold"]})

    def test_declined_cancelled_timeout_write_nothing_and_the_token_stays(self):
        pid, commit = self.preview()
        for answer in ("declined", TimeoutError(), asyncio.CancelledError(), "something else"):
            with self.subTest(answer=repr(answer)):
                self.confirm_answer = answer if not isinstance(answer, asyncio.CancelledError) else "declined"
                out = self.commit(pid, commit["token"])
                self.assertEqual(out["status"], "declined", out)
                self.assertIn("Do not call this tool again", out["note"])
        self.assertEqual(self.garmin.calls, [])
        self.assertEqual(self.store.tokens[ps.token_hash(commit["token"])]["status"], "issued")
        self.confirm_answer = "accepted"
        self.assertEqual(self.commit(pid, commit["token"])["status"], "done")

    def test_without_elicitation_the_token_path(self):
        self.confirm_answer = "unsupported"
        pid, commit = self.preview()
        self.assertEqual(self.commit(pid, commit["token"])["confirmed_by"], "token")

    def test_a_resumed_token_is_not_asked_again(self):
        self.confirm_answer = "accepted"
        pid, commit = self.preview()
        self.garmin.faults["create"] = [GarminConnectTooManyRequestsError("x")]
        self.commit(pid, commit["token"])
        self.confirm_answer = "declined"
        out = self.commit(pid, commit["token"])
        self.assertEqual((out["status"], out["confirmed_by"]), ("done", "elicitation"))
        self.assertEqual(len(self.confirm_calls), 1)

    def test_cancel_asks_too(self):
        pid, commit = self.preview()
        self.commit(pid, commit["token"])
        prev = self.cancel(pid)
        self.confirm_answer = "declined"
        self.assertEqual(self.cancel(pid, prev["cancel"]["token"])["status"], "declined")
        self.assertEqual(len(self.garmin.library), 3)
        self.assertEqual(self.confirm_calls[-1]["action"], "cancel")

    def test_default_hooks_refuse_without_plan_confirm(self):
        import sys
        from unittest import mock

        with mock.patch.dict(sys.modules, {"plan_confirm": None}):
            self.assertFalse(pw.default_may_write())
            self.assertEqual(run(pw.default_confirm(None, action="commit")), "declined")
        module = types.ModuleType("plan_confirm")
        module.caller_may_write = lambda: True

        async def confirm(ctx, **summary):
            return "accepted"

        module.confirm = confirm
        with mock.patch.dict(sys.modules, {"plan_confirm": module}):
            self.assertTrue(pw.default_may_write())
            self.assertEqual(run(pw.default_confirm(None, action="commit")), "accepted")
            module.caller_may_write = lambda: 1 / 0
            self.assertFalse(pw.default_may_write())


class Lifecycle(Base):
    """Rows that left Garmin, unsettled replacements, a cancelled tool call, stale cancel tokens."""

    def test_a_session_readded_after_its_delete_is_written_again(self):
        pid, c1 = self.preview()
        self.assertEqual(self.commit(pid, c1["token"])["status"], "done")
        _p, c2 = self.preview(sessions=default_sessions()[:2], plan_id=pid, content="b")
        self.assertEqual(self.commit(pid, c2["token"])["status"], "done")
        self.assertEqual(self.rows(pid)["w01-e2"]["state"], "deleted")
        _p, c3 = self.preview(plan_id=pid, content="c")
        self.assertEqual([(o["op"], o["key"]) for o in c3["ops"]], [("create", "w01-e2")])
        out = self.commit(pid, c3["token"])
        self.assertEqual((out["status"], [w["key"] for w in out["written"]]), ("done", ["w01-e2"]))
        self.assertEqual(self.rows(pid)["w01-e2"]["state"], "scheduled")
        self.assertEqual((len(self.garmin.library), len(self.garmin.calendar)), (3, 3))
        self.assertIsNone(self.preview(plan_id=pid, content="d")[1]["token"])

    def test_a_session_readded_while_its_delete_is_unfinished(self):
        pid, c1 = self.preview()
        self.commit(pid, c1["token"])
        _p, c2 = self.preview(sessions=default_sessions()[:2], plan_id=pid, content="b")
        self.garmin.faults["delete"] = [None, ConnectionError("API Error 503 - Service Unavailable")]
        self.assertEqual(self.commit(pid, c2["token"])["status"], "partial")
        self.assertEqual(self.rows(pid)["w01-e2"]["state"], "deleting")
        _p, c3 = self.preview(plan_id=pid, content="c")
        out = self.commit(pid, c3["token"])
        self.assertEqual(out["status"], "done", out)
        self.assertEqual(self.rows(pid)["w01-e2"]["state"], "scheduled")
        self.assertEqual((len(self.garmin.library), len(self.garmin.calendar)), (3, 3))

    def test_a_replacement_of_an_unknown_workout_deletes_the_old_one(self):
        day = TODAY + dt.timedelta(days=1)
        pid, c1 = self.preview(sessions=[session("w01-e1", day)])
        self.garmin.faults["schedule"] = ["block"]
        self.assertEqual(self.commit(pid, c1["token"])["status"], "unknown")
        self.garmin.blocked.set()
        self.assertTrue(pw.join_write_threads(10))
        old = self.rows(pid)["w01-e1"]["garmin_workout_id"]
        _p, c2 = self.preview(sessions=[session("w01-e1", day, km=10.0)], plan_id=pid, content="b")
        self.assertEqual([(o["op"], o["key"]) for o in c2["ops"]], [("create", "w01-e1")])
        self.clock.advance(minutes=3)
        out = self.commit(pid, c2["token"])
        self.assertEqual(out["status"], "done", out)
        self.assertNotIn(old, self.garmin.library)
        self.assertEqual(len(self.garmin.library), 1)
        self.assertEqual([e["workoutId"] for e in self.garmin.calendar], list(self.garmin.library))
        self.assertEqual(out["replaced"], [{"key": "w01-e1", "date": day.isoformat()}])

    def test_a_retired_row_left_on_garmin_is_deleted_by_the_next_commit(self):
        pid, c1 = self.preview()
        self.commit(pid, c1["token"])
        row = dict(self.rows(pid)["w01-e2"])
        retired = "w01-e2~" + row["dto_hash"][:8]
        self.store.put_workout(pid, {**row, "workout_key": retired})
        self.store.put_workout(pid, {**row, "state": "pending", "garmin_workout_id": None,
                                     "garmin_schedule_id": None})
        _p, c2 = self.preview(plan_id=pid, content="b")
        self.assertIn(("delete", retired), [(o["op"], o["key"]) for o in c2["ops"]])
        self.assertEqual(self.commit(pid, c2["token"])["status"], "done")
        self.assertEqual(self.rows(pid)[retired]["state"], "deleted")
        self.assertNotIn(row["garmin_workout_id"], self.garmin.library)

    def test_a_cancelled_call_keeps_the_lock_until_its_writes_end(self):
        pid, c1 = self.preview()
        svc = self.service(join_s=30)
        self.garmin.faults["create"] = [None, "block"]

        async def main():
            task = asyncio.create_task(svc.commit(plan_id=pid, commit_token=c1["token"]))
            for _ in range(250):
                await asyncio.sleep(0.02)
                if len([c for c in self.garmin.calls if c[1] == "workout-service/workout"]) >= 2:
                    break
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            second = json.loads(await svc.commit(plan_id=pid, commit_token=c1["token"]))
            self.garmin.blocked.set()
            return second

        second = run(main())
        self.assertEqual((second["status"], second["reason"]), ("refused", "busy"))
        self.assertTrue(pw.join_write_threads(10))
        self.assertEqual(len(self.garmin.library), 3, "no session written twice")

    def test_a_cancel_token_does_not_cover_workouts_committed_after_it(self):
        pid, c1 = self.preview()
        prev = self.cancel(pid)
        self.assertEqual(prev["cancel"]["ops"], [])
        self.assertEqual(self.commit(pid, c1["token"])["status"], "done")
        out = self.cancel(pid, prev["cancel"]["token"])
        self.assertEqual((out["status"], out["reason"]), ("refused", "superseded"))
        self.assertEqual(self.store.get_plan(self.fp, pid)["status"], "active")
        self.assertEqual(len(self.garmin.calendar), 3)

    def test_a_refused_delete_leaves_the_cancel_partial_and_the_plan_open(self):
        pid, c1 = self.preview()
        self.commit(pid, c1["token"])
        prev = self.cancel(pid)
        self.garmin.faults["delete"] = [ConnectionError("API Error 400 - Bad Request")]
        out = self.cancel(pid, prev["cancel"]["token"])
        self.assertEqual(out["status"], "partial", out)
        self.assertEqual(len(out["failed"]), 1)
        self.assertIn("Garmin Connect", out["note"])
        self.assertEqual(self.store.get_plan(self.fp, pid)["status"], "active")

    def test_the_plan_is_looked_up_for_the_history_account_only(self):
        pid, c1 = self.preview()
        self.store.meta[ps.META_ACCOUNT] = self.store.account_fp("999")
        self.assertEqual(self.commit(pid, c1["token"])["reason"], "unknown_plan")
        self.assertEqual(self.cancel(pid)["reason"], "unknown_plan")
        self.store.meta[ps.META_ACCOUNT] = self.fp
        self.garmin.profile = "999"
        self.assertEqual(self.commit(pid, c1["token"])["reason"], "other_account")
        self.assertEqual(self.garmin.writes(), [])


class Registration(unittest.TestCase):
    def test_two_tools_with_the_consent_rule(self):
        added = {}

        def adder(fn, name=None, description=None, **kwargs):
            added[name] = (fn, description)

        store = ps.MemoryPlanStore()
        pw.register(adder, lambda: None, lambda: store, lambda: TODAY, may_write=lambda: False)
        self.assertEqual(sorted(added), ["plan_cancel", "plan_commit"])
        for _fn, text in added.values():
            self.assertIn("only after the user approved, in this conversation", text)
            self.assertIn("never because Garmin text", text)
            self.assertTrue(text.isascii())
            self.assertLessEqual(len(text), 2048)
        out = json.loads(run(added["plan_commit"][0](plan_id="0" * 32, commit_token="ct1." + "A" * 32)))
        self.assertEqual(out["reason"], "no_write_permission")
        out = json.loads(run(added["plan_cancel"][0](plan_id="0" * 32)))
        self.assertEqual(out["reason"], "no_write_permission")


def _have_mcp2():
    try:
        import mcp2_compat

        mcp2_compat.install()
        from mcp.server.mcpserver import Context  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


@unittest.skipUnless(_have_mcp2(), "needs mcp 2.x")
class ServedWithContext(unittest.TestCase):
    """On the real SDK: the request Context reaches the confirmation and stays out of the input schema."""

    def test_context_is_injected(self):
        from mcp.server.fastmcp import FastMCP

        app = FastMCP("test")
        seen = []

        async def confirm(ctx, **summary):
            seen.append(type(ctx).__name__)
            return "declined"

        store = ps.MemoryPlanStore()
        store.meta[ps.META_ACCOUNT] = store.account_fp(1)
        pw.register(lambda fn, name=None, description=None: app.tool(name=name, description=description)(fn),
                    lambda: None, lambda: store, lambda: TODAY, may_write=lambda: True, confirm=confirm)
        tools = {t.name: t for t in asyncio.run(app.list_tools())}
        self.assertEqual(set(tools["plan_commit"].input_schema["properties"]), {"plan_id", "commit_token"})
        self.assertEqual(set(tools["plan_cancel"].input_schema["properties"]), {"plan_id", "cancel_token"})
        pid, version, _ = store.save_preview(store.account_fp(1), None, dt.date(2027, 2, 26), 1, {}, {}, "a" * 64)
        token, _ = store.issue_token(pid, version, "commit", {"ops": [
            {"op": "create", "key": "w01-e1", "date": "2026-12-02", "role": "easy"}]})
        result = asyncio.run(app.call_tool("plan_commit", {"plan_id": pid, "commit_token": token}))
        self.assertEqual(json.loads(result.content[0].text)["status"], "declined")
        self.assertEqual(seen, ["Context"])


class Classify(unittest.TestCase):
    def test_outcomes(self):
        class MaxRetry(Exception):
            pass

        class NewConnectionError(Exception):
            pass

        reason = NewConnectionError()
        wrapped = MaxRetry(types.SimpleNamespace(reason=reason))
        cases = {GarminConnectTooManyRequestsError("x"): pw.RATE, ConnectionError("API Error 429"): pw.RATE,
                 ConnectionError("API Error 404 - Not Found"): pw.GONE, ConnectionError("API Error 500"): pw.MAYBE,
                 ConnectionError("API Error 400"): pw.REJECTED, ConnectTimeout(): pw.NEVER_SENT,
                 ReadTimeout(): pw.MAYBE, wrapped: pw.NEVER_SENT, garmin_paths.GarminMethodRefused(): pw.NEVER_SENT,
                 TimeoutError(): pw.MAYBE}
        for exc, want in cases.items():
            with self.subTest(exc=type(exc).__name__):
                self.assertEqual(pw.classify(exc), want)

    def test_schedule_id_fields(self):
        self.assertEqual(pw.schedule_id_of({"workoutScheduleId": 5}), 5)
        self.assertEqual(pw.schedule_id_of({"id": "7"}), 7)
        self.assertIsNone(pw.schedule_id_of({"id": True}))
        self.assertIsNone(pw.schedule_id_of(None))


class Tokens(unittest.TestCase):
    def test_shape_hash_only_and_windows(self):
        clock = Clock()
        store = ps.MemoryPlanStore(clock=clock)
        fp = store.account_fp(1)
        pid, version, _ = store.save_preview(fp, None, dt.date(2027, 2, 26), 1, {}, {"a": 1}, "a" * 64)
        token, expires = store.issue_token(pid, version, "commit", {"ops": []})
        self.assertRegex(token, r"^ct1\.[A-Za-z0-9_-]{32}$")
        self.assertEqual(expires, T0 + dt.timedelta(minutes=30))
        self.assertNotIn(token, json.dumps(store.tokens, default=str))
        self.assertIn(ps.token_hash(token), store.tokens)
        with self.assertRaises(ValueError):
            store.token_row(token, pid, "cancel")
        with self.assertRaises(ps.TokenRefused):
            store.token_row(token, "1" * 32, "commit")
        clock.advance(minutes=29)
        store.claim_token(token)
        clock.advance(hours=23)
        self.assertEqual(store.token_row(token, pid, "commit")["status"], "running")
        store.finish_token(token, "done", {"status": "done"})
        clock.advance(days=3)
        self.assertEqual(store.token_row(token, pid, "commit")["result"], {"status": "done"})
        with self.assertRaises(ValueError):
            store.finish_token(token, "weird", {})
        with self.assertRaises(ValueError):
            store.issue_token(pid, 0, "commit", {})
        with self.assertRaises(ps.PlanNotFound):
            store.issue_token("2" * 32, 1, "commit", {})
        self.assertEqual(len(ps.new_token("cancel")), 36)

    def test_workout_rows_are_checked(self):
        store = ps.MemoryPlanStore()
        pid, _, _ = store.save_preview(store.account_fp(1), None, dt.date(2027, 2, 26), 1, {}, {}, "a" * 64)
        good = {"workout_key": "w01-e1", "version": 1, "date_local": TODAY, "role": "easy", "dto_hash": "b" * 64,
                "state": "pending"}
        store.put_workout(pid, good)
        for field, value in (("workout_key", "x"), ("version", 0), ("date_local", "2026-12-01"), ("role", "Bad!"),
                             ("dto_hash", "z"), ("state", "gone"), ("garmin_workout_id", -1)):
            with self.subTest(field=field), self.assertRaises(ValueError):
                store.put_workout(pid, {**good, field: value})
        with self.assertRaises(ps.PlanNotFound):
            store.put_workout("3" * 32, good)
        with self.assertRaises(ps.PlanNotFound):
            store.move_plan("3" * 32, "active")
        with self.assertRaises(ValueError):
            store.move_plan(pid, "weird")
        self.assertEqual(store.all_write_rows()[0]["plan_id"], pid)


class MethodAllowlist(unittest.TestCase):
    def setUp(self):
        self.addCleanup(garmin_paths.allow_plan_writes, ())

    def test_reads_only_by_default(self):
        ok = garmin_paths.garmin_method_ok
        self.assertTrue(ok("GET", "/wellness-service/x"))
        self.assertTrue(ok("POST", "graphql-gateway/graphql", {"json": {"query": "query{x}"}}))
        self.assertTrue(ok("post", "/graphql-gateway/graphql", {"json": {"query": "  query {x}"}}))
        for method, path, kwargs in (("POST", "graphql-gateway/graphql", {"json": {"query": "mutation{x}"}}),
                                     ("POST", "graphql-gateway/graphql", {"json": "query{x}"}),
                                     ("POST", "graphql-gateway/graphql", {}),
                                     ("POST", "workout-service/workout", {"json": {}}),
                                     ("POST", "workout-service/schedule/12", {}),
                                     ("DELETE", "workout-service/workout/12", {}),
                                     ("PUT", "workout-service/workout/12", {}),
                                     ("POST", "activity-service/activity", {})):
            with self.subTest(method=method, path=path):
                self.assertFalse(ok(method, path, kwargs))
                with self.assertRaises(garmin_paths.GarminMethodRefused):
                    garmin_paths.check_garmin_request(method, path, kwargs)
        with self.assertRaises(garmin_paths.GarminPathRefused):
            garmin_paths.check_garmin_request("GET", "x/../y")

    def test_a_graphql_body_must_be_one_anonymous_read(self):
        ok = garmin_paths.graphql_read_ok
        q = 'query{workoutScheduleSummariesScalar(startDate:"2026-12-01", endDate:"2026-12-02")}'
        self.assertTrue(ok({"query": q}))
        self.assertTrue(ok({"query": q, "variables": {}, "operationName": None}))
        self.assertTrue(ok({"query": 'query{x(name:"mutation subscription")}'}), "a quoted word is data")
        self.assertTrue(ok({"query": 'query{x(note:"""a "mutation" here""")}'}))
        for body in ({"query": "query a{x} mutation b{y}", "operationName": "b"},
                     {"query": "query a{x} mutation b{y}"},
                     {"query": "query{x} subscription{y}"},
                     {"query": 'query{x(a:"\\"") } mutation{y}'},
                     {"query": "# query\nmutation{y}"},
                     {"query": "query a{x}", "operationName": "a"},
                     {"query": q, "extensions": {"persistedQuery": {}}},
                     {"query": 'mutation{x(a:"query")}'},
                     {"query": None}, "query{x}", None):
            with self.subTest(body=body):
                self.assertFalse(ok(body))
                self.assertFalse(garmin_paths.garmin_method_ok("POST", garmin_paths.GRAPHQL_PATH, {"json": body}))

    def test_plan_writes_admit_exactly_four_paths(self):
        garmin_paths.allow_plan_writes(set(garmin_paths.WRITE_PATHS))
        ok = garmin_paths.garmin_method_ok
        self.assertTrue(ok("POST", "workout-service/workout"))
        self.assertTrue(ok("POST", "/workout-service/schedule/12"))
        self.assertTrue(ok("DELETE", "workout-service/schedule/12"))
        self.assertTrue(ok("DELETE", "workout-service/workout/12"))
        for method, path in (("POST", "workout-service/workout/12"), ("DELETE", "workout-service/workout"),
                             ("PUT", "workout-service/workout/12"), ("DELETE", "workout-service/schedule/0"),
                             ("POST", "workout-service/schedule/12/x"), ("DELETE", "activity-service/activity/1"),
                             ("POST", "workout-service/schedule/abc")):
            with self.subTest(method=method, path=path):
                self.assertFalse(ok(method, path))
        garmin_paths.allow_plan_writes({"delete"})
        self.assertFalse(ok("POST", "workout-service/workout"))
        self.assertEqual(garmin_paths.allowed_write_kinds(), frozenset({"delete"}))
        with self.assertRaises(ValueError):
            garmin_paths.allow_plan_writes({"put"})

    def test_the_guarded_run_request_refuses_a_write(self):
        calls = []
        guarded = garmin_paths.path_guarded_run_request(lambda c, m, p, *a, **k: calls.append((m, p)))
        guarded(None, "GET", "x-service/y")
        with self.assertRaises(garmin_paths.GarminMethodRefused):
            guarded(None, "DELETE", "workout-service/workout/1")
        self.assertEqual(calls, [("GET", "x-service/y")])

    def test_argument_checks(self):
        refused = garmin_paths.refused_argument
        token = "ct1." + "a" * 32
        self.assertIsNone(refused("plan_commit", {"plan_id": "0" * 32, "commit_token": token}))
        self.assertEqual(refused("plan_commit", {"commit_token": "xt1." + "a" * 32})[0], "commit_token")
        self.assertEqual(refused("plan_commit", {"commit_token": token + "/.."})[0], "commit_token")
        self.assertIsNone(refused("plan_cancel", {"plan_id": "0" * 32, "cancel_token": ""}))
        self.assertEqual(refused("plan_cancel", {"cancel_token": token})[0], "cancel_token")


class Cleanup(Base):
    def orphan(self, wid, desc, day=None, linked=None):
        with self.garmin.lock:
            self.garmin.library[wid] = {"workoutId": wid, "workoutName": "Coach Easy", "description": desc}
            if day is not None:
                self.garmin.calendar.append({"workoutId": wid, "scheduleDate": day.isoformat(), "id": wid + 1000,
                                             "workoutName": "Coach Easy", "workoutType": "running",
                                             "associatedActivityId": linked})

    def test_dry_run_and_run_delete_only_this_install(self):
        pid, commit = self.preview()
        self.commit(pid, commit["token"])
        tag = self.store.install_tag()
        later, earlier = TODAY + dt.timedelta(days=9), TODAY - dt.timedelta(days=3)
        self.orphan(601, f"coach-mcp {tag} {pid} w09-e1 h12345678", later)
        self.orphan(602, f"coach-mcp 0badc0de {pid} w01-e1 h12345678", later)    # another install
        self.orphan(603, None, later)
        self.orphan(604, f"coach-mcp {tag} {pid} w00-e1 h12345678", earlier)      # in the past
        self.orphan(605, f"coach-mcp {tag} {pid} w09-e2 h12345678", later, linked=77)
        self.orphan(606, f"coach-mcp {tag} {pid} w09-e3 h12345678")               # not on the calendar
        self.store.workout_rows[(pid, "w01-e1")]["linked_activity_id"] = 9
        before = list(self.garmin.calls)
        counts = pw.cleanup(self.store, self.garmin, TODAY, dry_run=True, sleep=lambda s: None)
        self.assertEqual(counts, {"rows": 2, "orphans": 0, "other_account": 0, "deleted": 0, "failed": 0})
        counts = pw.cleanup(self.store, self.garmin, TODAY, dry_run=True, orphans=True, sleep=lambda s: None)
        self.assertEqual(counts["orphans"], 1)
        self.assertEqual([c for c in self.garmin.calls if c not in before and c[0] != "READ"], [])
        counts = pw.cleanup(self.store, self.garmin, TODAY, dry_run=False, orphans=True, sleep=lambda s: None,
                            join_s=2)
        self.assertEqual(counts["deleted"], 3)
        self.assertEqual(set(self.garmin.library),
                         {602, 603, 604, 605, 606, self.rows(pid)["w01-e1"]["garmin_workout_id"]})

    def test_rows_of_another_garmin_account_are_kept(self):
        pid, commit = self.preview()
        self.commit(pid, commit["token"])
        self.garmin.profile = "999"
        counts = pw.cleanup(self.store, self.garmin, TODAY, dry_run=False, sleep=lambda s: None, join_s=2)
        self.assertEqual((counts["rows"], counts["other_account"], counts["deleted"]), (0, 3, 0))
        self.assertEqual(len(self.garmin.library), 3)
        self.assertEqual(self.garmin.writes()[-1][0], "POST", "no delete sent")

    def test_a_rate_limit_stops_the_cleanup(self):
        pid, commit = self.preview()
        self.commit(pid, commit["token"])
        self.garmin.faults["delete"] = [GarminConnectTooManyRequestsError("x")] * 2
        counts = pw.cleanup(self.store, self.garmin, TODAY, dry_run=False, sleep=lambda s: None, join_s=2)
        self.assertGreaterEqual(counts["failed"], 1)
        self.assertEqual(counts["deleted"], 0)

    def test_cli(self):
        out = io.StringIO()
        env = {"DATABASE_URL": "postgresql://ci@localhost/x", "GARMIN_AUTH": "full", "REPORT_TZ": "UTC"}
        self.assertEqual(pw.cleanup_cli(env, ["--now"], out=out), 2)
        self.assertEqual(pw.cleanup_cli(env, ["--dry-run", "--dry-run"], out=out), 2)
        self.assertEqual(pw.cleanup_cli({**env, "DATABASE_URL": ""}, [], out=out), 1)
        self.assertEqual(pw.cleanup_cli({**env, "GARMIN_AUTH": "key"}, [], out=out), 1)
        pid, commit = self.preview()
        self.commit(pid, commit["token"])
        garmin_paths.allow_plan_writes(())
        code = pw.cleanup_cli(env, ["--dry-run"], build_client=lambda: self.garmin,
                              build_store=lambda dsn: self.store, out=out, today=TODAY)
        self.assertEqual(code, 0)
        self.assertIn("would delete 3 workout(s)", out.getvalue())
        self.assertIn("library orphans not checked: add --orphans", out.getvalue())
        code = pw.cleanup_cli(env, ["--orphans", "--dry-run"], build_client=lambda: self.garmin,
                              build_store=lambda dsn: self.store, out=out, today=TODAY)
        self.assertEqual(code, 0)
        self.assertIn("0 library orphans with this install's marker", out.getvalue())
        code = pw.cleanup_cli(env, [], build_client=lambda: self.garmin, build_store=lambda dsn: self.store, out=out,
                              today=TODAY, sleep=lambda s: None)
        self.assertEqual(code, 0, out.getvalue())
        self.assertEqual(self.garmin.library, {})
        self.assertEqual(garmin_paths.allowed_write_kinds(), frozenset())
        self.store.is_ready = False
        self.assertEqual(pw.cleanup_cli(env, [], build_store=lambda dsn: self.store, out=out), 0)
        self.assertIn("nothing to clean", out.getvalue())
        self.assertEqual(pw.cleanup_cli(env, [], build_store=lambda dsn: 1 / 0, out=out), 1)
        self.assertNotIn("postgresql", out.getvalue())


class Marker(unittest.TestCase):
    def test_marker_and_dto(self):
        s = session("w01-e1", TODAY)
        dto = plan_dto.workout_dto(s, marker=pw.marker("abcd1234", "0" * 32, "w01-e1", "f" * 64))
        self.assertEqual(dto["description"], "coach-mcp abcd1234 " + "0" * 32 + " w01-e1 hffffffff")
        self.assertEqual(dto["workoutName"], "Coach Easy")


if __name__ == "__main__":
    unittest.main()
