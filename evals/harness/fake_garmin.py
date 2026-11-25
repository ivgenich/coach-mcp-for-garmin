"""The fake Garmin under the real server: answers from a state (state.py), faults, and a call log.

Full mode (GARMIN_AUTH=full): FakeGarmin stands in for garminconnect's Garmin client at the place upstream's
login puts it (behind upstream's own _GarminProxy), with the method names and signatures of garminconnect.Garmin
and its input checks (dates YYYY-MM-DD, positive ids, the activity limit), so a bad argument fails as it would.

Key mode (GARMIN_AUTH=key): the real key_mode.KeyModeGarmin runs, over FakePersonalApiHttp (its HTTP layer: the
Personal API paths mapped back to the same state) and FakeAuthenticator (a token for any key). Key mode's own
checks, path rules, error mapping and fail-closed names all run.

In both modes RecordingClient wraps the client: it logs every call (method, arguments, the tool that made it)
and applies the faults of the state. A faulted call raises what Garmin's failure of that group raises there
(full: garminconnect's exceptions; key: an HTTP status through key mode's own mapping), so the server's real
tool_errors classifier writes the text the model sees.

Writes (full mode only): FakeGarmin has garminconnect's upload_workout, schedule_workout, unschedule_workout and
delete_workout, and its garth-style `client` answers post/put/delete on the same workout-service paths (the raw
calls the plan executor makes). They change the World: a created workout answers get_workout_by_id, a scheduled
one shows in the calendar (query_garmin_graphql's workoutScheduleSummariesScalar) until it is unscheduled. A
write fault (state.WRITE_FAULT_GROUPS, keyed by evals/garmin_writes.write_op's names, "@N" lets the first N
through) raises what garminconnect's raw client raises for that failure: 429 and 5xx as
GarminConnectConnectionError("API Error 429 ..."), timeouts as requests' ConnectTimeout (never sent) and
ReadTimeout (raised after the write landed, so a retry without a check makes a duplicate).
"""

from __future__ import annotations

import base64
import copy
import inspect
import re
import threading

from ..garmin_writes import WRITE_METHODS, write_op
from .state import BYTES_KEY, FAULT_KEY, parse_write_fault

ACTIVITIES_PATH = "/activitylist-service/activities/search/activities"
METRICS_PATH = "/metrics-service/metrics/maxmet/daily"
HR_ZONES_PATH = "/biometric-service/heartRateZones"
COURSES_PATH = "/course-service/course"


class _Missing:
    """A default answer that is a Garmin failure, not a value."""

    def __init__(self, name):
        self.name = name

    def __repr__(self):
        return self.name


# Garmin has nothing under this id (HTTP 404).
NOT_FOUND = _Missing("NOT_FOUND")
# garminconnect's get_user_summary/get_stats on an empty day: "No data received from server".
NO_SUMMARY = _Missing("NO_SUMMARY")

# Every garminconnect.Garmin method the server's 31 tools call (README.md lists which tool calls which), and the
# answer when the state has none: Garmin's own "nothing here" for that call. "connectapi" covers both
# garmin.connectapi(path) and the garth client's garmin.client.connectapi(path) (courses); its default depends on
# the path (_connectapi_default).
METHODS = {
    "connectapi": None,
    "get_activities": [],
    "get_activities_by_date": [],
    "get_activity": NOT_FOUND,
    "get_activity_splits": NOT_FOUND,
    "get_activity_hr_in_timezones": [],
    "get_activity_power_in_timezones": [],
    "get_activity_details": NOT_FOUND,
    "get_activity_types": [],
    "download_activity": NOT_FOUND,
    "get_body_composition": {"dateWeightList": [], "totalAverage": {}},
    "query_garmin_graphql": None,
    "get_workout_by_id": NOT_FOUND,
    "get_workouts": [],
    "get_stats": NO_SUMMARY,
    "get_user_summary": NO_SUMMARY,
    "get_sleep_data": {"dailySleepDTO": {"calendarDate": None}},
    "get_hrv_data": None,
    "get_body_battery": [],
    "get_weekly_stress": [],
    "get_training_readiness": [],
    "get_training_status": {},
    "get_devices": [],
    "get_device_last_used": {},
    "get_user_profile": {},
    "get_max_metrics": [],
    "get_endurance_score": {},
    "get_lactate_threshold": {},
    "get_cycling_ftp": {},
    "get_race_predictions": {},
    "get_personal_record": [],
    "get_progress_summary_between_dates": [],
    "get_weigh_ins": {"dailyWeightSummaries": [], "totalAverage": {}},
    "get_gear": [],
    "get_gear_stats": {},
}
# Methods whose list answer under "*" is cut to the asked dates (first and second argument).
RANGE_METHODS = frozenset({"get_body_battery", "get_endurance_score", "get_progress_summary_between_dates"})
# The activity-list calls share one list (see World.activity_list).
ACTIVITY_LIST_METHODS = ("get_activities_by_date", "get_activities")
# Arguments garminconnect checks the same way in every method that has them.
_DATE_PARAMS = frozenset({"cdate", "startdate", "enddate", "end", "date"})
_ID_METHODS = frozenset({"get_activity", "get_activity_splits", "get_activity_hr_in_timezones",
                         "get_activity_power_in_timezones", "get_workout_by_id", "get_activity_details"})
