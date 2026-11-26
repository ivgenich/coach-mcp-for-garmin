"""plan_store: one contract suite on MemoryPlanStore and, with COACH_TEST_DATABASE_URL, on PgPlanStore.

Standard library only for the memory leg; the Postgres leg needs psycopg and a Postgres (CI's pgvector
service), where history_store's migrations create the plan tables (003).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import importlib.util
import os
import re
import time
import unittest

import history_store as hs
import plan_store as ps

DB_URL = os.environ.get("COACH_TEST_DATABASE_URL", "").strip()
HAVE_PSYCOPG = importlib.util.find_spec("psycopg") is not None
PG_SKIP = "needs psycopg and COACH_TEST_DATABASE_URL (Postgres with pgvector)"
RACE = dt.date(2027, 2, 26)
HERE = os.path.dirname(os.path.abspath(__file__))


def h(text):
    return hashlib.sha256(text.encode()).hexdigest()


class Clock:
    def __init__(self):
        self.now = dt.datetime(2026, 11, 25, 8, 0, tzinfo=dt.timezone.utc)

    def __call__(self):
        return self.now


class StoreContract:
    """Every PlanStore passes these."""

    def make(self):
        raise NotImplementedError

    def later(self):
        raise NotImplementedError

    def set_meta(self, key, value):
        raise NotImplementedError

    def test_meta_keys_are_history_stores(self):
        self.assertEqual((ps.META_SALT, ps.META_ACCOUNT, ps.META_MISMATCH),
                         (hs.META_SALT, hs.META_ACCOUNT, hs.META_MISMATCH))

    def setUp(self):
        self.store = self.make()
        self.fp = self.store.account_fp("123456")

    def save(self, plan_id=None, content="a", fp=None, race=RACE):
        return self.store.save_preview(fp or self.fp, plan_id, race, 1, {"race_date": race.isoformat()},
                                       {"weeks": [], "content": content}, h(content))

    def test_fingerprint_is_stable_and_one_way(self):
        self.assertRegex(self.fp, r"^[0-9a-f]{64}$")
        self.assertEqual(self.store.account_fp(123456), self.fp)
        self.assertNotEqual(self.store.account_fp("654321"), self.fp)
        self.assertNotIn("123456", self.fp)

    def test_first_preview_makes_a_draft(self):
        pid, version, changed = self.save()
        self.assertRegex(pid, r"^[0-9a-f]{32}$")
        self.assertEqual((version, changed), (1, True))
        row = self.store.get_plan(self.fp, pid)
        self.assertEqual(row["status"], "draft")
        self.assertEqual(row["current_version"], 1)
        self.assertEqual(row["version_row"]["body"]["content"], "a")
        self.assertEqual(row["version_row"]["rules_version"], 1)

    def test_same_content_stores_nothing_new(self):
        pid, _, _ = self.save()
        self.assertEqual(self.save(pid), (pid, 1, False))
        pid2, version, changed = self.save(pid, "b")
        self.assertEqual((pid2, version, changed), (pid, 2, True))
        self.assertEqual(self.store.get_plan(self.fp, pid)["version_row"]["body"]["content"], "b")

    def test_same_content_keeps_the_draft_alive(self):
        pid, _, _ = self.save()
        before = self.store.get_plan(self.fp, pid)["updated_at"]
        self.later()
        self.assertEqual(self.save(pid), (pid, 1, False))
        self.assertGreater(self.store.get_plan(self.fp, pid)["updated_at"], before)

    def test_plans_are_read_per_account(self):
        other = self.store.account_fp("654321")
        mine, _, _ = self.save(content="a")
        self.assertIsNone(self.store.get_plan(other))
        self.assertIsNone(self.store.get_plan(other, mine))
        theirs, _, _ = self.save(content="b", fp=other)
        self.assertEqual(self.store.get_plan(other)["plan_id"], theirs)
        self.assertEqual(self.store.get_plan(self.fp)["plan_id"], mine)
        self.assertIsNone(self.store.get_plan(self.fp, theirs))
        self.assertEqual(self.store.get_plan(self.fp, mine)["account_fp"], self.fp)
        with self.assertRaises(ValueError):
            self.store.get_plan("short")

    def test_history_account(self):
        self.assertIsNone(self.store.history_account())
        self.set_meta(hs.META_ACCOUNT, self.fp)
        self.assertEqual(self.store.history_account(), self.fp)
        self.set_meta(hs.META_MISMATCH, "yes")
        self.assertIsNone(self.store.history_account())
        self.set_meta(hs.META_MISMATCH, "no")
        self.assertEqual(self.store.history_account(), self.fp)
        self.set_meta(hs.META_ACCOUNT, "not-a-fingerprint")
        self.assertIsNone(self.store.history_account())

    def test_latest_open_plan_without_an_id(self):
        self.assertIsNone(self.store.get_plan(self.fp))
        first, _, _ = self.save(content="x")
        latest = self.store.get_plan(self.fp)
        self.assertEqual(latest["plan_id"], first)

    def test_unknown_or_foreign_plan(self):
        pid, _, _ = self.save()
        with self.assertRaises(ps.PlanNotFound):
            self.save("0" * 32, "c")
        with self.assertRaises(ps.PlanNotFound):
            self.save(pid, "c", fp=self.store.account_fp("999"))
        self.assertIsNone(self.store.get_plan(self.fp, "f" * 32))

    def test_values_are_checked_before_any_sql(self):
        for args in (("short", None, RACE, 1, {}, {}, h("a")),
                     (self.fp, "not-hex", RACE, 1, {}, {}, h("a")),
                     (self.fp, None, "2027-02-26", 1, {}, {}, h("a")),
                     (self.fp, None, RACE, 0, {}, {}, h("a")),
                     (self.fp, None, RACE, True, {}, {}, h("a")),
                     (self.fp, None, RACE, 1, [], {}, h("a")),
                     (self.fp, None, RACE, 1, {}, {"x": float("nan")}, h("a")),
                     (self.fp, None, RACE, 1, {}, {}, "abc")):
            with self.subTest(args=str(args)[:60]):
                with self.assertRaises(ValueError) as cm:
                    self.store.save_preview(*args)
                self.assertNotIn("not-hex", str(cm.exception))
        with self.assertRaises(ValueError):
            self.store.account_fp("")
        with self.assertRaises(ValueError):
            self.store.account_fp(True)

    def test_workouts_empty_in_stage_1(self):
        pid, _, _ = self.save()
        self.assertEqual(self.store.workouts(pid), [])
        self.assertEqual(self.store.live_workouts(), 0)

    def test_install_tag_is_kept(self):
        tag = self.store.install_tag()
        self.assertRegex(tag, r"^[0-9a-f]{8}$")
        self.assertEqual(self.store.install_tag(), tag)

    def test_retention_keeps_fresh_drafts(self):
        pid, _, _ = self.save()
        self.assertEqual(self.store.retention()["plans"], 0)
        self.assertIsNotNone(self.store.get_plan(self.fp, pid))

    def test_ready(self):
        self.assertTrue(self.store.ready())

    # --- Stage 3: tracking links and feedback ---

    def link(self, key="20261202-run", day=dt.date(2026, 12, 1), status="done", activity=101, role="easy"):
        return {"workout_key": key, "date_local": day, "role": role, "dto_hash": h(key),
                "linked_activity_id": activity, "link_status": status}

    def test_tracked_plan_is_the_accounts_latest_open_plan(self):
        self.assertIsNone(self.store.tracked_plan(self.fp))
        first, _, _ = self.save(content="x")
        other = self.store.account_fp("654321")
        foreign, _, _ = self.save(content="y", fp=other)
        self.assertEqual(self.store.tracked_plan(self.fp)["plan_id"], first)
        self.assertEqual(self.store.tracked_plan(self.fp)["version_row"]["body"]["content"], "x")
        self.assertEqual(self.store.tracked_plan(other)["plan_id"], foreign)
        with self.assertRaises(ValueError):
            self.store.tracked_plan("not-a-fingerprint")

    def test_links_are_upserted_as_local_rows(self):
        pid, _, _ = self.save()
        self.assertEqual(self.store.links(pid), [])
        self.assertEqual(self.store.save_links(pid, 1, [self.link(), self.link("20300220-run", dt.date(2026, 12, 6),
                                                                              "missed", None, "long")]), 2)
        rows = {r["workout_key"]: r for r in self.store.links(pid)}
        self.assertEqual(rows["20261202-run"]["state"], ps.LOCAL_STATE)
        self.assertEqual((rows["20261202-run"]["link_status"], rows["20261202-run"]["linked_activity_id"]), ("done", 101))
        self.assertIsNone(rows["20261202-run"]["garmin_workout_id"])
        self.assertIsNotNone(rows["20261202-run"]["link_checked_at"])
        self.assertEqual(rows["20300220-run"]["link_status"], "missed")
        self.store.save_links(pid, 2, [self.link(day=dt.date(2026, 12, 2), status="partial_late", activity=102)])
        rows = {r["workout_key"]: r for r in self.store.links(pid)}
        self.assertEqual((rows["20261202-run"]["link_status"], rows["20261202-run"]["linked_activity_id"],
                          rows["20261202-run"]["date_local"], rows["20261202-run"]["version"]),
                         ("partial_late", 102, dt.date(2026, 12, 2), 2))
        self.assertEqual(self.store.live_workouts(), 0)
        self.assertEqual(len(self.store.workouts(pid)), 2)

    def test_link_values_are_checked_before_any_sql(self):
        pid, _, _ = self.save()
        bad = [dict(workout_key="x; drop"), dict(date_local="2026-12-01"), dict(role="Easy run"),
               dict(dto_hash="nothex"), dict(linked_activity_id=-1), dict(linked_activity_id=True),
               dict(link_status="great")]
        for change in bad:
            with self.subTest(change=change):
                with self.assertRaises(ValueError) as cm:
                    self.store.save_links(pid, 1, [{**self.link(), **change}])
                self.assertNotIn("drop", str(cm.exception))
        with self.assertRaises(ValueError):
            self.store.save_links(pid, 0, [self.link()])
        with self.assertRaises(ps.PlanNotFound):
            self.store.save_links("a" * 32, 1, [self.link()])
        self.assertEqual(self.store.links(pid), [])

    def test_feedback_is_upserted(self):
        pid, _, _ = self.save()
        self.assertTrue(self.store.save_feedback(pid, "20261202-run", 4, 0, "none", False))
        self.assertFalse(self.store.save_feedback(pid, "20261202-run", 6, 3, "calf", True))
        self.assertTrue(self.store.save_feedback(pid, "20261203-run", 7, 0, "none", False))
        rows = self.store.feedback(pid)
        self.assertEqual([(r["workout_key"], r["rpe"], r["pain"], r["pain_location"], r["sharp"]) for r in rows],
                         [("20261202-run", 6, 3, "calf", True), ("20261203-run", 7, 0, "none", False)])
        for args in (("20261202-run", 0, 0, "none", False), ("20261202-run", 5, 11, "none", False),
                     ("20261202-run", 5, 1, "left knee", False), ("20261202-run", 5, 1, "knee", "yes"),
                     ("bad key", 5, 0, "none", False), ("20261202-run", True, 0, "none", False)):
            with self.subTest(args=args):
                with self.assertRaises(ValueError) as cm:
                    self.store.save_feedback(pid, *args)
                self.assertNotIn("left knee", str(cm.exception))
        with self.assertRaises(ps.PlanNotFound):
            self.store.save_feedback("a" * 32, "20261202-run", 4, 0, "none", False)


class WriteContract(StoreContract):
    """Stage 2: tokens and workout rows, on every PlanStore."""

    def test_token_round_trip(self):
        pid, version, _ = self.save()
        ops = {"kind": "commit", "ops": [{"op": "create", "key": "20261202-run", "date": "2026-12-02"}]}
        token, expires = self.store.issue_token(pid, version, "commit", ops)
        self.assertRegex(token, r"^ct1\.[A-Za-z0-9_-]{32}$")
        self.assertGreater(expires, self.store.now())
        row = self.store.token_row(token, pid, "commit")
        self.assertEqual((row["status"], row["version"], row["ops"]), ("issued", version, ops))
        self.assertEqual(row["ops_hash"], ps.ops_digest(ops))
        self.assertEqual(row["token_hash"], ps.token_hash(token))
        with self.assertRaises(ps.TokenRefused):
            self.store.token_row("ct1." + "B" * 32, pid, "commit")
        self.store.claim_token(token)
        row = self.store.token_row(token, pid, "commit")
        self.assertEqual(row["status"], "running")
        self.assertGreater(row["expires_at"], expires)
        self.store.finish_token(token, "done", {"status": "done", "written": []})
        row = self.store.token_row(token, pid, "commit")
        self.assertEqual((row["status"], row["result"]), ("done", {"status": "done", "written": []}))
        self.assertIsNotNone(row["done_at"])

    def test_workout_rows_and_plan_moves(self):
        pid, version, _ = self.save()
        row = {"workout_key": "20261202-run", "version": version, "date_local": dt.date(2026, 12, 2), "role": "easy",
               "dto_hash": h("dto"), "state": "pending"}
        self.store.put_workout(pid, row)
        self.store.put_workout(pid, {**row, "state": "scheduled", "garmin_workout_id": 11, "garmin_schedule_id": 12})
        self.store.put_workout(pid, {**row, "workout_key": "20261202-run~abcdef01", "state": "deleted"})
        rows = {r["workout_key"]: r for r in self.store.write_rows(pid)}
        self.assertEqual(rows["20261202-run"]["garmin_workout_id"], 11)
        self.assertEqual(rows["20261202-run"]["state"], "scheduled")
        self.assertIsNone(rows["20261202-run"]["linked_activity_id"])
        self.assertIsNotNone(rows["20261202-run"]["state_changed_at"])
        self.assertEqual(len(self.store.all_write_rows()), 2)
        self.assertEqual(self.store.live_workouts(), 1)
        self.store.move_plan(pid, "active")
        self.assertEqual(self.store.get_plan(self.fp, pid)["status"], "active")
        other, _, _ = self.save(content="b")
        with self.assertRaises(ps.PlanConflict):
            self.store.move_plan(other, "active")
        self.store.move_plan(pid, "cancelled")
        plan = self.store.get_plan(self.fp, pid)
        self.assertEqual(plan["status"], "cancelled")
        self.assertIsNotNone(plan["ended_at"])
        self.store.move_plan(other, "active")


    def test_a_plan_whose_race_passed_is_finished_and_blocks_nothing(self):
        race = dt.date(2026, 11, 1)
        old, _, _ = self.save(content="x", race=race)
        self.store.move_plan(old, "active")
        new, _, _ = self.save(content="y")
        with self.assertRaises(ps.PlanConflict):
            self.store.move_plan(new, "active")                    # no day: the plain check
        with self.assertRaises(ps.PlanConflict):
            self.store.move_plan(new, "active", today=race)        # race day itself: not passed yet
        self.store.move_plan(new, "active", today=race + dt.timedelta(days=1))
        ended = self.store.get_plan(self.fp, old)
        self.assertEqual(ended["status"], "finished")
        self.assertIsNotNone(ended["ended_at"])
        self.assertEqual(self.store.get_plan(self.fp, new)["status"], "active")
        self.assertEqual(self.store.get_plan(self.fp)["plan_id"], new)
        self.assertEqual(self.store.finish_past(RACE), 0, "the new plan's race is still ahead")
        self.assertEqual(self.store.finish_past(RACE + dt.timedelta(days=1)), 1)
        self.assertEqual(self.store.get_plan(self.fp, new)["status"], "finished")
        self.assertIsNone(self.store.get_plan(self.fp), "a finished plan is not open")
        self.assertEqual(self.store.finish_past(RACE + dt.timedelta(days=1)), 0)
        # Kept 392 days after it ended, then dropped with its rows.
        ended_at = self.store.get_plan(self.fp, old)["ended_at"]
        self.assertEqual(self.store.retention(ended_at + dt.timedelta(days=ps.ENDED_DAYS) - dt.timedelta(hours=1))
                         ["plans"], 0)
        self.assertGreaterEqual(self.store.retention(ended_at + dt.timedelta(days=ps.ENDED_DAYS, hours=1))["plans"],
                                1)
        self.assertIsNone(self.store.get_plan(self.fp, old))

    def test_finish_past_only_touches_active_plans_of_the_account(self):
        race = dt.date(2026, 11, 1)
        draft, _, _ = self.save(content="d", race=race)
        other_fp = self.store.account_fp("654321")
        foreign, _, _ = self.save(content="f", fp=other_fp, race=race)
        self.store.move_plan(foreign, "active")
        self.assertEqual(self.store.finish_past(race + dt.timedelta(days=1), self.fp), 0)
        self.assertEqual(self.store.get_plan(self.fp, draft)["status"], "draft")
        self.assertEqual(self.store.get_plan(other_fp, foreign)["status"], "active")
        with self.assertRaises(ValueError):
            self.store.finish_past("2026-11-02")

    def test_live_workouts_from_a_day_counts_what_cleanup_deletes(self):
        pid, version, _ = self.save()
        day = dt.date(2026, 12, 2)
        for key, state, wid, when in (("20261202-run", "scheduled", 11, day),
                                      ("20261205-run", "scheduled", 12, day - dt.timedelta(days=1)),   # past
                                      ("20300213-run", "unscheduled", 13, day),
                                      ("20300214-run", "deleted", 14, day),
                                      ("20300215-run", "unknown", None, day),                         # no id
                                      ("20300216-run", "scheduled", 16, day),                         # linked
                                      ("20261202-run~abcdef01-2", "scheduled", 15, day)):
            self.store.put_workout(pid, {"workout_key": key, "version": version, "date_local": when,
                                         "role": "easy", "dto_hash": h(key), "state": state,
                                         "garmin_workout_id": wid})
        self.store.save_links(pid, version, [self.link(key="20300216-run", day=day, activity=77)])
        self.assertEqual(self.store.live_workouts(on_or_after=day), 3)
        self.assertEqual(self.store.live_workouts(on_or_after=day + dt.timedelta(days=1)), 0)
        with self.assertRaises(ValueError):
            self.store.live_workouts(on_or_after="2026-12-02")

    def test_tracked_plan_prefers_the_active_one(self):
        active, _, _ = self.save(content="x")
        self.save(content="y")
        self.store.move_plan(active, "active")
        self.save(content="z")
        self.assertEqual(self.store.tracked_plan(self.fp)["plan_id"], active)

    def test_a_garmin_row_keeps_its_state_and_date(self):
        pid, _, _ = self.save()
        self.store.put_workout(pid, {"workout_key": "20261202-run", "version": 1, "date_local": dt.date(2026, 12, 1),
                                     "role": "easy", "dto_hash": h("dto"), "state": "scheduled",
                                     "garmin_workout_id": 9001})
        self.store.save_links(pid, 2, [self.link(day=dt.date(2026, 12, 3))])
        row = self.store.links(pid)[0]
        self.assertEqual((row["state"], row["date_local"], row["version"], row["link_status"],
                          row["garmin_workout_id"]), ("scheduled", dt.date(2026, 12, 1), 1, "done", 9001))
        self.assertEqual(self.store.live_workouts(), 1)

    def test_a_commit_takes_over_a_local_row_and_keeps_its_link(self):
        pid, _, _ = self.save()
        self.store.save_links(pid, 1, [self.link(status="done_early", activity=55)])
        self.store.put_workout(pid, {"workout_key": "20261202-run", "version": 2, "date_local": dt.date(2026, 12, 2),
                                     "role": "easy", "dto_hash": h("dto"), "state": "pending"})
        row = self.store.write_rows(pid)[0]
        self.assertEqual((row["state"], row["version"], row["date_local"], row["link_status"],
                          row["linked_activity_id"]), ("pending", 2, dt.date(2026, 12, 2), "done_early", 55))
        self.assertEqual(len(self.store.write_rows(pid)), 1)

    # --- Stage 4: plan_changes -------------------------------------------------------------------------------

    def change(self, n, kind="repeat_volume", km=40.0):
        return {"change_id": "c%016x" % n, "kind": kind, "from_value": {"value": {"km": 50.0}, "rule": "A1"},
                "to_value": {"value": {"km": km}, "edits": [{"op": "cap_week", "week_start": "2026-12-07",
                                                             "km": km}]},
                "reason": "You ran 60% of this week's due km.", "evidence": {"metric": "completion_pct",
                                                                             "value": 60.0, "threshold": 70}}

    def test_changes_round_trip_and_move_forward_only(self):
        pid, _, _ = self.save()
        week = dt.date(2026, 11, 30)
        rows = self.store.save_changes(pid, week, [self.change(1), self.change(2, "early_deload")])
        self.assertEqual([(r["change_id"], r["status"]) for r in rows],
                         [("c%016x" % 1, "proposed"), ("c%016x" % 2, "proposed")])
        self.assertEqual(rows[0]["to_value"]["edits"][0]["km"], 40.0)
        self.assertEqual((rows[0]["week_start"], rows[0]["kind"]), (week, "repeat_volume"))
        self.assertEqual(self.store.set_change_status(pid, ["c%016x" % 1], "approved"), 1)
        self.assertEqual(self.store.set_change_status(pid, ["c%016x" % 1], "proposed"), 0)   # never back
        self.assertEqual(self.store.set_change_status(pid, ["c%016x" % 1, "c%016x" % 9], "applied"), 1)
        # A review again: the applied one keeps its values; a proposed one not proposed again is removed.
        rows = self.store.save_changes(pid, week, [self.change(1, km=30.0), self.change(3)])
        self.assertEqual([(r["change_id"], r["status"]) for r in rows],
                         [("c%016x" % 1, "applied"), ("c%016x" % 3, "proposed")])
        self.assertEqual(rows[0]["to_value"]["value"]["km"], 40.0)
        self.assertEqual([r["change_id"] for r in self.store.changes_of(pid, ["c%016x" % 3])], ["c%016x" % 3])
        self.assertEqual(self.store.changes_of(pid, week_start=dt.date(2026, 12, 7)), [])
        other, _, _ = self.save(content="b", fp=self.store.account_fp("999"))
        self.assertEqual(self.store.set_change_status(other, ["c%016x" % 3], "approved"), 0)
        self.assertEqual(self.store.changes_of(other), [])

    def test_changes_are_checked(self):
        pid, _, _ = self.save()
        week = dt.date(2026, 11, 30)
        for bad in ({**self.change(1), "kind": "raise_volume"}, {**self.change(1), "change_id": "x1"},
                    {**self.change(1), "reason": "bad\nline"}, {**self.change(1), "to_value": {"x": "y" * 9000}}):
            with self.subTest(bad=str(bad)[:40]), self.assertRaises(ValueError):
                self.store.save_changes(pid, week, [bad])
        with self.assertRaises(ValueError):
            self.store.set_change_status(pid, ["c%016x" % 1], "rejected")
        with self.assertRaises(ps.PlanNotFound):
            self.store.save_changes("f" * 32, week, [self.change(1)])
        self.assertEqual(self.store.changes_of(pid), [])


class MemoryStore(WriteContract, unittest.TestCase):
    def make(self):
        self.clock = Clock()
        return ps.MemoryPlanStore(clock=self.clock)

    def test_feedback_goes_with_its_plan(self):
        pid, _, _ = self.save()
        self.store.save_feedback(pid, "20261202-run", 4, 0, "none", False)
        self.store.set_status(pid, "cancelled")
        self.clock.now += dt.timedelta(days=ps.ENDED_DAYS + 1)
        self.store.retention()
        self.assertEqual(self.store.feedback_rows, {})
    def later(self):
        self.clock.now += dt.timedelta(minutes=1)

    def set_meta(self, key, value):
        self.store.meta[key] = value

    def test_retention_drops_old_drafts_and_ended_plans(self):
        old, _, _ = self.save(content="old")
        self.clock.now += dt.timedelta(days=ps.DRAFT_DAYS + 1)
        fresh, _, _ = self.save(content="fresh")
        ended, _, _ = self.save(content="ended")
        self.store.set_status(ended, "cancelled")
        self.store.tokens["t"] = {"plan_id": fresh, "expires_at": self.clock.now - dt.timedelta(days=8)}
        out = self.store.retention()
        self.assertEqual(out, {"plans": 1, "tokens": 1})
        self.assertIsNone(self.store.get_plan(self.fp, old))
        self.assertIsNotNone(self.store.get_plan(self.fp, ended))
        self.clock.now += dt.timedelta(days=ps.ENDED_DAYS + 1)
        self.store.retention()
        self.assertIsNone(self.store.get_plan(self.fp, ended))

    def test_set_status_checks_the_value(self):
        pid, _, _ = self.save()
        with self.assertRaises(ValueError):
            self.store.set_status(pid, "running")
        self.store.set_status(pid, "active")
        self.assertEqual(self.store.get_plan(self.fp)["status"], "active")

    def test_live_workouts_counts_only_live_states(self):
        pid, _, _ = self.save()
        self.store.workout_rows[(pid, "20300220-run")] = {"workout_key": "20300220-run", "state": "scheduled",
                                                      "date_local": RACE}
        self.store.workout_rows[(pid, "20261202-run")] = {"workout_key": "20261202-run", "state": "deleted",
                                                    "date_local": RACE}
        self.assertEqual(self.store.live_workouts(), 1)
        self.assertEqual([r["workout_key"] for r in self.store.workouts(pid)], ["20261202-run", "20300220-run"])


class Helpers(unittest.TestCase):
    def test_fingerprint_matches_history_store(self):
        salt = "11" * 32
        self.assertEqual(ps.fingerprint(salt, " 42 "), hs.fingerprint(salt, "42"))
        self.assertEqual(ps.META_SALT, hs.META_SALT)

    def test_memory_store_shares_the_salt_rule(self):
        store = ps.MemoryPlanStore()
        store.meta[ps.META_SALT] = "22" * 32
        self.assertEqual(store.account_fp("7"), hs.fingerprint("22" * 32, "7"))

    def test_check_plan_id(self):
        self.assertEqual(ps.check_plan_id("a" * 32), "a" * 32)
        for bad in ("A" * 32, "a" * 31, None, 5, "a" * 32 + "\n"):
            with self.assertRaises(ValueError):
                ps.check_plan_id(bad)

    def test_for_history_store(self):
        attached = ps.MemoryPlanStore()
        mem = hs.MemoryHistoryStore()
        self.assertIsNone(ps.for_history_store(mem))
        mem.plan_store = attached
        self.assertIs(ps.for_history_store(mem), attached)

    def test_pg_store_hides_the_url(self):
        if not HAVE_PSYCOPG:
            self.skipTest("needs psycopg")
        self.assertEqual(repr(ps.PgPlanStore("postgresql://u:secret@h/db")), "PgPlanStore()")
        with self.assertRaises(ValueError):
            ps.PgPlanStore(" ")


class SweepRetention(unittest.TestCase):
    """history_jobs.sweep runs the plans' retention beside the history's; a failure never stops it."""

    def test_the_attached_store_is_swept(self):
        import history_jobs

        clock = Clock()
        plans = ps.MemoryPlanStore(clock=clock)
        fp = plans.account_fp("1")
        pid, _, _ = plans.save_preview(fp, None, RACE, 1, {}, {}, h("x"))
        clock.now += dt.timedelta(days=ps.DRAFT_DAYS + 1)
        history = hs.MemoryHistoryStore()
        history.plan_store = plans
        history_jobs._plan_retention(history)
        self.assertIsNone(plans.get_plan(fp, pid))

    def test_the_sweep_finishes_a_plan_whose_race_passed(self):
        import history_jobs

        plans = ps.MemoryPlanStore(clock=Clock())
        fp = plans.account_fp("1")
        pid, _, _ = plans.save_preview(fp, None, RACE, 1, {}, {}, h("x"))
        plans.move_plan(pid, "active")
        history = hs.MemoryHistoryStore()
        history.plan_store = plans
        history_jobs._plan_retention(history, RACE)
        self.assertEqual(plans.get_plan(fp, pid)["status"], "active")
        history_jobs._plan_retention(history, RACE + dt.timedelta(days=1))
        self.assertEqual(plans.get_plan(fp, pid)["status"], "finished")

    def test_a_failure_is_logged_with_its_type_only(self):
        import history_jobs

        class Broken:
            def retention(self):
                raise ps.StoreError("plans: database error TEST-ONLY")

        history = hs.MemoryHistoryStore()
        history.plan_store = Broken()
        with self.assertLogs("garmin_ext.history", "WARNING") as logs:
            history_jobs._plan_retention(history)
        self.assertEqual(logs.output, ["WARNING:garmin_ext.history:plans: retention skipped (StoreError)"])


class Migration(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(HERE, "history_sql", "003_training_plans.sql"), encoding="ascii") as fh:
            self.sql = fh.read()

    def test_creates_every_stage_table_without_checks(self):
        for table in ("plans", "plan_versions", "plan_workouts", "plan_tokens", "plan_feedback", "plan_changes"):
            self.assertIn(f"CREATE TABLE IF NOT EXISTS history.{table} (", self.sql)
        code = "\n".join(line.split("--")[0] for line in self.sql.splitlines())
        self.assertNotRegex(code.upper(), r"\bCHECK\b")
        self.assertNotIn("DROP ", code.upper())

    def test_states_in_code_match_the_comment(self):
        comment = re.search(r"-- state: ([a-z, \n-]+?)\.\n", self.sql).group(1)
        states = [s.strip(" -\n") for s in comment.replace("\n--", " ").split(",")]
        self.assertEqual(tuple(states), ps.WORKOUT_STATES)

    def test_memory_history_store_migrates_it(self):
        self.assertIn(3, [v for v, _n, _p in hs.migration_files()])


@unittest.skipUnless(DB_URL and HAVE_PSYCOPG, PG_SKIP)
class PostgresStore(WriteContract, unittest.TestCase):
    def make(self):
        history = hs.PgHistoryStore(DB_URL)
        history.forget_all()
        history.migrate()
        self.addCleanup(history.forget_all)
        self.history = history
        return ps.PgPlanStore(DB_URL)

    def later(self):
        time.sleep(0.01)   # updated_at is the transaction's now()

    def set_meta(self, key, value):
        self.history.meta_set(key, value)

    def test_not_ready_before_migration(self):
        hs.PgHistoryStore(DB_URL).forget_all()
        self.assertFalse(self.store.ready())
        with self.assertRaises(ps.StoreError) as cm:
            self.save()
        self.assertNotIn("secret", str(cm.exception))

    def test_retention_on_the_callers_connection(self):
        pid, _, _ = self.save()
        with self.store.transaction() as conn:
            self.assertEqual(self.store.retention(conn=conn), {"plans": 0, "tokens": 0})
        future = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=ps.DRAFT_DAYS + 1)
        self.assertEqual(self.store.retention(future)["plans"], 1)
        self.assertIsNone(self.store.get_plan(self.fp, pid))


if __name__ == "__main__":
    unittest.main()
