"""Which Garmin client calls change Garmin data: shared by the fake Garmin (evals/harness/fake_garmin.py, whose
write faults are keyed by these names) and the grading (evals/grading/checks.py, `garmin_writes_none`).

A write reaches Garmin either through garminconnect's methods (upload_workout, schedule_workout,
unschedule_workout, delete_workout) or through the raw client the plan executor uses
(garmin_client.client.post/put/delete with a workout-service path). write_op() names both the same way.
A POST to the GraphQL gateway whose query starts with "query" is a read (the calendar), never a write.
Standard library only.
"""

from __future__ import annotations

import re

# The garminconnect.Garmin write methods the plan executor may call, by the name a fault or a check uses.
WRITE_METHODS = ("upload_workout", "schedule_workout", "unschedule_workout", "delete_workout")
RAW_METHODS = ("client.post", "client.put", "client.delete", "client.patch")
GRAPHQL_PATH = "graphql-gateway/graphql"

_ROUTES = (
    ("POST", re.compile(r"workout-service/workout/?"), "upload_workout"),
    ("POST", re.compile(r"workout-service/schedule/\d+"), "schedule_workout"),
    ("DELETE", re.compile(r"workout-service/schedule/\d+"), "unschedule_workout"),
    ("DELETE", re.compile(r"workout-service/workout/\d+"), "delete_workout"),
)


def _path(args, kwargs):
    """The path argument of client.post/put/delete(domain, path, ...) as a bare string ('' when absent)."""
    args = list(args or ())
    path = args[1] if len(args) > 1 else (kwargs or {}).get("path", "")
    return str(path or "").lstrip("/").split("?", 1)[0]


def write_op(method, args=(), kwargs=None):
    """The write a client call makes ("upload_workout", ...), "write" for any other non-GET raw call that is
    not a GraphQL read, or None for a read. `method` is the name the call log records ("upload_workout",
    "client.post", "connectapi", ...)."""
    if method in WRITE_METHODS:
        return method
    if method not in RAW_METHODS:
        return None
    verb = method.split(".", 1)[1].upper()
    path = _path(args, kwargs)
    for want, rx, name in _ROUTES:
        if verb == want and rx.fullmatch(path):
            return name
    if verb == "POST" and path == GRAPHQL_PATH:
        body = (kwargs or {}).get("json")
        query = body.get("query") if isinstance(body, dict) else None
        if isinstance(query, str) and query.lstrip().startswith("query"):
            return None
    return "write"


def write_calls(calls):
    """The entries of a Garmin call log ({"method", "args", "kwargs", ...}) that change Garmin data."""
    out = []
    for c in calls or ():
        if isinstance(c, dict) and write_op(c.get("method"), c.get("args") or (), c.get("kwargs") or {}):
            out.append(c)
    return out


__all__ = ["WRITE_METHODS", "RAW_METHODS", "write_op", "write_calls"]
