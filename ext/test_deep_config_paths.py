"""Deep tests (release test phase): the Garmin path guard, the method allowlist, the GraphQL read check and the
argument checks before Garmin (garmin_paths.py), fuzzed with hostile values, with and without the plan writes,
and in the history worker.

    cd ext && python -m unittest test_deep_config_paths

Standard library only, except the classes that drive launch's ReadOnlyFastMCP (upstream garmin_mcp on mcp 2.x)
and the one that asks requests which body it sends. A test marked expectedFailure exposes a defect (its
"# BUG:" line says which); it stays until the code is fixed.
"""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import os
import time
import unittest
from unittest import mock

import feature_config
import garmin_paths
import tool_errors

from test_launch_support import (captured_records, FULL, HAVE_UPSTREAM, load_launch, quiet, Stubs, tool_error_records,
                                 UpstreamCase)

HERE = os.path.dirname(os.path.abspath(__file__))
GRAPHQL = garmin_paths.GRAPHQL_PATH
HAVE_REQUESTS = importlib.util.find_spec("requests") is not None
ALL_KINDS = frozenset(garmin_paths.WRITE_PATHS)


def writes(test, kinds):
    """Admit `kinds` for the rest of the test; none again when it ends."""
    garmin_paths.allow_plan_writes(kinds)
    test.addCleanup(garmin_paths.allow_plan_writes, ())


def gql(query, **extra):
    return {"json": {"query": query, **extra}}


class PathGuardFuzz(unittest.TestCase):
    """garmin_path_ok() against traversal, encoding, separator, query, fragment, case, look-alike and length
    tricks. Accepted paths are the plain ones garminconnect builds."""

    ACCEPTED = (
        "usersummary-service/usersummary/daily/MyName",
        "/usersummary-service/usersummary/daily/MyName",          # one leading slash: garminconnect strips it
        "usersummary-service/usersummary/daily/My Name",          # a display name with a plain space
        "personalrecord-service/personalrecord/prs/\xcdvar",    # a non-ASCII letter
        "personalrecord-service/personalrecord/prs/E\u0301va",    # a combining accent
        "personalrecord-service/personalrecord/prs/\u0430dmin",   # Cyrillic a: a letter, cannot change structure
        "activity-service/activity/123/",                         # a trailing slash
        "activity-service/activity/.hidden",                      # a dot that is not a segment
        "biometric-service/biometric/latestLactateThreshold?sport=RUNNING",
        "wellness-service/wellness/dailyEvents?calendarDate=2026-11-01",
        "gear-service/gear/filterGear?userProfilePk=1&start=0&limit=10",
        "Activity-Service/Activity/1",                            # case is Garmin's business, not structure
        "a" * 20000,                                              # very long, still plain
    )
    REFUSED = (
        "", ".", "..", "./a", "a/.", "a/./b", "a/..", "a/../b", "../userprofile-service/x",
        "workout-service/fbt-adaptive/x-/../../../userprofile-service/userprofile",
        "a/..b", "a/b..", "a/...",                     # ".." anywhere is refused, segment or not
        "a/%2e%2e/b", "a/%2E%2E/b", "a%2Fb", "a%2fb", "a/%252e%252e", "%", "a/%00",
        "a//b", "//a", "a/b//", "///",
        "a#frag", "a/#", "#",
        "a\\b", "a\\..\\b",
        "a?x=1", "a?sport", "a?sport=", "a?sport=x&", "a?sport=x&y=1", "a?sport=x?y", "a?sport=x=y",
        "a?sport=x/../../b", "a?sport=" + "x" * 65, "a?Sport=x", "a?sport=x#y", "a?sport=x y",
        "a\tb", "a\nb", "a\rb", "a\x00b", "a\x7fb", "a\x0bb", "a\x0cb",
        "a\xa0b", "a\u2003b", "a\u3000b", "a\u2028b", "a\u2029b",           # unicode spaces and separators
        "a\uff0e\uff0e/b", "a\u2024\u2024/b", "a\uff0fb", "a\u2215b", "a\u2044b", "a\u29f8b",   # look-alikes
        "a\ufe52b", "a\u200bb", "a\u200eb", "a\u202eb", "a\ufeffb",          # small dot, invisibles, bidi, BOM
        "a\U0001f600b",                                                        # an emoji is no letter
        "a/" + "../" * 5000 + "b", "a" * 20000 + "/..",
    )

    def test_accepted(self):
        for path in self.ACCEPTED:
            with self.subTest(path=path[:60]):
                self.assertTrue(garmin_paths.garmin_path_ok(path))
                garmin_paths.check_garmin_path(path)

    def test_refused(self):
        for path in self.REFUSED:
            with self.subTest(path=repr(path[:60])):
                self.assertFalse(garmin_paths.garmin_path_ok(path))
                with self.assertRaises(garmin_paths.GarminPathRefused) as cm:
                    garmin_paths.check_garmin_path(path)
                # The refusal never quotes the path, which is caller data.
                self.assertEqual(str(cm.exception), garmin_paths.PATH_REFUSED_TEXT)
                self.assertTrue(str(cm.exception).startswith("Invalid input:"))

    def test_non_strings_are_refused_without_raising(self):
        for value in (None, b"a/b", 1, 1.5, ["a"], {"a": 1}, object(), True):
            with self.subTest(value=type(value).__name__):
                self.assertFalse(garmin_paths.garmin_path_ok(value))

    def test_refusal_is_the_bad_input_group_however_it_arrives(self):
        exc = garmin_paths.GarminPathRefused()
        self.assertEqual(tool_errors.classify_exception(exc)[0], tool_errors.BAD_INPUT)
        self.assertEqual(tool_errors.classify_text("Error retrieving x: " + str(exc))[0], tool_errors.BAD_INPUT)

    def test_long_hostile_paths_are_fast(self):
        started = time.monotonic()
        for path in ("a/" * 200000, "%" * 200000, "a?" + "&".join(["sport=x"] * 50000), "\xe9" * 200000):
            garmin_paths.garmin_path_ok(path)
        self.assertLess(time.monotonic() - started, 5)


