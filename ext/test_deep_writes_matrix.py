"""Deep write tests, the fault matrix: every write step x every failure x the op's position (first, middle, last),
on plan_commit (upload, schedule), plan_cancel (unschedule, delete) and a commit's replace (unschedule, delete),
plus the reads before the writes (stats, calendar) and the library listing of a reconciliation.

Each case checks the answer (status, retry_after_s, remaining, written/deleted/failed), the rows' states, then
resumes with the same token until done and checks that Garmin holds one workout per session and one calendar
entry per workout (no double write). Standard library only.
"""

from __future__ import annotations

import datetime as dt
import unittest

import test_deep_writes_base as b
from test_deep_writes_base import BAD_JSON, after, answer, before, http

KEYS = ["w01-e1", "w01-q1", "w01-e2"]

RATE, NEVER, LANDED, LOST, REJECTED, NO_ID, OK = "rate", "never", "landed", "lost", "rejected", "no_id", "ok"

WRITE_FAULTS = {
    "429_exception": (lambda: before(b.GarminConnectTooManyRequestsError("Too many")), RATE),
    "429_http": (lambda: before(http(429)), RATE),
    "connect_timeout": (lambda: before(b.ConnectTimeout("connect")), NEVER),
    "read_timeout_landed": (lambda: after(b.ReadTimeout("read")), LANDED),
    "read_timeout_lost": (lambda: before(b.ReadTimeout("read")), LOST),
    "5xx_landed": (lambda: after(http(502)), LANDED),
    "5xx_lost": (lambda: before(http(503)), LOST),
    "4xx_rejected": (lambda: before(http(400)), REJECTED),
    "403_rejected": (lambda: before(http(403)), REJECTED),
    "404": (lambda: before(http(404)), REJECTED),
    "malformed_json": (lambda: answer(lambda i: BAD_JSON), NO_ID),
    "empty_object": (lambda: answer(lambda i: {}), NO_ID),
    "id_null": (lambda: answer(lambda i: {"workoutId": None, "workoutScheduleId": None}), NO_ID),
    "id_bool": (lambda: answer(lambda i: {"workoutId": True, "workoutScheduleId": True}), NO_ID),
    "id_zero": (lambda: answer(lambda i: {"workoutId": 0, "workoutScheduleId": 0}), NO_ID),
    "id_negative": (lambda: answer(lambda i: {"workoutId": -i, "workoutScheduleId": -i}), NO_ID),
    "id_text": (lambda: answer(lambda i: {"workoutId": "abc", "workoutScheduleId": "abc"}), NO_ID),
    "id_list": (lambda: answer(lambda i: {"workoutId": [i], "workoutScheduleId": [i]}), NO_ID),
    "id_too_big": (lambda: answer(lambda i: {"workoutId": 10 ** 19, "workoutScheduleId": 10 ** 19}), NO_ID),
    "answer_is_a_list": (lambda: answer(lambda i: [{"workoutId": i}]), NO_ID),
    "id_as_string": (lambda: answer(lambda i: {"workoutId": str(i), "workoutScheduleId": str(i)}), OK),
}

DELETE_FAULTS = {
    "429_exception": (lambda: before(b.GarminConnectTooManyRequestsError("Too many")), RATE),
    "429_http": (lambda: before(http(429)), RATE),
    "connect_timeout": (lambda: before(b.ConnectTimeout("connect")), NEVER),
    "read_timeout_landed": (lambda: after(b.ReadTimeout("read")), LANDED),
    "read_timeout_lost": (lambda: before(b.ReadTimeout("read")), LOST),
    "5xx_landed": (lambda: after(http(500)), LANDED),
    "5xx_lost": (lambda: before(http(503)), LOST),
    "4xx_rejected": (lambda: before(http(400)), REJECTED),
    "404_gone": (lambda: after(http(404)), OK),
    "malformed_json": (lambda: answer(lambda i: BAD_JSON), OK),
}

RETRY = {RATE: 900, NEVER: 60}


