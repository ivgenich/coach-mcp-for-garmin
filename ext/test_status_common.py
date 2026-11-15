"""Tests for status_common. Standard library only:

    python3 -m unittest discover -s ext -v
"""

import asyncio
import datetime as dt
import os
import threading
import time
import unittest
from unittest import mock
from zoneinfo import ZoneInfo

import status_common as sc

TZ = ZoneInfo("Europe/Athens")  # tests set their own zone


class TimeHelpers(unittest.TestCase):
    def test_parse_date(self):
        self.assertEqual(sc.parse_date("2026-11-10", TZ), dt.date(2026, 11, 10))
        self.assertEqual(sc.parse_date("", TZ), sc.today_local(TZ))
        self.assertEqual(sc.parse_date(None, TZ), sc.today_local(TZ))
        with self.assertRaises(ValueError):
            sc.parse_date("10/11/2026", TZ)

    def test_english_forms(self):
        d = dt.date(2026, 11, 10)
        self.assertEqual(sc.en_date(d), "10 Nov")
        self.assertEqual(sc.en_date(dt.date(2027, 1, 3)), "3 Jan")
        self.assertEqual(sc.en_weekday(d), "Tuesday")
        self.assertEqual(sc.en_weekday(d, short=True), "Tue")
        self.assertEqual(sc.en_weekday(dt.date(2026, 11, 15)), "Sunday")

    def test_isolate_wraps_only_right_to_left_names(self):
        heb = "\u05d0\u05d9\u05e0\u05d8\u05e8\u05d5\u05d5\u05dc\u05d9\u05dd 5x1000"
        self.assertEqual(sc.isolate(heb), "\u2068" + heb + "\u2069")
        self.assertEqual(sc.isolate("Easy run 5x1000"), "Easy run 5x1000")
        self.assertIsNone(sc.isolate(None))
        self.assertEqual(sc.isolate(""), "")

    def test_check_report_tz(self):
        # REPORT_TZ is required: no default zone, and launch.py --check-config gets the reason.
        self.assertEqual(sc.check_report_tz({"REPORT_TZ": "Europe/London"}), [])
        self.assertEqual(sc.check_report_tz({"REPORT_TZ": "UTC"}), [])
        self.assertEqual(sc.check_report_tz({"REPORT_TZ": " Europe/London "}), [])  # spaces dropped
        for env in ({}, {"REPORT_TZ": ""}, {"REPORT_TZ": "   "}):
            with self.subTest(env=env):
                (err,) = sc.check_report_tz(env)
                self.assertIn("REPORT_TZ is not set", err)
                self.assertIn("Europe/London", err)  # says what a value looks like
        for bad in ("Mars/Base", "Europe", "../etc/passwd", "/etc/localtime", "zone.tab", "x" * 500):
            with self.subTest(bad=bad[:20]):
                (err,) = sc.check_report_tz({"REPORT_TZ": bad})
                self.assertTrue(err.startswith("REPORT_TZ: unknown time zone"), err)
                self.assertLess(len(err), 200)  # a long bad value is cut
        # launch.py --check-config passes os.environ itself.
        with mock.patch.dict(os.environ, {"REPORT_TZ": "Europe/Athens"}):
            self.assertEqual(sc.check_report_tz(os.environ), [])
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(len(sc.check_report_tz(os.environ)), 1)
        self.assertFalse(hasattr(sc, "DEFAULT_TZ"))  # no baked-in zone

    def test_zone_requires_report_tz(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "REPORT_TZ is not set"):
                sc.tz_name()
            with self.assertRaisesRegex(ValueError, "REPORT_TZ is not set"):
                sc.zone()
            self.assertEqual(sc.zone("Europe/Athens").key, "Europe/Athens")  # an explicit zone needs no env; gitleaks:allow (a zone name, not a key)
        with mock.patch.dict(os.environ, {"REPORT_TZ": " Europe/London "}):
            self.assertEqual((sc.tz_name(), sc.zone().key), ("Europe/London", "Europe/London"))
        with mock.patch.dict(os.environ, {"REPORT_TZ": "Mars/Base"}):
            with self.assertRaisesRegex(ValueError, "^REPORT_TZ: unknown time zone 'Mars/Base'"):
                sc.zone()
        for bad in ("Mars/Base", "../x", 5):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, "^unknown time zone"):
                sc.zone(bad)

    def test_hours_to_hmm(self):
        self.assertEqual(sc.hours_to_hmm(6.85), "6:51")
        self.assertEqual(sc.hours_to_hmm(7), "7:00")
        self.assertEqual(sc.hours_to_hmm(0.999), "1:00")
        self.assertIsNone(sc.hours_to_hmm(None))
        self.assertIsNone(sc.hours_to_hmm("4"))


