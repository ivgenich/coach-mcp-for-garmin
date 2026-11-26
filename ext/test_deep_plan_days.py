"""Deep multi-day simulation of a training plan on Garmin: the clock moves day by day, the plan is previewed and
committed again and again, runs appear and are linked, overrides, a weekly review, blocked dates, a cancel and
--plan-cleanup happen, and after EVERY step a set of invariants is checked on the fake Garmin.

    cd ext && REQUIRE_UPSTREAM=1 python -B -m unittest test_deep_plan_days -v

Why: every other plan test builds and writes on the same day. A plan rebuilt on a later day must not delete and
upload again the sessions that did not change, must not skip new ones, must not delete what is only outside the
commit window, and must never touch what is not ours or what is done.

The real code paths run: plan_tools.PlanService (plan_preview, plan_get, log_feedback, accept_changes),
plan_writes.WriteService (plan_commit, plan_cancel, plan_apply_changes) and its Executor, plan_tracking through
daily_status' plan lookup (the daily report's refresh), plan_review_tools.ReviewService (plan_review_week) and
plan_writes.cleanup (--plan-cleanup). Only Garmin and Postgres are fakes (SimGarmin, MemoryPlanStore).

SimGarmin follows docs/training-plan.md "Garmin facts": the GraphQL calendar read filters by its dates and its
entries carry workoutId, scheduledWorkoutId and scheduleDate (plus the fields of
fixtures/daily_status/schedule.json); the upload answers the workout JSON with workoutId; the schedule POST
answers workoutScheduleId; the library lists the newest workout first with its description; unschedule and
delete answer 204; a second DELETE (or a schedule of a deleted workout) fails with
GarminConnectConnectionError("API Error 404 - ..."). Deleting a workout does NOT remove its calendar entries here
(unverified on Garmin, so the code must unschedule first): an entry left behind is a violation.

The scenarios use public behaviour only (session keys are read from the answers, never built), so they run on
any key scheme. Violations are collected per step (day, time, step, invariant) and the test fails with them.
"""

from __future__ import annotations

import asyncio
import copy
import datetime as dt
import json
import os
import re
import threading
import unittest
from typing import Any, Dict, List, Optional, Set, Tuple
from unittest import mock
from zoneinfo import ZoneInfo

import daily_status as ds
import garmin_paths
import plan_dto
import plan_review_tools as prt
import plan_store as ps
import plan_tools as pt
import plan_tracking
import plan_writes as pw

try:
    from test_launch_support import HAVE_UPSTREAM
except Exception:  # noqa: BLE001 - the module is skipped below
    HAVE_UPSTREAM = False

NEEDS = "needs upstream garmin_mcp on mcp 2.x (plan_dto normalises and checks workouts with upstream's module)"
UTC = dt.timezone.utc
PROFILE = 123456
ACTIVITIES_PATH = "/activitylist-service/activities/search/activities"
MAX_SHOWN = 25
LINKED = plan_tracking.SETTLED_POSITIVE
RUN_TYPE = {"typeId": 1, "typeKey": "running", "parentTypeId": 17, "isHidden": False, "restricted": False,
            "trimmable": True}


class GarminConnectConnectionError(Exception):
    """garminconnect's error for an HTTP failure (same name and module); no response attached."""
    __module__ = "garminconnect.exceptions"


def http_404():
    return GarminConnectConnectionError("API Error 404 - Not Found")


class Resp:
    def __init__(self, payload, status=200):
        self.status_code = status
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")   # 204: no body
        return copy.deepcopy(self._payload)


def content_of(dto: Dict[str, Any]) -> str:
    """A workout's content, whatever its marker: plan_dto's hash of the DTO without the description."""
    return plan_dto.dto_hash({k: v for k, v in dto.items() if k != "description"})


def run(coro):
    return asyncio.run(coro)


# --- the fake Garmin ---------------------------------------------------------------------------------------------

class SimRaw:
    """garminconnect's raw Client (post/delete) as plan_writes and cleanup call it."""

    def __init__(self, g: "SimGarmin") -> None:
        self.g = g

    def post(self, domain, path, json=None, timeout=None, **kwargs):
        assert domain == "connectapi" and timeout is not None, (domain, timeout)
        garmin_paths.check_garmin_request("POST", path, {"json": json})
        g = self.g
        with g.lock:
            if path == "workout-service/workout":
                wid = g.new_id("workout")
                g.library[wid] = {"workoutId": wid, "workoutName": json["workoutName"],
                                  "description": json.get("description"), "dto": copy.deepcopy(json),
                                  "seq": g.new_id("seq"), "ours": True, "dates": []}
                g.log.append({"op": "upload", "workout": wid, "date": None, "content": content_of(json),
                              "today": g.sim.today()})
                answer = {**copy.deepcopy(json), "workoutId": wid, "ownerId": PROFILE}
                return Resp(answer)
            m = re.fullmatch(r"workout-service/schedule/(\d+)", path)
            assert m, path
            wid = int(m.group(1))
            w = g.library.get(wid)
            if w is None:
                raise http_404()
            day = str(json["date"])
            sid = g.new_id("schedule")
            dto = w["dto"]
            g.calendar[sid] = {
                "workoutId": wid, "scheduledWorkoutId": sid, "scheduleDate": day, "workoutName": w["workoutName"],
                "workoutType": "running", "estimatedDurationInSecs": dto.get("estimatedDurationInSecs"),
                "estimatedDistanceInMeters": dto.get("estimatedDistanceInMeters"), "associatedActivityId": None,
                "tpType": None, "isRestDay": False, "race": False}
            w["dates"].append(day)
            g.log.append({"op": "schedule", "workout": wid, "schedule": sid, "date": day,
                          "content": content_of(dto) if w["ours"] else None, "today": g.sim.today()})
            return Resp({"workoutScheduleId": sid, "workout": {"workoutId": wid}, "calendarDate": day})

    def delete(self, domain, path, timeout=None, **kwargs):
        assert domain == "connectapi" and timeout is not None, (domain, timeout)
        garmin_paths.check_garmin_request("DELETE", path, {})
        g = self.g
        m = re.fullmatch(r"workout-service/(schedule|workout)/(\d+)", path)
        assert m, path
        n = int(m.group(2))
        with g.lock:
            if m.group(1) == "schedule":
                entry = g.calendar.pop(n, None)
                if entry is None:
                    raise http_404()
                w = g.library.get(entry["workoutId"])
                g.log.append({"op": "unschedule", "workout": entry["workoutId"], "schedule": n,
                              "date": entry["scheduleDate"], "linked": entry.get("associatedActivityId"),
                              "ours": bool(w and w["ours"]) or entry["workoutId"] in g.ours_ever,
                              "content": content_of(w["dto"]) if w and w.get("dto") else None,
                              "today": g.sim.today()})
                return Resp(None, 204)
            w = g.library.pop(n, None)
            if w is None:
                raise http_404()
            # Not verified on Garmin: here a deleted workout's calendar entries stay (the code must unschedule).
            on = sorted(e["scheduleDate"] for e in g.calendar.values() if e["workoutId"] == n)
            g.log.append({"op": "delete", "workout": n, "date": (on or w["dates"] or [None])[-1], "on": on,
                          "ours": w["ours"], "content": content_of(w["dto"]) if w.get("dto") else None,
                          "today": g.sim.today()})
            return Resp(None, 204)