class CommitMatrix(b.DeepBase):
    def _case(self, step, name, make, cls, p):
        pid, c = self.preview()
        self.garmin.faults[step] = {p: make()}
        out = self.commit(pid, c["token"])
        self.assertNotIn(p, self.garmin.faults[step], "the fault was hit")
        rows = self.rows(pid)
        key = KEYS[p - 1]
        done_before = KEYS[:p - 1]
        if cls in (RATE, NEVER):
            self.assertEqual(out["status"], "partial", out)
            self.assertEqual(out["retry_after_s"], RETRY[cls])
            self.assertEqual(out["remaining"], 3 - p + 1)
            self.assertEqual([w["key"] for w in out["written"]], done_before)
            self.assertEqual(rows[key]["state"], "pending" if step == "upload" else "uploaded")
            self.assertEqual(self.token_status(c["token"]), "partial")
        elif cls in (LANDED, LOST) or (cls == NO_ID and step == "upload"):
            self.assertEqual(out["status"], "unknown", out)
            self.assertEqual(out["retry_after_s"], b.pw.RETRY_AFTER_UNKNOWN_S)
            self.assertIn("plan_get", out["note"])
            self.assertEqual(out["remaining"], 3 - p + 1)
            self.assertEqual(rows[key]["state"], "unknown")
            self.assertEqual(bool(rows[key]["garmin_workout_id"]), step == "schedule")
            for later in KEYS[p:]:
                self.assertNotIn(later, rows, "the call stops at an unknown write")
            # Again before the quarantine: the unknown row waits, the other rows are written.
            again = self.commit(pid, c["token"])
            self.assertEqual(again["status"], "partial", again)
            self.assertEqual(again["retry_after_s"], b.pw.RETRY_AFTER_UNKNOWN_S)
            self.assertEqual(self.rows(pid)[key]["state"], "unknown")
            self.clock.advance(minutes=2, seconds=1)
        elif cls == REJECTED:
            # A refused upload or schedule leaves that day off the calendar: partial, never "every write is done".
            self.assertEqual(out["status"], "partial", out)
            self.assertIsNone(out["retry_after_s"])
            self.assertNotIn("Every write", out["note"])
            self.assertEqual(out["failed"], [{"key": key, "date": rows[key]["date_local"].isoformat()}])
            self.assertEqual(rows[key]["state"], "failed" if step == "upload" else "uploaded")
            self.assertEqual([w["key"] for w in out["written"]], [k for k in KEYS if k != key])
            n_lib = 2 if step == "upload" else 3
            self.assertEqual(len(self.garmin.library), n_lib)
            self.assertEqual(len(self.garmin.calendar), 2)
            self.assertEqual(self.token_status(c["token"]), "partial")
            again = self.commit(pid, c["token"])   # Garmin takes it this time
            self.assertEqual(again["status"], "done", again)
            self.assert_no_double_writes()
            return
        else:   # OK, or a schedule answer without a usable entry id
            self.assertEqual(out["status"], "done", out)
            self.assertEqual(rows[key]["state"], "scheduled")
            if step == "schedule" and cls == NO_ID:
                self.assertIsNone(rows[key]["garmin_schedule_id"])
            self.assert_no_double_writes()
            # The plan's own workouts can still be cancelled (the entry id is looked up on the calendar).
            prev = self.cancel(pid)
            gone = self.cancel(pid, prev["cancel"]["token"])
            self.assertEqual(gone["status"], "done", gone)
            self.assertEqual(self.garmin.calendar, [])
            self.assertEqual(self.garmin.library, {})
            return
        final = None
        for _ in range(4):
            final = self.commit(pid, c["token"])
            if final["status"] == "done":
                break
        self.assertEqual(final["status"], "done", final)
        self.assert_no_double_writes()
        self.assertEqual({r["state"] for k, r in self.rows(pid).items()}, {"scheduled"})

    def test_matrix(self):
        for step in ("upload", "schedule"):
            for name, (make, cls) in WRITE_FAULTS.items():
                for p in (1, 2, 3):
                    with self.subTest(step=step, fault=name, position=p):
                        self.reset()
                        self._case(step, name, make, cls, p)


class CancelMatrix(b.DeepBase):
    def _case(self, step, make, cls, p):
        pid, _c = self.committed()
        prev = self.cancel(pid)
        self.assertEqual([o["key"] for o in prev["cancel"]["ops"]], KEYS)
        token = prev["cancel"]["token"]
        self.garmin.faults[step] = {p: make()}
        out = self.cancel(pid, token)
        self.assertNotIn(p, self.garmin.faults[step], "the fault was hit")
        rows = self.rows(pid)
        key = KEYS[p - 1]
        if cls == OK:
            self.assertEqual(out["status"], "done", out)
            self.assertEqual(self.store.get_plan(self.fp, pid)["status"], "cancelled")
            self.assertEqual(self.garmin.calendar, [])
            return
        self.assertEqual(out["status"], "partial", out)
        self.assertEqual(self.store.get_plan(self.fp, pid)["status"], "active", "the plan stays open")
        self.assertEqual([d["key"] for d in out["deleted"]], [k for k in (KEYS[:p - 1] if cls != REJECTED else KEYS)
                                                              if k != key])
        self.assertEqual(rows[key]["state"], "unscheduling" if step == "unschedule" else "deleting")
        if cls == REJECTED:
            self.assertEqual(out["failed"], [{"key": key, "date": rows[key]["date_local"].isoformat(),
                                              "role": rows[key]["role"]}])
            self.assertIsNone(out["retry_after_s"])
            self.assertIn("Garmin Connect", out["note"])
            self.assertEqual(out["remaining"], 0)
        else:
            self.assertEqual(out["retry_after_s"], RETRY.get(cls, b.pw.RETRY_AFTER_NETWORK_S))
            self.assertEqual(out["remaining"], 3 - p + 1)
        final = self.cancel(pid, token)
        self.assertEqual(final["status"], "done", final)
        self.assertEqual(self.store.get_plan(self.fp, pid)["status"], "cancelled")
        self.assertEqual(self.garmin.calendar, [])
        self.assertEqual(self.garmin.library, {})
        self.assertEqual({r["state"] for r in self.rows(pid).values()}, {"deleted"})
        self.assertTrue(self.cancel(pid, token)["replayed"])

    def test_matrix(self):
        for step in ("unschedule", "delete"):
            for name, (make, cls) in DELETE_FAULTS.items():
                for p in (1, 2, 3):
                    with self.subTest(step=step, fault=name, position=p):
                        self.reset()
                        self._case(step, make, cls, p)


