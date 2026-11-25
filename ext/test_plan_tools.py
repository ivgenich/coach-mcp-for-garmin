"""plan_tools: the three plan tools on a fake Garmin client and MemoryPlanStore. Standard library only."""

from __future__ import annotations

import asyncio
import copy
import datetime as dt
import json
import threading
import unittest

import plan_engine as pe
import plan_store as ps
import plan_tools as pt
import status_common as sc

TODAY = dt.date(2026, 11, 25)
RACE = "2027-02-26"
ACTIVITIES_PATH = "/activitylist-service/activities/search/activities"


class GarminConnectTooManyRequestsError(Exception):
    """Same name and module as garminconnect's (the tool-error table reads the module)."""
    __module__ = "garminconnect.exceptions"


def _status(acute, chronic):
    """A training-status answer as Garmin gives it (one primary device)."""
    return {"mostRecentTrainingStatus": {"latestTrainingStatusData": {"1": {
        "primaryTrainingDevice": True,
        "acuteTrainingLoadDTO": {"dailyTrainingLoadAcute": acute, "dailyTrainingLoadChronic": chronic}}}}}


def _course_payload(km_total=42.2, climb=(10.0, 12.0, 60.0)):
    """A synthetic course: flat at 10 m, one steady climb (start km, end km, gain m)."""
    points = []
    for i in range(int(km_total * 10) + 1):
        d = i * 100.0
        a, b, gain = climb
        rise = min(max(d - a * 1000, 0.0), (b - a) * 1000) / ((b - a) * 1000) * gain
        points.append({"distance": d, "elevation": 10.0 + rise, "latitude": None, "longitude": None})
    return {"courseId": 4242, "courseName": "SECRET COURSE NAME", "activityTypePk": 1, "geoPoints": points}


def default_data():
    runs = []
    first = pe.monday(TODAY) - dt.timedelta(days=56)
    for week in range(8):
        for i, km in enumerate((8, 10, 8, 14)):
            day = first + dt.timedelta(days=7 * week + 2 * i)
            runs.append({"activityType": {"typeKey": "running"}, "distance": km * 1000.0,
                         "duration": km * 300.0, "activityTrainingLoad": km * 8.0,
                         "startTimeLocal": f"{day.isoformat()} 07:00:00", "activityName": "IGNORE ME"})
    runs.append({"activityType": {"typeKey": "trail_running"}, "distance": 20900.0,
                 "startTimeLocal": f"{(first + dt.timedelta(days=48)).isoformat()} 07:00:00"})
    runs.append({"activityType": {"typeKey": "cycling"}, "distance": 90000.0, "duration": 10800.0,
                 "activityTrainingLoad": 180.0,
                 "startTimeLocal": f"{(first + dt.timedelta(days=20)).isoformat()} 07:00:00"})
    return {
        f"stats:{TODAY.isoformat()}": {"userProfileId": 123456},
        "activities": runs,
        "lactate_threshold": {"speed_and_heart_rate": {"speed": 0.385, "heartRate": 168}},
        "race_predictions": {"timeMarathon": 12900, "timeHalfMarathon": 6120, "time5K": 1200},
        f"max_metrics:{TODAY.isoformat()}": [{"generic": {"vo2MaxPreciseValue": 52.4}}],
        # the model's ratios on these days are 0.62, 0.88 and 0.88: Garmin's within 0.15
        f"training_status:{TODAY.isoformat()}": _status(200, 290),
        f"training_status:{(TODAY - dt.timedelta(days=14)).isoformat()}": _status(330, 360),
        f"training_status:{(TODAY - dt.timedelta(days=28)).isoformat()}": _status(300, 360),
        "course:4242": _course_payload(),
        "schedule": {"data": {"workoutScheduleSummariesScalar": [
            {"scheduleDate": "2026-12-16", "workoutName": "VO2 intervals", "workoutType": "running",
             "estimatedDurationInSecs": 3600, "isRestDay": False},
            {"scheduleDate": "2026-12-19", "workoutName": "Coach Long run", "workoutType": "running",
             "estimatedDistanceInMeters": 20000},
        ]}},
    }