# Sport families of garminconnect's activityType filter (a parent type matches its children).
_TYPE_FAMILIES = {
    "running": ("running", "treadmill_running", "trail_running", "track_running", "street_running",
                "indoor_running", "virtual_run"),
    "cycling": ("cycling", "road_biking", "virtual_ride", "indoor_cycling", "mountain_biking", "gravel_cycling"),
    "swimming": ("swimming", "lap_swimming", "open_water_swimming"),
}
_GRAPHQL_ROOT_RE = re.compile(r"query\s*\{\s*([A-Za-z_]+)")
_GRAPHQL_DATES_RE = re.compile(r'startDate:\s*"(\d{4}-\d{2}-\d{2})"\s*,\s*endDate:\s*"(\d{4}-\d{2}-\d{2})"')
SCHEDULE_ROOT = "workoutScheduleSummariesScalar"
# Ids the fake gives the workouts and calendar entries it creates (far from every fixture's ids).
FIRST_WORKOUT_ID = 91000001
FIRST_SCHEDULE_ID = 92000001
_WORKOUT_PATH_RE = re.compile(r"workout-service/workout/(\d+)")
_SCHEDULE_PATH_RE = re.compile(r"workout-service/schedule/(\d+)")


class FaultSignal(Exception):
    """Raised inside the fake when a call must fail with `group`; each mode turns it into its own exception."""

    def __init__(self, group):
        super().__init__(group)
        self.group = group


