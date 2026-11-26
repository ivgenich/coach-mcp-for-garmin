"""Shared fakes for the deep write tests (test_deep_writes*.py). No tests here.

DeepGarmin is test_plan_writes' FakeGarmin with a richer raw client: a fault is scripted per step (upload,
schedule, unschedule, delete) and per occurrence of that step (1-based, or "*" for every one), and says whether
the request lands on Garmin before it fails:

    before(exc)    raised before the request reaches Garmin (nothing lands)
    after(exc)     the request lands, then exc is raised (a read timeout or a 5xx after the write)
    answer(fn)     the request lands and answers fn(new_id) instead of the normal JSON (BAD_JSON: not JSON)
    BLOCK          waits for `blocked` (cleanup sets it), then lands and answers normally

Every write records (step, path, mono time) so tests can check spacing and count double writes. A blocked
request waits on an Event that the cleanup sets, and the cleanup joins the helper's threads (ci-unittest fails on
leaked threads).
"""

from __future__ import annotations

import asyncio
import copy
import datetime as dt
import json
import re
import types
import unittest

import garmin_paths
import plan_store as ps
import plan_writes as pw
import test_plan_writes as tpw

TODAY = tpw.TODAY
PROFILE = tpw.PROFILE
T0 = tpw.T0
session = tpw.session
plan_answer = tpw.plan_answer
default_sessions = tpw.default_sessions
GarminConnectTooManyRequestsError = tpw.GarminConnectTooManyRequestsError
ConnectTimeout = tpw.ConnectTimeout
ReadTimeout = tpw.ReadTimeout

BAD_JSON = object()


class _Fault:
    def __init__(self, kind, value=None):
        self.kind, self.value = kind, value

    def __repr__(self):
        return f"{self.kind}({type(self.value).__name__})"


def before(exc):
    return _Fault("before", exc)


def after(exc):
    return _Fault("after", exc)


def answer(fn):
    return _Fault("answer", fn)


BLOCK = _Fault("block")


class DResp:
    def __init__(self, payload):
        self.status_code = 200
        self._payload = payload

    def json(self):
        if self._payload is BAD_JSON:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return copy.deepcopy(self._payload)


def http(status):
    """garminconnect's raw client error for an HTTP status."""
    return ConnectionError(f"API Error {status} - TEST")


class DeepRaw:
    def __init__(self, garmin):
        self.g = garmin

    def _fault(self, step):
        with self.g.lock:
            self.g.counts[step] = self.g.counts.get(step, 0) + 1
            n = self.g.counts[step]
            table = self.g.faults.get(step) or {}
            fault = table.pop(n, None)
            if fault is None:
                fault = table.get("*")
        if fault is not None and fault.kind == "before":
            raise fault.value
        if fault is BLOCK:
            self.g.blocked.wait(10)
        return fault

    def _finish(self, fault, normal, new_id):
        if fault is not None and fault.kind == "after":
            raise fault.value
        if fault is not None and fault.kind == "answer":
            payload = fault.value(new_id)
            return DResp(payload)
        return DResp(normal)

    def _log(self, step, path):
        self.g.calls.append(("POST" if step in ("upload", "schedule") else "DELETE", path))
        self.g.write_log.append((step, path, self.g.mono[0]))

    def post(self, domain, path, json=None, timeout=None, **kwargs):
        assert domain == "connectapi" and timeout is not None
        garmin_paths.check_garmin_request("POST", path, {"json": json})
        if self.g.on_write is not None:
            self.g.on_write("POST", path)
        if path == "workout-service/workout":
            self._log("upload", path)
            fault = self._fault("upload")
            with self.g.lock:
                wid = self.g.next_id()
                self.g.library[wid] = {"workoutId": wid, "workoutName": json["workoutName"],
                                       "description": json.get("description"), "dto": json}
            return self._finish(fault, {"workoutId": wid, "workoutName": json["workoutName"]}, wid)
        m = re.fullmatch(r"workout-service/schedule/(\d+)", path)
        assert m, path
        self._log("schedule", path)
        fault = self._fault("schedule")
        wid = int(m.group(1))
        with self.g.lock:
            if wid not in self.g.library:
                raise http(404)
            sid = self.g.next_id()
            w = self.g.library[wid]
            self.g.calendar.append({"workoutId": wid, "scheduleDate": json["date"], "workoutName": w["workoutName"],
                                    "workoutType": "running", "scheduledWorkoutId": sid})
        return self._finish(fault, {"workoutScheduleId": sid, "workout": {"workoutId": wid}}, sid)

    def delete(self, domain, path, timeout=None, **kwargs):
        assert domain == "connectapi" and timeout is not None
        garmin_paths.check_garmin_request("DELETE", path, {})
        if self.g.on_write is not None:
            self.g.on_write("DELETE", path)
        m = re.fullmatch(r"workout-service/(schedule|workout)/(\d+)", path)
        assert m, path
        step = "unschedule" if m.group(1) == "schedule" else "delete"
        self._log(step, path)
        n = int(m.group(2))
        if step == "delete" and n in self.g.reject_delete_ids:
            raise http(400)
        if step == "unschedule" and any(e.get("scheduledWorkoutId") == n and e.get("workoutId") in self.g.reject_delete_ids
                                        for e in self.g.calendar):
            raise http(400)
        fault = self._fault(step)
        with self.g.lock:
            if step == "unschedule":
                before_n = len(self.g.calendar)
                self.g.calendar = [e for e in self.g.calendar if e.get("scheduledWorkoutId") != n]
                if len(self.g.calendar) == before_n:
                    raise http(404)
            else:
                if n not in self.g.library:
                    raise http(404)
                del self.g.library[n]
                # its calendar entries stay: Garmin is not known to remove them with the workout
        return self._finish(fault, {}, n)


