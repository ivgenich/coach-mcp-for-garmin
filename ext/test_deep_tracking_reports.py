"""Deep tests of the plan in get_daily_status and get_weekly_status, end to end with the real plan_tracking on a
MemoryPlanStore (no fake tracking module). Standard library only.

The switch off (byte for byte the answer without plans), a store that raises, is slow or has no plan, no
profile id and the weekly fallbacks, key mode and the probe, the rest day, feedback_due / feedback_for,
today[i].plan for ours, not ours and retired rows, the weekly counts, report times around midnight in
Asia/Jerusalem and Europe/London, and a week that does not start on Monday.

The plan lookup runs on a daemon thread with a 3 s timeout: every test joins those threads before it ends.
"""

from __future__ import annotations

import asyncio
import copy
import datetime as dt
import hashlib
import json
import logging
import threading
import time
import unittest
from unittest import mock
from zoneinfo import ZoneInfo

import mcp2_compat

mcp2_compat.register_finder()

import daily_status as ds  # noqa: E402
import plan_store as ps  # noqa: E402
import plan_tracking as tr  # noqa: E402
import status_common as sc  # noqa: E402
import test_weekly_status as tws  # noqa: E402
import weekly_status as ws  # noqa: E402

UTC = dt.timezone.utc
TZ_NAME = "Asia/Jerusalem"
TZ = ZoneInfo(TZ_NAME)
PROFILE = 71000001
RACE = dt.date(2027, 4, 4)
MON = dt.date(2026, 11, 30)
LOGGERS = tuple(sorted({"garmin_ext.plans", "garmin_ext.status", ds.log.name}))   # ds.log is garmin_ext.plans


def day(n):
    return MON + dt.timedelta(days=n)


def session(key, when, role="easy", km=8.0, minutes=48):
    return {"key": key, "date": when.isoformat(), "role": role, "km": km, "minutes": minutes}


def body(sessions, weeks=2, start=MON):
    out = []
    for n in range(weeks):
        s = start + dt.timedelta(days=7 * n)
        e = s + dt.timedelta(days=6)
        out.append({"week": n + 1, "start": s.isoformat(), "end": e.isoformat(),
                    "sessions": [x for x in sessions if s.isoformat() <= x["date"] <= e.isoformat()]})
    return {"weeks": out, "content_hash": "r"}


def run(aid, when, hour=7, type_key="running", km=8.0, minutes=48.0, **extra):
    out = {"activityId": aid, "activityType": {"typeKey": type_key}, "activityName": "IGNORE ME",
           "startTimeLocal": f"{when.isoformat()} {hour:02d}:00:00", "distance": km * 1000.0 if km else None,
           "duration": minutes * 60.0 if minutes else None, "ownerId": None}
    out.update(extra)
    return out


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


def plan_store(sessions, weeks=2, start=MON, created=dt.datetime(2026, 11, 1, tzinfo=UTC)):
    store = ps.MemoryPlanStore(clock=Clock(created))
    fp = store.account_fp(str(PROFILE))
    pid, _, _ = store.save_preview(fp, None, RACE, 1, {}, body(sessions, weeks, start),
                                   hashlib.sha256(json.dumps(sessions).encode()).hexdigest())
    return store, fp, pid


class Spy:
    """A PlanStore wrapper: records the calls, raises or blocks where told."""

    def __init__(self, inner, raise_in=(), block_in=(), gate=None, none_in=()):
        self.inner, self.raise_in, self.block_in, self.gate, self.none_in = inner, raise_in, block_in, gate, none_in
        self.calls = []

    def __getattr__(self, name):
        attr = getattr(self.inner, name)
        if not callable(attr):
            return attr

        def call(*args, **kw):
            self.calls.append(name)
            if name in self.block_in:
                self.gate.wait(10)
            if name in self.raise_in:
                raise ps.StoreError("plans: database error SECRET-ROW")
            if name in self.none_in:
                return None
            return attr(*args, **kw)
        return call


def join_plan_threads(gate=None):
    if gate is not None:
        gate.set()
    for thread in threading.enumerate():
        if thread.name == "plan-status":
            thread.join(10)


def stable(status):
    status = dict(status)
    status.pop("sources", None)   # call timings
    return sc.compact_json(status)