class Payloads(unittest.TestCase):
    def test_num_and_rnd(self):
        self.assertIsNone(sc.num(True))
        self.assertIsNone(sc.num("5"))
        self.assertIsNone(sc.num(float("nan")))
        self.assertEqual(sc.rnd(4.6), 5)
        self.assertIsInstance(sc.rnd(4.6), int)
        self.assertEqual(sc.rnd(6.85, 1), round(6.85, 1))
        self.assertIsNone(sc.rnd(None, 1))

    def test_acwr_ratio_from_loads(self):
        # Garmin cuts its ratio to one decimal: 500/720 = 0.694 arrives as 0.6.
        self.assertEqual(sc.acwr_ratio(500, 720, 0.6), 0.69)
        # Just over the yellow threshold must not read as 1.3.
        self.assertEqual(sc.acwr_ratio(1390, 1000, 1.3), 1.39)
        # Fallback to Garmin's ratio only when a load is missing or chronic is 0.
        self.assertEqual(sc.acwr_ratio(None, 720, 0.6), 0.6)
        self.assertEqual(sc.acwr_ratio(500, 0, 1.2), 1.2)
        self.assertEqual(sc.acwr_ratio(500, None, None), None)
        self.assertIsNone(sc.acwr_ratio(-1, 720, -0.5))
        self.assertIsNone(sc.acwr_ratio(True, 720, "x"))

    def test_dig(self):
        d = {"a": {"b": [{"c": 1}, None]}, "n": None}
        self.assertEqual(sc.dig(d, "a", "b", 0, "c"), 1)
        self.assertIsNone(sc.dig(d, "a", "b", 1, "c"))
        self.assertIsNone(sc.dig(d, "a", "x"))
        self.assertEqual(sc.dig(d, "n", default=7), 7)
        self.assertEqual(sc.dig(d, "a", "b", 5, default="miss"), "miss")
        self.assertIsNone(sc.dig("not a dict", "a"))

    def test_key_paths_has_no_values(self):
        paths = sc.key_paths({"dailySleepDTO": {"sleepTimeSeconds": 24660, "id": 99}, "list": [{"x": 1}]})
        self.assertIn("dailySleepDTO.sleepTimeSeconds", paths)
        self.assertIn("list[].x", paths)
        self.assertNotIn("24660", " ".join(paths))

    def test_sport_group(self):
        for key, group in [
            ("running", "run"), ("treadmill_running", "run"), ("virtual_run", "run"),
            ("virtual_ride", "bike"), ("road_biking", "bike"), ("indoor_cycling", "bike"),
            ("cycling", "bike"), ("lap_swimming", "swim"), ("open_water_swimming", "swim"),
            ("strength_training", "other"), (None, "other"),
        ]:
            self.assertEqual(sc.sport_group(key), group, key)

    def test_match_completions(self):
        scheduled = [
            {"date": "2026-03-16", "sport": "run", "activity_id": None},   # matched by date+sport
            {"date": "2026-03-16", "sport": "run", "activity_id": None},   # second run that day: second activity
            {"date": "2026-03-17", "sport": "bike", "activity_id": 501},   # linked by Garmin
            {"date": "2026-03-18", "sport": "swim", "activity_id": None},  # no swim that day
            {"date": "2026-03-17", "sport": "bike", "activity_id": None},  # only bike that day is the linked one
        ]
        activities = [
            {"date": "2026-03-16", "sport": "run", "id": 101},
            {"date": "2026-03-16", "sport": "run", "id": 102},
            {"date": "2026-03-17", "sport": "bike", "id": 501},
            {"date": "2026-03-18", "sport": "run", "id": 103},
        ]
        self.assertEqual(sc.match_completions(scheduled, activities), [True, True, True, False, False])
        self.assertEqual(sc.match_completions([], activities), [])
        self.assertEqual(sc.match_completions(scheduled[:1], []), [False])

    def test_match_completion_shifts_moved_sessions(self):
        # A week where sessions moved days: one run done on Fri for two planned
        # runs (Wed, Sat: the nearer plan, Sat, gets it), a ride planned Thu
        # done Tue, the Saturday ride done on its day, a swim planned Thu done Wed.
        scheduled = [
            {"date": "2026-03-18", "sport": "run", "activity_id": None},
            {"date": "2026-03-19", "sport": "swim", "activity_id": None},
            {"date": "2026-03-19", "sport": "bike", "activity_id": None},
            {"date": "2026-03-21", "sport": "run", "activity_id": None},
            {"date": "2026-03-21", "sport": "bike", "activity_id": None},
        ]
        activities = [
            {"date": "2026-03-17", "sport": "bike", "id": 1},
            {"date": "2026-03-18", "sport": "swim", "id": 2},
            {"date": "2026-03-20", "sport": "run", "id": 3},
            {"date": "2026-03-21", "sport": "bike", "id": 4},
        ]
        self.assertEqual(sc.match_completion_shifts(scheduled, activities, 0), [None, None, None, None, 0])
        self.assertEqual(sc.match_completion_shifts(scheduled, activities, 6), [None, 1, 2, 1, 0])
        # Same-day matches win before moved ones: the Saturday ride keeps its
        # own activity even though Thursday's ride is listed first.
        self.assertEqual(sc.match_completion_shifts(scheduled, activities, 1), [None, 1, None, 1, 0])
        self.assertEqual(sc.match_completions(scheduled, activities), [False, False, False, False, True])

    def test_match_completion_shifts_edge_cases(self):
        # Activities without ids are still used once each; bad dates never match.
        scheduled = [{"date": "2026-03-18", "sport": "run", "activity_id": None}] * 2
        activities = [{"date": "2026-03-18", "sport": "run", "id": None}, {"date": "bad", "sport": "run", "id": None}]
        self.assertEqual(sc.match_completion_shifts(scheduled, activities, 6), [0, None])
        linked = [{"date": "2026-03-18", "sport": "run", "activity_id": 9}, {"date": "2026-03-18", "sport": "run", "activity_id": None}]
        self.assertEqual(sc.match_completion_shifts(linked, [{"date": "2026-03-18", "sport": "run", "id": 9}], 6), [0, None])

    def test_compact_json_keeps_hebrew(self):
        word = "\u05e9\u05d9\u05e0\u05d4"  # a Hebrew word, written as escapes
        self.assertEqual(sc.compact_json({"a": word}), '{"a":"' + word + '"}')