class MethodAllowlistWritesOff(unittest.TestCase):
    """The default: GET and the GraphQL read query, nothing else, whatever the path or the spelling."""

    def setUp(self):
        writes(self, ())

    def test_get_in_any_case_passes(self):
        for verb in ("GET", "get", "Get", "gEt"):
            with self.subTest(verb=verb):
                self.assertTrue(garmin_paths.garmin_method_ok(verb, "activitylist-service/activities"))

    def test_every_other_verb_is_refused_on_every_path(self):
        paths = ("workout-service/workout", "workout-service/workout/1", "workout-service/schedule/1",
                 "/workout-service/workout", "activity-service/activity/1", GRAPHQL, "userprofile-service/x",
                 "upload-service/upload", "")
        verbs = ("POST", "post", "PUT", "put", "PATCH", "DELETE", "delete", "HEAD", "head", "OPTIONS", "TRACE",
                 "CONNECT", "PROPFIND", "GET ", " GET", "G\u0395T", "GET\n", "", None, b"GET", 0)
        for verb in verbs:
            for path in paths:
                if path == GRAPHQL and str(verb).upper() == "POST":
                    continue
                with self.subTest(verb=repr(verb), path=path):
                    self.assertFalse(garmin_paths.garmin_method_ok(verb, path, {}))

    def test_check_refuses_the_path_first_then_the_method(self):
        with self.assertRaises(garmin_paths.GarminPathRefused):
            garmin_paths.check_garmin_request("POST", "workout-service/../workout", {})
        with self.assertRaises(garmin_paths.GarminMethodRefused) as cm:
            garmin_paths.check_garmin_request("POST", "workout-service/workout", {"json": {"workoutName": "x"}})
        self.assertEqual(str(cm.exception), garmin_paths.METHOD_REFUSED_TEXT)
        self.assertNotIn("workout", str(cm.exception).split(":")[0] + "x")   # fixed text, never the path
        garmin_paths.check_garmin_request("GET", "workout-service/workout/1", {})

    def test_the_guarded_run_request_never_calls_garmin_for_a_refused_write(self):
        sent = []
        guarded = garmin_paths.path_guarded_run_request(lambda self, m, p, *a, **k: sent.append((m, p)) or "ok")
        for method, path in (("POST", "workout-service/workout"), ("DELETE", "workout-service/workout/1"),
                             ("DELETE", "workout-service/schedule/1"), ("POST", "workout-service/schedule/1"),
                             ("PUT", "workout-service/workout/1")):
            with self.subTest(method=method, path=path), self.assertRaises(garmin_paths.GarminMethodRefused):
                guarded(object(), method, path, json={})
        self.assertEqual(sent, [])
        self.assertEqual(guarded(object(), "GET", "workout-service/workout/1"), "ok")
        self.assertEqual(sent, [("GET", "workout-service/workout/1")])