class FakeClient:
    garmin_connect_activities = ACTIVITIES_PATH

    def __init__(self, data=None, fail=None):
        self.data = default_data() if data is None else data
        self.fail = fail or {}
        self.calls = []
        self.params = []
        self._lock = threading.Lock()

    def _serve(self, key, empty):
        with self._lock:
            self.calls.append(key)
        if key in self.fail:
            raise self.fail[key]
        return copy.deepcopy(self.data.get(key, empty))

    def get_stats(self, d):
        return self._serve(f"stats:{d}", {})

    def connectapi(self, path, params=None):
        if path.startswith("/course-service/course/"):
            return self._serve("course:" + path.rsplit("/", 1)[1], {})
        assert path == ACTIVITIES_PATH, path
        self.params.append(params)
        return self._serve("activities", [])

    def get_training_status(self, d):
        return self._serve(f"training_status:{d}", {})

    def get_lactate_threshold(self):
        return self._serve("lactate_threshold", {})

    def get_race_predictions(self):
        return self._serve("race_predictions", {})

    def get_max_metrics(self, d):
        return self._serve(f"max_metrics:{d}", [])

    def query_garmin_graphql(self, query):
        return self._serve("schedule", {"data": {"workoutScheduleSummariesScalar": []}})


class BrokenStore:
    def __init__(self, ready=True):
        self._ready = ready

    def ready(self):
        if self._ready is None:
            raise ps.StoreError("plans: cannot connect to the database")
        return self._ready

    def account_fp(self, profile_id):
        raise ps.StoreError("plans: database error")

    def history_account(self):
        raise ps.StoreError("plans: database error")

    def get_plan(self, account_fp, plan_id=None):
        raise ps.StoreError("plans: database error")


def run(coro):
    return asyncio.run(coro)


ARGS = {"race_date": RACE, "goal_time": "3:30:00", "b_races": "2027-01-29:tri_half"}


class Base(unittest.TestCase):
    def setUp(self):
        self.client = FakeClient()
        self.store = ps.MemoryPlanStore()
        # the account the history index recorded: the fake Garmin's
        self.store.meta[ps.META_ACCOUNT] = self.store.account_fp("123456")
        self.svc = pt.PlanService(lambda: self.client, lambda: self.store, lambda: TODAY)

    def assess(self, **kw):
        return run(self.svc.assess(**{**ARGS, **kw}))

    def preview(self, **kw):
        return run(self.svc.preview(**{**ARGS, **kw}))


class Assess(Base):
    def test_numbers_from_garmin(self):
        out = json.loads(self.assess())
        self.assertEqual(out["kind"], "plan_assessment")
        self.assertEqual([w["km"] for w in out["history"]], [40.0] * 6 + [60.9, 40.0])
        self.assertEqual(out["longest_run_8w_km"], 20.9)
        self.assertAlmostEqual(out["lt_speed_mps"], 3.85, places=2)
        self.assertEqual(out["vo2max"], 52.4)
        self.assertEqual(out["prediction"], "3:35:00")
        self.assertEqual(out["sources_failed"], [])
        self.assertIn("fixed plan rules", out["note"])
        self.assertNotIn("IGNORE ME", json.dumps(out))

    def test_activity_query_covers_eight_full_weeks(self):
        self.assess()
        self.assertEqual(self.client.params[0]["startDate"], "2026-09-28")
        self.assertEqual(self.client.params[0]["endDate"], TODAY.isoformat())
        self.assertEqual(self.client.calls[0], f"stats:{TODAY.isoformat()}")

    def test_bad_arguments_never_echo_the_value(self):
        cases = [{"race_date": "2027-02-30"}, {"race_date": "soon secret"}, {"race_date": "2026-11-30"},
                 {"race_distance": "ultra secret"}, {"goal_time": "fast secret"}, {"b_races": "2027-01-29:swim"},
                 {"b_races": "2028-01-01:run"}, {"run_days": "funday"}, {"quality_per_week": 5},
                 {"max_long_km": "x"}, {"long_run_day": "secret"}, {"rest_days": "mon,tue,wed,thu,fri,sat,sun"}]
        for case in cases:
            with self.subTest(case=case):
                out = self.assess(**case)
                self.assertTrue(out.startswith("Error: Invalid input: "), out)
                self.assertNotIn("secret", out)
        self.assertEqual(self.client.calls, [])

    def test_systemic_failure_answers_like_the_status_tools(self):
        self.client.fail = {f"stats:{TODAY.isoformat()}": GarminConnectTooManyRequestsError(
            "Garmin rate limit hit: 429. Wait a few minutes")}
        self.assertEqual(self.assess(), "Error: Garmin rate limit")
        self.assertEqual(self.client.calls, [f"stats:{TODAY.isoformat()}"])

    def test_a_failed_source_is_listed(self):
        self.client.fail = {"lactate_threshold": RuntimeError("boom")}
        out = json.loads(self.assess())
        self.assertEqual(out["sources_failed"], ["lactate_threshold"])
        self.assertIsNone(out["lt_pace"])