class Quiet(unittest.TestCase):
    """Silences the report loggers (assertLogs still sees them) and joins the plan-status threads."""

    def setUp(self):
        saved = []
        for name in LOGGERS:
            logger = logging.getLogger(name)
            saved.append((logger, logger.handlers[:], logger.propagate))
            logger.handlers, logger.propagate = [logging.NullHandler()], False

        def restore():
            for logger, handlers, propagate in reversed(saved):
                logger.handlers, logger.propagate = handlers, propagate
        self.addCleanup(restore)
        self.addCleanup(join_plan_threads)
        self.addCleanup(tr.configure, None)
        tr.configure(None)

    def use(self, store):
        tr.configure(lambda: store)


# --- daily ------------------------------------------------------------------------------------------------------

class DailyClient:
    """The Garmin calls get_daily_status makes, from a dict (missing keys: Garmin's empty answers)."""

    garmin_connect_activities = "/activitylist-service/activities/search/activities"

    def __init__(self, data):
        self.data = data
        self.calls = []
        self._lock = threading.Lock()

    def _serve(self, key, empty):
        with self._lock:
            self.calls.append(key)
        return copy.deepcopy(self.data.get(key, empty))

    def get_stats(self, d):
        return self._serve(f"stats:{d}", {})

    def get_sleep_data(self, d):
        return self._serve(f"sleep:{d}", {})

    def get_training_readiness(self, d):
        return self._serve(f"readiness:{d}", [])

    def get_hrv_data(self, d):
        return self._serve(f"hrv:{d}", {})

    def get_training_status(self, d):
        return self._serve(f"training_status:{d}", {})

    def query_garmin_graphql(self, query):
        return self._serve("schedule", {"data": {"workoutScheduleSummariesScalar": []}})

    def get_workout_by_id(self, workout_id):
        return self._serve(f"workout:{workout_id}", {})

    def get_lactate_threshold(self, **kw):
        return self._serve("lactate_threshold", {})

    def get_cycling_ftp(self):
        return self._serve("cycling_ftp", {})

    def connectapi(self, path, params=None):
        if path == self.garmin_connect_activities:
            return self._serve(f"activities:{params['startDate']}", [])
        return self._serve(path, {})


def entry(workout_id, when, name="Easy run", sport="running", activity=None):
    return {"workoutId": workout_id, "scheduleDate": when.isoformat(), "workoutName": name, "workoutType": sport,
            "estimatedDurationInSecs": 2880, "estimatedDistanceInMeters": 8000.0, "associatedActivityId": activity}


def daily_data(when, acts_prev=(), acts_today=(), entries=(), profile="stats"):
    data = {f"activities:{(when - dt.timedelta(days=1)).isoformat()}": list(acts_prev),
            f"activities:{when.isoformat()}": list(acts_today),
            "schedule": {"data": {"workoutScheduleSummariesScalar": list(entries)}}}
    if profile == "stats":
        data[f"stats:{when.isoformat()}"] = {"userProfileId": PROFILE}
    elif profile == "sleep":
        data[f"sleep:{when.isoformat()}"] = {"dailySleepDTO": {"userProfilePK": PROFILE, "calendarDate": None}}
    elif profile == "hrv":
        data[f"hrv:{when.isoformat()}"] = {"userProfilePk": PROFILE}
    return data


def build_daily(data, when=None, now=None, tz=TZ_NAME, **kw):
    when = when or day(1)
    now = now or dt.datetime(when.year, when.month, when.day, 7, 0, tzinfo=ZoneInfo(tz))
    return asyncio.run(ds.build_daily_status(DailyClient(data), kw.pop("date", when.isoformat()), tz=tz, now=now,
                                             **kw))


def golden_daily(data, **kw):
    async def nothing(*a, **k):
        return None

    with mock.patch.object(ds, "add_plan", nothing):
        return build_daily(data, **kw)