def json_safe(value):
    """`value` with anything JSON cannot hold written as str (an enum, bytes)."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    if isinstance(value, bytes):
        return f"<{len(value)} bytes>"
    return str(value)


def _day(value):
    return value[:10] if isinstance(value, str) and len(value) >= 10 else None


def _item_day(item):
    if not isinstance(item, dict):
        return None
    for key in ("calendarDate", "date", "summaryDate", "startTimeLocal", "scheduleDate"):
        day = _day(item.get(key))
        if day:
            return day
    return None


def _in_range(items, start, end):
    if not isinstance(items, list):
        return items
    return [item for item in items if _item_day(item) is None
            or ((start is None or _item_day(item) >= start) and (end is None or _item_day(item) <= end))]


def decode_answer(answer):
    """A state answer as the client returns it: {"$base64": ...} -> bytes, {"$fault": group} -> FaultSignal."""
    if isinstance(answer, dict) and FAULT_KEY in answer:
        raise FaultSignal(answer[FAULT_KEY])
    if isinstance(answer, dict) and set(answer) == {BYTES_KEY}:
        return base64.b64decode(answer[BYTES_KEY])
    return copy.deepcopy(answer)


# The fields of a get_workouts item the fake answers (Garmin's list carries no steps).
_LIBRARY_KEYS = ("workoutId", "workoutName", "description", "sportType", "estimatedDurationInSecs",
                 "estimatedDistanceInMeters")


class World:
    """The answers of one state, shared by both modes' fakes."""

    def __init__(self, state):
        self.responses = (state or {}).get("responses", {})
        self.workouts = {}   # workout id -> the workout JSON as created (with its workoutId)
        self.schedules = {}  # calendar entry id -> {"workoutId", "date"}
        self._ids = [FIRST_WORKOUT_ID, FIRST_SCHEDULE_ID]
        self._write_lock = threading.Lock()

    # -- writes (full mode)

    def create_workout(self, payload):
        if not isinstance(payload, dict):
            raise WriteRejected(400)
        with self._write_lock:
            wid = self._ids[0]
            self._ids[0] += 1
            workout = copy.deepcopy(payload)
            workout["workoutId"] = wid
            self.workouts[wid] = workout
            return copy.deepcopy(workout)

    def schedule(self, workout_id, date):
        with self._write_lock:
            wid = int(workout_id)
            if wid not in self.workouts and str(wid) not in self.responses.get("get_workout_by_id", {}):
                raise WriteRejected(404)
            sid = self._ids[1]
            self._ids[1] += 1
            self.schedules[sid] = {"workoutId": wid, "date": str(date)[:10]}
            return {"workoutScheduleId": sid, "calendarDate": str(date)[:10],
                    "workout": copy.deepcopy(self.workouts.get(wid) or {"workoutId": wid})}

    def unschedule(self, schedule_id):
        with self._write_lock:
            if self.schedules.pop(int(schedule_id), None) is None:
                raise WriteRejected(404)
            return {}

    def delete_workout(self, workout_id):
        with self._write_lock:
            wid = int(workout_id)
            if self.workouts.pop(wid, None) is None:
                raise WriteRejected(404)
            for sid in [k for k, v in self.schedules.items() if v["workoutId"] == wid]:
                del self.schedules[sid]   # deleting a workout removes its calendar entries
            return {}

    def library(self, start=0, limit=100):
        """get_workouts: the state's own library ("*") then the workouts the fake's writes created, paged."""
        answer, _ = self.lookup("get_workouts", "*")
        items = list(decode_answer(answer) or []) if answer is not None else []
        with self._write_lock:
            items += [{k: copy.deepcopy(v) for k, v in w.items() if k in _LIBRARY_KEYS}
                      for _, w in sorted(self.workouts.items())]
        return items[int(start):int(start) + int(limit)]

    def schedule_entries(self):
        """The calendar entries the fake's own writes made, as workoutScheduleSummariesScalar items."""
        with self._write_lock:
            out = []
            for sid, entry in sorted(self.schedules.items()):
                w = self.workouts.get(entry["workoutId"]) or {}
                out.append({"scheduleDate": entry["date"], "workoutName": w.get("workoutName"),
                            "workoutType": ((w.get("sportType") or {}).get("sportTypeKey") or "running"),
                            "estimatedDurationInSecs": w.get("estimatedDurationInSecs"),
                            "estimatedDistanceInMeters": w.get("estimatedDistanceInMeters"),
                            "isRestDay": False, "workoutId": entry["workoutId"], "workoutScheduleId": sid,
                            "associatedActivityId": None})
            return out

    def lookup(self, method, key):
        """(answer, found under "*") for responses[method][key], else [method]["*"], else (None, None)."""
        answers = self.responses.get(method, {})
        if key is not None and str(key) in answers:
            return answers[str(key)], False
        if "*" in answers:
            return answers["*"], True
        return None, None

    def answer(self, method, key, start=None, end=None):
        """The decoded answer, or METHODS' default (a value, NOT_FOUND or NO_SUMMARY)."""
        if method == "get_workout_by_id" and str(key).isdigit():
            with self._write_lock:
                if int(key) in self.workouts:
                    return copy.deepcopy(self.workouts[int(key)])
        answer, wildcard = self.lookup(method, key)
        if wildcard is None:
            default = METHODS.get(method)
            return copy.deepcopy(default) if not isinstance(default, _Missing) else default
        value = decode_answer(answer)
        if wildcard and method in RANGE_METHODS:
            value = _in_range(value, start, end)
        return value

    # -- the activity list

    def activity_list(self, method):
        """The account's activity list for an activity-list call by `method`: its own "*" answer, else the first
        of connectapi[ACTIVITIES_PATH], get_activities["*"], get_activities_by_date["*"]. Newest first."""
        keys = [(method, "*")] if method != "connectapi" else []
        keys += [("connectapi", ACTIVITIES_PATH), ("get_activities", "*"), ("get_activities_by_date", "*")]
        for name, key in keys:
            answers = self.responses.get(name, {})
            if key in answers:
                items = decode_answer(answers[key])
                break
        else:
            items = []
        if not isinstance(items, list):
            return items
        return sorted(items, key=lambda a: str(a.get("startTimeLocal") or "") if isinstance(a, dict) else "",
                      reverse=True)

    def activities(self, method, start_date=None, end_date=None, activity_type=None, start=0, limit=None):
        """Activities as Garmin's search answers them: by start date (local), type family, then the page."""
        items = self.activity_list(method)
        if not isinstance(items, list):
            return items
        items = _in_range(items, _day(start_date), _day(end_date)) if (start_date or end_date) else items
        if activity_type:
            family = _TYPE_FAMILIES.get(str(activity_type), (str(activity_type),))
            items = [a for a in items if isinstance(a, dict)
                     and (a.get("activityType") or {}).get("typeKey") in family]
        start = int(start or 0)
        return items[start:start + int(limit)] if limit is not None else items[start:]

    # -- connectapi paths and GraphQL

    def connectapi(self, path, params=None):
        params = params or {}
        bare = "/" + path.lstrip("/") if isinstance(path, str) else path
        if bare == ACTIVITIES_PATH:
            return self.activities("connectapi", params.get("startDate"), params.get("endDate"),
                                   params.get("activityType"), params.get("start") or 0, params.get("limit"))
        answers = self.responses.get("connectapi", {})
        for key in (path, bare, bare.lstrip("/")):
            if key in answers:
                return decode_answer(answers[key])
        prefixes = sorted((k for k in answers if k.endswith("*")), key=len, reverse=True)
        for key in prefixes:
            if bare.startswith("/" + key[:-1].lstrip("/")):
                return decode_answer(answers[key])
        return _connectapi_default(bare)

    def graphql(self, query):
        text = query.get("query") if isinstance(query, dict) else query
        text = text if isinstance(text, str) else ""
        m = _GRAPHQL_ROOT_RE.search(text)
        root = m.group(1) if m else "*"
        answers = self.responses.get("query_garmin_graphql", {})
        if root in answers:
            value = decode_answer(answers[root])
        elif "*" in answers and root in ((decode_answer(answers["*"]) or {}).get("data") or {}):
            value = decode_answer(answers["*"])
        else:
            value = {"data": {root: [] if root == SCHEDULE_ROOT else None}}
        dates = _GRAPHQL_DATES_RE.search(text)
        data = value.get("data") if isinstance(value, dict) else None
        if root == SCHEDULE_ROOT and isinstance(data, dict) and isinstance(data.get(SCHEDULE_ROOT), list):
            data[SCHEDULE_ROOT] = data[SCHEDULE_ROOT] + self.schedule_entries()
        if dates and isinstance(data, dict) and isinstance(data.get(SCHEDULE_ROOT), list):
            data[SCHEDULE_ROOT] = [e for e in data[SCHEDULE_ROOT] if not isinstance(e, dict)
                                   or dates.group(1) <= str(e.get("scheduleDate") or "")[:10] <= dates.group(2)]
        return value


