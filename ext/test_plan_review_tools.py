"""plan_review_week, plan_preview(accept_changes) and plan_apply_changes end to end, on one fake Garmin (the
reads of test_plan_tools, the writes of test_plan_writes) and MemoryPlanStore. Standard library only. Every
test joins the write helper's threads in its cleanup (ci-unittest fails on leaked threads)."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import unittest
import unittest.mock

import garmin_paths
import plan_engine as pe
import plan_review as pr
import plan_review_tools as prt
import plan_store as ps
import plan_tools as pt
import plan_writes as pw
import test_plan_tools as tpt
import test_plan_writes as tpw

BUILT = tpt.TODAY                       # 2027-11-24, a Wednesday: the plan is built and committed
REVIEW = dt.date(2027, 12, 6)           # the Monday after week 1 (2027-11-29): nothing was run in it
WEEK = dt.date(2027, 11, 29)
T0 = dt.datetime(2027, 11, 24, 7, 0, tzinfo=dt.timezone.utc)


class Garmin(tpw.FakeGarmin):
    """Writes and the calendar as test_plan_writes' fake, every other read as test_plan_tools' fake."""

    def __init__(self):
        super().__init__()
        self.reads = tpt.FakeClient()
        # no marathon or half prediction: these flows were written for the easy band on LT multiples (from the
        # marathon prediction the first weeks place less and the reviews propose other changes)
        self.reads.data["race_predictions"] = {"time5K": 1200}
        self.profile = 123456

    garmin_connect_activities = tpt.ACTIVITIES_PATH

    def get_stats(self, day):
        self._read("stats")
        return {"userProfileId": self.profile}

    def connectapi(self, path, params=None):
        return self.reads.connectapi(path, params)

    def get_training_status(self, d):
        return self.reads.get_training_status(d)

    def get_lactate_threshold(self):
        return self.reads.get_lactate_threshold()

    def get_race_predictions(self):
        return self.reads.get_race_predictions()

    def get_max_metrics(self, d):
        return self.reads.get_max_metrics(d)

    def get_hrv_data(self, d):
        return {}

    def get_sleep_data(self, d):
        return {}


def run(coro):
    return asyncio.run(coro)


class Flow(unittest.TestCase):
    COMMIT_DAYS = 14
    # extra preview arguments per test: week 2 capped under week 1, so pain in week 1 leaves nothing to hold
    # (the planned ACWR cap decides which of the two weeks is lighter; the cap only reduces, so it changes nothing else)
    PLAN_ARGS = {"test_advice_only_stores_no_version_and_gives_no_token": {
        "overrides": json.dumps([{"kind": "cap_week_km", "week_start": "2027-12-06", "km": 38}])}}

    def setUp(self):
        garmin_paths.allow_plan_writes(set(garmin_paths.WRITE_PATHS))
        self.addCleanup(garmin_paths.allow_plan_writes, ())
        self.garmin = Garmin()
        self.addCleanup(self._release)
        self.clock = tpw.Clock(T0)
        self.today = BUILT
        self.store = ps.MemoryPlanStore(clock=self.clock)
        self.store.meta[ps.META_ACCOUNT] = self.store.account_fp("123456")
        self.may_write = True
        self.confirm_answer = "unsupported"
        self.confirm_calls = []
        self.plans = pt.PlanService(lambda: self.garmin, lambda: self.store, lambda: self.today)
        self.plans.commit_hook = pw.commit_section

        async def confirm(ctx, **summary):
            self.confirm_calls.append(summary)
            return self.confirm_answer

        self.writes = pw.WriteService(lambda: self.garmin, lambda: self.store, lambda: self.today,
                                      may_write=lambda: self.may_write, confirm=confirm,
                                      executor_options={"sleep": lambda s: None, "join_s": 0.3})
        self.review_svc = prt.ReviewService(lambda: self.garmin, lambda: self.store, lambda: self.today,
                                            daily_level=self._level)
        self.review_svc.apply_hook = pw.apply_section
        self.level = None
        # Build and commit 14 days, then a week passes with nothing run.
        args = dict(tpt.ARGS, **getattr(self, "PLAN_ARGS", {}).get(self._testMethodName, {}))
        out = json.loads(run(self.plans.preview(**args, commit_days=self.COMMIT_DAYS)))
        self.assertTrue(out["invariants"]["ok"], out["invariants"]["failed"])
        self.pid = out["stored"]["plan_id"]
        for _ in range(6):   # at most 8 writes per call
            done = json.loads(run(self.writes.commit(plan_id=self.pid, commit_token=out["commit"]["token"])))
            if done["status"] != "partial":
                break
        self.assertEqual(done["status"], "done", done)
        self.today = REVIEW
        self.clock.advance(days=12)
        # Easy runs at RPE 6 in week 0 and week 1 (rule A4: it always lowers the next week), and pain 5 after
        # week 1's first easy run (rule A6: proposed only where the next week has more to hold or drop).
        week0, week1 = ([s["key"] for s in w["sessions"] if s["role"] in ("easy", "strides")] for w in out["weeks"][:2])
        self.store.save_feedback(self.pid, week0[0], 6, 0, "none", False)
        self.store.save_feedback(self.pid, week1[0], 6, 5, "calf", False)

    async def _level(self, client, day):
        return self.level

    def _release(self):
        self.garmin.blocked.set()
        self.assertTrue(pw.join_write_threads(10))

    def apply(self, token, ids):
        for _ in range(6):
            out = json.loads(run(self.writes.apply(self.pid, token, ids)))
            if out.get("status") != "partial":
                return out
        return out

    def review(self, **kw):
        return json.loads(run(self.review_svc.review(**{"plan_id": self.pid, **kw})))

    def test_review_proposes_and_nothing_changes_until_approved(self):
        writes_before = len(self.garmin.writes())
        out = self.review(week_start="2027-11-29")
        self.assertEqual((out["kind"], out["rules_version"], out["week_start"]), ("plan_review", 1, "2027-11-29"))
        rules = {c["rule"] for c in out["changes"]}
        self.assertIn("A4", rules)          # easy RPE 6 two weeks running
        self.assertEqual(out["metrics"]["completion_pct"], 0.0)
        self.assertNotIn("A7", rules)       # the long run is not settled yet: not a missed week
        self.assertTrue(all("physiotherapist or doctor" in c["advice"] for c in out["changes"] if c["rule"] == "A6"))
        self.assertTrue(all(c["status"] == "proposed" and c["reason"] for c in out["changes"]))
        self.assertTrue(all("edits" not in c for c in out["changes"]))
        self.assertEqual(len(self.garmin.writes()), writes_before)
        self.assertEqual(self.store.get_plan(self.store.account_fp("123456"), self.pid)["current_version"], 1)
        # The same review again stores the same ids.
        again = self.review(week_start="")
        self.assertEqual([c["change_id"] for c in again["changes"]], [c["change_id"] for c in out["changes"]])
        self.assertIn("plan_apply_changes", out["note"])
        self.assertTrue(out["apply"]["token"].startswith("at1."), out["apply"])

    def _refresh_args(self):
        seen = []
        real = prt.plan_tracking.refresh_plan

        def spy(store, plan, activities, scheduled, **kw):
            seen.append((activities, kw.get("cut")))
            return real(store, plan, activities, scheduled, **kw)

        with unittest.mock.patch.object(prt.plan_tracking, "refresh_plan", spy):
            out = self.review(week_start="2027-11-29")
        self.assertEqual(len(seen), 1)
        return out, seen[0]

    def test_a_failed_activity_search_settles_nothing(self):
        self.garmin.reads.fail["activities"] = RuntimeError("Garmin did not answer")
        with self.assertLogs(level="WARNING"):   # the failed read is logged, its traceback kept out of the run
            out, (activities, cut) = self._refresh_args()
        self.assertIn("activities", out["sources_failed"])
        self.assertIsNone(activities)   # not [] (which would settle the week's sessions missed)
        self.assertFalse(cut)
        self.assertTrue(all(r.get("link_status") not in ("missed", "replaced") for r in self.store.links(self.pid)))

    def test_a_full_activity_page_is_passed_as_cut(self):
        self.garmin.reads.data["activities"] = [
            {"activityId": 9000 + i, "activityType": {"typeKey": "walking"}, "startTimeLocal": "2027-11-19 08:00:00",
             "distance": 1000.0, "duration": 600.0} for i in range(prt.ACTIVITY_LIMIT)]
        _out, (activities, cut) = self._refresh_args()
        self.assertEqual(len(activities), prt.ACTIVITY_LIMIT)
        self.assertTrue(cut)
        self.garmin.reads.data["activities"] = []
        _out, (activities, cut) = self._refresh_args()
        self.assertEqual((activities, cut), ([], False))

    def test_bad_week_and_no_plan(self):
        self.assertIn("Monday", run(self.review_svc.review(plan_id=self.pid, week_start="2027-11-30")))
        self.assertIn("begun", run(self.review_svc.review(plan_id=self.pid, week_start="2027-12-13")))
        self.assertIn("not a week of this plan",
                      run(self.review_svc.review(plan_id=self.pid, week_start="2025-12-01")))
        self.assertTrue(run(self.review_svc.review(plan_id="f" * 32)).startswith("No data"))

    def test_accept_changes_builds_a_checked_version(self):
        out = self.review(week_start="2027-11-29")
        ids = [c["change_id"] for c in out["changes"]]
        acc = json.loads(run(self.plans.preview(plan_id=self.pid, accept_changes=",".join(ids + ["c" + "0" * 16]))))
        self.assertTrue(acc["invariants"]["ok"], acc["invariants"]["failed"])
        self.assertEqual(acc["stored"]["version"], 2)
        self.assertEqual(sorted(acc["changes"]["accepted"]), sorted(ids))
        self.assertEqual(acc["changes"]["refused"][-1]["rule"], "unknown_change")
        statuses = {r["change_id"]: r["status"] for r in self.store.changes_of(self.pid)}
        self.assertEqual(set(statuses.values()), {"approved"})
        self.assertTrue(run(self.plans.preview(plan_id="", accept_changes=ids[0])).startswith("Error: Invalid"))
        self.assertTrue(run(self.plans.preview(plan_id=self.pid, accept_changes="x")).startswith("Error: Invalid"))

    def test_apply_writes_once_and_replays(self):
        out = self.review(week_start="2027-11-29")
        apply = out["apply"]
        ids = ",".join(apply["change_ids"])
        self.assertEqual(json.loads(run(self.writes.apply(self.pid, apply["token"], "c" + "0" * 16)))["reason"],
                         "changes_mismatch")
        done = self.apply(apply["token"], ids)
        self.assertEqual(done["status"], "done", done)
        self.assertEqual(done["version"], 2)
        self.assertEqual(sorted(done["change_ids"]), sorted(apply["change_ids"]))
        self.assertEqual({r["status"] for r in self.store.changes_of(self.pid, apply["change_ids"])}, {"applied"})
        n = len(self.garmin.writes())
        again = json.loads(run(self.writes.apply(self.pid, apply["token"], ids)))
        self.assertTrue(again["replayed"])
        self.assertEqual(len(self.garmin.writes()), n)
        self.assertTrue(all(o["date"] > REVIEW.isoformat() for o in apply["ops"]) if isinstance(apply["ops"], list)
                        else True)
        # Today's workout is never touched without an A8 change.
        today_writes = [c for c in self.garmin.writes() if REVIEW.isoformat() in str(c)]
        self.assertEqual(today_writes, [])

    def test_apply_refusals(self):
        apply = self.review(week_start="2027-11-29")["apply"]
        ids = ",".join(apply["change_ids"])
        self.may_write = False
        self.assertEqual(json.loads(run(self.writes.apply(self.pid, apply["token"], ids)))["reason"],
                         "no_write_permission")
        self.may_write = True
        self.clock.advance(minutes=31)
        self.assertEqual(json.loads(run(self.writes.apply(self.pid, apply["token"], ids)))["reason"], "expired")
        # superseded: another version stored after the review (accepting the same changes would store the
        # version the token carries: that one resumes instead)
        apply = self.review(week_start="2027-11-29")["apply"]
        ids = ",".join(apply["change_ids"])
        run(self.plans.preview(**{**tpt.ARGS, "goal_time": "3:20:00"}, plan_id=self.pid))
        self.assertEqual(json.loads(run(self.writes.apply(self.pid, apply["token"], ids)))["reason"], "superseded")
        self.assertTrue(run(self.writes.apply(self.pid, "ct1." + "A" * 32, ids)).startswith("Error: Invalid"))
        self.assertTrue(run(self.writes.apply(self.pid, apply["token"], "")).startswith("Error: Invalid"))

    def test_declined_writes_nothing(self):
        apply = self.review(week_start="2027-11-29")["apply"]
        self.confirm_answer = "declined"
        n = len(self.garmin.writes())
        out = json.loads(run(self.writes.apply(self.pid, apply["token"], ",".join(apply["change_ids"]))))
        self.assertEqual(out["status"], "declined")
        self.assertEqual(len(self.garmin.writes()), n)
        self.assertEqual(self.confirm_calls[-1]["action"], "apply")

    def test_a_change_no_longer_proposed_supersedes_the_token(self):
        apply = self.review(week_start="2027-11-29")["apply"]
        self.store.save_changes(self.pid, WEEK, [])     # a later review of the week proposes nothing
        out = json.loads(run(self.writes.apply(self.pid, apply["token"], ",".join(apply["change_ids"]))))
        self.assertEqual(out["reason"], "superseded")

    def test_advice_only_stores_no_version_and_gives_no_token(self):
        # Only pain left: next week has no quality and is lighter, so A6 is its advice alone.
        for key in [r["workout_key"] for r in self.store.feedback(self.pid)]:
            self.store.save_feedback(self.pid, key, 3, 0, "none", False)
        week1 = [r for r in self.store.links(self.pid) if WEEK <= r["date_local"] < REVIEW]
        self.store.save_feedback(self.pid, str(week1[0]["workout_key"]), 3, 6, "calf", True)
        out = self.review(week_start="2027-11-29")
        kinds = {c["kind"] for c in out["changes"]}
        self.assertIn("pain_advice", kinds)
        advice = [c["change_id"] for c in out["changes"] if c["kind"] == "pain_advice"]
        acc = json.loads(run(self.plans.preview(plan_id=self.pid, accept_changes=",".join(advice))))
        self.assertEqual(acc["kind"], "plan_changes")
        self.assertEqual((acc["stored"]["changed"], acc["changes"]["accepted"]), (False, advice))
        self.assertEqual(self.store.get_plan(self.store.account_fp("123456"), self.pid)["current_version"], 1)
        if kinds == {"pain_advice"}:
            self.assertIsNone(out["apply"]["token"])

    def test_a_draft_is_never_reviewed(self):
        # Only the active plan is tracked and reviewed: a draft (here the only open plan) is refused with a
        # fixed text, by its plan_id and as the default, and nothing is stored for it.
        self.store.move_plan(self.pid, "cancelled")
        draft = json.loads(run(self.plans.preview(**tpt.ARGS)))["stored"]["plan_id"]
        self.assertEqual(self.store.get_plan(self.store.account_fp("123456"), draft)["status"], "draft")
        self.assertEqual(run(self.review_svc.review(plan_id=draft, week_start="2027-11-29")), pt.TEXT_DRAFT)
        self.assertEqual(run(self.review_svc.review(plan_id="", week_start="")), pt.TEXT_DRAFT)
        self.assertEqual((self.store.links(draft), self.store.changes_of(draft)), ([], []))

    def test_without_writes_no_apply(self):
        self.review_svc.apply_hook = None
        out = self.review(week_start="2027-11-29")
        self.assertNotIn("apply", out)
        self.assertNotIn("plan_apply_changes", out["note"])


class RedDay(Flow):
    """A8 end to end: 21 days on Garmin; on the threshold day (2027-12-14) the daily level is red."""

    COMMIT_DAYS = 21

    def test_apply_eases_only_todays_workout(self):
        for r in self.store.feedback(self.pid):    # no other rule fires
            self.store.save_feedback(self.pid, r["workout_key"], 3, 0, "none", False)
        self.today = dt.date(2027, 12, 14)
        self.clock.advance(days=8)
        self.level = "red"
        before = {r["workout_key"]: (r["date_local"], r["role"], r["dto_hash"]) for r in self.store.write_rows(self.pid)
                  if "~" not in r["workout_key"]}
        self.assertEqual(before.get("20271214-run", (None, None))[1], "threshold")
        out = self.review(week_start="2027-11-29")
        self.assertEqual([c["rule"] for c in out["changes"]], ["A8"])
        apply = out["apply"]
        self.assertEqual(apply["dates"], ["2027-12-14"])
        done = self.apply(apply["token"], ",".join(apply["change_ids"]))
        self.assertEqual(done["status"], "done", done)
        after = {r["workout_key"]: (r["date_local"], r["role"], r["dto_hash"]) for r in self.store.write_rows(self.pid)
                 if "~" not in r["workout_key"] and r["state"] in ("uploaded", "scheduled")}
        self.assertEqual(after["20271214-run"][1], "easy")
        for key, row in before.items():
            if key != "20271214-run" and key in after:
                self.assertEqual(after[key], row, key)
        fp = self.store.account_fp("123456")
        body = self.store.get_plan(fp, self.pid)["version_row"]["body"]
        q = next(s for w in body["weeks"] for s in w["sessions"] if s["key"] == "20271214-run")
        self.assertEqual(q["role"], "easy")


class Units(unittest.TestCase):
    def test_default_week_and_change_ids(self):
        self.assertEqual(prt.default_week(dt.date(2027, 12, 6)), dt.date(2027, 11, 29))
        self.assertEqual(prt.default_week(dt.date(2027, 12, 5)), dt.date(2027, 11, 29))
        self.assertEqual(pw.parse_change_ids("c%016x, c%016x" % (2, 1)), ["c%016x" % 1, "c%016x" % 2])
        for bad in ("", "c12", ",".join("c%016x" % i for i in range(21))):
            with self.assertRaises(pw.BadInput):
                pw.parse_change_ids(bad)

    def test_argument_checks(self):
        check = garmin_paths.ARGUMENT_CHECKS
        ok = "c" + "a" * 16
        self.assertTrue(check["plan_apply_changes"]["change_ids"][0](ok + "," + ok))
        self.assertFalse(check["plan_apply_changes"]["change_ids"][0](""))
        self.assertTrue(check["plan_preview"]["accept_changes"][0](""))
        self.assertFalse(check["plan_preview"]["accept_changes"][0]("cXYZ"))
        self.assertTrue(check["plan_apply_changes"]["apply_token"][0]("at1." + "A" * 32))
        self.assertFalse(check["plan_apply_changes"]["apply_token"][0]("ct1." + "A" * 32))
        self.assertTrue(check["plan_review_week"]["week_start"][0](""))
        self.assertTrue(check["plan_review_week"]["week_start"][0]("2027-11-29"))
        self.assertFalse(check["plan_review_week"]["week_start"][0]("30/11/2026"))

    def test_registration(self):
        added = {}
        prt.register(lambda fn, name=None, description=None, **kw: added.setdefault(name, description),
                     lambda: None, lambda: None, lambda: REVIEW, writes=True)
        self.assertIn("apply", added["plan_review_week"])
        self.assertTrue(added["plan_review_week"].isascii() and len(added["plan_review_week"]) < 2048)
        tools = {}
        pw.register(lambda fn, name=None, description=None, **kw: tools.setdefault(name, description),
                    lambda: None, lambda: None, lambda: REVIEW, apply=True)
        self.assertIn("plan_apply_changes", tools)
        self.assertLess(len(tools["plan_apply_changes"]), 2048)
        self.assertIsNotNone(pe.RULES and pr.REVIEW_RULES)


if __name__ == "__main__":
    unittest.main()