class Preview(Base):
    def test_preview_is_stored_once_per_content(self):
        out = json.loads(self.preview())
        self.assertEqual(out["kind"], "training_plan")
        stored = out["stored"]
        self.assertRegex(stored["plan_id"], r"^[0-9a-f]{32}$")
        self.assertEqual((stored["version"], stored["changed"]), (1, True))
        again = json.loads(self.preview(plan_id=stored["plan_id"]))
        self.assertEqual(again["stored"], {"plan_id": stored["plan_id"], "version": 1, "changed": False})
        self.assertEqual(again["content_hash"], out["content_hash"])
        row = self.store.get_plan(self.store.account_fp("123456"), stored["plan_id"])
        self.assertEqual(row["version_row"]["content_hash"], out["content_hash"])
        self.assertNotIn("window", row["version_row"]["body"])

    def test_answer_shape(self):
        out = json.loads(self.preview())
        sessions = [s for w in out["weeks"] for s in w["sessions"]]
        self.assertTrue(sessions)
        self.assertFalse(any("steps" in s for s in sessions))
        self.assertIn("dto_valid", out["invariants"]["checked"])
        self.assertIn("cross_engine", out["invariants"]["checked"])
        self.assertTrue(out["invariants"]["ok"], out["invariants"]["failed"])
        for item in out["window"]:
            day = dt.date.fromisoformat(item["date"])
            self.assertTrue(TODAY <= day <= TODAY + dt.timedelta(days=6))
            self.assertTrue(item["workout"].startswith("Coach "))
        self.assertTrue(out["window"])

    def test_fixed_calendar_sessions_but_not_our_own(self):
        out = json.loads(self.preview())
        hard = {s["date"] for w in out["weeks"] for s in w["sessions"] if s["hard_or_long"]}
        self.assertFalse({"2026-12-15", "2026-12-16", "2026-12-17"} & hard)
        # "Coach Long run" on 2026-12-19 is the plan's own name: not a fixed session.
        snap = run(pt.fetch_snapshot(self.client, TODAY, dt.date(2027, 2, 26)))[0]
        self.assertEqual([f.date.isoformat() for f in snap.fixed], ["2026-12-16"])
        self.assertTrue(snap.fixed[0].hard)

    def test_overrides_and_refusals(self):
        base = json.loads(self.preview())
        key = next(s["key"] for w in base["weeks"] for s in w["sessions"] if s["role"] == "threshold")
        edits = json.dumps([{"kind": "easy", "key": key}, {"kind": "rest", "date": RACE}])
        out = json.loads(self.preview(overrides=edits))
        self.assertEqual(out["overrides"]["applied"], [{"kind": "easy", "key": key}])
        self.assertEqual(out["overrides"]["refused"][0]["rule"], "race_day")

    def test_bad_overrides(self):
        for value in ("not json", "{}", '[{"kind": "grow"}]', '[{"kind": "easy", "key": "../x"}]',
                      '[{"kind": "cap_week_km", "week_start": "2026-12-07", "km": -1}]',
                      '[{"kind": "easy", "key": "w01-long", "extra": 1}]',
                      json.dumps([{"kind": "rest", "date": "2026-12-01"}] * 21)):
            with self.subTest(value=value[:40]):
                self.assertTrue(self.preview(overrides=value).startswith("Error: Invalid input: "))

    def test_bad_plan_id(self):
        self.assertTrue(self.preview(plan_id="nope").startswith("Error: Invalid input: "))

    def test_foreign_plan_id_is_not_saved(self):
        out = json.loads(self.preview(plan_id="0" * 32))
        self.assertIsNone(out["stored"]["plan_id"])
        self.assertIn("no open plan", out["note"])

    def test_no_profile_id_means_not_saved(self):
        self.client.data[f"stats:{TODAY.isoformat()}"] = {}
        out = json.loads(self.preview())
        self.assertIsNone(out["stored"]["plan_id"])
        self.assertIn("account id", out["note"])

    def test_database_down_still_previews(self):
        for ready, word in ((True, "did not answer"), (False, "still starting"), (None, "did not answer")):
            with self.subTest(ready=ready):
                self.svc = pt.PlanService(lambda: self.client, lambda r=ready: BrokenStore(r), lambda: TODAY)
                with self.assertLogs("garmin_ext.plans", "WARNING"):
                    out = json.loads(self.preview())
                self.assertIsNone(out["stored"]["plan_id"])
                self.assertIn(word, out["note"])
                self.assertTrue(out["weeks"])