class DailySwitch(Quiet):
    def test_switch_off_is_byte_identical(self):
        data = daily_data(day(1), acts_today=[run(1, day(1))], entries=[entry(7001, day(1))])
        golden = stable(golden_daily(data, text_format="markdown"))

        def broken():
            raise ps.StoreError("plans: cannot connect to the database")

        for factory in (None, lambda: None, broken):
            with self.subTest(factory=factory):
                tr.configure(factory)
                if factory is broken:
                    with self.assertLogs("garmin_ext.plans", "WARNING") as logs:
                        out = build_daily(data, text_format="markdown")
                    self.assertEqual(logs.output, ["WARNING:garmin_ext.plans:plans: the plan store is not available "
                                                   "for tracking (StoreError)"])
                else:
                    out = build_daily(data, text_format="markdown")
                self.assertEqual(stable(out), golden)
                self.assertNotIn("plan", out)

    def test_switch_on_with_no_plan_is_null(self):
        store = ps.MemoryPlanStore()
        self.use(store)
        out = build_daily(daily_data(day(1)), text_format="markdown")
        self.assertIsNone(out["plan"])
        self.assertNotIn("Plan:", out["text"])

    def test_timeouts_are_the_documented_three_seconds(self):
        self.assertEqual((ds.PLAN_TIMEOUT_S, ws.PLAN_TIMEOUT_S), (3.0, 3.0))