class Fetching(unittest.TestCase):
    def test_fetch_sync_success_and_failure(self):
        ok = sc.fetch_sync(lambda x: x * 2, 21)
        self.assertTrue(ok["ok"])
        self.assertEqual(ok["data"], 42)

        def boom():
            raise RuntimeError("404 for url: https://connectapi.garmin.com/x/MyName?date=1")

        bad = sc.fetch_sync(boom)
        self.assertFalse(bad["ok"])
        self.assertNotIn("MyName", bad["error"])
        self.assertIn("<url>", bad["error"])
        self.assertIn("ms", bad)

        def echo_path():
            raise RuntimeError('API client error (404): {"path": "/usersummary-service/usersummary/daily/MyName"}')

        echoed = sc.fetch_sync(echo_path)
        self.assertNotIn("MyName", echoed["error"])
        self.assertIn("<path>", echoed["error"])
        self.assertIn("404", echoed["error"])

    def test_fetch_all_parallel_first_and_timeout(self):
        order = []
        lock = threading.Lock()

        def rec(name, delay=0.0):
            time.sleep(delay)
            with lock:
                order.append(name)
            return name

        def hang():
            time.sleep(2)
            return "late"

        calls = {
            "stats": sc.call(rec, "stats"),
            "a": sc.call(rec, "a", 0.05),
            "b": sc.call(rec, "b", 0.05),
            "slow": sc.call(hang),
            "err": sc.call(lambda: 1 / 0),
        }
        results = asyncio.run(sc.fetch_all(calls, first="stats", timeout_s=0.5, concurrency=4))
        self.assertEqual(order[0], "stats")
        self.assertEqual(sc.data_of(results["a"]), "a")
        self.assertFalse(results["slow"]["ok"])
        self.assertIn("timeout", results["slow"]["error"])
        self.assertFalse(results["err"]["ok"])
        self.assertIsNone(sc.data_of(results["err"]))
        meta = sc.source_meta(results)
        self.assertEqual(set(meta), set(calls))
        self.assertNotIn("data", meta["a"])
        self.assertIn("error", meta["err"])

    def test_systemic_first_failure_skips_the_rest(self):
        seen = []

        def auth_fail():
            raise RuntimeError("Garmin authentication failed: 401. Re-run 'garmin-mcp-auth'")

        calls = {"stats": sc.call(auth_fail), "a": sc.call(seen.append, "a"), "b": sc.call(seen.append, "b")}
        results = asyncio.run(sc.fetch_all(calls, first="stats"))
        self.assertEqual(seen, [])
        self.assertTrue(results["a"]["error"].startswith("skipped:"))
        self.assertEqual(set(results), {"stats", "a", "b"})

    def test_non_systemic_first_failure_runs_the_rest(self):
        calls = {"stats": sc.call(lambda: {}["missing"]), "a": sc.call(lambda: "a")}
        results = asyncio.run(sc.fetch_all(calls, first="stats"))
        self.assertEqual(sc.data_of(results["a"]), "a")

    def test_deadline_skips_unstarted_calls(self):
        def slow():
            time.sleep(0.4)
            return 1

        calls = {f"c{i}": sc.call(slow) for i in range(4)}
        started = time.monotonic()
        results = asyncio.run(sc.fetch_all(calls, concurrency=1, timeout_s=5, deadline_s=0.6))
        self.assertLess(time.monotonic() - started, 1.5)
        errors = [r.get("error", "") for r in results.values() if not r["ok"]]
        self.assertTrue(any(e.startswith("skipped:") or e.startswith("timeout") for e in errors))
        self.assertEqual(len(results), 4)

    def test_is_systemic_error(self):
        self.assertTrue(sc.is_systemic_error("Garmin rate limit hit: 429"))
        self.assertTrue(sc.is_systemic_error("TimeoutError: Garmin request 'get_stats' did not return within 90s"))
        self.assertFalse(sc.is_systemic_error("KeyError: 'dailySleepDTO'"))

    # The exact wording upstream's proxy gives garminconnect's errors.
    HINT = "Garmin Connect may be unreachable; check your network connection or try again later."

    def test_data_errors_are_not_systemic(self):
        empty = f"GarminConnectConnectionError: Garmin Connect request failed: No data received from server. {self.HINT}"
        missing = f"GarminConnectConnectionError: Garmin Connect request failed: API client error (404): 404 Client Error. {self.HINT}"
        self.assertFalse(sc.is_systemic_error(empty))
        self.assertFalse(sc.is_systemic_error(missing))

    def test_network_errors_are_systemic(self):
        dns = (
            "GarminConnectConnectionError: Garmin Connect request failed: Connection error: "
            "HTTPSConnectionPool(host='connectapi.garmin.com', port=443): Max retries exceeded with url: /x "
            f"(Caused by NameResolutionError('Failed to resolve')). {self.HINT}"
        )
        self.assertTrue(sc.is_systemic_error(dns))
        self.assertTrue(sc.is_systemic_error(
            "GarminConnectAuthenticationError: Garmin authentication failed: Authentication failed: 401. "
            "Re-run 'garmin-mcp-auth' to refresh your tokens and restart the server."
        ))

    def test_empty_first_call_does_not_skip_the_rest(self):
        hint = self.HINT

        class GarminConnectConnectionError(Exception):
            pass

        def empty_day():
            raise GarminConnectConnectionError(f"Garmin Connect request failed: No data received from server. {hint}")

        calls = {"stats": sc.call(empty_day), "a": sc.call(lambda: "a")}
        results = asyncio.run(sc.fetch_all(calls, first="stats"))
        self.assertFalse(results["stats"]["ok"])
        self.assertEqual(sc.data_of(results["a"]), "a")

    def test_systemic_judged_past_the_truncation(self):
        def network_down():
            raise ConnectionError("x" * 300 + " Max retries exceeded with url")

        res = sc.fetch_sync(network_down)
        self.assertLessEqual(len(res["error"]), 200)
        self.assertTrue(res["systemic"])
        self.assertNotIn("systemic", sc.source_meta({"s": res})["s"])


if __name__ == "__main__":
    unittest.main()