def _connectapi_default(path):
    if path == COURSES_PATH:
        return []
    if path.startswith(HR_ZONES_PATH):
        return []
    if path.startswith(METRICS_PATH):
        return []
    return NOT_FOUND


def graphql_root(query):
    text = query.get("query") if isinstance(query, dict) else query
    m = _GRAPHQL_ROOT_RE.search(text if isinstance(text, str) else "")
    return m.group(1) if m else None


# --- full mode ---------------------------------------------------------------------------------------------

def _garminconnect():
    import garminconnect

    return garminconnect


def full_mode_exception(group):
    """The exception garminconnect raises for a Garmin failure of `group` (what Garmin's HTTP answer, or a read
    timeout for unavailable, becomes in garminconnect.Garmin.connectapi), or a plain RuntimeError for internal
    (a bug, not Garmin)."""
    from garminconnect import (GarminConnectAuthenticationError, GarminConnectConnectionError,
                               GarminConnectTooManyRequestsError)

    return {
        "login": lambda: GarminConnectAuthenticationError("Authentication failed: API Error 401 Unauthorized"),
        "rate_limited": lambda: GarminConnectTooManyRequestsError("Rate limit exceeded: API Error 429"),
        "no_data": lambda: GarminConnectConnectionError("API client error (404): API Error 404 Not Found"),
        "bad_input": lambda: GarminConnectConnectionError("API client error (400): API Error 400 Bad Request"),
        "unavailable": lambda: GarminConnectConnectionError("Connection error: Read timed out (read timeout=30)"),
        "internal": lambda: RuntimeError("synthetic internal failure (eval fault)"),
    }[group]()