class MethodAllowlistWritesOn(unittest.TestCase):
    """With PLAN_WRITES=on (every kind admitted): exactly the four workout-service write shapes."""

    def setUp(self):
        writes(self, ALL_KINDS)

    def test_the_four_writes_pass(self):
        for method, path in (("POST", "workout-service/workout"), ("post", "/workout-service/workout"),
                             ("POST", "workout-service/schedule/1"), ("DELETE", "workout-service/schedule/42"),
                             ("DELETE", "workout-service/workout/" + "9" * 19), ("delete", "workout-service/workout/7")):
            with self.subTest(method=method, path=path):
                self.assertTrue(garmin_paths.garmin_method_ok(method, path, {}))
                garmin_paths.check_garmin_request(method, path, {})

    def test_near_misses_are_refused(self):
        refused = (
            ("PUT", "workout-service/workout/1"), ("PATCH", "workout-service/workout/1"),
            ("POST", "workout-service/workout/1"), ("POST", "workout-service/workout/"),
            ("POST", "workout-service/workouts"), ("POST", "Workout-Service/Workout"),
            ("POST", "workout-service/workout?x=1"), ("POST", "workout-service/workout#x"),
            ("POST", "workout-service/workout\n"), ("POST", " workout-service/workout"),
            ("POST", "workout-service/workout/../workout"), ("POST", "workout-service//workout"),
            ("DELETE", "workout-service/workout"), ("DELETE", "workout-service/workout/0"),
            ("DELETE", "workout-service/workout/01"), ("DELETE", "workout-service/workout/-1"),
            ("DELETE", "workout-service/workout/+1"), ("DELETE", "workout-service/workout/1.0"),
            ("DELETE", "workout-service/workout/" + "9" * 20), ("DELETE", "workout-service/workout/\uff11"),
            ("DELETE", "workout-service/workout/\u0661"), ("DELETE", "workout-service/workout/1/"),
            ("DELETE", "workout-service/workout/1/x"), ("DELETE", "workout-service/workout/1?x=1"),
            ("DELETE", "workout-service/schedule"), ("DELETE", "workout-service/schedule/0"),
            ("POST", "workout-service/schedule"), ("POST", "workout-service/schedule/1/2"),
            ("DELETE", "userprofile-service/userprofile/1"), ("DELETE", "activity-service/activity/1"),
            ("POST", "upload-service/upload"), ("POST", "activity-service/activity"),
            ("HEAD", "workout-service/workout"), ("OPTIONS", "workout-service/workout"),
            ("DELETE", "workout-service/fbt-adaptive/1"), ("POST", "trainingplan-service/trainingplan"),
        )
        for method, path in refused:
            with self.subTest(method=method, path=repr(path)):
                self.assertFalse(garmin_paths.garmin_method_ok(method, path, {}))
                with self.assertRaises((garmin_paths.GarminMethodRefused, garmin_paths.GarminPathRefused)):
                    garmin_paths.check_garmin_request(method, path, {})

    def test_each_kind_admits_only_its_own_shape(self):
        shapes = {"create": ("POST", "workout-service/workout"), "schedule": ("POST", "workout-service/schedule/1"),
                  "unschedule": ("DELETE", "workout-service/schedule/1"),
                  "delete": ("DELETE", "workout-service/workout/1")}
        for kind in shapes:
            garmin_paths.allow_plan_writes({kind})
            for other, (method, path) in shapes.items():
                with self.subTest(kind=kind, other=other):
                    self.assertEqual(garmin_paths.garmin_method_ok(method, path, {}), other == kind)

    def test_cleanup_admits_only_the_two_deletes(self):
        garmin_paths.allow_plan_writes({"unschedule", "delete"})
        self.assertFalse(garmin_paths.garmin_method_ok("POST", "workout-service/workout", {}))
        self.assertFalse(garmin_paths.garmin_method_ok("POST", "workout-service/schedule/1", {}))
        self.assertTrue(garmin_paths.garmin_method_ok("DELETE", "workout-service/workout/1", {}))

    def test_an_unknown_kind_changes_nothing(self):
        for kinds in ({"update"}, {"create", "put"}, {""}, {"CREATE"}):
            with self.subTest(kinds=sorted(kinds)), self.assertRaises(ValueError):
                garmin_paths.allow_plan_writes(kinds)
            self.assertEqual(garmin_paths.allowed_write_kinds(), ALL_KINDS)

    def test_none_again_refuses_every_write(self):
        garmin_paths.allow_plan_writes(())
        self.assertEqual(garmin_paths.allowed_write_kinds(), frozenset())
        self.assertFalse(garmin_paths.garmin_method_ok("POST", "workout-service/workout", {}))


