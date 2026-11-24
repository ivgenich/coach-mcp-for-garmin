"""Tests for status_common. Standard library only:

    python3 -m unittest discover -s ext -v
"""

import asyncio
import datetime as dt
import logging
import os
import re
import secrets
import threading
import time
import unittest
from unittest import mock
from zoneinfo import ZoneInfo

import status_common as sc
import tool_errors

TZ = ZoneInfo("Europe/Athens")  # tests set their own zone
STATUS_LOG = logging.getLogger("garmin_ext.status")
_saved_log = None


def setUpModule():
    # fetch_all logs every failed source. Keep the test output clean; assertLogs still sees them.
    global _saved_log
    _saved_log = (STATUS_LOG.handlers[:], STATUS_LOG.propagate)
    STATUS_LOG.handlers, STATUS_LOG.propagate = [logging.NullHandler()], False


def tearDownModule():
    STATUS_LOG.handlers, STATUS_LOG.propagate = _saved_log


def garmin_error(name, message, module="garminconnect.exceptions"):
    """An exception of a class from garminconnect's module: the error types the tool-error table reads."""
    return type(name, (Exception,), {"__module__": module})(message)


def raiser(exc):
    def fail():
        raise exc
    return fail


INTERNAL_RE = re.compile(r"^internal error \(ref [0-9a-f]{8}\)$")
# The exact wording upstream's proxy gives garminconnect's errors.
HINT = "Garmin Connect may be unreachable; check your network connection or try again later."
PENDING_LOGIN = "Garmin login failed. Run 'garmin-mcp-auth' to authenticate, then restart the server."
STALL = ("Garmin request 'get_stats' did not return within 90s and was abandoned; raise GARMIN_MCP_CALL_TIMEOUT "
         "if Garmin is slow")


class TimeHelpers(unittest.TestCase):
    def test_parse_date(self):
        self.assertEqual(sc.parse_date("2026-11-10", TZ), dt.date(2026, 11, 10))
        self.assertEqual(sc.parse_date("", TZ), sc.today_local(TZ))
        self.assertEqual(sc.parse_date(None, TZ), sc.today_local(TZ))
        with self.assertRaises(ValueError):
            sc.parse_date("10/11/2026", TZ)

    def test_report_dates_stay_a_year_inside_the_calendar(self):
        # The reports read days around their date, and date arithmetic past year 1 or 9999 raises
        # OverflowError (an internal error); such a date is a bad argument with the date detail instead.
        self.assertEqual(sc.parse_date("0002-01-01", TZ), dt.date(2, 1, 1))
        self.assertEqual(sc.parse_date("9998-12-31", TZ), dt.date(9998, 12, 31))
        for value in ("0001-01-01", "0001-12-31", "9999-01-01", "9999-12-31"):
            with self.subTest(value=value), self.assertRaises(sc.BadArgument) as ctx:
                sc.parse_date(value, TZ)
            self.assertEqual(str(ctx.exception),
                             f"date must be YYYY-MM-DD between 0002-01-01 and 9998-12-31, got '{value}'")
            for group_detail in (tool_errors.classify_exception(ctx.exception)[:2],
                                 tool_errors.classify_text(f"Error: {ctx.exception}")):
                self.assertEqual(group_detail, (tool_errors.BAD_INPUT, tool_errors.DATE_DETAIL))
        with self.assertRaises(sc.BadArgument) as ctx:
            sc.report_date(dt.date.max, "end_date", "9999-12-31")
        self.assertTrue(str(ctx.exception).startswith("end_date must be YYYY-MM-DD between "), str(ctx.exception))
        self.assertEqual(tool_errors.classify_text(f"Error: {ctx.exception}"),
                         (tool_errors.BAD_INPUT, tool_errors.DATE_DETAIL))
        self.assertEqual(sc.report_date(dt.date(2026, 11, 10), "end_date", "2026-11-10"), dt.date(2026, 11, 10))

    def test_argument_errors_are_bad_argument(self):
        # The tools answer only BadArgument (and SystemicFailure) as text; it stays a ValueError.
        for cls in (sc.BadArgument, sc.NoData):
            self.assertTrue(issubclass(cls, ValueError) and issubclass(cls, tool_errors.KnownFailure), cls)
        self.assertFalse(issubclass(sc.SystemicFailure, ValueError))
        self.assertTrue(issubclass(sc.SystemicFailure, tool_errors.KnownFailure))
        with self.assertRaises(sc.BadArgument) as ctx:
            sc.parse_date("10/11/2026", TZ)
        self.assertEqual(tool_errors.classify_exception(ctx.exception)[:2],
                         (tool_errors.BAD_INPUT, tool_errors.DATE_DETAIL))
        with self.assertRaises(sc.BadArgument):
            sc.zone("Mars/Base")
        with mock.patch.dict(os.environ, {}, clear=True), self.assertRaises(sc.BadArgument) as ctx:
            sc.zone()
        # The REPORT_TZ texts group as internal: the server owner fixes them, not the caller.
        self.assertEqual(tool_errors.classify_exception(ctx.exception)[0], tool_errors.INTERNAL)

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
        # U+FEFF (a byte order mark) is not right-to-left; Arabic presentation forms still are.
        self.assertEqual(sc.isolate("\ufeffEasy"), "\ufeffEasy")
        self.assertEqual(sc.isolate("\ufefbx"), "\u2068\ufefbx\u2069")

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