class WriteRejected(Exception):
    """Garmin refused a write with this HTTP status (400 bad body, 404 no such id)."""

    def __init__(self, status):
        super().__init__(f"HTTP {status}")
        self.status = status


_STATUS_TEXT = {400: "Bad Request", 404: "Not Found", 429: "Too Many Requests", 503: "Service Unavailable"}


def http_error(status):
    """What garminconnect's raw client (Client._run_request) raises for an HTTP error status."""
    from garminconnect import GarminConnectConnectionError

    return GarminConnectConnectionError(f"API Error {status} - {_STATUS_TEXT.get(status, 'Error')}")


def write_exception(group):
    """What a Garmin write of fault `group` raises in full mode (state.WRITE_FAULT_GROUPS). For read_timeout,
    CallLog raises it after the write landed."""
    import requests

    return {
        "rate_limited": lambda: http_error(429),
        "server_error": lambda: http_error(503),
        "connect_timeout": lambda: requests.exceptions.ConnectTimeout("Connection to connect.garmin.com timed out"),
        "read_timeout": lambda: requests.exceptions.ReadTimeout("Read timed out (read timeout=15)"),
        "login": lambda: full_mode_exception("login"),
        "internal": lambda: full_mode_exception("internal"),
    }[group]()


class _Response:
    """requests.Response as client.post/put/delete return it without api=True."""

    def __init__(self, body):
        self._body = body
        self.status_code = 204 if body == {} else 200
        self.content = b"" if body == {} else b"{}"

    def json(self):
        return copy.deepcopy(self._body)


def _full_value(value):
    if value is NOT_FOUND:
        raise full_mode_exception("no_data")
    if value is NO_SUMMARY:
        from garminconnect import GarminConnectConnectionError

        raise GarminConnectConnectionError("No data received from server")
    return value


class _FakeGarth:
    """garmin.client: the garth-style client the course tools call (connectapi, domain)."""

    domain = "garmin.com"

    def __init__(self, world):
        self._world = world

    def connectapi(self, path, **kwargs):
        try:
            return _full_value(self._world.connectapi(path, kwargs.get("params")))
        except FaultSignal as signal:
            raise full_mode_exception(signal.group) from None

    # The raw calls garminconnect's write methods (and the plan executor) make: (domain, path, json=, api=).

    def post(self, _domain, path, **kwargs):
        return self._write("POST", path, kwargs)

    def put(self, _domain, path, **kwargs):
        return self._write("PUT", path, kwargs)

    def delete(self, _domain, path, **kwargs):
        return self._write("DELETE", path, kwargs)

    def _write(self, verb, path, kwargs):
        bare = str(path or "").lstrip("/").split("?", 1)[0]
        body = kwargs.get("json")
        op = write_op("client." + verb.lower(), ("connectapi", bare), {"json": body})
        world = self._world
        try:
            if op is None:   # a GraphQL read through the raw client
                result = world.graphql(body)
            elif op == "upload_workout":
                result = world.create_workout(body)
            elif op == "schedule_workout":
                date = body.get("date") if isinstance(body, dict) else None
                if not isinstance(date, str):
                    raise WriteRejected(400)
                result = world.schedule(_SCHEDULE_PATH_RE.fullmatch(bare).group(1), date)
            elif op == "unschedule_workout":
                result = world.unschedule(_SCHEDULE_PATH_RE.fullmatch(bare).group(1))
            elif op == "delete_workout":
                result = world.delete_workout(_WORKOUT_PATH_RE.fullmatch(bare).group(1))
            else:
                raise WriteRejected(404)
        except WriteRejected as exc:
            raise http_error(exc.status) from None
        return result if kwargs.get("api") else _Response(result)