class PreviewWithWrites(Base):
    """PLAN_WRITES=on: the preview answers a commit token for the window's sessions."""

    def setUp(self):
        super().setUp()
        import plan_writes

        self.svc.commit_hook = plan_writes.commit_section

    def test_commit_token_and_ops(self):
        out = json.loads(self.preview(commit_days=7))
        commit = out["commit"]
        if not out["invariants"]["ok"]:
            self.assertIsNone(commit["token"])
            return
        self.assertTrue(commit["token"].startswith("ct1."))
        self.assertEqual(commit["days"], 7)
        self.assertEqual({o["op"] for o in commit["ops"]}, {"create"})
        self.assertEqual(sorted(o["date"] for o in commit["ops"]), [w["date"] for w in out["window"]])
        self.assertTrue(all(TODAY.isoformat() <= o["date"] <= "2026-12-01" for o in commit["ops"]))
        self.assertNotIn("dto", json.dumps(commit))

    def test_rules_v2_output_gets_a_token_and_course_id_binds_it(self):
        out = json.loads(self.preview(commit_days=7, course_id="4242"))
        self.assertTrue(out["invariants"]["ok"], out["invariants"]["failed"])
        self.assertTrue(out["acwr"]["enforced"])
        self.assertTrue(out["race_pacing"]["segments"])
        self.assertTrue(out["commit"]["token"].startswith("ct1."))
        pid = out["stored"]["plan_id"]
        # the course is an input: no course is another version with its own token
        other = json.loads(self.preview(commit_days=7, plan_id=pid))
        self.assertNotEqual(other["content_hash"], out["content_hash"])
        self.assertEqual((other["stored"]["version"], other["stored"]["changed"]), (2, True))
        self.assertTrue(other["commit"]["token"].startswith("ct1."))
        self.assertNotEqual(other["commit"]["token"], out["commit"]["token"])
        self.assertNotIn("course_id", other["inputs"])
        self.assertEqual(out["inputs"]["course_id"], 4242)

    def test_writes_on_tool_takes_course_id(self):
        added = {}
        pt.register(lambda fn, name=None, description=None, **kw: added.setdefault(name, fn), None,
                    lambda: self.client, writes=True, store=self.store, today=lambda: TODAY)
        out = json.loads(run(added["plan_preview"](**ARGS, course_id="4242")))
        self.assertEqual(out["race_pacing"]["course_id"], 4242)
        self.assertIn("commit", out)

    def test_bad_commit_days_and_no_token_without_a_store(self):
        self.assertTrue(self.preview(commit_days=40).startswith("Error: Invalid input: commit_days"))
        svc = pt.PlanService(lambda: self.client, lambda: BrokenStore(), lambda: TODAY)
        svc.commit_hook = lambda *a: 1 / 0
        with self.assertLogs("garmin_ext.plans", "WARNING"):
            out = json.loads(run(svc.preview(**ARGS)))
        self.assertIsNone(out["commit"]["token"])

    def test_without_writes_no_commit_object(self):
        self.svc.commit_hook = None
        self.assertNotIn("commit", json.loads(self.preview()))