class GraphQLReadCheck(unittest.TestCase):
    """POST to the GraphQL gateway: one anonymous read query only. Run with the writes off and on: the writes
    never widen what the gateway admits."""

    READS = (
        "query { a }", "query{a}", "  \n\t query { a }", "query Q { a }", "query ($d: String) { a(d: $d) }",
        'query { a(s: "mutation { b }") }', 'query { a(s: "subscription") }',
        'query { a(s: """mutation { b }""") }', 'query { a(s: """x \\""" mutation""") }',
        "query { a } # mutation { b }", "query { a } # subscription\n", "# mutation\nquery { a }",
        "query { mutations mutationCount is_mutation subscriptionId }",
        'query { a(s: "\\"mutation\\"") }', 'query { a(s: "#") } # "',
        "query { a }\n" + "# x\n" * 1000, "query {" + " a" * 100000 + " }",
    )
    WRITES_OR_JUNK = (
        "mutation { b }", " mutation{b}", "query { a } mutation { b }", "query { a }\nmutation { b }",
        "query{a}\nsubscription{b}", "subscription { b }", 'query { a(s: "x") } mutation { b }',
        'query { a(s: "\\"") } mutation { b }', 'query { a(s: "\\\\") } mutation { b }',
        'query { a(s: "unterminated mutation { b }', 'query { a(s: """unterminated mutation { b }',
        'query { a(s: "x\nmutation { b }") }', 'query { a } # x "\nmutation { b } "',
        'query { a } # """\nmutation { b } """', 'query { a(s: "\\"\\"\\"") } mutation { b }',
        'query{a}"""x"""mutation{b}', "query { a }\u2028mutation { b }", "query{a},mutation{b}",
        "{ a }", "QUERY { a }", "Query { a }", "\ufeffquery { a }", ",query { a }", "", "   ", "q",
        "# query\nmutation { b }", "#\nmutation{b}", 'mutation { b } # query',
    )

    def check(self, body, kwargs=None):
        return garmin_paths.garmin_method_ok("POST", GRAPHQL, gql(body) if kwargs is None else kwargs)

    def test_reads_pass_and_writes_are_refused_with_the_writes_off_and_on(self):
        for kinds in ((), ALL_KINDS):
            writes(self, kinds)
            for body in self.READS:
                with self.subTest(kinds=sorted(kinds), read=body[:60]):
                    self.assertTrue(self.check(body))
            for body in self.WRITES_OR_JUNK:
                with self.subTest(kinds=sorted(kinds), refused=body[:60]):
                    self.assertFalse(self.check(body))

    def test_body_shape(self):
        refused = (
            {}, {"json": None}, {"json": "query { a }"}, {"json": b'{"query":"query{a}"}'},
            {"json": [{"query": "query { a }"}]}, {"json": [{"query": "query { a }"}, {"query": "mutation { b }"}]},
            {"json": {"query": None}}, {"json": {"query": 1}}, {"json": {"query": ["query { a }"]}},
            {"json": {}}, {"json": {"variables": {}}},
            gql("query { a }", operationName="Q"), gql("query Q { a } mutation M { b }", operationName="Q"),
            gql("query { a }", operationName=0), gql("query { a }", operationName=[]),
            gql("query { a }", extensions={"persistedQuery": {"sha256Hash": "x"}}), gql("query { a }", id="1"),
            gql("query { a }", Query="mutation { b }"), {"data": '{"query": "query { a }"}'},
            {"files": {"f": b"x"}},
        )
        for kwargs in refused:
            with self.subTest(kwargs=repr(kwargs)[:80]):
                self.assertFalse(garmin_paths.garmin_method_ok("POST", GRAPHQL, kwargs))
        for kwargs in (gql("query { a }", operationName=None), gql("query { a }", operationName=""),
                       gql("query ($x: Int) { a(x: $x) }", variables={"x": "mutation { b }"})):
            with self.subTest(kwargs=repr(kwargs)[:80]):
                self.assertTrue(garmin_paths.garmin_method_ok("POST", GRAPHQL, kwargs))
        self.assertFalse(garmin_paths.garmin_method_ok("POST", GRAPHQL, None))

    def test_other_verbs_and_paths_to_the_gateway(self):
        for method, path in (("PUT", GRAPHQL), ("DELETE", GRAPHQL), ("PATCH", GRAPHQL),
                             ("POST", GRAPHQL + "/"), ("POST", "graphql-gateway/graphql?x=1"),
                             ("POST", "Graphql-Gateway/graphql"), ("POST", "graphql-gateway/graphql/x"),
                             ("POST", "graphql-gateway"), ("POST", "web-gateway/graphql")):
            with self.subTest(method=method, path=path):
                self.assertFalse(garmin_paths.garmin_method_ok(method, path, gql("query { a }")))
        self.assertTrue(garmin_paths.garmin_method_ok("post", "/" + GRAPHQL, gql("query { a }")))

    def test_hostile_bodies_are_fast(self):
        bodies = ['"""' + "\\" * 50000, '"' + "\\" * 50000, '"""' + '""' * 50000, "#" * 100000,
                  '"""' + '\\"""' * 20000, '"' * 100000, "query{a}" + '"a' * 50000, "query{" + "{" * 100000]
        started = time.monotonic()
        for body in bodies:
            garmin_paths.graphql_read_ok({"query": body})
        self.assertLess(time.monotonic() - started, 5)

    def test_a_carriage_return_ends_a_comment(self):
        self.assertFalse(self.check("query { a } # x\rmutation { b }"))

    def test_a_carriage_return_comment_cannot_hide_a_leading_mutation(self):
        self.assertFalse(self.check("# x\rmutation { b }\nquery { a }"))

    def test_a_read_json_cannot_ride_with_a_data_body(self):
        self.assertFalse(garmin_paths.garmin_method_ok(
            "POST", GRAPHQL, {"json": {"query": "query { a }"}, "data": '{"query": "mutation { b }"}'}))