class FakeGarmin:
    """garminconnect.Garmin's methods of METHODS, answered from a World; any other name is an AttributeError
    (as on a real client), so a tool that needs a method the fake lacks fails loudly as an internal error."""

    garmin_connect_activities = ACTIVITIES_PATH
    garmin_connect_metrics_url = METRICS_PATH
    display_name = "synthetic-user"
    full_name = "Synthetic Athlete"
    unit_system = "metric"

    def __init__(self, state):
        self._world = World(state)
        self.client = _FakeGarth(self._world)

    def __repr__(self):
        return "FakeGarmin()"

    # garminconnect's write methods, through the raw client as garminconnect does (same checks, same paths).

    def upload_workout(self, workout_json):
        if isinstance(workout_json, str):
            import json as _json

            workout_json = _json.loads(workout_json)
        if not isinstance(workout_json, (dict, list)):
            raise ValueError("workout_json must be a JSON object or array")
        return self.client.post("connectapi", "/workout-service/workout", json=workout_json, api=True)

    def schedule_workout(self, workout_id, date_str):
        gc = _garminconnect()
        workout_id = gc._validate_positive_integer(int(workout_id), "workout_id")
        date_str = gc._validate_date_format(date_str, "date_str")
        return self.client.post("connectapi", f"/workout-service/schedule/{workout_id}", json={"date": date_str},
                                api=True)

    def unschedule_workout(self, scheduled_workout_id):
        gc = _garminconnect()
        sid = gc._validate_positive_integer(int(scheduled_workout_id), "scheduled_workout_id")
        return self.client.delete("connectapi", f"/workout-service/schedule/{sid}", api=True)

    def delete_workout(self, workout_id):
        gc = _garminconnect()
        workout_id = gc._validate_positive_integer(int(workout_id), "workout_id")
        return self.client.delete("connectapi", f"/workout-service/workout/{workout_id}", api=True)

    @property
    def world(self):
        """The answers and the fake's own writes (tests and the harness's plan-write fake read them)."""
        return self._world

    def __getattr__(self, name):
        if name.startswith("__") or name not in METHODS:
            raise AttributeError(f"'Garmin' object has no attribute {name!r}")

        def method(*args, **kwargs):
            try:
                return _full_value(self._serve(name, args, kwargs))
            except FaultSignal as signal:
                raise full_mode_exception(signal.group) from None

        method.__name__ = name
        return method

    def _serve(self, name, args, kwargs):
        gc = _garminconnect()
        if name == "connectapi":
            path = args[0] if args else kwargs.get("path")
            return self._world.connectapi(path, kwargs.get("params"))
        bound = inspect.signature(getattr(gc.Garmin, name)).bind(self, *args, **kwargs)
        bound.apply_defaults()
        params = dict(list(bound.arguments.items())[1:])
        _check(gc, name, params)
        if name == "query_garmin_graphql":
            return self._world.graphql(params["query"])
        if name == "get_workouts":
            return self._world.library(params["start"], params["limit"])
        if name == "get_activities":
            return self._world.activities(name, activity_type=params.get("activitytype"),
                                          start=params["start"], limit=params["limit"])
        if name == "get_activities_by_date":
            first = params.get("startdate")
            answer, wildcard = self._world.lookup(name, first)
            if wildcard is False:
                return decode_answer(answer)
            return self._world.activities(name, first, params.get("enddate") or first, params.get("activitytype"))
        values = list(params.values())
        first = values[0] if values else None
        second = values[1] if len(values) > 1 else None
        return self._world.answer(name, first, _day(first), _day(second) or _day(first))