class DailyPlan(Quiet):
    def setUp(self):
        super().setUp()
        self.store, self.fp, self.pid = plan_store([session("20300210-run", day(0)), session("20261202-run", day(1)),
                                                    session("20261203-run", day(2), role="threshold", km=10.0,
                                                            minutes=55)])
        self.use(self.store)

    def test_todays_session_done_and_feedback_due(self):
        self.store.put_workout(self.pid, {"workout_key": "20261202-run", "version": 1, "date_local": day(1), "role": "easy",
                                          "dto_hash": "0" * 64, "state": "scheduled", "garmin_workout_id": 7001})
        self.store.put_workout(self.pid, {"workout_key": "20261202-run~abcdef01", "version": 1, "date_local": day(1),
                                          "role": "easy", "dto_hash": "1" * 64, "state": "deleted",
                                          "garmin_workout_id": 7003})
        data = daily_data(day(1), acts_today=[run(1, day(1))],
                          entries=[entry(7001, day(1)), entry(7002, day(1), "Other"), entry(7003, day(1), "Old")])
        out = build_daily(data, text_format="markdown")
        self.assertEqual(out["plan"], {"workout_key": "20261202-run", "date": "2026-12-01", "role": "easy", "sport": "run",
                                       "planned_km": 8.0, "planned_min": 48, "link_status": "done",
                                       "feedback_due": True, "feedback_for": "20261202-run"})
        plans = [w.get("plan") for w in out["today"]]
        self.assertEqual(plans, [{"workout_key": "20261202-run", "role": "easy"}, None, None])
        self.assertIn("Plan: easy run 8 km 48 min (done); feedback due (effort and pain)", out["text"])
        golden = golden_daily(data)
        for key in ("level", "flags", "recommendation", "missing", "rules_version"):
            self.assertEqual(out[key], golden[key])
        self.assertNotIn("plan", out["missing"])

    def test_feedback_for_prefers_today_then_yesterday(self):
        data = daily_data(day(1), acts_prev=[run(1, day(0))], acts_today=[run(2, day(1))])
        self.assertEqual(build_daily(data)["plan"]["feedback_for"], "20261202-run")
        self.store.save_feedback(self.pid, "20261202-run", 4, 0, "none", False)
        out = build_daily(data)["plan"]
        self.assertEqual((out["feedback_due"], out["feedback_for"]), (True, "20300210-run"))
        self.store.save_feedback(self.pid, "20300210-run", 4, 0, "none", False)
        out = build_daily(data, text_format="markdown")
        self.assertEqual((out["plan"]["feedback_due"], out["plan"]["feedback_for"]), (False, None))
        self.assertNotIn("feedback due", out["text"])

    def test_partial_is_due_replaced_and_missed_are_not(self):
        for status, due in (("partial_late", True), ("done_early", True), ("replaced", False), ("missed", False)):
            with self.subTest(status=status):
                self.store.save_links(self.pid, 1, [{"workout_key": "20300210-run", "date_local": day(0), "role": "easy",
                                                     "dto_hash": "0" * 64, "linked_activity_id": None,
                                                     "link_status": status}])
                out = build_daily(daily_data(day(1)))["plan"]
                self.assertEqual(out["feedback_due"], due)
                self.assertEqual(out["feedback_for"], "20300210-run" if due else None)

    def test_a_gmt_only_run_is_dated_by_the_day_it_was_listed_under(self):
        # Garmin lists a run under Tuesday (the watch abroad, 20:00 in New York) with startTimeGMT only:
        # 01:00 GMT Wednesday is 03:00 Wednesday in REPORT_TZ. The daily report dates it Tuesday, the day it was
        # asked for, so the plan refresh in the same report does too: Tuesday's session is done.
        gmt_only = run(1, day(1), startTimeGMT=f"{day(2).isoformat()} 01:00:00")
        gmt_only.pop("startTimeLocal")
        out = build_daily(daily_data(day(1), acts_today=[gmt_only]))
        self.assertEqual(out["plan"]["link_status"], "done")
        listed = ds.plan_activities({"activities_today": [gmt_only]}, day(1), TZ)
        self.assertEqual(sc.activity_times(listed[0], TZ), (day(1), None))
        same_day = run(2, day(1), startTimeGMT=f"{day(1).isoformat()} 05:30:00")
        same_day.pop("startTimeLocal")
        listed = ds.plan_activities({"activities_prev": [same_day]}, day(2), TZ)
        self.assertEqual(sc.activity_times(listed[0], TZ), (day(1), dt.datetime(2026, 12, 1, 7, 30)))
        self.assertNotIn("startTimeLocal", same_day)               # the payload itself is not changed

    def test_two_days_back_is_not_due(self):
        data = daily_data(day(2), acts_prev=[], acts_today=[])
        self.store.save_links(self.pid, 1, [{"workout_key": "20300210-run", "date_local": day(0), "role": "easy",
                                             "dto_hash": "0" * 64, "linked_activity_id": 5, "link_status": "done"}])
        self.assertFalse(build_daily(data, when=day(2))["plan"]["feedback_due"])

    def test_rest_day_with_and_without_feedback_due(self):
        when = day(3)                                                  # Thursday: no session
        data = daily_data(when, acts_prev=[run(1, day(2), km=10.0, minutes=55.0)])
        out = build_daily(data, when=when, text_format="markdown")
        self.assertEqual(out["plan"], {"workout_key": None, "date": "2026-12-03", "role": "rest", "sport": None,
                                       "planned_km": None, "planned_min": None, "link_status": None,
                                       "feedback_due": True, "feedback_for": "20261203-run"})
        self.assertIn("Plan: rest day; feedback due (effort and pain)", out["text"])
        self.store.save_feedback(self.pid, "20261203-run", 6, 0, "none", False)
        out = build_daily(data, when=when, text_format="markdown")
        lines = [line for line in out["text"].splitlines() if "Plan:" in line]
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].rstrip().endswith("Plan: rest day"), lines)

    def test_outside_the_plan_weeks_is_null(self):
        for when in (day(-1), day(14)):
            with self.subTest(when=when):
                out = build_daily(daily_data(when), when=when, text_format="markdown")
                self.assertIsNone(out["plan"])
                self.assertNotIn("Plan:", out["text"])

    def test_the_daily_refresh_never_settles(self):
        for n in range(0, 6):
            build_daily(daily_data(day(n)), when=day(n))
        self.assertEqual(self.store.links(self.pid), [])
        out = build_daily(daily_data(day(5)), when=day(5))
        self.assertEqual(out["plan"]["role"], "rest")

    def test_the_plan_is_read_once_per_report(self):
        spy = Spy(self.store)
        self.use(spy)
        build_daily(daily_data(day(1), acts_today=[run(1, day(1))], entries=[entry(7001, day(1))]))
        self.assertEqual(spy.calls, ["account_fp", "tracked_plan", "links", "save_links", "feedback"])

    def test_profile_id_from_sleep_or_hrv(self):
        for source in ("sleep", "hrv"):
            with self.subTest(source=source):
                out = build_daily(daily_data(day(1), acts_today=[run(1, day(1))], profile=source))
                self.assertEqual(out["plan"]["link_status"], "done")

    def test_no_profile_id_no_plan(self):
        data = daily_data(day(1), acts_today=[run(1, day(1))], profile=None)
        spy = Spy(self.store)
        self.use(spy)
        out = build_daily(data, text_format="markdown")
        self.assertNotIn("plan", out)
        self.assertEqual(stable(out), stable(golden_daily(data, text_format="markdown")))
        self.assertEqual(spy.calls, [])

    def test_another_account_has_no_plan(self):
        data = daily_data(day(1))
        data[f"stats:{day(1).isoformat()}"] = {"userProfileId": PROFILE + 1}
        self.assertIsNone(build_daily(data)["plan"])

    def test_key_mode_and_probe_never_look(self):
        spy = Spy(self.store)
        self.use(spy)
        data = daily_data(day(1), acts_today=[run(1, day(1))])
        self.assertNotIn("plan", build_daily(data, key_mode=True))
        self.assertNotIn("plan", build_daily(data, probe=True))
        self.assertEqual(spy.calls, [])