class ReplaceMatrix(b.DeepBase):
    """A replaced day: the new workout first, then the old one unscheduled and deleted; a fault on the old one's
    unschedule or delete never leaves it on Garmin for good."""

    def _case(self, step, make, cls):
        pid, _c = self.committed()
        old = self.rows(pid)["w01-e2"]
        changed = b.default_sessions()
        changed[2] = b.session("w01-e2", b.TODAY + dt.timedelta(days=4), km=11.0)
        _p, c2 = self.preview(sessions=changed, plan_id=pid, content="b")
        retired = f"w01-e2~{old['dto_hash'][:8]}"
        self.assertEqual([(o["op"], o["key"]) for o in c2["ops"]], [("create", "w01-e2"), ("delete", retired)])
        self.garmin.faults[step] = {1: make()}
        out = self.commit(pid, c2["token"])
        self.assertNotIn(1, self.garmin.faults[step], "the fault was hit")
        rows = self.rows(pid)
        self.assertEqual(rows["w01-e2"]["state"], "scheduled", "the new workout is written first")
        if cls == OK:
            self.assertEqual(out["status"], "done", out)
            self.assertNotIn(old["garmin_workout_id"], self.garmin.library)
            return
        self.assertEqual(rows[retired]["state"], "unscheduling" if step == "unschedule" else "deleting")
        if cls == REJECTED:
            self.assertEqual(out["status"], "done", out)
            self.assertEqual(len(out["failed"]), 1)
            # The next commit deletes the old one (a replaced workout always goes).
            _p, c3 = self.preview(plan_id=pid, sessions=changed, content="c")
            self.assertIn(("delete", retired), [(o["op"], o["key"]) for o in c3["ops"]])
            final = self.commit(pid, c3["token"])
        else:
            self.assertEqual(out["status"], "partial", out)
            self.assertEqual(out["retry_after_s"], RETRY.get(cls, b.pw.RETRY_AFTER_NETWORK_S))
            final = self.commit(pid, c2["token"])
        self.assertEqual(final["status"], "done", final)
        self.assertNotIn(old["garmin_workout_id"], self.garmin.library)
        self.assertEqual(self.rows(pid)[retired]["state"], "deleted")
        self.assert_no_double_writes()

    def test_matrix(self):
        for step in ("unschedule", "delete"):
            for name, (make, cls) in DELETE_FAULTS.items():
                with self.subTest(step=step, fault=name):
                    self.reset()
                    self._case(step, make, cls)