class DeepGarmin(tpw.FakeGarmin):
    def __init__(self, mono):
        super().__init__()
        self.client = DeepRaw(self)
        self.faults = {}
        self.counts = {}
        self.write_log = []
        self.mono = mono
        self.reject_delete_ids = set()   # Garmin refuses (400) deleting or unscheduling these workouts
        self.on_write = None
        self.library_answer = None     # a callable replacing get_workouts' answer once (malformed listings)

    def get_workouts(self, start, limit):
        self._read("library")
        if self.library_answer is not None:
            fn, self.library_answer = self.library_answer, None
            return fn()
        with self.lock:
            return [{k: v for k, v in w.items() if k != "dto"} for w in self.library.values()][start:start + limit]

    def uploads(self):
        return [c for c in self.write_log if c[0] == "upload"]

    def per_workout_entries(self):
        out = {}
        for e in self.calendar:
            out[e["workoutId"]] = out.get(e["workoutId"], 0) + 1
        return out


def run(coro):
    return asyncio.run(coro)


class DeepBase(unittest.TestCase):
    """A fresh fake Garmin, MemoryPlanStore, clocks and WriteService; reset() makes new ones (for subTests)."""

    def setUp(self):
        garmin_paths.allow_plan_writes(set(garmin_paths.WRITE_PATHS))
        self.addCleanup(garmin_paths.allow_plan_writes, ())
        self._garmins = []
        self.addCleanup(self._release_all)
        self.reset()

    def _release_all(self):
        for g in self._garmins:
            g.blocked.set()
        self.assertTrue(pw.join_write_threads(10), "a write thread outlived the test")

    def make_store(self):
        return ps.MemoryPlanStore(clock=self.clock)

    def make_profile(self):
        return PROFILE

    def reset(self):
        if getattr(self, "garmin", None) is not None:
            self.garmin.blocked.set()
            self.assertTrue(pw.join_write_threads(10))
        self.mono = [0.0]
        self.sleeps = []
        self.garmin = DeepGarmin(self.mono)
        self._garmins.append(self.garmin)
        self.clock = tpw.Clock()
        self.profile = self.make_profile()
        self.garmin.profile = self.profile
        self.store = self.make_store()
        self.fp = self.store.account_fp(self.profile)
        self.set_history_account(self.fp)
        self.confirm_answer = "unsupported"
        self.confirm_calls = []
        self.confirm_binds = []
        self.may_write = True
        self.svc = self.service()

    def set_history_account(self, fp):
        self.store.meta[ps.META_ACCOUNT] = fp

    def sleep(self, s):
        self.sleeps.append(s)
        self.mono[0] += s

    def service(self, store=None, **options):
        async def confirm(ctx, **summary):
            self.confirm_binds.append(summary.pop("bind", None))
            self.confirm_calls.append(summary)
            if isinstance(self.confirm_answer, BaseException):
                raise self.confirm_answer
            return self.confirm_answer

        opts = {"sleep": self.sleep, "join_s": 0.3, "clock": lambda: self.mono[0], **options}
        st = store if store is not None else self.store
        return pw.WriteService(lambda: self.garmin, lambda: st, lambda: TODAY,
                               may_write=lambda: self.may_write, confirm=confirm, executor_options=opts)

    def preview(self, sessions=None, plan_id=None, content="a", days=14, fixed=(), lt=None, store=None):
        st = store if store is not None else self.store
        answer_ = plan_answer(sessions if sessions is not None else default_sessions(), content)
        pid, version, _changed = st.save_preview(self.fp, plan_id, dt.date(2027, 2, 26), 1, {"x": 1}, answer_,
                                                 answer_["content_hash"])
        snap = types.SimpleNamespace(fixed=fixed, lt_speed_mps=lt)
        commit = pw.commit_section(st, answer_, snap, {"plan_id": pid, "version": version}, TODAY, days,
                                   self.profile)
        return pid, commit

    def commit(self, pid, token, svc=None):
        return json.loads(run((svc or self.svc).commit(plan_id=pid, commit_token=token)))

    def cancel(self, pid, token="", svc=None):
        return json.loads(run((svc or self.svc).cancel(plan_id=pid, cancel_token=token)))

    def rows(self, pid, store=None):
        return {r["workout_key"]: r for r in (store or self.store).write_rows(pid)}

    def token_status(self, token):
        return self.store.tokens[ps.token_hash(token)]["status"]

    def committed(self, sessions=None):
        pid, c = self.preview(sessions=sessions)
        out = self.commit(pid, c["token"])
        self.assertEqual(out["status"], "done", out)
        return pid, c

    def assert_no_double_writes(self, sessions=3):
        """One library workout per session and one calendar entry per workout."""
        ours = [w for w in self.garmin.library.values() if str(w.get("description") or "").startswith("coach-mcp ")]
        self.assertEqual(len(ours), sessions, "workouts on Garmin")
        marks = [w["description"] for w in ours]
        self.assertEqual(len(set(marks)), len(marks), "two workouts with the same marker")
        entries = self.garmin.per_workout_entries()
        self.assertTrue(all(n == 1 for n in entries.values()), entries)