def _check(gc, name, params):
    """garminconnect's own input checks for these arguments (same exceptions and texts)."""
    for key, value in params.items():
        if key in _DATE_PARAMS and value is not None:
            gc._validate_date_format(value, key)
    if name in _ID_METHODS:
        key = "workout_id" if name == "get_workout_by_id" else "activity_id"
        gc._validate_positive_integer(int(params[key]), key)
    if name == "get_activities":
        gc._validate_non_negative_integer(params["start"], "start")
        gc._validate_positive_integer(params["limit"], "limit")
        if params["limit"] > gc.MAX_ACTIVITY_LIMIT:
            raise ValueError(f"limit cannot exceed {gc.MAX_ACTIVITY_LIMIT}")


# --- key mode ----------------------------------------------------------------------------------------------

class HttpError(Exception):
    """Stands in for garmin_personal_api's HttpError: key mode reads only its class name and status_code."""

    def __init__(self, status_code):
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


KEY_MODE_STATUS = {"login": 401, "rate_limited": 429, "no_data": 404, "bad_input": 400}


def key_mode_exception(group):
    """What the Personal API client raises for a failure of `group` (an HTTP status, or requests' read timeout
    for unavailable); key mode maps it to its own exception."""
    if group == "internal":
        return RuntimeError("synthetic internal failure (eval fault)")
    if group == "unavailable":
        import requests

        return requests.exceptions.ReadTimeout("Read timed out (read timeout=30)")
    return HttpError(KEY_MODE_STATUS[group])


class _Token:
    def __init__(self, access_token, expires_at):
        self.access_token, self.expires_at = access_token, expires_at


class FakeAuthenticator:
    """Garmin's PersonalApiAuthenticator: a fresh 24-hour token for any key (no cache file written)."""

    def __init__(self, clock):
        self._clock = clock
        self.requests = 0

    def obtain_token(self, api_key):
        self.requests += 1
        # A fake JWT (alg none, sub eval), built from parts so no secret scanner reads it as a real token.
        token = ".".join(["eyJhbGciOiJub25lIn0", "eyJzdWIiOiJldmFsIn0", "ZXZhbC10b2tlbi0" + str(self.requests)])
        return _Token(token, self._clock() + 86400)


_SLEEP_PATH = "/sleep-service/sleep/dailySleep"
_SUMMARY_PATH = "/usersummary-service/usersummary/daily"
_BODY_BATTERY_PATH = "/wellness-service/wellness/bodyBattery/reports/daily"
_ACTIVITY_RE = re.compile(r"/activity-service/activity/(\d+)")
_HRV_RE = re.compile(r"/hrv-service/hrv/(\d{4}-\d{2}-\d{2})")
_WEIGHT_RE = re.compile(r"/weight-service/weight/range/(\d{4}-\d{2}-\d{2})/(\d{4}-\d{2}-\d{2})")


class FakePersonalApiHttp:
    """key_mode.KeyModeGarmin's HTTP layer (get(token, path, params), download(token, activity_id)): each
    Personal API path is answered from the same state as full mode, under the garminconnect method key mode
    stands in for. Faults come from RecordingClient through a per-thread slot."""

    def __init__(self, state, slot):
        self._world = World(state)
        self._slot = slot

    def _fault(self):
        group = getattr(self._slot, "group", None)
        if group is not None:
            raise key_mode_exception(group)

    def get(self, token, path, params):
        self._fault()
        try:
            return _key_value(self._get(path, params or {}))
        except FaultSignal as signal:
            raise key_mode_exception(signal.group) from None

    def download(self, token, activity_id):
        self._fault()
        try:
            return _key_value(self._world.answer("download_activity", activity_id))
        except FaultSignal as signal:
            raise key_mode_exception(signal.group) from None

    def _get(self, path, params):
        world = self._world
        if path == ACTIVITIES_PATH:
            return world.activities("connectapi", params.get("startDate"), params.get("endDate"),
                                    params.get("activityType"), params.get("start") or 0, params.get("limit"))
        m = _ACTIVITY_RE.fullmatch(path)
        if m:
            return world.answer("get_activity", m.group(1))
        if path == _SUMMARY_PATH:
            day = params.get("calendarDate")
            answer, wildcard = world.lookup("get_stats", day)
            value = world.answer("get_stats" if wildcard is not None else "get_user_summary", day)
            return None if value is NO_SUMMARY else value
        if path == _SLEEP_PATH:
            answer = world.answer("get_sleep_data", params.get("date"))
            return answer.get("dailySleepDTO") if isinstance(answer, dict) else None
        m = _HRV_RE.fullmatch(path)
        if m:
            return world.answer("get_hrv_data", m.group(1))
        if path == _BODY_BATTERY_PATH:
            start, end = params.get("startDate"), params.get("endDate")
            return world.answer("get_body_battery", start, start, end)
        m = _WEIGHT_RE.fullmatch(path)
        if m:
            return world.answer("get_weigh_ins", m.group(1))
        if path.startswith(METRICS_PATH + "/"):
            return world.connectapi(path)
        if path == HR_ZONES_PATH + "/v2":
            return world.connectapi(HR_ZONES_PATH)
        return NOT_FOUND