class ReadFaults(b.DeepBase):
    """The reads before any write: a failure refuses, writes nothing and leaves the token unused."""

    FAULTS = {
        "429": (b.GarminConnectTooManyRequestsError("Too many"), 900),
        "connect_timeout": (b.ConnectTimeout("connect"), None),
        "read_timeout": (b.ReadTimeout("read"), None),
        "5xx": (http(503), None),
        "4xx": (http(400), None),
    }

    def test_stats_and_calendar(self):
        for read, reason in (("stats", "garmin_unreadable"), ("calendar", "calendar_unreadable")):
            for name, (exc, retry) in self.FAULTS.items():
                with self.subTest(read=read, fault=name):
                    self.reset()
                    pid, c = self.preview()
                    self.garmin.read_faults[read] = exc
                    out = self.commit(pid, c["token"])
                    self.assertEqual((out["status"], out["reason"]), ("refused", reason))
                    self.assertEqual(out.get("retry_after_s"), retry)
                    self.assertEqual(self.garmin.writes(), [])
                    self.assertEqual(self.token_status(c["token"]), "issued")
                    self.assertEqual(self.store.get_plan(self.fp, pid)["status"], "draft")
                    del self.garmin.read_faults[read]
                    self.assertEqual(self.commit(pid, c["token"])["status"], "done")

    def test_malformed_stats_and_calendar(self):
        cases = {
            "no_profile": ("get_stats", lambda d: {}, "garmin_unreadable"),
            # A bool or blank profile id reads as "another account" (refused all the same, nothing written).
            "profile_bool": ("get_stats", lambda d: {"userProfileId": True}, "other_account"),
            "profile_blank": ("get_stats", lambda d: {"userProfileId": "  "}, "other_account"),
            "stats_none": ("get_stats", lambda d: None, "garmin_unreadable"),
            "calendar_none": ("query_garmin_graphql", lambda q: None, "calendar_unreadable"),
            "calendar_errors": ("query_garmin_graphql", lambda q: {"errors": [{"message": "x"}]},
                                "calendar_unreadable"),
            "calendar_not_a_list": ("query_garmin_graphql",
                                    lambda q: {"data": {"workoutScheduleSummariesScalar": {"x": 1}}},
                                    "calendar_unreadable"),
        }
        for name, (attr, fn, reason) in cases.items():
            with self.subTest(case=name):
                self.reset()
                pid, c = self.preview()
                setattr(self.garmin, attr, fn)
                out = self.commit(pid, c["token"])
                self.assertEqual((out["status"], out["reason"]), ("refused", reason), out)
                self.assertEqual(self.garmin.writes(), [])
                self.assertEqual(self.token_status(c["token"]), "issued")


class LibraryFaults(b.DeepBase):
    """The library listing is read only to settle an unknown upload (adopt it by its marker)."""

    def unknown_upload(self, landed=True):
        pid, c = self.preview(sessions=[b.session("w01-e1", b.TODAY + dt.timedelta(days=1))])
        self.garmin.faults["upload"] = {1: after(b.ReadTimeout("read")) if landed else before(b.ReadTimeout("x"))}
        self.assertEqual(self.commit(pid, c["token"])["status"], "unknown")
        self.clock.advance(minutes=3)
        return pid, c

    def test_a_failed_listing_keeps_the_row_unknown_and_uploads_nothing(self):
        for name, exc in (("connect_timeout", b.ConnectTimeout("c")), ("read_timeout", b.ReadTimeout("r")),
                          ("5xx", http(502)), ("4xx", http(400))):
            with self.subTest(fault=name):
                self.reset()
                pid, c = self.unknown_upload()
                self.garmin.read_faults["library"] = exc
                out = self.commit(pid, c["token"])
                self.assertEqual(out["status"], "partial", out)
                self.assertEqual(out["retry_after_s"], b.pw.RETRY_AFTER_UNKNOWN_S)
                self.assertEqual(self.rows(pid)["w01-e1"]["state"], "unknown")
                self.assertEqual(len(self.garmin.uploads()), 1)
                del self.garmin.read_faults["library"]
                self.assertEqual(self.commit(pid, c["token"])["status"], "done")
                self.assert_no_double_writes(1)

    def test_a_rate_limited_listing_stops_the_call(self):
        pid, c = self.preview(sessions=[b.session("w01-e1", b.TODAY + dt.timedelta(days=1)),
                                        b.session("w01-e2", b.TODAY + dt.timedelta(days=3))])
        self.garmin.faults["upload"] = {1: after(b.ReadTimeout("read"))}
        self.assertEqual(self.commit(pid, c["token"])["status"], "unknown")
        self.clock.advance(minutes=3)
        self.garmin.read_faults["library"] = b.GarminConnectTooManyRequestsError("Too many")
        writes = len(self.garmin.writes())
        out = self.commit(pid, c["token"])
        self.assertEqual(len(self.garmin.writes()), writes, "no write after Garmin answered 429")
        self.assertEqual((out["status"], out["retry_after_s"]), ("partial", 900))

    def test_a_malformed_listing_never_uploads_twice_dict(self):
        self._malformed(lambda: {"workouts": []})

    def test_a_malformed_listing_never_uploads_twice_none(self):
        self._malformed(lambda: None)

    def _malformed(self, fn):
        pid, c = self.unknown_upload()
        self.garmin.library_answer = fn
        self.commit(pid, c["token"])
        self.clock.advance(minutes=3)
        for _ in range(3):
            if self.commit(pid, c["token"])["status"] == "done":
                break
        self.assertEqual(len(self.garmin.uploads()), 1, "the landed upload is sent again")
        self.assert_no_double_writes(1)

    def test_a_lost_upload_is_sent_again_once(self):
        pid, c = self.unknown_upload(landed=False)
        out = self.commit(pid, c["token"])
        self.assertEqual(out["status"], "done", out)
        self.assertEqual(len(self.garmin.uploads()), 2)
        self.assert_no_double_writes(1)


if __name__ == "__main__":
    unittest.main()