@unittest.skipUnless(HAVE_REQUESTS, "needs requests")
class WhatRequestsSends(unittest.TestCase):
    """The facts behind test_a_read_json_cannot_ride_with_a_data_body: requests (garminconnect's HTTP client)
    puts `data` on the wire, not `json`, when both are given."""

    def test_data_wins_over_json(self):
        import requests

        prepared = requests.Request("POST", "https://connectapi.garmin.com/" + GRAPHQL,
                                    json={"query": "query { a }"}, data='{"query": "mutation { b }"}').prepare()
        body = prepared.body if isinstance(prepared.body, str) else prepared.body.decode()
        self.assertIn("mutation", body)
        self.assertNotIn("query { a }", body)


class WorkerNeverWrites(unittest.TestCase):
    """The history worker and enqueue job never admit a Garmin write: no code of theirs calls
    allow_plan_writes(), launch.main() hands them off before it does, and PLAN_* on their services is a config
    error."""

    def test_no_worker_module_admits_writes(self):
        for name in ("worker.py", "worker_garmin.py", "enqueue.py", "history_jobs.py", "jobqueue.py",
                     "history_store.py", "history_extract.py", "feature_startup.py"):
            with self.subTest(module=name), open(os.path.join(HERE, name), encoding="utf-8") as f:
                self.assertNotIn("allow_plan_writes", f.read())

    def test_main_hands_the_worker_off_before_any_write_is_admitted(self):
        import base64
        import secrets

        writes(self, ())
        env = {**FULL, "ROLE": "worker", "HISTORY_SEARCH": "on", "REPORT_TZ": "UTC",
               "DATABASE_URL": "postgresql://u:TEST-ONLY@db.example.internal/x",
               "HISTORY_TOKEN_KEY": base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()}
        for role in ("worker", "enqueue"):
            launch = load_launch(self, Stubs().modules)
            seen = []

            def run_role(features):
                seen.append((features.role, garmin_paths.allowed_write_kinds(), features.plan_writes))
                return 0

            with self.subTest(role=role), mock.patch.dict(os.environ, {**env, "ROLE": role}, clear=True), \
                    mock.patch.object(launch, "_run_role", run_role), self.assertRaises(SystemExit) as cm:
                launch.main([])
            self.assertEqual(cm.exception.code, 0)
            self.assertEqual(seen, [(role, frozenset(), False)])
        # PLAN_WRITES (or PLAN_TOOLS) on a worker or enqueue service stops main() before anything starts.
        for role in ("worker", "enqueue"):
            for extra in ({"PLAN_WRITES": "on"}, {"PLAN_TOOLS": "on", "PLAN_WRITES": "on"}, {"PLAN_TOOLS": "on"}):
                launch = load_launch(self, Stubs().modules)
                ran = mock.Mock(return_value=0)
                with self.subTest(role=role, extra=sorted(extra)), \
                        mock.patch.dict(os.environ, {**env, "ROLE": role, **extra}, clear=True), \
                        mock.patch.object(launch, "_run_role", ran), mock.patch("sys.stderr"), \
                        self.assertRaises(SystemExit) as cm:
                    launch.main([])
                self.assertEqual(cm.exception.code, 1)
                ran.assert_not_called()
                self.assertEqual(garmin_paths.allowed_write_kinds(), frozenset())
                self.assertIn(feature_config.ERR_P6, feature_config.problems({**env, "ROLE": role, **extra}))

    def test_the_worker_installs_the_same_guard(self):
        import worker_garmin

        self.assertIn("garmin_paths.install_path_guard()", inspect.getsource(worker_garmin._garminconnect_factory))


# --- Argument checks before Garmin -------------------------------------------------------------------------