class SimGarmin:
    """upstream's Garmin client as the plan code sees it: reads on the object, raw writes on .client."""

    garmin_connect_activities = ACTIVITIES_PATH

    def __init__(self, sim: "Sim") -> None:
        self.sim = sim
        self.lock = threading.RLock()
        self.library: Dict[int, Dict[str, Any]] = {}
        self.calendar: Dict[int, Dict[str, Any]] = {}
        self.activities: List[Dict[str, Any]] = []
        self.log: List[Dict[str, Any]] = []
        self.ours_ever: Set[int] = set()
        self._ids = {"workout": 1_403_000_000, "schedule": 1_660_000_000, "seq": 0, "activity": 21_000_000_000}
        self.client = SimRaw(self)

    def new_id(self, kind: str) -> int:
        self._ids[kind] += 1 if kind == "seq" else 7
        if kind == "workout":
            self.ours_ever.add(self._ids[kind])
        return self._ids[kind]

    # seeds (not ours) ---------------------------------------------------------------------------------------

    def seed(self, day: dt.date, name: str, sport: str, minutes: int, km: Optional[float] = None,
             tp: bool = True) -> int:
        with self.lock:
            wid = self._ids["workout"] = self._ids["workout"] + 7
            dto = {"workoutName": name, "sportType": {"sportTypeKey": sport},
                   "estimatedDurationInSecs": minutes * 60}
            self.library[wid] = {"workoutId": wid, "workoutName": name,
                                 "description": "TrainingPeaks workout" if tp else "my own session",
                                 "dto": dto, "seq": self.new_id("seq"), "ours": False, "dates": [day.isoformat()]}
            sid = self.new_id("schedule")
            self.calendar[sid] = {
                "workoutId": wid, "scheduledWorkoutId": sid, "scheduleDate": day.isoformat(), "workoutName": name,
                "workoutType": sport, "estimatedDurationInSecs": minutes * 60,
                "estimatedDistanceInMeters": km * 1000.0 if km else None, "associatedActivityId": None,
                "tpType": "WORKOUT" if tp else None, "isRestDay": False, "race": False}
            return wid

    def plant_orphan(self, tag: str, day: dt.date) -> int:
        """A workout with this install's marker that no row owns (an earlier database), on the calendar."""
        with self.lock:
            wid = self.new_id("workout")
            dto = {"workoutName": "Coach Easy run", "sportType": {"sportTypeId": 1, "sportTypeKey": "running"},
                   "description": f"coach-mcp {tag} {'0' * 32} w09-e1 h12345678", "estimatedDurationInSecs": 2400}
            self.library[wid] = {"workoutId": wid, "workoutName": dto["workoutName"], "description": dto["description"],
                                 "dto": dto, "seq": self.new_id("seq"), "ours": True, "dates": [day.isoformat()]}
            sid = self.new_id("schedule")
            self.calendar[sid] = {"workoutId": wid, "scheduledWorkoutId": sid, "scheduleDate": day.isoformat(),
                                  "workoutName": dto["workoutName"], "workoutType": "running",
                                  "estimatedDurationInSecs": 2400, "estimatedDistanceInMeters": None,
                                  "associatedActivityId": None, "tpType": None, "isRestDay": False, "race": False}
            return wid

    # reads ----------------------------------------------------------------------------------------------------

    def get_stats(self, day):
        return {"userProfileId": PROFILE, "calendarDate": str(day)}

    def connectapi(self, path, params=None, **kwargs):
        assert path == ACTIVITIES_PATH, path
        params = params or {}
        first, last = str(params.get("startDate")), str(params.get("endDate"))
        start, limit = int(params.get("start") or 0), int(params.get("limit") or 20)
        with self.lock:
            items = [a for a in self.activities if first <= a["startTimeLocal"][:10] <= last]
        items.sort(key=lambda a: a["startTimeGMT"], reverse=True)   # newest first
        return copy.deepcopy(items[start:start + limit])

    def get_lactate_threshold(self):
        return {"speed_and_heart_rate": {"calendarDate": "2027-10-19T07:40:00.0", "speed": 0.355, "heartRate": 168},
                "power": {}}

    def get_race_predictions(self):
        return {"timeMarathon": 14220, "timeHalfMarathon": 6780, "time5K": 1350}

    def get_max_metrics(self, day):
        return [{"generic": {"calendarDate": str(day), "vo2MaxPreciseValue": 52.4}}]

    def get_training_status(self, day):
        return {}   # no Garmin load: the planned-load cap is not enforced, so a rebuild changes only by the date

    def get_hrv_data(self, day):
        return {}

    def get_sleep_data(self, day):
        return {}

    def query_garmin_graphql(self, query):
        text = query["query"]
        m = re.fullmatch(r'query\{workoutScheduleSummariesScalar\(startDate:"([0-9-]{10})", '
                         r'endDate:"([0-9-]{10})"\)\}', text)
        assert m, text
        first, last = m.group(1), m.group(2)
        with self.lock:
            items = [copy.deepcopy(e) for e in self.calendar.values() if first <= e["scheduleDate"] <= last]
        items.sort(key=lambda e: (e["scheduleDate"], e["scheduledWorkoutId"]))
        return {"data": {"workoutScheduleSummariesScalar": items}}

    def get_workouts(self, start=0, limit=100):
        with self.lock:
            items = sorted(self.library.values(), key=lambda w: w["seq"], reverse=True)   # newest first
            return [{"workoutId": w["workoutId"], "workoutName": w["workoutName"], "description": w["description"],
                     "sportType": {"sportTypeId": 1, "sportTypeKey": "running"}} for w in items][start:start + limit]

    def get_workout_by_id(self, workout_id):
        with self.lock:
            w = self.library.get(int(workout_id))
            if w is None:
                raise http_404()
            return {**copy.deepcopy(w["dto"]), "workoutId": w["workoutId"]}

    # views for the checks -------------------------------------------------------------------------------------

    def ours_entries(self) -> List[Dict[str, Any]]:
        with self.lock:
            out = []
            for e in self.calendar.values():
                w = self.library.get(e["workoutId"])
                if (w is not None and w["ours"]) or (w is None and e["workoutId"] in self.ours_ever):
                    out.append({**e, "content": content_of(w["dto"]) if w is not None else None})
            return sorted(out, key=lambda e: (e["scheduleDate"], e["scheduledWorkoutId"]))

    def foreign_state(self) -> str:
        with self.lock:
            lib = {k: {x: y for x, y in v.items() if x != "seq"} for k, v in self.library.items() if not v["ours"]}
            cal = {k: v for k, v in self.calendar.items() if self.library.get(v["workoutId"], {}).get("ours") is False
                   or (v["workoutId"] not in self.ours_ever and v["workoutId"] not in self.library)}
            return json.dumps({"library": lib, "calendar": cal}, sort_keys=True, default=str)


