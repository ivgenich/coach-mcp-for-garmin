"""Probe garmin_paths' method/path allowlist and the GraphQL read check with bypass attempts (prints only)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "ext"))
import garmin_paths as gp  # noqa: E402

gp.allow_plan_writes(set())
G = "graphql-gateway/graphql"
graphql = {
    "plain read": {"query": "query{workoutScheduleSummariesScalar(startDate:\"2026-12-01\", endDate:\"2026-12-02\")}"},
    "second op": {"query": "query{a} mutation{b}"},
    "mutation in block str then real": {"query": 'query{a(x:"""\\""" mutation{b} """)}'},
    "string hides close": {"query": 'query{a(x:"\\"") } mutation{b}'},
    "comment CR": {"query": "query{a} #x\rmutation{b}"},
    "BOM prefix": {"query": "﻿mutation{b}"},
    "operationName": {"query": "query A{a} query B{b}", "operationName": "B"},
    "extensions": {"query": "query{a}", "extensions": {"persistedQuery": {}}},
    "subscription": {"query": "query{a} subscription{b}"},
    "query-named mutation": {"query": "query mutation{a}"},
    "unicode ws": {"query": "query{a} mutation{b}"},
    "nbsp before keyword": {"query": "query{a} mutation{b}"},
    "comma": {"query": "query{a},mutation{b}"},
}
for name, body in graphql.items():
    print(f"graphql {name!r:36} -> {'ALLOWED' if gp.garmin_method_ok('POST', G, {'json': body}) else 'refused'}")
print("graphql via data= kw           ->", gp.garmin_method_ok("POST", G, {"data": "mutation{b}", "json": graphql["plain read"]}))
print("graphql leading slash          ->", gp.garmin_method_ok("POST", "/" + G, {"json": graphql["plain read"]}))

print("--- writes with the switch OFF")
for m, p in [("POST", "workout-service/workout"), ("DELETE", "workout-service/workout/1"), ("PUT", "x"),
             ("PATCH", "workout-service/workout/1")]:
    print(f"{m:6} {p:34} -> {gp.garmin_method_ok(m, p, {})}")
gp.allow_plan_writes(set(gp.WRITE_PATHS))
print("--- writes with the switch ON")
for m, p in [("POST", "workout-service/workout"), ("POST", "/workout-service/workout"),
             ("POST", "workout-service/workout?start=1"), ("DELETE", "workout-service/workout/1/"),
             ("DELETE", "workout-service/workout/01"), ("DELETE", "workout-service/workout/1%2F..%2Fx"),
             ("DELETE", "workout-service/schedule/1"), ("POST", "workout-service/schedule/1"),
             ("PUT", "workout-service/workout/1"), ("DELETE", "workout-service/workout/١"),
             ("post", "workout-service/workout")]:
    try:
        gp.check_garmin_request(m, p, {})
        verdict = "ALLOWED"
    except Exception as exc:  # noqa: BLE001
        verdict = type(exc).__name__
    print(f"{m:6} {p!r:42} -> {verdict}")
gp.allow_plan_writes(set())