HEX32 = "0123456789abcdef" * 2
TOKEN_BODY = "A" * 10 + "z" * 10 + "0123456789" + "-_"
assert len(TOKEN_BODY) == 32
CHANGE = "c" + "0123456789abcdef"
# Hostile values every string argument refuses, whatever its rule.
HOSTILE = ("' OR 1=1 --", "1; DROP TABLE plans; --", "\x00", "a\x00", "x" * 100000, "\u202e", "../", "%2e%2e",
           "${jndi:ldap://x}", "<script>", "\n", " ", "\t", "\ufeff", None, [], {}, 1.5, object())

# tool -> argument -> (accepted values, refused values). Every argument of ARGUMENT_CHECKS must have a row here.
CASES = {
    "date": (("2026-11-01", "2024-02-29", "0001-01-01", "9999-12-31"),
             ("2026-02-29", "2026-02-30", "2026-13-01", "2026-00-10", "2026-11-00", "0000-01-01", "20261101",
              "2026-11-01T00:00", "2026-11-01 ", " 2026-11-01", "2026-11-01\n", "2026-1-01", "26-11-01",
              "\uff12\uff10\uff12\uff16-11-01", "\u0662\u0660\u0662\u0666-11-01", "2026/11/01", "2026-11-01/x",
              "2026-11-01?x", "2026-11-01#x", "", 20261101, True)),
    "plan_id": (("", HEX32), (HEX32.upper(), HEX32[:31], HEX32 + "0", "g" * 32, HEX32[:31] + "\n", " " + HEX32[:31],
                             "0x" + HEX32[:30], 1, 0, False, HEX32[:31] + "\u0430")),
    "course_id": (("", "0", "1", "123456789012345", 0, 1, 10 ** 15 - 1),
                  ("00", "01", "-1", "+1", "1.0", "1e3", "1234567890123456", "0x1f", " 1", "1 ", "\uff11", "\u0661",
                   10 ** 15, -1, True, False, 1.0, "1/../../x")),
    "accept_changes": (("", CHANGE, CHANGE + "," + CHANGE, CHANGE + ";" + CHANGE, " " + CHANGE + " , " + CHANGE,
                        ",".join([CHANGE] * 20)),
                       (",", " , ", ";", "c" + "0" * 15, "c" + "0" * 17, "C" + "0" * 16, "c" + "G" * 16,
                        "c" + "A" * 16, ",".join([CHANGE] * 21), CHANGE + ",x", CHANGE + "\x00", 1)),
    "change_ids": ((CHANGE, CHANGE + "," + CHANGE, ",".join([CHANGE] * 20)),
                   ("", " ", ",", ",".join([CHANGE] * 21), "c" + "0" * 15, CHANGE.upper(), 1)),
    "commit_token": (("ct1." + TOKEN_BODY,),
                     ("xt1." + TOKEN_BODY, "at1." + TOKEN_BODY, "CT1." + TOKEN_BODY, "ct2." + TOKEN_BODY,
                      "ct1." + TOKEN_BODY[:31], "ct1." + TOKEN_BODY + "A", "ct1." + TOKEN_BODY[:31] + "=",
                      "ct1." + TOKEN_BODY[:31] + "\n", "ct1" + TOKEN_BODY, "ct1." + TOKEN_BODY[:31] + "\u0430", "")),
    "cancel_token": (("", "xt1." + TOKEN_BODY),
                     ("ct1." + TOKEN_BODY, "at1." + TOKEN_BODY, "xt1." + TOKEN_BODY[:31], "xt1." + TOKEN_BODY + "x",
                      "xt1." + TOKEN_BODY[:31] + "+", "XT1." + TOKEN_BODY)),
    "apply_token": (("at1." + TOKEN_BODY,),
                    ("", "ct1." + TOKEN_BODY, "xt1." + TOKEN_BODY, "at1." + TOKEN_BODY[:31], "at1." + TOKEN_BODY[:31] + ".")),
    "workout_key": (("20261206-run", "20270226-race", "20270129-b", "20261130-run"),
                    ("w03-long", "2026126-run", "202612060-run", "20261206-", "20261206-long", "20261206-RUN",
                     "20261206_run", "20261206-ru n", "20261206-run\n", "20261206-run~0123abcd", "", "x0261206-run",
                     "2026-12-06-run", 20261206)),
    "rpe": ((1, 5, 10), (0, 11, -1, True, False, 5.0, "5", None, 10 ** 30)),
    "pain": ((0, 5, 10), (-1, 11, True, 0.0, "0", None)),
    "pain_location": (("", "none", "knee", "lower_back", "other"),
                      ("Knee", "knee ", " knee", "lower back", "lower-back", "kne", "elbow", "\u043a", 1, None)),
    "sharp": ((True, False), (0, 1, "true", "false", None, "")),
    "week_start": (("", "2026-11-02"), ("2026-02-30", "2026-11-02T00:00", "x", 0)),
}
COLUMN = {"race_date": "date", "start_date": "date", "end_date": "date"}
PLAN_TOOL_NAMES = frozenset({"plan_assess", "plan_preview", "plan_get", "log_feedback", "plan_commit", "plan_cancel",
                             "plan_review_week", "plan_apply_changes"})