class Get(Base):
    def test_get_returns_the_stored_preview_without_garmin(self):
        stored = json.loads(self.preview())["stored"]
        self.client.calls.clear()
        out = json.loads(self.svc.get(""))
        self.assertEqual(out["stored"]["plan_id"], stored["plan_id"])
        self.assertEqual(out["stored"]["status"], "draft")
        self.assertRegex(out["stored"]["built_on"], r"^\d{4}-\d{2}-\d{2}$")
        self.assertIn("stored preview", out["note"])
        self.assertEqual(self.client.calls, [])
        self.assertEqual(json.loads(self.svc.get(stored["plan_id"]))["stored"]["version"], 1)

    def test_empty_and_unknown(self):
        self.assertEqual(self.svc.get(""), pt.TEXT_NO_PLAN)
        self.assertEqual(self.svc.get("a" * 32), pt.TEXT_NOT_FOUND)
        self.assertTrue(self.svc.get("../etc").startswith("Error: Invalid input: "))

    def test_database_texts(self):
        for ready, text in ((True, pt.TEXT_DB), (False, pt.TEXT_STARTING), (None, pt.TEXT_DB)):
            with self.subTest(ready=ready):
                svc = pt.PlanService(lambda: self.client, lambda r=ready: BrokenStore(r), lambda: TODAY)
                with self.assertLogs("garmin_ext.plans", "WARNING") as logs:
                    self.assertEqual(svc.get(""), text)
                self.assertEqual(logs.output, ["WARNING:garmin_ext.plans:plans: plan_get failed (StoreError)"])

    def test_only_the_history_accounts_plans(self):
        stored = json.loads(self.preview())["stored"]
        self.store.meta[ps.META_ACCOUNT] = self.store.account_fp("999999")   # another Garmin account
        self.assertEqual(self.svc.get(""), pt.TEXT_NO_PLAN)
        self.assertEqual(self.svc.get(stored["plan_id"]), pt.TEXT_NOT_FOUND)
        self.store.meta[ps.META_ACCOUNT] = self.store.account_fp("123456")
        self.store.meta[ps.META_MISMATCH] = "yes"
        self.assertEqual(self.svc.get(""), pt.TEXT_NO_ACCOUNT)
        del self.store.meta[ps.META_MISMATCH]
        self.assertEqual(json.loads(self.svc.get(""))["stored"]["plan_id"], stored["plan_id"])
        for value in (None, "", "not-a-fingerprint"):
            with self.subTest(value=value):
                if value is None:
                    del self.store.meta[ps.META_ACCOUNT]
                else:
                    self.store.meta[ps.META_ACCOUNT] = value
                self.assertEqual(self.svc.get(""), pt.TEXT_NO_ACCOUNT)
                self.assertEqual(self.svc.get(stored["plan_id"]), pt.TEXT_NO_ACCOUNT)
        self.assertTrue(pt.TEXT_NO_ACCOUNT.startswith("No data: "))

    def test_older_rules_are_flagged(self):
        stored = json.loads(self.preview())["stored"]
        self.store.versions[(stored["plan_id"], 1)]["rules_version"] = 0
        self.assertTrue(json.loads(self.svc.get(""))["stored"]["rules_changed"])


class Registration(unittest.TestCase):
    def setUp(self):
        self.added = {}

        def adder(fn, name=None, description=None, **kwargs):
            self.added[name] = (fn, description)
            return fn
        self.adder = adder

    def test_three_read_tools_and_the_write_tools_only_with_writes(self):
        pt.register(self.adder, None, lambda: FakeClient(), store=ps.MemoryPlanStore(), today=lambda: TODAY)
        self.assertEqual(sorted(self.added), sorted(pt.TOOL_NAMES))
        self.assertNotIn("commit_days", self.added["plan_preview"][0].__code__.co_varnames)
        self.assertNotIn("plan_commit", self.added["plan_preview"][1])
        self.added.clear()
        pt.register(self.adder, None, lambda: FakeClient(), writes=True, store=ps.MemoryPlanStore(),
                    today=lambda: TODAY)
        self.assertEqual(sorted(self.added), sorted(pt.TOOL_NAMES + ("plan_cancel", "plan_commit")))
        self.assertIn("commit_days", self.added["plan_preview"][0].__code__.co_varnames)
        self.assertIn("plan_commit", self.added["plan_preview"][1])

    def test_descriptions_carry_the_roles(self):
        pt.register(self.adder, None, lambda: FakeClient(), store=ps.MemoryPlanStore(), today=lambda: TODAY)
        self.assertIn("intake interviewer", self.added["plan_assess"][1])
        self.assertIn("coach critic", self.added["plan_preview"][1])
        self.assertIn("never Garmin", self.added["plan_get"][1])
        for _fn, text in self.added.values():
            self.assertTrue(text.isascii())

    def test_registered_functions_run(self):
        store = ps.MemoryPlanStore()
        store.meta[ps.META_ACCOUNT] = store.account_fp("123456")
        client = FakeClient()
        pt.register(self.adder, None, lambda: client, store=store, today=lambda: TODAY)
        out = json.loads(run(self.added["plan_preview"][0](**ARGS)))
        self.assertTrue(out["stored"]["changed"])
        self.assertIn("plan_assessment", run(self.added["plan_assess"][0](race_date=RACE)))
        got = json.loads(run(self.added["plan_get"][0]()))
        self.assertEqual(got["stored"]["plan_id"], out["stored"]["plan_id"])

    def test_store_from_config_is_built_once(self):
        class Cfg:
            database_url = "postgresql://u:p@h/db"
        made = []
        orig = ps.PgPlanStore
        pt._STORE.clear()
        try:
            ps.PgPlanStore = lambda dsn: made.append(dsn) or object()
            first = pt.store_from_config(Cfg())
            self.assertIs(pt.store_from_config(Cfg()), first)
            self.assertEqual(len(made), 1)
        finally:
            ps.PgPlanStore = orig
            pt._STORE.clear()