# --- the simulation ----------------------------------------------------------------------------------------------

BASE_INPUTS = {"race_date": "2028-02-25", "race_distance": "marathon", "goal_time": "3:52:00",
               "b_races": "2028-01-28:tri_half"}


class Sim:
    """One athlete, one fake Garmin, one plan store and the real services, on a simulated clock."""

    def __init__(self, case: unittest.TestCase, name: str, tz: str = "UTC", inputs: Optional[Dict[str, Any]] = None,
                 history_until: Optional[dt.date] = None) -> None:
        self.case = case
        self.name = name
        self.zone = ZoneInfo(tz)
        self.now = dt.datetime(2027, 11, 30, 12, 0, tzinfo=UTC)
        self.mono = [0.0]
        self.garmin = SimGarmin(self)
        self.store = ps.MemoryPlanStore(clock=lambda: self.now)
        self.fp = self.store.account_fp(PROFILE)
        self.store.meta[ps.META_ACCOUNT] = self.fp
        self.plans = pt.PlanService(lambda: self.garmin, lambda: self.store, self.today)
        self.plans.commit_hook = pw.commit_section
        self.confirms: List[Dict[str, Any]] = []
        self.writes = pw.WriteService(lambda: self.garmin, lambda: self.store, self.today,
                                      may_write=lambda: True, confirm=self._confirm,
                                      executor_options={"sleep": self._sleep, "clock": lambda: self.mono[0],
                                                        "join_s": 10.0})
        self.reviews = prt.ReviewService(lambda: self.garmin, lambda: self.store, self.today,
                                         daily_level=self._level)
        self.reviews.apply_hook = pw.apply_section
        self.inputs = dict(inputs or BASE_INPUTS)
        self.pid: Optional[str] = None
        self.step = "setup"
        self.violations: List[str] = []
        self.protected: Dict[int, str] = {}      # Garmin workout id -> why it may never be deleted or rewritten
        self.seen: Dict[str, List[Dict[str, Any]]] = {}   # date -> its sessions in the last version before it
        self.ran: Dict[str, int] = {}            # session date -> the activity the athlete ran for it
        self.seed_calendar()
        self.history(history_until or dt.date(2027, 12, 28))
        self.foreign = self.garmin.foreign_state()

    # clock ------------------------------------------------------------------------------------------------------

    def at(self, day: dt.date, hour: int = 7, minute: int = 0) -> None:
        moment = dt.datetime.combine(day, dt.time(hour, minute), tzinfo=self.zone).astimezone(UTC)
        self.case.assertGreaterEqual(moment, self.now, "the simulated clock only moves forward")
        self.now = moment

    def today(self) -> dt.date:
        return self.now.astimezone(self.zone).date()

    def _sleep(self, seconds: float) -> None:
        self.mono[0] += seconds

    async def _confirm(self, ctx, **summary):
        summary.pop("bind", None)
        self.confirms.append(summary)
        return "accepted"

    async def _level(self, client, day):
        return None

    # violations ---------------------------------------------------------------------------------------------

    def fail(self, invariant: str, detail: str) -> None:
        local = self.now.astimezone(self.zone)
        self.violations.append(f"[{self.name}] {local:%a %Y-%m-%d %H:%M} {self.zone.key} | {self.step} | "
                               f"{invariant}: {detail}")

    def report(self) -> None:
        if not self.violations:
            return
        first: Dict[str, List[Any]] = {}
        for line in self.violations:   # the same violation on later steps is counted, not repeated
            what = line.split(" | ", 2)[2]
            first.setdefault(what, [line, 0])[1] += 1
        lines = [line + (f"  (and on {n - 1} later step(s))" if n > 1 else "") for line, n in first.values()]
        shown = lines if os.environ.get("PLAN_DAYS_ALL") else lines[:MAX_SHOWN]
        more = len(lines) - len(shown)
        self.case.fail(f"{len(self.violations)} invariant violation(s) in {self.name}:\n  " + "\n  ".join(shown)
                       + (f"\n  ... and {more} more distinct" if more else ""))

    # Garmin seeds -----------------------------------------------------------------------------------------------

    def seed_calendar(self) -> None:
        """TrainingPeaks swim, bike and run sessions and the user's own "Coach easy" (a long easy run that is not
        ours), from December to April."""
        g = self.garmin
        day = dt.date(2027, 11, 29)
        while day <= dt.date(2028, 4, 30):
            if day.weekday() == 0:
                g.seed(day, "Recovery swim", "lap_swimming", 45, 2.0)
            if day.weekday() == 2:
                g.seed(day, "Endurance ride Z2", "cycling", 60, 30.0)
            day += dt.timedelta(days=1)
        for day, name, sport, minutes in ((dt.date(2028, 1, 11), "VO2max bike intervals 5x4", "cycling", 70),
                                          (dt.date(2028, 1, 20), "Tempo run 3x10", "running", 60),
                                          (dt.date(2028, 2, 8), "Threshold swim 10x100", "lap_swimming", 50),
                                          (dt.date(2028, 3, 16), "Brick bike+run", "cycling", 80),
                                          (dt.date(2028, 3, 28), "Tempo run 2x15", "running", 65)):
            g.seed(day, name, sport, minutes)
        for day in (dt.date(2028, 1, 6), dt.date(2028, 1, 13), dt.date(2028, 2, 3), dt.date(2028, 3, 15),
                    dt.date(2028, 3, 29)):
            g.seed(day, "Coach easy", "running", 85, 16.0, tp=False)

    def history(self, until: dt.date) -> None:
        """Ten weeks of runs before `until` (Tue 8, Thu 10, Sat 8, Sun 14 km): Garmin's activity list."""
        first = until - dt.timedelta(days=70)
        day = first
        while day < until:
            km = {1: 8, 3: 10, 5: 8, 6: 14}.get(day.weekday())
            if km:
                self.activity(day, float(km), 6.0 * km, hour=7)
            day += dt.timedelta(days=1)

    def activity(self, day: dt.date, km: float, minutes: float, hour: int = 18, minute: int = 0,
                 sport: str = "running") -> int:
        start = dt.datetime.combine(day, dt.time(hour, minute), tzinfo=self.zone)
        with self.garmin.lock:
            aid = self.garmin.new_id("activity")
            self.garmin.activities.append({
                "activityId": aid, "activityName": "Evening run" if hour >= 12 else "Morning run",
                "startTimeLocal": start.strftime("%Y-%m-%d %H:%M:%S"),
                "startTimeGMT": start.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S"),
                "activityType": dict(RUN_TYPE) if sport == "running" else {"typeId": 2, "typeKey": sport},
                "distance": km * 1000.0, "duration": minutes * 60.0, "movingDuration": minutes * 60.0 - 10,
                "averageSpeed": km * 1000.0 / (minutes * 60.0), "sportTypeId": 1, "manualActivity": False})
        return aid

    # the plan as stored -------------------------------------------------------------------------------------

    def body(self) -> Dict[str, Any]:
        plan = self.store.get_plan(self.fp, self.pid) if self.pid else None
        return (plan or {}).get("version_row", {}).get("body") or {}

    def sessions(self) -> List[Dict[str, Any]]:
        """The current version's runs (sessions with a workout): key, date, role, km, minutes, content, hard."""
        out = []
        for s, dto in plan_dto.session_dtos(self.body()):
            out.append({"key": s["key"], "date": s["date"], "role": s["role"], "km": s.get("km"),
                        "minutes": s.get("minutes"), "content": content_of(dto), "hard": bool(s.get("hard_or_long"))})
        return out

    def links(self) -> Dict[str, Dict[str, Any]]:
        return {r["workout_key"]: r for r in self.store.links(self.pid)} if self.pid else {}

    def linked_dates(self) -> Set[str]:
        """Dates of the sessions the athlete ran and the server linked (a stored link to the activity, or
        Garmin's own link on the calendar entry)."""
        linked = {r["linked_activity_id"] for r in self.links().values()
                  if r.get("link_status") in LINKED and r.get("linked_activity_id")}
        out = {d for d, aid in self.ran.items() if aid in linked}
        out |= {e["scheduleDate"] for e in self.garmin.ours_entries() if e.get("associatedActivityId")}
        return out

    # steps ------------------------------------------------------------------------------------------------------

    def preview(self, label: str = "preview", days: int = 7, **extra: Any) -> Optional[Dict[str, Any]]:
        self.step = f"{label} (commit_days {days})"
        args = {**self.inputs, **extra}
        text = run(self.plans.preview(plan_id=self.pid or "", commit_days=days, **args))
        try:
            out = json.loads(text)
        except ValueError:
            self.fail("preview answers", text[:300])
            return None
        if not out.get("invariants", {}).get("ok"):
            self.fail("plan invariants", json.dumps(out.get("invariants", {}).get("failed"))[:300])
        stored = out.get("stored") or {}
        if not stored.get("plan_id"):
            self.fail("preview stored", json.dumps(stored))
            return None
        self.pid = stored["plan_id"]
        out["_today"], out["_days"] = self.today(), days
        today = self.today().isoformat()
        # The athlete runs what the watch has: a day keeps its sessions of the last version that still had it
        # in the future (a new version never plans today).
        self.seen = {d: v for d, v in self.seen.items() if d <= today}
        for s in self.sessions():
            if s["date"] > today:
                self.seen.setdefault(s["date"], []).append(s)
        self.check_spacing()
        self.check_all()
        return out

    def commit(self, out: Optional[Dict[str, Any]], label: str = "commit") -> Optional[Dict[str, Any]]:
        if out is None:
            return None
        self.step = f"{label} of the {out['_today']} preview"
        c = out.get("commit") or {}
        # A token issued on an earlier day (a preview before midnight) never writes today's session: every op is
        # checked against today when it runs.
        first = self.today() if out["_today"] >= self.today() else self.today() + dt.timedelta(days=1)
        window = (first, out["_today"] + dt.timedelta(days=out["_days"] - 1))
        if not c.get("token"):
            if c.get("why") != "every session of the window is already on Garmin":
                self.fail("commit token", f"no token: {c.get('why')}")
            self.check_window(window)
            self.check_all()
            return None
        # A create outside the window only replaces a day already on Garmin whose workout changed.
        replaced = {o["date"] for o in c.get("ops") or [] if o["op"] == "delete"}
        outside = [o["date"] for o in c.get("ops") or [] if o["op"] == "create" and o["date"] not in replaced
                   and not out["_today"].isoformat() <= o["date"] <= window[1].isoformat()]
        if outside:
            self.fail("commit ops", f"creates outside the window {out['_today']}..{window[1]}: {outside}")
        return self._run_token("commit", lambda: self.writes.commit(plan_id=self.pid, commit_token=c["token"]),
                               window)

    def _run_token(self, action: str, call, window: Optional[Tuple[dt.date, dt.date]]) -> Optional[Dict[str, Any]]:
        before = self.garmin.ours_entries()
        n_log, n_confirm = len(self.garmin.log), len(self.confirms)
        res: Dict[str, Any] = {}
        for _ in range(12):
            text = run(call())
            try:
                res = json.loads(text)
            except ValueError:
                self.fail(f"{action} answers", text[:300])
                return None
            if res.get("status") != "partial" or res.get("retry_after_s") != 0:
                break
        executed = self.garmin.log[n_log:]
        if res.get("status") != "done":
            self.fail(f"{action} done", f"status {res.get('status')} reason {res.get('reason')} "
                                        f"failed {res.get('failed')} note {str(res.get('note'))[:160]}")
        if res.get("conflicts"):
            self.fail(f"{action} conflicts", f"days skipped as a conflict with another run: {res['conflicts']}")
        summary = self.confirms[n_confirm] if len(self.confirms) > n_confirm else None
        self.check_summary(action, summary, executed)
        self.check_writes(executed, action)
        if action in ("commit", "apply"):
            self.check_unchanged(before, executed, window)
            if window is not None and res.get("status") == "done":
                self.check_window(window)
        self.check_all()
        return res

    def morning(self, day: dt.date, days: int = 7, hour: int = 7, **extra: Any) -> Optional[Dict[str, Any]]:
        """The daily report's refresh, then plan_preview with the same inputs and plan_commit."""
        self.at(day, hour)
        self.track(f"morning report {day}")
        out = self.preview(days=days, **extra)
        self.at(day, hour, 10)
        self.commit(out)
        return out

    def run_session(self, day: dt.date, share: float = 1.0, on: Optional[dt.date] = None, hour: int = 18,
                    minute: int = 0, garmin_link: bool = False, expect: str = "done") -> Optional[int]:
        """The athlete runs the plan's session of `day` (its first run that day) on `on` (default: that day) at
        `share` of its km and time; then the evening report refreshes the links."""
        todays = self.seen.get(day.isoformat()) or []
        if not todays:
            return None
        s = todays[0]
        when = on or day
        self.at(when, hour, minute)
        self.step = f"run of the {day} {s['role']} ({share:.0%}) on {when}"
        aid = self.activity(when, round((s["km"] or 8.0) * share, 2), round((s["minutes"] or 48) * share, 1),
                            hour=hour, minute=minute)
        self.ran[day.isoformat()] = aid
        if garmin_link:
            with self.garmin.lock:
                for e in self.garmin.calendar.values():
                    if e["scheduleDate"] == day.isoformat() and e["workoutId"] in self.garmin.ours_ever:
                        e["associatedActivityId"] = aid
                        self.protected[e["workoutId"]] = f"Garmin linked it to activity {aid}"
        if hour < 21:
            self.at(when, 21, 30)
        else:
            self.at(when, hour, minute + 10)
        self.track(f"evening report {when}")
        status = [r.get("link_status") for r in self.links().values() if r.get("linked_activity_id") == aid]
        if status != [expect]:
            self.fail("run linked", f"the {day} {s['role']} run on {when} ({share:.0%}) should be linked "
                                    f"{expect}; the plan's links to it: {status or 'none'}")
        return aid

    def track(self, label: str) -> None:
        """get_daily_status' plan lookup: yesterday's and today's activities and the calendar."""
        self.step = label
        day = self.today()
        g = self.garmin
        payloads = {"stats": g.get_stats(day.isoformat()),
                    "activities_prev": g.connectapi(ACTIVITIES_PATH, params={
                        "startDate": (day - dt.timedelta(days=1)).isoformat(),
                        "endDate": (day - dt.timedelta(days=1)).isoformat(), "start": 0, "limit": 20}),
                    "activities_today": g.connectapi(ACTIVITIES_PATH, params={
                        "startDate": day.isoformat(), "endDate": day.isoformat(), "start": 0, "limit": 20})}
        scheduled = ds.schedule_entries(g.query_garmin_graphql(ds.schedule_query(
            (day - dt.timedelta(days=1)).isoformat(), day.isoformat())))
        ds._daily_plan_lookup(plan_tracking, self.store, day, self.zone, payloads, scheduled, [], day)
        self.protect_linked()
        self.check_all()

    def protect_linked(self) -> None:
        """Every workout of a session the athlete ran, once a link says so, may never be deleted or rewritten."""
        linked = self.linked_dates()
        for r in self.links().values():
            if r.get("link_status") in LINKED and r.get("garmin_workout_id"):
                self.protected.setdefault(int(r["garmin_workout_id"]), f"its row {r['workout_key']} is linked")
        for e in self.garmin.ours_entries():
            if e["scheduleDate"] in linked:
                self.protected.setdefault(e["workoutId"], f"the session of {e['scheduleDate']} was run")

    def feedback(self, day: dt.date, rpe: int, pain: int = 0, location: str = "none") -> None:
        """log_feedback for the session the athlete ran that day, by the key the plan showed for it."""
        keys = [s["key"] for s in self.seen.get(day.isoformat()) or []]
        if not keys:
            return
        self.step = f"log_feedback {day} ({keys[0]})"
        text = self.plans.feedback(workout_key=keys[0], rpe=rpe, pain=pain, pain_location=location, sharp=False,
                                   plan_id=self.pid)
        try:
            out = json.loads(text)
        except ValueError:
            out = {"answer": text[:200]}
        if not out.get("stored"):
            self.fail("log_feedback", json.dumps(out)[:240])

    def plan_get(self) -> Dict[str, Any]:
        self.step = "plan_get"
        return json.loads(self.plans.get(plan_id=self.pid))

    def review_and_apply(self, week_start: dt.date) -> Optional[Dict[str, Any]]:
        self.step = f"plan_review_week {week_start}"
        text = run(self.reviews.review(plan_id=self.pid, week_start=week_start.isoformat()))
        try:
            out = json.loads(text)
        except ValueError:
            self.fail("plan_review_week answers", text[:300])
            return None
        apply = out.get("apply") or {}
        if not apply.get("token"):
            self.fail("review apply token", f"no apply token: {apply.get('why')} changes "
                                            f"{[c.get('rule') for c in out.get('changes') or []]}")
            return out
        on = [e["scheduleDate"] for e in self.garmin.ours_entries() if e["scheduleDate"] > self.today().isoformat()]
        window = (self.today() + dt.timedelta(days=1), dt.date.fromisoformat(max(on))) if on else None
        self.step = f"plan_apply_changes {apply['change_ids']}"
        ids = ",".join(apply["change_ids"])
        self._run_token("apply", lambda: self.writes.apply(self.pid, apply["token"], ids), window)
        # the watch now has the applied version's sessions after today (as after a preview)
        today = self.today().isoformat()
        self.seen = {d: v for d, v in self.seen.items() if d <= today}
        for s in self.sessions():
            if s["date"] > today:
                self.seen.setdefault(s["date"], []).append(s)
        return out

    def cancel(self) -> None:
        self.step = "plan_cancel preview"
        out = json.loads(run(self.writes.cancel(plan_id=self.pid)))
        token = (out.get("cancel") or {}).get("token")
        if not token:
            self.fail("cancel token", json.dumps(out)[:200])
            return
        self.step = "plan_cancel"
        self._run_token("cancel", lambda: self.writes.cancel(plan_id=self.pid, cancel_token=token), None)
        self.check_nothing_future("after plan_cancel", today_kept=True)

    def cleanup(self, orphans: bool = True) -> Dict[str, int]:
        self.step = f"--plan-cleanup{' --orphans' if orphans else ''}"
        n_log = len(self.garmin.log)
        counts = pw.cleanup(self.store, self.garmin, self.today(), dry_run=False, orphans=orphans,
                            sleep=self._sleep, join_s=10.0)
        self.check_writes(self.garmin.log[n_log:])
        if counts.get("failed"):
            self.fail("cleanup", f"counts {counts}")
        self.check_nothing_future("after --plan-cleanup")
        self.check_all()
        return counts

    # invariants -------------------------------------------------------------------------------------------------

    def check_all(self) -> None:
        g = self.garmin
        with g.lock:
            entries = list(g.calendar.values())
            library = dict(g.library)
        # no entry whose workout is gone
        for e in entries:
            if e["workoutId"] not in library:
                self.fail("entry of a deleted workout",
                          f"calendar entry {e['scheduledWorkoutId']} on {e['scheduleDate']} points to workout "
                          f"{e['workoutId']}, which is no longer in the library")
        # none of our workouts left in the library off the calendar
        on_calendar = {e["workoutId"] for e in entries}
        for wid, w in library.items():
            if w["ours"] and wid not in on_calendar:
                self.fail("workout left off the calendar", f"our workout {wid} ({w['workoutName']}, last date "
                                                           f"{(w['dates'] or ['none'])[-1]}) is in the library with "
                                                           "no calendar entry")
        # never two of our entries for one session or date
        ours = g.ours_entries()
        planned: Dict[str, int] = {}
        for s in self.sessions():
            planned[s["date"]] = planned.get(s["date"], 0) + 1
        by_day: Dict[str, List[Dict[str, Any]]] = {}
        for e in ours:
            by_day.setdefault(e["scheduleDate"], []).append(e)
        for day, items in sorted(by_day.items()):
            per_workout: Dict[int, int] = {}
            for e in items:
                per_workout[e["workoutId"]] = per_workout.get(e["workoutId"], 0) + 1
            if any(n > 1 for n in per_workout.values()):
                self.fail("two entries of one workout", f"{day}: {per_workout}")
            limit = max(1, planned.get(day, 0))
            if len(items) > limit:
                self.fail("two of our entries on one day",
                          f"{day}: {len(items)} of our workouts ({[e['workoutName'] for e in items]}, ids "
                          f"{[e['workoutId'] for e in items]}) for {planned.get(day, 0)} planned session(s)")
        # non-ours entries and workouts untouched
        if g.foreign_state() != self.foreign:
            self.fail("not ours touched", "a TrainingPeaks or user workout or calendar entry changed")

    def check_spacing(self) -> None:
        """The current version keeps every non-ours hard or long session (TrainingPeaks, the user's "Coach easy")
        apart from its hard or long days, and plans no run on a day with another run."""
        today = self.today().isoformat()
        foreign_hard: Dict[str, str] = {}
        foreign_run: Dict[str, str] = {}
        athlete = {"lt_speed_mps": None, "lt_hr": None, "ftp_w": None}
        with self.garmin.lock:
            for e in self.garmin.calendar.values():
                w = self.garmin.library.get(e["workoutId"])
                if w is None or w["ours"]:
                    continue
                c = ds.classify_workout(dict(e), None, athlete=athlete)
                if c.get("hard") or c.get("long"):
                    foreign_hard[e["scheduleDate"]] = e["workoutName"]
                if c.get("sport") == "run":
                    foreign_run[e["scheduleDate"]] = e["workoutName"]
        for s in self.sessions():
            if s["date"] < today:
                continue
            if s["date"] in foreign_run:
                self.fail("run on a day with another run",
                          f"the plan puts its {s['role']} ({s['key']}) on {s['date']}, the day of "
                          f"'{foreign_run[s['date']]}' (not ours)")
            if not s["hard"]:
                continue
            d = dt.date.fromisoformat(s["date"])
            for k in (-1, 0, 1):
                near = (d + dt.timedelta(days=k)).isoformat()
                if near in foreign_hard:
                    self.fail("hard-day spacing", f"the plan's {s['role']} on {s['date']} touches "
                                                  f"'{foreign_hard[near]}' (not ours, hard or long) on {near}")

    def check_summary(self, action: str, summary: Optional[Dict[str, Any]], executed: List[Dict[str, Any]]) -> None:
        if summary is None:
            if executed:
                self.fail("confirmation", f"{len(executed)} Garmin writes without a confirmation summary")
            return
        schedules = [x for x in executed if x["op"] == "schedule"]
        deletes = [x for x in executed if x["op"] == "delete"]
        if action == "cancel":
            want = (summary.get("count", 0), summary.get("dates", []))
            got = (len(deletes), sorted({str(x["date"]) for x in deletes}))
            if want != got:
                self.fail("confirmation matches the writes", f"cancel confirmed {want[0]} removal(s) on {want[1]}, "
                                                             f"deleted {got[0]} on {got[1]}")
            return
        want_add = (summary.get("count", 0), summary.get("dates", []))
        got_add = (len(schedules), sorted({x["date"] for x in schedules}))
        want_rm = (summary.get("removes", 0), summary.get("remove_dates", []))
        got_rm = (len(deletes), sorted({str(x["date"]) for x in deletes}))
        if want_add != got_add:
            self.fail("confirmation matches the writes", f"{action} confirmed adding {want_add[0]} on {want_add[1]}, "
                                                         f"scheduled {got_add[0]} on {got_add[1]}")
        if want_rm != got_rm:
            self.fail("confirmation matches the writes", f"{action} confirmed removing {want_rm[0]} on {want_rm[1]}, "
                                                         f"deleted {got_rm[0]} on {got_rm[1]}")

    def check_writes(self, executed: List[Dict[str, Any]], action: str = "cleanup") -> None:
        """Past and done sessions never deleted or rewritten; today's never removed by a commit or apply unless it
        schedules today's new workout (a red-day change, rule A8); non-ours never written."""
        rescheduled = {x["date"] for x in executed if x["op"] == "schedule"}
        for x in executed:
            today = x["today"].isoformat()
            if action in ("commit", "apply") and x["op"] in ("unschedule", "delete") and str(x.get("date")) == today \
                    and today not in rescheduled:
                self.fail("today's session removed", f"{action}: {x['op']} of today's ({today}) workout "
                                                     f"{x['workout']}; the athlete has nothing to run today")
            if x["op"] in ("unschedule", "delete") and not x.get("ours"):
                self.fail("not ours touched", f"{x['op']} of workout {x['workout']} on {x['date']} (not ours)")
            if x["op"] in ("unschedule", "delete", "schedule") and x.get("date") and str(x["date"]) < today:
                self.fail("past session touched", f"{x['op']} of workout {x['workout']} dated {x['date']} "
                                                  f"(today {today})")
            if x["op"] in ("unschedule", "delete") and x["workout"] in self.protected:
                self.fail("done session deleted", f"{x['op']} of workout {x['workout']} on {x['date']}: "
                                                  f"{self.protected[x['workout']]}")
            if x["op"] == "unschedule" and x.get("linked"):
                self.fail("done session deleted", f"unschedule of entry {x['schedule']} on {x['date']}, which "
                                                  f"Garmin linked to activity {x['linked']}")
        linked = self.linked_dates()
        for x in executed:
            if x["op"] == "schedule" and x["date"] in linked and x["date"] <= x["today"].isoformat():
                self.fail("done session rewritten", f"a new workout scheduled on {x['date']}, whose session was "
                                                    "already run")

    def check_unchanged(self, before: List[Dict[str, Any]], executed: List[Dict[str, Any]],
                        window: Optional[Tuple[dt.date, dt.date]] = None) -> None:
        """A session on Garmin whose date and content the current version still has stays: not unscheduled, not
        deleted, not uploaded again."""
        today = self.today().isoformat()
        wanted = {(s["date"], s["content"]) for s in self.sessions()}
        removed = {x["schedule"] for x in executed if x["op"] == "unschedule"}
        removed_w = {x["workout"] for x in executed if x["op"] == "delete"}
        new = [(x["date"], x["content"]) for x in executed if x["op"] == "schedule"]
        counted: Dict[Tuple[str, str], int] = {}
        for e in before:
            counted[(e["scheduleDate"], e["content"])] = counted.get((e["scheduleDate"], e["content"]), 0) + 1
        for e in before:
            pair = (e["scheduleDate"], e["content"])
            if e["scheduleDate"] < today or pair not in wanted or counted[pair] > 1:
                continue
            if e["scheduledWorkoutId"] in removed or e["workoutId"] in removed_w:
                outside = window is not None and e["scheduleDate"] > window[1].isoformat()
                self.fail("unchanged session deleted" + (" outside the commit window" if outside else ""),
                          f"{e['workoutName']} on {e['scheduleDate']} (workout {e['workoutId']}) is unchanged in "
                          "the plan but was " + ("unscheduled and deleted" if e["workoutId"] in removed_w
                                                 else "unscheduled")
                          + (f" (window {window[0]}..{window[1]})" if window is not None else ""))
            if pair in new:
                self.fail("unchanged session uploaded again",
                          f"{e['workoutName']} on {e['scheduleDate']} was already on Garmin with this content")

    def check_window(self, window: Tuple[dt.date, dt.date]) -> None:
        """After a done commit every planned session of the window is on Garmin once, with its content; no day
        from today on holds more of our workouts than the plan has sessions, and none holds one the plan dropped."""
        first, last = window[0].isoformat(), window[1].isoformat()
        linked = self.linked_dates()
        ours = self.garmin.ours_entries()
        sessions = self.sessions()
        for s in sessions:
            if not first <= s["date"] <= last or s["date"] in linked:
                continue
            same = [e for e in ours if e["scheduleDate"] == s["date"] and e["content"] == s["content"]]
            if len(same) != 1:
                others = [e["workoutName"] for e in ours if e["scheduleDate"] == s["date"]]
                self.fail("window session on Garmin",
                          f"the plan's {s['role']} ({s['key']}, {s['km']} km) on {s['date']} is on Garmin "
                          f"{len(same)} time(s) with its content (our workouts that day: {others}); window "
                          f"{first}..{last}")
        planned_days = {s["date"] for s in sessions}
        today = self.today().isoformat()
        for e in ours:   # today stays: a new version never plans today, so nothing replaces its workout
            if first <= e["scheduleDate"] <= last and e["scheduleDate"] != today and e["scheduleDate"] not in planned_days \
                    and e["scheduleDate"] not in linked:
                self.fail("dropped session left on Garmin",
                          f"our {e['workoutName']} (workout {e['workoutId']}) on {e['scheduleDate']}: the plan has "
                          "no session that day any more")

    def check_nothing_future(self, label: str, today_kept: bool = False) -> None:
        """Nothing of ours from today on (after today with `today_kept`: plan_cancel never deletes today's)."""
        today = self.today().isoformat()
        linked = self.linked_dates()
        left = [e for e in self.garmin.ours_entries() if e["scheduleDate"] >= today
                and not (e["scheduleDate"] == today and (today_kept or e["scheduleDate"] in linked
                                                         or e.get("associatedActivityId")))]
        if left:
            self.fail(f"nothing of ours from today on {label}",
                      f"{len(left)} of our entries left: {[(e['scheduleDate'], e['workoutName']) for e in left]}")
        with self.garmin.lock:
            gone = [wid for wid, w in self.garmin.library.items() if w["ours"]
                    and not any(e["workoutId"] == wid for e in self.garmin.calendar.values())]
        if gone:
            self.fail(f"no library leftovers {label}", f"our workouts with no calendar entry: {gone}")