def cases_for(argument):
    return CASES[COLUMN.get(argument, argument)]


class ArgumentChecks(unittest.TestCase):
    """Every argument of every tool in ARGUMENT_CHECKS: boundary values pass, malicious ones are refused, and
    the check never raises."""

    def test_every_checked_argument_has_cases(self):
        for tool, checks in garmin_paths.ARGUMENT_CHECKS.items():
            for argument in checks:
                with self.subTest(tool=tool, argument=argument):
                    self.assertIn(COLUMN.get(argument, argument), set(CASES) | {"workout_id"})

    def test_every_plan_tool_has_a_row(self):
        launch = load_launch(self, Stubs().modules)
        plan_tools = (launch.PLAN_READ_TOOLS | launch.PLAN_REVIEW_TOOLS | launch.OWN_WRITE_TOOLS)
        self.assertEqual(plan_tools, PLAN_TOOL_NAMES)
        for tool in plan_tools:
            with self.subTest(tool=tool):
                self.assertIn(tool, garmin_paths.ARGUMENT_CHECKS)

    def test_boundaries_and_hostile_values(self):
        for tool, checks in garmin_paths.ARGUMENT_CHECKS.items():
            if tool in ("get_workout_by_id",):
                continue
            for argument, (check, detail) in checks.items():
                good, bad = cases_for(argument)
                for value in good:
                    with self.subTest(tool=tool, argument=argument, good=repr(value)[:40]):
                        self.assertTrue(check(value))
                        self.assertIsNone(garmin_paths.refused_argument(tool, {argument: value}))
                for value in tuple(bad) + tuple(v for v in HOSTILE if not (argument == "sharp" and v is None)):
                    if argument in ("cancel_token", "plan_id", "accept_changes", "pain_location", "week_start") \
                            and value == "":
                        continue
                    if argument == "accept_changes" and isinstance(value, str) and not value.strip():
                        continue   # optional: blank means no changes accepted
                    with self.subTest(tool=tool, argument=argument, bad=repr(value)[:40]):
                        self.assertFalse(check(value))
                        self.assertEqual(garmin_paths.refused_argument(tool, {argument: value}), (argument, detail))

    def test_workout_ids(self):
        check = garmin_paths.ARGUMENT_CHECKS["get_workout_by_id"]["workout_id"][0]
        for value in (1, 10 ** 19 - 1, "1", "9" * 19, "01234567-89ab-cdef-0123-456789ABCDEF"):
            with self.subTest(good=value):
                self.assertTrue(check(value))
        for value in (0, -1, 10 ** 19, True, "0", "01", "9" * 20, "x-/../../../userprofile-service", "-",
                      "01234567-89ab-cdef-0123-456789abcdeg", "01234567-89ab-cdef-0123-456789abcdef\n",
                      "{01234567-89ab-cdef-0123-456789abcdef}", 1.0) + HOSTILE:
            with self.subTest(bad=repr(value)[:40]):
                self.assertFalse(check(value))

    def test_the_first_bad_argument_in_table_order_is_named(self):
        bad = {"plan_id": "X", "commit_token": "X"}
        self.assertEqual(garmin_paths.refused_argument("plan_commit", bad)[0], "plan_id")
        self.assertIsNone(garmin_paths.refused_argument("plan_commit", {}))                 # absent: the SDK's job
        self.assertIsNone(garmin_paths.refused_argument("plan_commit", None))
        self.assertIsNone(garmin_paths.refused_argument("plan_commit", [("plan_id", "X")]))
        self.assertIsNone(garmin_paths.refused_argument("no_such_tool", {"plan_id": "X"}))
        self.assertIsNone(garmin_paths.refused_argument("plan_commit", {"unrelated": "' OR 1=1"}))

    def test_the_rules_agree_with_the_store(self):
        import plan_store

        self.assertEqual(plan_store.TOKEN_RE.pattern, garmin_paths._TOKEN_RE.pattern)
        self.assertEqual(plan_store.CHANGE_ID_RE.pattern, garmin_paths._CHANGE_ID_RE.pattern)
        self.assertEqual(set(plan_store.TOKEN_PREFIXES.values()), {"ct1", "xt1", "at1"})
        for action, check in (("commit", "commit_token"), ("cancel", "cancel_token"), ("apply", "apply_token")):
            minted = plan_store.new_token(action)
            with self.subTest(action=action):
                self.assertTrue(cases_check(check)(minted))
                for other in {"commit_token", "cancel_token", "apply_token"} - {check}:
                    self.assertFalse(cases_check(other)(minted))
        # Every session key the argument check admits is one the store admits, and the shapes agree.
        self.assertEqual(plan_store.SESSION_KEY_RE.pattern, garmin_paths._WORKOUT_KEY_RE.pattern)
        for key in CASES["workout_key"][0]:
            self.assertTrue(plan_store.key_ok(key, retired=False))