def _key_value(value):
    if value is NOT_FOUND:
        raise HttpError(404)
    if value is NO_SUMMARY:
        return None
    return value


# --- the call log and the faults (both modes) --------------------------------------------------------------

class CallLog:
    """What the fake was asked, and which faults apply. Shared by the server and the RecordingClient."""

    def __init__(self, faults, key_mode):
        self.faults = dict(faults or {})
        self.key_mode = key_mode
        # Faults on a Garmin write: (group, how many writes pass first), counted per write.
        self.write_faults = {name: parse_write_fault(self.faults.pop(name))
                             for name in WRITE_METHODS if name in self.faults}
        self.write_counts = {}
        self.calls = []
        self.missing = []
        self.tool = None
        self.slot = threading.local()
        self._lock = threading.Lock()

    def fault_for(self, method, args, kwargs):
        names = [method]
        if method.startswith("client."):
            names.append(method[len("client."):])
        if method in ACTIVITY_LIST_METHODS or (method == "connectapi" and args
                                               and "/" + str(args[0]).lstrip("/") == ACTIVITIES_PATH):
            names.append("get_activities_by_date")
        if self.tool:
            names.append(self.tool)
        for name in names:
            if name in self.faults:
                return self.faults[name]
        return None

    def record(self, method, args, kwargs):
        entry = {"tool": self.tool, "method": method, "args": json_safe(list(args)),
                 "kwargs": json_safe(kwargs)}
        with self._lock:
            self.calls.append(entry)

    def note_missing(self, method):
        with self._lock:
            self.missing.append({"tool": self.tool, "method": method})

    def invoke(self, method, fn, args, kwargs):
        self.record(method, args, kwargs)
        op = write_op(method, args, kwargs)
        if op in self.write_faults and not self.key_mode:
            with self._lock:
                self.write_counts[op] = self.write_counts.get(op, 0) + 1
                count = self.write_counts[op]
            group, after = self.write_faults[op]
            if count > after:
                if group == "read_timeout":
                    fn(*args, **kwargs)   # the write lands; its answer is lost
                raise write_exception(group)
        group = self.fault_for(method, args, kwargs)
        if group is None:
            return fn(*args, **kwargs)
        if not self.key_mode:
            raise full_mode_exception(group)
        self.slot.group = group
        try:
            return fn(*args, **kwargs)
        finally:
            self.slot.group = None


class RecordingClient:
    """Wraps the logged-in client (FakeGarmin, or key mode's KeyModeGarmin): every method call goes through
    CallLog.invoke(); plain attributes pass through; `client` (garth's client) is wrapped too, its calls named
    "client.<method>". A name the client lacks raises its own AttributeError, noted in CallLog.missing."""

    def __init__(self, inner, log, prefix=""):
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_log", log)
        object.__setattr__(self, "_prefix", prefix)

    def __repr__(self):
        return f"RecordingClient({self._inner!r})"

    def __getattr__(self, name):
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        full = self._prefix + name
        try:
            attr = getattr(self._inner, name)
        except AttributeError:
            self._log.note_missing(full)
            raise
        if name == "client" and not self._prefix and attr is not None and not callable(attr):
            return RecordingClient(attr, self._log, "client.")
        if not callable(attr):
            return attr
        log = self._log

        def invoke(*args, **kwargs):
            return log.invoke(full, attr, args, kwargs)

        invoke.__name__ = name
        return invoke