# --- the scenarios -----------------------------------------------------------------------------------------------

def days(first: dt.date, last: dt.date):
    day = first
    while day <= last:
        yield day
        day += dt.timedelta(days=1)


@unittest.skipUnless(HAVE_UPSTREAM, NEEDS)
class PlanDays(unittest.TestCase):
    def setUp(self):
        garmin_paths.allow_plan_writes(set(garmin_paths.WRITE_PATHS))
        self.addCleanup(garmin_paths.allow_plan_writes, ())
        self.addCleanup(lambda: self.assertTrue(pw.join_write_threads(10), "a write thread outlived the test"))
        self.addCleanup(plan_tracking.configure, None)
        env = mock.patch.dict(os.environ, {"REPORT_TZ": "UTC"})
        env.start()
        self.addCleanup(env.stop)

    def sim(self, name, tz="UTC", **kw):
        os.environ["REPORT_TZ"] = tz
        return Sim(self, name, tz=tz, **kw)

    def inputs_on(self, sim: Sim, day: dt.date) -> None:
        """The B race leaves the inputs once it is not after today (plan_preview refuses it then)."""
        if sim.inputs.get("b_races") and day >= dt.date(2028, 1, 28):
            sim.inputs.pop("b_races")

    def test_0_control_one_build(self):
        """Control: one build, 14 days committed, the athlete runs them, no later preview. Every invariant holds
        (the paths every other plan test covers), so a failure elsewhere is the later day's doing."""
        sim = self.sim("control", inputs={"race_date": "2028-02-25", "goal_time": "3:52:00"},
                       history_until=dt.date(2028, 2, 6))
        sim.morning(dt.date(2028, 2, 6), days=14)
        for day in days(dt.date(2028, 2, 7), dt.date(2028, 2, 19)):
            sim.at(day, 7)
            sim.track(f"morning report {day}")
            if sim.run_session(day):
                sim.feedback(day, rpe=4)
        sim.at(dt.date(2028, 2, 20), 9)
        sim.cancel()
        sim.report()

    def test_1_weekly_rhythm(self):
        """Build on a Wednesday, commit 7 days; every following morning preview with the same inputs and commit
        7 days; the athlete runs each session. Five weeks across the year end, a month end and the B race."""
        sim = self.sim("weekly rhythm")
        for day in days(dt.date(2027, 12, 29), dt.date(2028, 2, 2)):
            self.inputs_on(sim, day)
            sim.morning(day)
            sim.run_session(day)
        sim.report()

    def test_2_commit_14_then_7(self):
        """Commit 14 days, then the next mornings preview and commit with the default 7."""
        sim = self.sim("commit 14 then 7", history_until=dt.date(2028, 1, 11))
        sim.morning(dt.date(2028, 1, 12), days=14)
        for day in days(dt.date(2028, 1, 13), dt.date(2028, 1, 19)):
            sim.morning(day, days=7)
        sim.report()

    def test_3_runs_linked_then_commit(self):
        """Runs every planned day: some partial, one missed, one moved to the next day, one Garmin linked itself;
        tracking links them and feedback is logged; each morning the plan is previewed and committed again."""
        sim = self.sim("runs and tracking")
        partial = {dt.date(2028, 1, 5), dt.date(2028, 1, 11)}
        missed = {dt.date(2028, 1, 8)}
        moved = {dt.date(2028, 1, 13): dt.date(2028, 1, 14)}
        for day in days(dt.date(2028, 1, 2), dt.date(2028, 1, 18)):
            sim.morning(day)
            for was, on in moved.items():
                if on == day:
                    sim.run_session(was, on=day, hour=8, expect="done_late")
            if day in missed or day in moved:
                continue
            if sim.run_session(day, share=0.6 if day in partial else 1.0, garmin_link=day == dt.date(2028, 1, 9),
                               expect="partial" if day in partial else "done"):
                sim.feedback(day, rpe=4)
        sim.report()

    def test_4_override_review_blocked_dates(self):
        """An override after days are committed, a weekly-review change applied, travel dates blocked mid-plan."""
        # no B race: the week after the reviewed one then has quality sessions for the pain change to drop (with
        # the B race it is the lighter B week, and the review is advice only, with no apply token)
        sim = self.sim("override, review, travel", inputs={"race_date": "2028-02-25", "goal_time": "3:52:00"},
                       history_until=dt.date(2028, 1, 16))
        first = sim.morning(dt.date(2028, 1, 17))
        sim.run_session(dt.date(2028, 1, 17))
        # the next morning: rest on an easy day of the committed window and the quality session made easy
        window = [s for s in sim.sessions() if "2028-01-19" <= s["date"] <= "2028-01-23"]
        easy = next((s["date"] for s in window if s["role"] == "easy"), None)
        quality = next((s["date"] for s in window if s["role"] in ("threshold", "intervals", "mp")), None)
        overrides = [o for o in ({"kind": "rest", "date": easy} if easy else None,
                                 {"kind": "easy", "date": quality} if quality else None) if o]
        self.assertTrue(first and overrides, "the week has an easy and a quality session to edit")
        sim.inputs["overrides"] = json.dumps(overrides)
        sim.morning(dt.date(2028, 1, 18), days=14)   # next week on Garmin too, for the review's apply
        # no preview for the rest of the week (the review reads the week from the stored version), pain once
        pain_day = None
        for day in days(dt.date(2028, 1, 18), dt.date(2028, 1, 23)):
            if day > dt.date(2028, 1, 18):
                sim.at(day, 7)
                sim.track(f"morning report {day}")
            if sim.run_session(day):
                pain_day = pain_day or (day if day > dt.date(2028, 1, 18) else None)
                sim.feedback(day, rpe=6, pain=5 if day == pain_day else 0,
                             location="calf" if day == pain_day else "none")
        sim.at(dt.date(2028, 1, 24), 8)
        sim.inputs.pop("overrides", None)
        sim.review_and_apply(dt.date(2028, 1, 17))
        for day in days(dt.date(2028, 1, 25), dt.date(2028, 1, 26)):
            self.inputs_on(sim, day)
            sim.at(day, 7)
            sim.track(f"morning report {day}")
            sim.run_session(day)
        sim.inputs["blocked_dates"] = "2028-01-30,2028-01-31,2028-02-01"
        for day in days(dt.date(2028, 1, 27), dt.date(2028, 2, 4)):
            self.inputs_on(sim, day)
            sim.morning(day)
            sim.run_session(day)
        blocked = {"2028-01-30", "2028-01-31", "2028-02-01"}
        on_blocked = [e for e in sim.garmin.ours_entries() if e["scheduleDate"] in blocked]
        if on_blocked:
            sim.fail("blocked dates", f"our workouts on blocked dates: {[e['scheduleDate'] for e in on_blocked]}")
        sim.report()

    def test_5_cancel_and_cleanup(self):
        """Cancel mid-plan; then a new plan and --plan-cleanup --orphans with a marker orphan on the calendar."""
        sim = self.sim("cancel and cleanup", inputs={"race_date": "2028-02-25", "goal_time": "3:52:00"},
                       history_until=dt.date(2028, 2, 1))
        sim.morning(dt.date(2028, 2, 2), days=10)
        for day in days(dt.date(2028, 2, 2), dt.date(2028, 2, 4)):
            if day > dt.date(2028, 2, 2):
                sim.morning(day, days=10)
            sim.run_session(day)
        sim.at(dt.date(2028, 2, 5), 9)
        sim.cancel()
        sim.pid = None
        sim.morning(dt.date(2028, 2, 6), days=14)
        sim.garmin.plant_orphan(sim.store.install_tag(), dt.date(2028, 2, 9))
        sim.at(dt.date(2028, 2, 7), 9)
        sim.cleanup(orphans=True)
        sim.report()

    def _boundaries(self, name, tz, race, first, last, preview_at, commit_at, run_at):
        sim = self.sim(name, tz=tz, inputs={"race_date": race, "race_distance": "half", "goal_time": "1:40:00"},
                       history_until=first)
        for day in days(first, last):
            sim.at(day, *preview_at)
            sim.track(f"report {day}")
            out = sim.preview()
            if day == first:
                built = (sim.plan_get().get("stored") or {}).get("built_on")
                if built != day.isoformat():
                    sim.fail("plan_get built_on", f"built on {day} local ({tz}), plan_get says {built}")
            nxt = day + dt.timedelta(days=1)
            sim.at(nxt if commit_at[0] < preview_at[0] else day, *commit_at)
            sim.commit(out)
            sim.run_session(nxt, hour=run_at[0], minute=run_at[1])
        sim.report()

    def test_6a_late_evening_new_york_dst(self):
        """REPORT_TZ America/New_York (UTC-5, then -4 from 2028-03-12): preview at 23:40 local (the next day in
        UTC), commit after local midnight, runs at 23:15 local (the next UTC day), across the DST change."""
        self._boundaries("New York evenings, DST", "America/New_York", "2028-04-23", dt.date(2028, 3, 8),
                         dt.date(2028, 3, 18), (23, 40), (0, 5), (23, 15))

    def test_6b_after_midnight_jerusalem_dst(self):
        """REPORT_TZ Asia/Jerusalem (UTC+2, then +3 from 2028-03-24): preview at 23:50 local, commit at 00:15
        (still the previous day in UTC), runs at 00:40 local (the previous UTC day), across the DST change."""
        self._boundaries("Jerusalem midnights, DST", "Asia/Jerusalem", "2028-05-05", dt.date(2028, 3, 20),
                         dt.date(2028, 3, 30), (23, 50), (0, 15), (0, 40))


    def test_7_review_after_a_later_preview(self):
        """The weekly review of last week after the plan was previewed again on its Sunday (same inputs): the
        week's sessions must still be reviewable."""
        sim = self.sim("review after a later preview", inputs={"race_date": "2028-02-25", "goal_time": "3:52:00"},
                       history_until=dt.date(2028, 1, 31))
        sim.morning(dt.date(2028, 1, 31))
        for day in days(dt.date(2028, 2, 1), dt.date(2028, 2, 5)):
            sim.run_session(day)
        sim.morning(dt.date(2028, 2, 6))
        sim.run_session(dt.date(2028, 2, 6))
        sim.at(dt.date(2028, 2, 7), 8)
        sim.step = "plan_review_week 2028-01-31"
        text = run(sim.reviews.review(plan_id=sim.pid, week_start="2028-01-31"))
        try:
            out = json.loads(text)
            if not out["metrics"].get("planned_km"):
                sim.fail("review of last week", f"no planned km for the week: {json.dumps(out['metrics'])[:300]}")
        except (ValueError, KeyError):
            sim.fail("review of last week", text[:300])
        sim.report()


if __name__ == "__main__":
    unittest.main()