class Parsing(unittest.TestCase):
    def test_days_and_defaults(self):
        i = pt.plan_inputs(TODAY, RACE, run_days="tue,thu,sat,sun")
        self.assertEqual(i.run_days, (1, 3, 5, 6))
        self.assertEqual(i.rest_days, (0, 2, 4))
        j = pt.plan_inputs(TODAY, RACE, rest_days="mon")
        self.assertEqual(j.run_days, (1, 2, 3, 4, 5, 6))
        self.assertEqual(pt.plan_inputs(TODAY, RACE).rest_days, pe.PlanInputs(race_date=TODAY).rest_days)

    def test_goal_b_races_blocked(self):
        i = pt.plan_inputs(TODAY, RACE, goal_time="3:29:59", b_races="2027-01-29:tri_full; 2027-01-10",
                           blocked_dates="2026-12-24,2026-12-24,2026-12-31")
        self.assertEqual(i.goal_time_s, 3 * 3600 + 29 * 60 + 59)
        self.assertEqual([(b.date.isoformat(), b.kind) for b in i.b_races],
                         [("2027-01-10", "tri_half"), ("2027-01-29", "tri_full")])
        self.assertEqual(len(i.blocked_dates), 2)

    def test_snapshot_parsers(self):
        self.assertEqual(pt.predictions([{"timeMarathon": 99}, {"timeMarathon": 12000}]), (("marathon", 12000),))
        self.assertEqual(pt.predictions("x"), ())
        self.assertIsNone(pt.vo2max([{"generic": {"vo2MaxValue": 400}}]))
        self.assertEqual(pt.vo2max({"generic": {"vo2MaxValue": 50}}), 50)
        weeks, runs, longest = pt.run_history([{"activityType": {"typeKey": "running"}, "distance": 5000,
                                                "startTimeLocal": "bad"}, "junk"], TODAY)
        self.assertEqual((sum(weeks), sum(runs), longest), (0, 0, None))

    def test_snapshot_failure_of_schedule(self):
        client = FakeClient()
        client.data["schedule"] = {"errors": ["x"]}
        snap, pid, failed = run(pt.fetch_snapshot(client, TODAY, dt.date(2027, 2, 26)))
        self.assertEqual(failed, ["schedule"])
        self.assertEqual(pid, "123456")
        self.assertEqual(snap.fixed, ())
        self.assertIsInstance(sc.compact_json({"x": 1}), str)