class ReportText(unittest.TestCase):
    """The text helpers both status reports share: one behaviour each."""

    def test_num_txt(self):
        cases = ((3, "3"), (3.0, "3"), (1.5, "1.5"), (1.23456, "1.23"), (0.105, "0.1"), (-2.5, "-2.5"),
                 (-0.001, "0"), (None, "no data"), ("3", "no data"), (True, "no data"), (float("nan"), "no data"))
        for value, text in cases:
            with self.subTest(value=value):
                self.assertEqual(sc.num_txt(value), text)

    def test_md_escape_includes_the_tilde(self):
        self.assertEqual(sc.md_escape("~~fast~~ *x* [a](b) <i> #1 a|b `c` _d_ \\"),
                         "\\~\\~fast\\~\\~ \\*x\\* \\[a\\](b) \\<i\\> \\#1 a\\|b \\`c\\` \\_d\\_ \\\\")

    def test_word_count_reads_bold_as_a_space(self):
        self.assertEqual(sc.word_count("**Sleep:**7 h - **x**"), 5)
        self.assertEqual(sc.word_count(""), 0)

    def test_text_format(self):
        for value in ("", " ", None, "\t"):
            self.assertEqual(sc.text_format(value), "")
        for value in ("markdown", " Markdown ", "MARKDOWN\n"):
            self.assertEqual(sc.text_format(value), "markdown")
        for value in ("html", 5, "markdown2"):
            with self.subTest(value=value), self.assertRaises(sc.BadArgument) as ctx:
                sc.text_format(value)
            self.assertTrue(str(ctx.exception).startswith("text_format must be '' or 'markdown', got "))
            self.assertEqual(tool_errors.classify_exception(ctx.exception)[0], tool_errors.BAD_INPUT)

    def test_local_now(self):
        zone = sc.zone("Europe/Berlin")
        naive = dt.datetime(2026, 7, 1, 8, 0)
        self.assertEqual(sc.local_now(zone, naive), naive.replace(tzinfo=zone))
        utc = dt.datetime(2026, 7, 1, 6, 0, tzinfo=dt.timezone.utc)
        self.assertEqual(sc.local_now(zone, utc).hour, 8)
        self.assertEqual(sc.local_now(zone).tzinfo, zone)

    def test_client_method_and_activities_call(self):
        class Client:
            not_callable = 3

            def connectapi(self, path, **kwargs):
                return path, kwargs

        self.assertEqual(sc.client_method(Client(), "connectapi")("p", params={"a": 1}), ("p", {"params": {"a": 1}}))
        with self.assertRaises(AttributeError):
            sc.client_method(Client(), "not_callable")()
        with self.assertRaises(AttributeError):
            sc.client_method(Client(), "missing")()
        # No activity path on the client: garminconnect's.
        self.assertEqual(sc.activities_call(Client())(params={}), (sc.ACTIVITIES_PATH, {"params": {}}))
        Client.garmin_connect_activities = "/own/path"
        self.assertEqual(sc.activities_call(Client())()[0], "/own/path")


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
            ("mountain_biking", "bike"), ("e_bike_mountain", "bike"), ("e_bike_fitness", "bike"),
            ("gravel_cycling", "bike"), ("virtual_run", "run"),
            # R10b: motor sports are not rides.
            ("motorcycling", "other"), ("motorcycling_v2", "other"), ("Motorcycling", "other"),
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
        self.assertNotIn("_failure", ok)

        # A RuntimeError is not a Garmin error type: an internal error with a ref, no exception text.
        bad = sc.fetch_sync(raiser(RuntimeError("404 for url: https://connectapi.garmin.com/x/MyName?date=1")))
        self.assertFalse(bad["ok"])
        self.assertRegex(bad["error"], INTERNAL_RE)
        self.assertNotIn("MyName", bad["error"])
        self.assertNotIn("garmin.com", bad["error"])
        self.assertIn("ms", bad)

        echoed = sc.fetch_sync(raiser(garmin_error(
            "GarminConnectConnectionError",
            'API client error (404): {"path": "/usersummary-service/usersummary/daily/MyName"}')))
        self.assertEqual(echoed["error"], "no data from Garmin")  # the source phrase: no name, no path
        self.assertFalse(echoed["systemic"])

    def test_fetch_sync_error_is_the_source_phrase(self):
        # One sample per group: the error is the source phrase, never the exception text.
        samples = (
            (RuntimeError(PENDING_LOGIN), "Garmin login missing or expired", True),
            (RuntimeError("Garmin login did not finish within 90s. Run 'garmin-mcp-auth' to verify your credentials, "
                          "then restart the server."), "Garmin login missing or expired", True),
            (garmin_error("GarminConnectTooManyRequestsError", "Garmin rate limit hit: Too many requests (429)."),
             "Garmin rate limit", True),
            (garmin_error("GarminConnectConnectionError", "Garmin Connect request failed: API Error 400 - Bad Request"),
             "invalid input", False),
            (garmin_error("GarminConnectConnectionError",
                          f"Garmin Connect request failed: No data received from server. {HINT}"),
             "no data from Garmin", False),
            (TimeoutError(STALL), "Garmin Connect did not answer", True),
            (KeyError("dailySleepDTO"), INTERNAL_RE, False),
        )
        for exc, phrase, systemic in samples:
            with self.subTest(exc=type(exc).__name__, phrase=str(phrase)):
                res = sc.fetch_sync(raiser(exc))
                if isinstance(phrase, str):
                    self.assertEqual(res["error"], phrase)
                else:
                    self.assertRegex(res["error"], phrase)
                self.assertIs(res["systemic"], systemic)
                group, ref, full, original = res["_failure"]  # kept on the direct result, for fetch_all's log
                self.assertIs(original, exc)
                self.assertIn(str(exc.args[0]), full)
                if group == tool_errors.INTERNAL:
                    self.assertEqual(res["error"], f"internal error (ref {ref})")
                else:
                    self.assertIsNone(ref)

    def test_systemic_follows_the_group(self):
        # Only login, rate limit and a network-wide "unavailable" end a report; a 404 never does, even when
        # its body holds "429" or "timeout", and a bug's text cannot make it systemic.
        not_found_json = ("Garmin Connect request failed: API Error 404 - {'error': 'Not Found', "
                          f"'traceId': '7c4291e0b5aa3f10'}}. {HINT}")
        not_found_html = ("Garmin Connect request failed: API Error 404 - <html><body><h1>404</h1>request 4290 "
                          f"timeout</body></html>. {HINT}")
        dns = ("Garmin Connect request failed: Connection error: HTTPSConnectionPool(host='connectapi.garmin.com', "
               "port=443): Max retries exceeded with url: /x (Caused by NameResolutionError('Failed to resolve')). "
               f"{HINT}")
        cases = (
            (garmin_error("GarminConnectConnectionError", not_found_json), "no data from Garmin", False),
            (garmin_error("GarminConnectConnectionError", not_found_html), "no data from Garmin", False),
            (garmin_error("GarminConnectAuthenticationError", "Garmin authentication failed: API Error 401 - "
                          "Unauthorized. Re-run 'garmin-mcp-auth' to refresh your tokens."),
             "Garmin login missing or expired", True),
            (garmin_error("GarminConnectTooManyRequestsError", "Garmin rate limit hit: API Error 429 - Too Many "
                          "Requests. Wait a few minutes."), "Garmin rate limit", True),
            (TimeoutError(STALL), "Garmin Connect did not answer", True),
            (garmin_error("GarminConnectConnectionError", dns), "Garmin Connect did not answer", True),
            (ConnectionRefusedError(61, "Connection refused"), "Garmin Connect did not answer", True),
            (garmin_error("GarminConnectConnectionError",
                          f"Garmin Connect request failed: API client error (403): Forbidden. {HINT}"),
             "Garmin Connect did not answer", True),
            # One endpoint answering 5xx is not a network-wide failure: the rest of the report still runs.
            (garmin_error("GarminConnectConnectionError",
                          f"Garmin Connect request failed: API Error 503 - Service Unavailable. {HINT}"),
             "Garmin Connect did not answer", False),
            (RuntimeError("timed out"), INTERNAL_RE, False),
            (KeyError("rate limit"), INTERNAL_RE, False),
        )
        for exc, phrase, systemic in cases:
            with self.subTest(exc=str(exc)[:60]):
                res = sc.fetch_sync(raiser(exc))
                if isinstance(phrase, str):
                    self.assertEqual(res["error"], phrase)
                else:
                    self.assertRegex(res["error"], phrase)
                self.assertIs(res["systemic"], systemic)
                results = asyncio.run(sc.fetch_all({"first": sc.call(raiser(exc)), "b": sc.call(lambda: "b")},
                                                   first="first"))
                self.assertEqual(sc.data_of(results["b"]), None if systemic else "b")
                self.assertEqual(sc.systemic_failure(results, "first"), phrase if systemic else None)

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

    def test_skip_never_calls_and_says_why(self):
        # Key mode: skipped names get the fixed reason and are never called.
        seen = []
        calls = {"stats": sc.call(seen.append, "stats"), "a": sc.call(seen.append, "a"),
                 "b": sc.call(seen.append, "b"), "c": sc.call(seen.append, "c")}
        results = asyncio.run(sc.fetch_all(calls, first="stats", skip=frozenset({"b"})))
        self.assertEqual(sorted(seen), ["a", "c", "stats"])
        self.assertEqual(list(results), ["stats", "a", "b", "c"])
        self.assertEqual(results["b"]["error"], "skipped: not available with a Garmin Personal API key")
        self.assertEqual((results["b"]["ok"], results["b"]["ms"]), (False, 0))
        self.assertEqual(set(sc.source_meta({"b": results["b"]})["b"]), {"ok", "fetched_at", "ms", "error"})
        # Without `first` too, and a list works as well as a frozenset.
        seen.clear()
        results = asyncio.run(sc.fetch_all(calls, skip=["a", "stats"]))
        self.assertEqual(sorted(seen), ["b", "c"])
        self.assertEqual(results["stats"]["error"], "skipped: " + sc.NOT_WITH_KEY)

    def test_systemic_first_failure_keeps_the_skip_reasons(self):
        seen = []

        def auth_fail():
            raise RuntimeError("Garmin authentication failed: 401. Re-run 'garmin-mcp-auth'")

        calls = {"stats": sc.call(auth_fail), "a": sc.call(seen.append, "a"), "b": sc.call(seen.append, "b")}
        results = asyncio.run(sc.fetch_all(calls, first="stats", skip={"b"}))
        self.assertEqual(seen, [])
        self.assertEqual(results["b"]["error"], "skipped: " + sc.NOT_WITH_KEY)
        self.assertTrue(results["a"]["error"].startswith("skipped: stats failed: "), results["a"])
        self.assertEqual(sc.systemic_failure(results, "stats"), "Garmin login missing or expired")

    def test_first_in_skip_is_a_programming_error(self):
        seen = []
        with self.assertRaises(ValueError):
            asyncio.run(sc.fetch_all({"stats": sc.call(seen.append, 1)}, first="stats", skip={"stats"}))
        self.assertEqual(seen, [])

    def test_key_mode_texts(self):
        self.assertEqual(sc.NOT_WITH_KEY, "not available with a Garmin Personal API key")
        note = sc.KEY_MODE_NOTE
        self.assertTrue(note.isascii() and note.isprintable())
        self.assertEqual(len(note), 181)
        self.assertTrue(note.startswith("This server reads Garmin through a Personal API key, so "))

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

    def test_failed_sources_keep_their_group_and_feed_the_capture(self):
        rate_limited = garmin_error("GarminConnectTooManyRequestsError", "Garmin rate limit hit: API Error 429")
        no_data = garmin_error("GarminConnectConnectionError", "No data received from server")
        calls = {"first": sc.call(lambda: "ok"), "rl": sc.call(raiser(rate_limited)), "nd": sc.call(raiser(no_data))}
        token, groups = sc.capture_outcomes()
        try:
            results = asyncio.run(sc.fetch_all(calls, first="first"))
        finally:
            sc.end_capture(token)
        self.assertEqual(results["rl"]["group"], tool_errors.RATE_LIMITED)
        self.assertEqual(results["nd"]["group"], tool_errors.NO_DATA)
        self.assertNotIn("group", results["first"])
        self.assertEqual(groups, {tool_errors.RATE_LIMITED})  # only the groups that start a cooldown
        self.assertEqual(sc.guard_group(groups), tool_errors.RATE_LIMITED)
        self.assertNotIn("group", sc.source_meta(results)["rl"])  # the answer's "sources" do not change
        # Without a capture nothing is collected and nothing fails.
        self.assertFalse(asyncio.run(sc.fetch_all(calls, first="first"))["rl"]["ok"])

    def test_guard_group_puts_a_rate_limit_before_a_login(self):
        self.assertIsNone(sc.guard_group([]))
        self.assertIsNone(sc.guard_group([tool_errors.UNAVAILABLE, None]))
        self.assertEqual(sc.guard_group([tool_errors.LOGIN]), tool_errors.LOGIN)
        self.assertEqual(sc.guard_group([tool_errors.LOGIN, tool_errors.RATE_LIMITED]), tool_errors.RATE_LIMITED)

    def test_concurrent_reports_share_one_process_limit(self):
        active, peak = [0], [0]
        lock = threading.Lock()

        def work():
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            time.sleep(0.05)
            with lock:
                active[0] -= 1
            return 1

        async def two_reports():
            calls = {f"c{i}": sc.call(work) for i in range(4)}
            return await asyncio.gather(sc.fetch_all(calls), sc.fetch_all(calls))

        with mock.patch.object(sc, "PROCESS_CONCURRENCY", 3):
            first, second = asyncio.run(two_reports())
        self.assertTrue(all(r["ok"] for r in [*first.values(), *second.values()]))
        self.assertEqual(peak[0], 3)  # 8 calls, each report allowing 6, at most 3 at once together
        self.assertEqual(sc.PROCESS_CONCURRENCY, sc.DEFAULT_CONCURRENCY)

    def test_is_systemic_error(self):
        self.assertTrue(sc.is_systemic_error("Garmin rate limit hit: 429"))
        self.assertTrue(sc.is_systemic_error("TimeoutError: Garmin request 'get_stats' did not return within 90s"))
        self.assertFalse(sc.is_systemic_error("KeyError: 'dailySleepDTO'"))
        for text in ("API Error 401 - Unauthorized", "API Error 403 - Forbidden", "Not authenticated",
                     "API client error (401)", "API client error (403): Forbidden", "API Error 429",
                     "Garmin returned 429", "API client error (429)"):
            self.assertTrue(sc.is_systemic_error(text), text)
        # A bare 429 inside an id or a trace id is not a rate limit.
        for text in ("API Error 404 - Not Found", "No data received from server", "activity id 4291 not found",
                     "API Error 404 - {'error': 'Not Found', 'traceId': '7c4291e0b5aa3f10'}", "API Error 500"):
            self.assertFalse(sc.is_systemic_error(text), text)

    def test_systemic_failure_names_the_reason(self):
        login = sc.fetch_sync(raiser(RuntimeError(PENDING_LOGIN)))
        self.assertEqual(sc.systemic_failure({"stats": login}, "stats"), "Garmin login missing or expired")
        own_timeout = {"ok": False, "error": "timeout after 30s", "systemic": True}
        self.assertEqual(sc.systemic_failure({"stats": own_timeout}, "stats"), "timeout after 30s")
        empty = sc.fetch_sync(raiser(garmin_error("GarminConnectConnectionError", "No data received from server")))
        for results in ({"stats": sc.fetch_sync(lambda: 1)}, {"stats": empty}, {}, {"other": login}, None,
                        # Only a login, rate-limit or unavailable phrase (or our timeout) ends a report.
                        {"stats": {"ok": False, "error": "internal error (ref 0123abcd)", "systemic": True}},
                        {"stats": {"ok": False, "error": "no data from Garmin", "systemic": True}},
                        {"stats": {"ok": False, "error": "Garmin rate limit", "systemic": False}}):
            with self.subTest(results=results):
                self.assertIsNone(sc.systemic_failure(results, "stats"))

    def test_systemic_failure_text_keeps_its_group(self):
        # The tools answer "Error: <phrase>"; the tool-error wrapper must read each phrase back to its own group.
        for phrase, group in (("Garmin login missing or expired", tool_errors.LOGIN),
                              ("Garmin rate limit", tool_errors.RATE_LIMITED),
                              ("Garmin Connect did not answer", tool_errors.UNAVAILABLE),
                              ("timeout after 30s", tool_errors.UNAVAILABLE)):
            with self.subTest(phrase=phrase):
                exc = sc.SystemicFailure(phrase)
                self.assertEqual(str(exc), phrase)
                self.assertEqual(tool_errors.classify_exception(exc)[0], group)
                self.assertEqual(tool_errors.classify_text(f"Error: {exc}")[0], group)

    def test_fetch_all_logs_each_failed_source(self):
        calls = {
            "login": sc.call(raiser(RuntimeError(PENDING_LOGIN))),
            "rate": sc.call(raiser(garmin_error("GarminConnectTooManyRequestsError", "Too many requests (429)"))),
            "down": sc.call(raiser(garmin_error("GarminConnectConnectionError", "API Error 503 - unavailable"))),
            "bad": sc.call(raiser(garmin_error("GarminConnectConnectionError", "API Error 400 - Bad Request"))),
            "empty": sc.call(raiser(garmin_error("GarminConnectConnectionError", "No data received from server"))),
            "bug": sc.call(raiser(KeyError("dailySleepDTO"))),
            "slow": sc.call(time.sleep, 0.6),
            "fine": sc.call(lambda: "ok"),
        }
        with self.assertLogs("garmin_ext.status", level="DEBUG") as logs:
            results = asyncio.run(sc.fetch_all(calls, timeout_s=0.3, concurrency=8))
        self.assertTrue(all("_failure" not in res for res in results.values()))
        by_source = {}
        for record in logs.records:
            message = record.getMessage()
            self.assertTrue(message.startswith("status source failed: "), message)
            source = re.search(r"\bsource=(\w+)", message).group(1)
            self.assertNotIn(source, by_source, "one line per failed call")
            by_source[source] = record
        self.assertEqual(set(by_source), set(calls) - {"fine"})
        levels = {"login": logging.WARNING, "rate": logging.WARNING, "down": logging.WARNING, "slow": logging.WARNING,
                  "bad": logging.INFO, "empty": logging.DEBUG, "bug": logging.ERROR}
        self.assertEqual({source: record.levelno for source, record in by_source.items()}, levels)
        self.assertEqual(by_source["rate"].getMessage(), "status source failed: source=rate group=rate_limited: "
                         "GarminConnectTooManyRequestsError: Too many requests (429)")
        # fetch_all's own timeout: group unavailable, the "timeout after Ns" text, no traceback.
        self.assertEqual(by_source["slow"].getMessage(),
                         "status source failed: source=slow group=unavailable: timeout after 0s")
        self.assertEqual(results["slow"]["error"], "timeout after 0s")
        # An internal error logs its ref (the one in the source's error) and its traceback.
        bug = by_source["bug"]
        ref = re.match(r"internal error \(ref ([0-9a-f]{8})\)$", results["bug"]["error"]).group(1)
        self.assertEqual(bug.getMessage(),
                         f"status source failed: ref={ref} source=bug group=internal: KeyError: 'dailySleepDTO'")
        self.assertIsNotNone(bug.exc_info)
        self.assertIsInstance(bug.exc_info[1], KeyError)
        for source in set(levels) - {"bug"}:
            self.assertFalse(by_source[source].exc_info, source)

    def test_fetch_all_log_lines_are_redacted(self):
        # The last field of a "status source failed:" line is tool_errors.redact(original text):
        # one line, no token, no email, never the literal "rate limit:". Secrets are built at run time.
        token = "TEST-ONLY-" + secrets.token_urlsafe(32)
        bearer = "TEST-ONLY-" + secrets.token_urlsafe(32)
        email = "runner" + "@" + "example.invalid"
        text = f"API Error 503 - access_token={token} for {email}, Authorization: Bearer {bearer}\nRate limit: x"
        calls = {
            "garmin": sc.call(raiser(garmin_error("GarminConnectConnectionError", text))),  # a Garmin error type
            "bug": sc.call(raiser(KeyError(text))),  # internal; its str() is the repr of the text
            "crash": sc.call(raiser(RuntimeError(text))),  # internal, with a real line break
        }
        with self.assertLogs("garmin_ext.status", level="DEBUG") as logs:
            results = asyncio.run(sc.fetch_all(calls))
        self.assertTrue(all(not res["ok"] for res in results.values()))
        by_source = {re.search(r"\bsource=(\w+)", r.getMessage()).group(1): r for r in logs.records}
        self.assertEqual(set(by_source), set(calls))
        self.assertEqual(by_source["garmin"].levelno, logging.WARNING)  # the rate-limit row, from the Garmin type
        for source in ("bug", "crash"):
            self.assertEqual(by_source[source].levelno, logging.ERROR, source)
            self.assertIn("group=internal: ", by_source[source].getMessage())
        for source, record in by_source.items():
            message = record.getMessage()  # the line itself; a traceback (internal only) is not redacted
            with self.subTest(source=source):
                for secret in (token, bearer, email, "\n", "\r", "Rate limit:", "rate limit:"):
                    self.assertNotIn(secret, message)
                self.assertIn("access_token=<redacted>", message)
                self.assertIn("Bearer <redacted>", message)
                self.assertIn("<email>", message)
                self.assertIn("rate limit - x", message)

    def test_unusable_answer_is_a_failed_source_with_one_log_line(self):
        # An error inside a normal answer (GraphQL: HTTP 200 with "data": null) gets a source phrase and a log
        # line like any failed call; the text logged is the caller's fixed text.
        res = sc.fetch_sync(lambda: {"data": None, "errors": [{"message": "boom"}]})
        with self.assertLogs("garmin_ext.status", level="DEBUG") as logs:
            failed = sc.unusable_answer(res, "schedule", "GraphQL answer without data")
        self.assertTrue(res["ok"])  # the fetch result itself is not changed
        self.assertEqual(sc.source_meta({"schedule": failed})["schedule"],
                         {"ok": False, "fetched_at": res["fetched_at"], "ms": res["ms"],
                          "error": "Garmin Connect did not answer"})
        self.assertIsNone(sc.data_of(failed))
        self.assertIsNone(sc.systemic_failure({"schedule": failed}, "schedule"))  # never ends a report
        self.assertEqual([(r.levelno, r.getMessage()) for r in logs.records],
                         [(logging.WARNING,
                           "status source failed: source=schedule group=unavailable: GraphQL answer without data")])

    def test_skipped_calls_are_not_logged(self):
        seen = []
        calls = {"stats": sc.call(raiser(RuntimeError(PENDING_LOGIN))), "a": sc.call(seen.append, "a"),
                 "b": sc.call(seen.append, "b")}
        with self.assertLogs("garmin_ext.status", level="DEBUG") as logs:
            results = asyncio.run(sc.fetch_all(calls, first="stats"))
        self.assertEqual(seen, [])
        self.assertEqual(results["a"]["error"], "skipped: stats failed: Garmin login missing or expired")
        self.assertEqual([r.getMessage() for r in logs.records],
                         ["status source failed: source=stats group=login: RuntimeError: " + PENDING_LOGIN])
        # A call the report deadline never started is skipped, and not logged either.
        with self.assertNoLogs("garmin_ext.status", level="DEBUG"):
            results = asyncio.run(sc.fetch_all({"x": sc.call(lambda: 1)}, deadline_s=0))
        self.assertEqual(results["x"]["error"], "skipped: report deadline reached")

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
        empty_day = garmin_error("GarminConnectConnectionError",
                                 f"Garmin Connect request failed: No data received from server. {self.HINT}")
        calls = {"stats": sc.call(raiser(empty_day)), "a": sc.call(lambda: "a")}
        results = asyncio.run(sc.fetch_all(calls, first="stats"))
        self.assertFalse(results["stats"]["ok"])
        self.assertEqual(results["stats"]["error"], "no data from Garmin")
        self.assertEqual(sc.data_of(results["a"]), "a")

    def test_systemic_judged_past_the_truncation(self):
        def network_down():
            raise ConnectionError("x" * 300 + " Max retries exceeded with url")

        res = sc.fetch_sync(network_down)
        self.assertEqual(res["error"], "Garmin Connect did not answer")
        self.assertTrue(res["systemic"])
        self.assertEqual(set(sc.source_meta({"s": res})["s"]), {"ok", "fetched_at", "ms", "error"})


if __name__ == "__main__":
    unittest.main()