class DailyStoreFailures(Quiet):
    def setUp(self):
        super().setUp()
        self.store, self.fp, self.pid = plan_store([session("20261202-run", day(1))])
        self.data = daily_data(day(1), acts_today=[run(1, day(1))])
        self.golden = stable(golden_daily(self.data))

    def test_account_lookup_failing_leaves_no_plan(self):
        self.use(Spy(self.store, raise_in=("account_fp",)))
        with self.assertLogs("garmin_ext.plans", "WARNING") as logs:
            out = build_daily(self.data)
        self.assertEqual(stable(out), self.golden)
        self.assertEqual(logs.output, ["WARNING:garmin_ext.plans:plans: no account for tracking (StoreError)"])

    def test_plan_read_failing_is_null(self):
        for name in ("tracked_plan", "links"):
            with self.subTest(name=name):
                self.use(Spy(self.store, raise_in=(name,)))
                with self.assertLogs("garmin_ext.plans", "WARNING") as logs:
                    out = build_daily(self.data, text_format="markdown")
                self.assertIsNone(out["plan"])
                self.assertNotIn("Plan:", out["text"])
                self.assertFalse(any("SECRET" in line for line in logs.output))
                self.assertTrue(all(line.endswith("(StoreError)") for line in logs.output), logs.output)

    def test_saving_links_failing_still_shows_the_session(self):
        self.use(Spy(self.store, raise_in=("save_links",)))
        with self.assertLogs("garmin_ext.plans", "WARNING"):
            out = build_daily(self.data)
        self.assertEqual((out["plan"]["workout_key"], out["plan"]["link_status"]), ("20261202-run", None))

    def test_feedback_read_failing_is_null(self):
        self.use(Spy(self.store, raise_in=("feedback",)))
        with self.assertLogs("garmin_ext.plans", "WARNING"):
            out = build_daily(self.data)
        self.assertIsNone(out["plan"])

    def test_store_returning_none(self):
        self.use(Spy(self.store, none_in=("tracked_plan",)))
        self.assertIsNone(build_daily(self.data)["plan"])

    def test_slow_store_is_cut_and_the_report_succeeds(self):
        gate = threading.Event()
        self.addCleanup(join_plan_threads, gate)
        self.use(Spy(self.store, block_in=("tracked_plan",), gate=gate))
        with mock.patch.object(ds, "PLAN_TIMEOUT_S", 0.2), self.assertLogs(ds.log, "WARNING") as logs:
            started = time.monotonic()
            out = build_daily(self.data)
        self.assertLess(time.monotonic() - started, 5.0)
        self.assertEqual(stable(out), self.golden)
        self.assertIn("longer than 0.2s", logs.output[-1])
        join_plan_threads(gate)
        # The held lookup finished after the answer: its links are stored, the answer stayed without them.
        self.assertEqual([r["link_status"] for r in self.store.links(self.pid)], ["done"])