def cases_check(argument):
    for checks in garmin_paths.ARGUMENT_CHECKS.values():
        if argument in checks:
            return checks[argument][0]
    raise KeyError(argument)


MARK = "TEST-ONLY-MARK-7f3a"
HOSTILE_MARKED = ("' OR 1=1 -- " + MARK, MARK + "\x00", MARK * 2000, "../" + MARK, MARK + "\u202e", MARK + "\n")


@unittest.skipUnless(HAVE_UPSTREAM, "needs upstream garmin_mcp on mcp 2.x")
class RefusedBeforeTheCall(UpstreamCase):
    """Through launch's ReadOnlyFastMCP: a hostile value of any checked argument is answered bad input before
    the call guard counts a call and before the tool (its Garmin or database call) runs, and the one log line
    names the argument, never its value."""

    def setUp(self):
        super().setUp()
        from mcp.server.mcpserver import MCPServer

        self.launch = load_launch(self, Stubs().modules)
        self.ran = []
        self.guard = mock.Mock()
        self.launch._GUARD = self.guard
        names = frozenset(garmin_paths.ARGUMENT_CHECKS)
        self.server = self.launch.read_only_server_class(MCPServer, names)("deep")
        for name in names:
            self.server.add_tool(self.stub(name, sorted(garmin_paths.ARGUMENT_CHECKS[name])), name=name)

    def stub(self, name, arguments):
        namespace = {"Any": object, "ran": self.ran}
        params = ", ".join(f"{a}: Any = None" for a in arguments)
        exec(f"def {name}({params}) -> str:\n    ran.append({name!r})\n    return '{{}}'", namespace)  # noqa: S102
        return namespace[name]

    def call(self, name, arguments):
        return asyncio.run(self.server.call_tool(name, arguments))

    def test_hostile_values_never_reach_the_tool_the_guard_or_the_log(self):
        quiet(self, "mcp")
        for tool, checks in garmin_paths.ARGUMENT_CHECKS.items():
            for argument in checks:
                for value in HOSTILE_MARKED:
                    with self.subTest(tool=tool, argument=argument, value=repr(value)[:30]), \
                            captured_records() as records:
                        result = self.call(tool, {argument: value})
                        self.assertTrue(result.is_error)
                        text = result.content[0].text
                        self.assertTrue(text.startswith("Error: Invalid input"), text[:80])
                        self.assertNotIn(MARK, text)
                        lines = tool_error_records(records)
                        self.assertEqual(len(lines), 1)
                        self.assertIn(f"argument {argument} refused before Garmin", lines[0].getMessage())
                        for record in records:
                            self.assertNotIn(MARK, record.getMessage())
                            self.assertNotIn(MARK, repr(record.args))
        self.assertEqual(self.ran, [])
        self.guard.before_call.assert_not_called()
        self.guard.live_call_started.assert_not_called()

    def test_a_good_value_reaches_the_tool(self):
        quiet(self, "mcp")
        result = self.call("plan_get", {"plan_id": HEX32})
        self.assertFalse(result.is_error)
        self.assertEqual(self.ran, ["plan_get"])
        result = self.call("plan_commit", {"plan_id": "", "commit_token": "ct1." + TOKEN_BODY})
        self.assertFalse(result.is_error)
        self.assertEqual(self.ran, ["plan_get", "plan_commit"])

    def test_a_check_that_breaks_refuses(self):
        quiet(self, "mcp")

        def broken(value):
            raise RuntimeError(MARK)

        with mock.patch.dict(garmin_paths.ARGUMENT_CHECKS, {"plan_get": {"plan_id": (broken, "x")}}), \
                captured_records() as records:
            result = self.call("plan_get", {"plan_id": HEX32})
        self.assertTrue(result.is_error)
        self.assertEqual(self.ran, [])
        self.assertFalse([r for r in records if MARK in r.getMessage()])

    def test_an_unknown_tool_name_never_reaches_the_log_with_its_value(self):
        quiet(self, "mcp")
        with captured_records() as records:
            result = self.call("no_such_tool", {"plan_id": MARK})
        self.assertTrue(result.is_error)
        self.assertNotIn(MARK, "\n".join(r.getMessage() for r in records))