class LoadsAndPacing(Base):
    """Rules v2: the planned all-sport ACWR's inputs and the race pacing's course read."""

    def test_activity_loads_and_garmin_loads_reach_the_plan(self):
        out = json.loads(self.preview())
        acwr = out["acwr"]
        self.assertTrue(acwr["enforced"], acwr["reason"])
        self.assertEqual(acwr["load_per_min"]["run"], 1.6)
        self.assertEqual(acwr["load_per_min"]["bike"], 1.0)
        self.assertEqual(len(acwr["checks"]), 3)
        self.assertEqual(out["sources_failed"], [])
        self.assertNotIn("race_pacing", out)
        self.assertFalse([c for c in self.client.calls if c.startswith("course:")])
        self.assertEqual(sum(c.startswith("training_status:") for c in self.client.calls), 3)

    def test_assess_reports_garmins_ratio_today(self):
        out = json.loads(self.assess())
        self.assertEqual(out["acwr"], 0.69)

    def test_failed_training_status_means_not_enforced(self):
        self.client.fail = {f"training_status:{TODAY.isoformat()}": RuntimeError("boom"),
                            f"training_status:{(TODAY - dt.timedelta(days=14)).isoformat()}": RuntimeError("boom")}
        with self.assertLogs("garmin_ext.status", "WARNING"):
            out = json.loads(self.preview())
        self.assertFalse(out["acwr"]["enforced"])
        self.assertEqual(out["sources_failed"], ["training_status", "training_status_14d"])
        self.assertIn("acwr_not_enforced", [w["id"] for w in out["warnings"]])

    def test_course_id_adds_race_pacing_from_one_course_read(self):
        out = json.loads(self.preview(course_id="4242"))
        rp = out["race_pacing"]
        self.assertEqual(rp["course_id"], 4242)
        self.assertEqual([s["kind"] for s in rp["segments"]], ["flat", "climb", "flat"])
        self.assertEqual(rp["segments"][1]["grade_pct"], 3.0)
        self.assertEqual(self.client.calls.count("course:4242"), 1)
        self.assertNotIn("SECRET", json.dumps(out))
        stored = json.loads(self.svc.get(out["stored"]["plan_id"]))
        self.assertEqual(stored["race_pacing"], rp)
        self.assertEqual(stored["inputs"]["course_id"], 4242)

    def test_unknown_course_is_listed_as_failed(self):
        out = json.loads(self.preview(course_id="99"))
        self.assertEqual(out["race_pacing"]["missing"], ["course"])
        self.assertEqual(out["sources_failed"], ["course"])
        self.client.data["course:98"] = {"geoPoints": [{"distance": 0, "elevation": None}]}
        self.assertEqual(json.loads(self.preview(course_id="98"))["race_pacing"]["missing"], ["course"])

    def test_bad_course_id_never_reaches_garmin(self):
        for value in ("../x", "-1", "12a", "0012", "1" * 16, True):
            with self.subTest(value=value):
                out = self.preview(course_id=value)
                self.assertTrue(out.startswith("Error: Invalid input: "), out)
                self.assertNotIn("../x", out)
        self.assertEqual(self.client.calls, [])
        self.assertIsNone(pt.parse_course_id(""))
        self.assertIsNone(pt.parse_course_id(0))
        self.assertEqual(pt.parse_course_id(17), 17)

    def test_parsers(self):
        acts = pt.activity_loads([
            "junk", {"startTimeLocal": "2026-11-20 07:00:00", "duration": 0, "activityTrainingLoad": 5},
            {"startTimeLocal": "2026-11-20 07:00:00", "duration": 600, "activityTrainingLoad": None},
            {"startTimeLocal": "2020-01-01 07:00:00", "duration": 600, "activityTrainingLoad": 5},
            {"activityType": {"typeKey": "lap_swimming"}, "startTimeLocal": "2026-11-20 07:00:00",
             "duration": 1800, "activityTrainingLoad": 30, "distance": 2000}], TODAY)
        self.assertEqual([(a.sport, a.load, a.meters) for a in acts], [("swim", 30.0, 2000.0)])
        self.assertEqual(pt.garmin_loads({"training_status": _status(100, 0)}, TODAY), ())
        self.assertEqual(pt.garmin_loads({"training_status": _status(100, 90)}, TODAY),
                         (pe.GarminLoad(TODAY, 100.0, 90.0),))
        self.assertIsNone(pt.course_of("x"))
        self.assertIsNone(pt.course_of({"geoPoints": [{"distance": 0, "elevation": 3}]}))
        course = pt.course_of(_course_payload())
        self.assertEqual(course.distance_m, 42200.0)
        self.assertEqual(len(course.climbs), 1)
        fixed = pt.fixed_sessions([{"scheduleDate": "2026-12-16", "workoutName": "Ride", "workoutType": "cycling",
                                    "estimatedDurationInSecs": 7200}], {})
        self.assertEqual(fixed[0].minutes, 120.0)


if __name__ == "__main__":
    unittest.main()