class DailyClock(Quiet):
    def test_report_just_after_and_before_midnight(self):
        for tz_name in ("Asia/Jerusalem", "Europe/London", "Europe/Berlin"):
            zone = ZoneInfo(tz_name)
            for local, expected_day in ((dt.datetime(2026, 12, 2, 0, 5), day(2)),
                                        (dt.datetime(2026, 12, 1, 23, 55), day(1))):
                with self.subTest(tz=tz_name, local=local):
                    store, fp, pid = plan_store([session("20261202-run", day(1)), session("20261203-run", day(2),
                                                                                    role="threshold")])
                    self.use(store)
                    now = local.replace(tzinfo=zone).astimezone(UTC)        # the server clock in UTC
                    data = daily_data(expected_day, acts_prev=[run(1, day(0))] if expected_day == day(1) else
                                      [run(1, day(1), hour=23)])
                    out = asyncio.run(ds.build_daily_status(DailyClient(data), "", tz=tz_name, now=now))
                    self.assertEqual(out["plan"]["date"], expected_day.isoformat())
                    self.assertEqual(out["plan"]["workout_key"], "20261203-run" if expected_day == day(2) else "20261202-run")
                    if expected_day == day(2):
                        # Yesterday's 23:00 run completed yesterday's session: feedback due for it.
                        self.assertEqual(out["plan"]["feedback_for"], "20261202-run")
                    else:
                        # Monday's run, the day before Tuesday's session: done_early.
                        self.assertEqual(out["plan"]["link_status"], "done_early")

    def test_dst_end_weekend_in_jerusalem(self):
        # Israel leaves DST on Sunday 25 Oct 2026; a GMT-only run at 21:30 GMT on Saturday is 00:30 Sunday local.
        sunday = dt.date(2026, 10, 25)
        store, fp, pid = plan_store([session("20261202-run", sunday)], weeks=1, start=dt.date(2026, 10, 19),
                                    created=dt.datetime(2026, 10, 1, tzinfo=UTC))
        self.use(store)
        act = {"activityId": 3, "activityType": {"typeKey": "running"}, "startTimeGMT": "2026-10-24 21:30:00",
               "distance": 8000.0, "duration": 2880.0}
        data = daily_data(sunday, acts_today=[act])
        out = build_daily(data, when=sunday)
        self.assertEqual(out["plan"]["link_status"], "done")


# --- weekly -----------------------------------------------------------------------------------------------------

def weekly_fx(acts, schedule_items=(), device=True, hrv=None, sleep=None):
    fx = copy.deepcopy(tws.FX)
    fx["activities"] = list(acts)
    fx["schedule"] = {"data": {"workoutScheduleSummariesScalar": list(schedule_items)}}
    fx["device_last_used"] = {"userProfileNumber": PROFILE} if device else {"userDeviceId": 1}
    fx["hrv"] = hrv or {}
    fx["sleep"] = sleep or {}
    return fx


def build_weekly(fx, end_date="", now=None, tz=TZ_NAME, fail=None, **kw):
    now = now or dt.datetime(2026, 12, 7, 6, 30, tzinfo=ZoneInfo(tz))
    with mock.patch.object(ws, "_load_fit_helpers", tws.fit_helpers()):
        return asyncio.run(ws.build_weekly_status(tws.FakeClient(fx, fail=fail), end_date, tz=tz, now=now, **kw))


def golden_weekly(fx, **kw):
    async def nothing(*a, **k):
        return False, None

    with mock.patch.object(ws, "plan_counts", nothing):
        return build_weekly(fx, **kw)


# The week Mon 30 Nov .. Sun 6 Dec, reported on Mon 7 Dec: Monday missed, Wednesday run on Thursday, Friday a
# ride, Saturday a short long run, Sunday not settled yet (its late day is today).
WEEK = [session("20261202-run", day(0)), session("20261205-run", day(2)), session("20300213-run", day(4), minutes=36),
        session("20300221-run", day(5), role="long", km=20.0, minutes=120), session("20300214-run", day(6), km=6.0, minutes=36)]
WEEK_ACTS = [run(11, day(3)), run(12, day(4), type_key="road_biking", km=21.0, minutes=45.0),
             run(13, day(5), km=12.0, minutes=75.0)]
WEEK_COUNTS = {"planned": 5, "done": 1, "partial": 1, "missed": 1, "replaced": 1, "moved": 1, "open": 1}


class WeeklySwitch(Quiet):
    def setUp(self):
        super().setUp()
        ws._drift_cache.clear()
        self.addCleanup(ws._drift_cache.clear)

    def test_switch_off_is_byte_identical(self):
        fx = weekly_fx(WEEK_ACTS)
        golden = stable(golden_weekly(fx, text_format="markdown"))

        def broken():
            raise ps.StoreError("plans: cannot connect to the database")

        for factory in (None, lambda: None, broken):
            with self.subTest(factory=factory):
                tr.configure(factory)
                out = build_weekly(fx, text_format="markdown")
                self.assertEqual(stable(out), golden)
                self.assertNotIn("training_plan", out)


class WeeklyCounts(Quiet):
    def setUp(self):
        super().setUp()
        ws._drift_cache.clear()
        self.addCleanup(ws._drift_cache.clear)
        self.store, self.fp, self.pid = plan_store(WEEK)
        self.use(self.store)

    def test_counts_and_text(self):
        out = build_weekly(weekly_fx(WEEK_ACTS), text_format="markdown")
        self.assertEqual(out["training_plan"], WEEK_COUNTS)
        keys = list(out)
        self.assertEqual(keys.index("training_plan"), keys.index("plan") + 1)
        self.assertIn("Training plan: 1 of 5 sessions done, 1 partly done, 1 moved, 1 replaced, 1 missed, "
                      "1 still open", out["text"])
        links = {r["workout_key"]: (r["link_status"], r["linked_activity_id"]) for r in self.store.links(self.pid)}
        self.assertEqual(links, {"20261202-run": ("missed", None), "20261205-run": ("done_late", 11), "20300213-run": ("replaced", 12),
                                 "20300221-run": ("partial", 13)})
        golden = golden_weekly(weekly_fx(WEEK_ACTS))
        for key in ("level", "flags", "missing", "plan", "rules_version"):
            self.assertEqual(out[key], golden[key])

    def test_the_next_weekly_settles_sunday(self):
        build_weekly(weekly_fx(WEEK_ACTS))
        # Next Monday's report (the week 7..13 Dec, no session of this plan): its 14 days cover Sunday's window.
        out = build_weekly(weekly_fx(WEEK_ACTS), now=dt.datetime(2026, 12, 14, 6, 30, tzinfo=TZ))
        self.assertIsNone(out["training_plan"])
        self.assertEqual(tr.week_counts(self.store, self.fp, MON), dict(WEEK_COUNTS, missed=2, open=0))

    def test_report_times_around_midnight(self):
        for tz_name in ("Asia/Jerusalem", "Europe/London"):
            for hour, minute in ((0, 5), (23, 55)):
                with self.subTest(tz=tz_name, at=(hour, minute)):
                    store, fp, pid = plan_store(WEEK)
                    self.use(store)
                    local = dt.datetime(2026, 12, 7, hour, minute, tzinfo=ZoneInfo(tz_name))
                    out = build_weekly(weekly_fx(WEEK_ACTS), now=local.astimezone(UTC), tz=tz_name)
                    self.assertEqual((out["start"], out["end"]), ("2026-11-30", "2026-12-06"))
                    self.assertEqual(out["training_plan"], WEEK_COUNTS)

    def test_a_week_not_starting_monday(self):
        # end_date Wednesday 2 Dec: the week is Thu 26 Nov .. Wed 2 Dec; the plan starts on Monday 30 Nov. The
        # 14 days end on Wednesday, so Wednesday's session (run on Thursday) is still open.
        out = build_weekly(weekly_fx(WEEK_ACTS), end_date=day(2).isoformat())
        self.assertEqual(out["start"], "2026-11-26")
        self.assertEqual(out["training_plan"], {"planned": 2, "done": 0, "partial": 0, "missed": 1, "replaced": 0,
                                                "moved": 0, "open": 1})
        out = build_weekly(weekly_fx(WEEK_ACTS), end_date=day(9).isoformat(),
                           now=dt.datetime(2026, 12, 11, 6, 30, tzinfo=TZ))      # Thu 3 .. Wed 9 Dec
        self.assertEqual((out["training_plan"]["planned"], out["training_plan"]["missed"]), (3, 1))

    def test_no_session_that_week_is_null(self):
        out = build_weekly(weekly_fx([]), end_date="2026-11-22", text_format="markdown",
                           now=dt.datetime(2026, 11, 23, 6, 30, tzinfo=TZ))
        self.assertIsNone(out["training_plan"])
        self.assertNotIn("Training plan:", out["text"])

    def test_garmin_link_in_the_weeks_calendar(self):
        self.store.put_workout(self.pid, {"workout_key": "20261202-run", "version": 1, "date_local": day(0), "role": "easy",
                                          "dto_hash": "0" * 64, "state": "scheduled", "garmin_workout_id": 8001})
        items = [{"workoutId": 8001, "scheduleDate": day(0).isoformat(), "workoutType": "running",
                  "associatedActivityId": 555, "isRestDay": False}]
        out = build_weekly(weekly_fx(WEEK_ACTS, items))
        self.assertEqual(out["training_plan"]["done"], 2)
        self.assertEqual(out["training_plan"]["missed"], 0)

    def test_a_stored_done_survives_a_failed_activity_search(self):
        build_weekly(weekly_fx(WEEK_ACTS))
        out = build_weekly(weekly_fx(WEEK_ACTS), fail={"connectapi": tws.GarminConnectConnectionError("down")})
        self.assertEqual(out["training_plan"]["done"], 1)
        self.assertEqual(out["training_plan"]["partial"], 1)
        self.assertEqual(out["training_plan"]["replaced"], 1)

    def test_a_failed_activity_search_settles_nothing(self):
        out = build_weekly(weekly_fx(WEEK_ACTS), fail={"connectapi": tws.GarminConnectConnectionError("down")})
        self.assertIn("volume", out["missing"])
        self.assertEqual(out["training_plan"]["missed"], 0)

    def test_a_cut_activity_list_settles_nothing_it_may_miss(self):
        newest = [run(1000 + i, day(6) if i < 50 else day(5), hour=5 + i % 12, type_key="walking", km=1.0,
                      minutes=10.0) for i in range(100)]
        out = build_weekly(weekly_fx(newest))
        self.assertIn("volume", out["missing"])
        self.assertEqual(out["training_plan"]["missed"], 0)


class WeeklyAccount(Quiet):
    def setUp(self):
        super().setUp()
        ws._drift_cache.clear()
        self.addCleanup(ws._drift_cache.clear)
        self.store, self.fp, self.pid = plan_store(WEEK)
        self.use(self.store)

    def test_profile_id_sources_and_fallbacks(self):
        sleep_day = day(3).isoformat()
        cases = {
            "hrv": dict(device=False, hrv={day(6).isoformat(): {"userProfilePk": PROFILE}}),
            "sleep": dict(device=False, sleep={sleep_day: {"dailySleepDTO": {"userProfilePK": PROFILE}}}),
            "device": dict(device=True),
        }
        for name, kw in cases.items():
            with self.subTest(source=name):
                self.assertEqual(build_weekly(weekly_fx(WEEK_ACTS, **kw))["training_plan"], WEEK_COUNTS)
        owned = [dict(a, ownerId=PROFILE) for a in WEEK_ACTS]
        self.assertEqual(build_weekly(weekly_fx(owned, device=False))["training_plan"], WEEK_COUNTS)

    def test_no_profile_id_leaves_it_out(self):
        fx = weekly_fx(WEEK_ACTS, device=False)
        spy = Spy(self.store)
        self.use(spy)
        out = build_weekly(fx, text_format="markdown")
        self.assertNotIn("training_plan", out)
        self.assertEqual(stable(out), stable(golden_weekly(fx, text_format="markdown")))
        self.assertEqual(spy.calls, [])

    def test_key_mode_never_looks(self):
        spy = Spy(self.store)
        self.use(spy)
        out = build_weekly(weekly_fx(WEEK_ACTS), key_mode=True)
        self.assertNotIn("training_plan", out)
        self.assertEqual(spy.calls, [])

    def test_store_failures(self):
        fx = weekly_fx(WEEK_ACTS)
        golden = stable(golden_weekly(fx))
        self.use(Spy(self.store, raise_in=("account_fp",)))
        with self.assertLogs("garmin_ext.plans", "WARNING"):
            self.assertEqual(stable(build_weekly(fx)), golden)
        for name in ("tracked_plan", "links"):
            with self.subTest(name=name):
                self.use(Spy(self.store, raise_in=(name,)))
                with self.assertLogs("garmin_ext.plans", "WARNING") as logs:
                    out = build_weekly(fx)
                self.assertIsNone(out["training_plan"])
                self.assertFalse(any("SECRET" in line for line in logs.output))
        self.use(Spy(self.store, none_in=("tracked_plan",)))
        self.assertIsNone(build_weekly(fx)["training_plan"])

    def test_slow_store_leaves_it_out(self):
        fx = weekly_fx(WEEK_ACTS)
        gate = threading.Event()
        self.addCleanup(join_plan_threads, gate)
        self.use(Spy(self.store, block_in=("links",), gate=gate))
        with mock.patch.object(ws, "PLAN_TIMEOUT_S", 0.2), self.assertLogs(ds.log, "WARNING"):
            out = build_weekly(fx)
        self.assertNotIn("training_plan", out)
        join_plan_threads(gate)


if __name__ == "__main__":
    unittest.main()
