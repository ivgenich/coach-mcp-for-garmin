"""Tests for tool_errors. Standard library only:

    python3 -m unittest discover -s ext -v
"""

import importlib
import importlib.util
import inspect
import random
import re
import secrets
import time
import unittest

import text_hygiene as th
import tool_errors as te

HAVE_UPSTREAM = importlib.util.find_spec("garmin_mcp") is not None
REF = "0a1b2c3d"
HINT = te.UPSTREAM_HINTS[0]
STALL = ("Garmin request 'get_stats' did not return within 90s and was abandoned. This is usually a transient stall "
         "on Garmin's side \u2014 please try again. (Adjust with GARMIN_MCP_CALL_TIMEOUT, or set it to 0 to disable "
         "the limit.)")
PENDING_LOGIN = "Garmin login failed. Run 'garmin-mcp-auth' to authenticate, then restart the server."
LOGIN_WAIT = ("Garmin login did not finish within 90s. Run 'garmin-mcp-auth' to verify your credentials, then "
              "restart the server.")


def fit_failure(activity_id):
    """Upstream's FIT failure answer (UP/activity_analysis.py:1888-1946), built as upstream builds it: int() of the
    caller's id comes first; a numeric id then reaches Garmin, which (with no login) fails with the login text."""
    try:
        activity_id = int(activity_id)
        raise RuntimeError(PENDING_LOGIN)
    except Exception as e:  # noqa: BLE001 - upstream's own form
        return f"Error downloading FIT data for activity {activity_id}: {str(e)}"


# A long id, a colon, a line break, an id longer than classify_text's 4,000-character window, and an id
# past int()'s 4,300-digit limit.
FIT_TEXTS = tuple(fit_failure(i) for i in ("1" * 47, "1:2", "x\nrate limit", "a" * 5000, "1" * 5000, "garmin login"))


class ToolError(Exception):
    """Stand-in for the SDK's ToolError: classify_exception() knows it by class name."""


class UnexpectedToolError(ToolError):
    pass


class BadArgument(te.KnownFailure, ValueError):
    pass


class NoData(te.KnownFailure, ValueError):
    pass


class SystemicFailure(te.KnownFailure):
    pass


def garmin_error(name, message, module="garminconnect.exceptions"):
    return type(name, (Exception,), {"__module__": module})(message)


def raise_it(exc, cause=None):
    if cause is None:
        raise exc
    raise exc from cause


def raised(func):
    """The exception `func` raises, with __cause__ and __context__ as Python sets them."""
    try:
        func()
    except BaseException as exc:  # noqa: BLE001 - the test inspects it
        return exc
    raise AssertionError("did not raise")


def through_wrappers(func, tool="get_daily_status"):
    """`func`'s exception as the override sees it: the adapter's ToolError(str(exc)) from exc, inside the SDK's
    ToolError("Error executing tool <name>: ...") from that."""
    def adapter():
        try:
            func()
        except Exception as exc:
            raise ToolError(str(exc)) from exc

    def sdk():
        try:
            adapter()
        except ToolError as exc:
            raise ToolError(f"Error executing tool {tool}: {exc}") from exc

    return raised(sdk)


def key_error_while_handling_a_timeout():
    try:
        raise TimeoutError(STALL)
    except TimeoutError:
        return {}["sleep"]


class Cfg:
    pass


class ValidationError(Exception):
    """Duck-typed stand-in for pydantic_core.ValidationError."""

    def __init__(self, errors):
        super().__init__(f"{len(errors)} validation error for get_activityArguments")
        self._errors = errors

    def errors(self, include_url=True, include_input=True):
        out = []
        for err in self._errors:
            err = dict(err)
            if not include_url:
                err.pop("url", None)
            if not include_input:
                err.pop("input", None)
            out.append(err)
        return out


class OldValidationError(ValidationError):
    def errors(self):  # an older errors() without keyword arguments
        return list(self._errors)


OldValidationError.__name__ = "ValidationError"


INT_ERROR = {"type": "int_parsing", "loc": ("activity_id",), "msg": "Input should be a valid integer",
             "input": "garmin login", "ctx": {"error": "rate limit"},
             "url": "https://errors.pydantic.dev/2/v/int_parsing"}

CLIENT_TEXTS = {
    te.LOGIN: "Error: Garmin login missing or expired. This server cannot read Garmin data until its owner signs in "
              "to Garmin again: run garmin-mcp-auth on the server, then restart it. Retrying will not help until then.",
    te.RATE_LIMITED: "Error: Garmin rate limit. Garmin is refusing requests from this server for now. Stop calling "
                     "Garmin tools for at least 15 minutes, even if the user asks you to try again: each call can "
                     "extend the block. Tell the user to try again in 15 minutes or later.",
    te.BAD_INPUT: "Error: Invalid input: check the arguments against the tool description. Fix the arguments and call "
                  "the tool again.",
    te.NO_DATA: "No data: Garmin has nothing for this request. Check the id or the date (ids come from get_activities, "
                "get_scheduled_workouts or get_courses); repeating the same call will not help.",
    te.UNAVAILABLE: "Error: Garmin Connect did not answer (a timeout, a network problem or an error on Garmin's side). "
                    "Retry this call at most once; if it fails again, stop calling Garmin tools and tell the user. "
                    "Try again in a few minutes.",
    te.INTERNAL: "Error: Internal server error (ref 0a1b2c3d). Retrying once later may help. If it fails again, tell "
                 "the user; the server owner can find the details in the server log under this ref.",
}
ALL_DETAILS = (te.DETAIL_UNKNOWN_TOOL, te.DATE_DETAIL, te.RANGE_MAX_DETAIL.format(n=9999), te.RANGE_DETAIL,
               te.ORDER_DETAIL, te.BOTH_DATES_DETAIL, te.TEXT_FORMAT_DETAIL, te.LIMIT_DETAIL.format(n=999999),
               te.NUMBER_DETAIL, te.RANGE_VALUE_DETAIL, te.GARMIN_400_DETAIL, te.GENERIC_DETAIL)


class Constants(unittest.TestCase):
    def test_groups(self):
        self.assertEqual(te.GROUPS, ("login", "rate_limited", "bad_input", "no_data", "unavailable", "internal"))
        self.assertEqual(te.IS_ERROR, {"login": True, "rate_limited": True, "bad_input": True, "no_data": False,
                                       "unavailable": True, "internal": True})
        self.assertEqual(te.GROUP_PHRASE, {"login": "Garmin login missing or expired",
                                           "rate_limited": "Garmin rate limit", "bad_input": "Invalid input",
                                           "no_data": "No data:", "unavailable": "Garmin Connect did not answer",
                                           "internal": "Internal server error"})
        for group in te.GROUPS:
            text = te.client_text(group, ref=REF)
            self.assertIn(te.GROUP_PHRASE[group], text)

    def test_texts_and_details(self):
        self.assertEqual(te.FAILURE_RE.pattern, r"^Error(?: [^\n]{1,200}?)?: ")  # the head may hold colons (was [^:\n]{1,80})
        self.assertEqual(te.PLAIN_FAILURE_PREFIXES, (
            "Invalid date format:", "Date range too large", "end_date must be on or after start_date",
            "Provide both start_date and end_date", "Unexpected response format", "fitparse library is not installed"))
        self.assertEqual(te.ECHO_FAILURE_PREFIXES, ("Error downloading FIT data for activity ",))
        self.assertEqual(te.UPSTREAM_HINTS, (
            "Garmin Connect may be unreachable; check your network connection or try again later",))
        self.assertEqual(te.DETAIL_UNKNOWN_TOOL, "there is no tool with that name; check the tool list")
        self.assertEqual(te.DATE_DETAIL, "dates must be real calendar dates written YYYY-MM-DD")
        self.assertEqual(te.RANGE_MAX_DETAIL, "the date range is too long: at most {n} days")
        self.assertEqual(te.RANGE_DETAIL, "the date range is too long")
        self.assertEqual(te.ORDER_DETAIL, "end_date must be on or after start_date")
        self.assertEqual(te.BOTH_DATES_DETAIL, "give both start_date and end_date, or neither")
        self.assertEqual(te.TEXT_FORMAT_DETAIL, 'text_format must be empty or "markdown"')
        self.assertEqual(te.LIMIT_DETAIL, "limit is too large: at most {n}")
        self.assertEqual(te.NUMBER_DETAIL, "an id, limit or count must be a whole number")
        self.assertEqual(te.RANGE_VALUE_DETAIL, "an argument is outside its allowed values; check the tool description")
        self.assertEqual(te.GARMIN_400_DETAIL,
                         "Garmin rejected the request parameters; check them against the tool description")
        self.assertEqual(te.GENERIC_DETAIL, "check the arguments against the tool description")
        self.assertTrue(issubclass(te.KnownFailure, Exception))


class FailureText(unittest.TestCase):
    """Which texts count as failures."""

    def test_failure_forms(self):
        forms = ("Error: x", "Error retrieving activities: x", "  Error fetching course details: API Error 401",
                 "Error building course profile: x", "Error downloading FIT data for activity 1: x",
                 "Error executing tool get_stats: x", "Error retrieving heart-rate zones: x",
                 "Error: date must be YYYY-MM-DD, got 'x'", "Error retrieving activity 14295551: x",
                 "\nError: Internal server error (ref 0a1b2c3d). Retrying once later may help.",
                 "Error " + "w" * 200 + ": x", "Error retrieving activity 1:2: x")
        for text in forms + tuple(prefix + " x" for prefix in te.PLAIN_FAILURE_PREFIXES) + FIT_TEXTS:
            with self.subTest(ascii(text[:60])):
                self.assertTrue(te.is_failure_text(text))

    def test_not_failures(self):
        for text in ("No HRV data found between 2026-11-09 and 2026-11-10.", '{"error": "x"}', "[]", "Errors: none",
                     "", "   ", None, 42, b"Error: x", "Error", "Error:x", "Error " + "w" * 201 + ": x",
                     "Error x\ny: z", "No data: Garmin has nothing for this request.", "Warning: x",
                     "error: lower case", "No FIT data returned for activity 1"):
            with self.subTest(repr(text)):
                self.assertFalse(te.is_failure_text(text))


# Samples: (text, group, detail).
SAMPLES = (
    ("Error retrieving activities: Garmin login failed. Run 'garmin-mcp-auth' to authenticate, then restart the "
     "server.", te.LOGIN, None),
    ("Garmin login did not finish within 90s. Run 'garmin-mcp-auth' ...", te.LOGIN, None),
    ("Error retrieving stats: Garmin Connect request failed: Connection error: Not authenticated. " + HINT + ".",
     te.LOGIN, None),
    ("Error retrieving stats: Garmin rate limit hit: Rate limit exceeded ... Wait a few minutes before retrying.",
     te.RATE_LIMITED, None),
    ("Error retrieving activity 14295551: Garmin Connect request failed: HTTP error: API Error 404 - Not Found. "
     + HINT + ".", te.NO_DATA, None),
    ("Error retrieving stats: Garmin Connect request failed: HTTP error: API Error 503 - <html><body>x</body></html>",
     te.UNAVAILABLE, None),
    ("Error retrieving stats: Garmin Connect request failed: HTTP error: API Error 403 - Forbidden", te.UNAVAILABLE,
     None),
    ("Error retrieving X: " + STALL, te.UNAVAILABLE, None),
    ("Error retrieving stats: Garmin Connect request failed: Connection error: HTTPSConnectionPool("
     "host='example.invalid', port=443): Max retries exceeded with url: /x", te.UNAVAILABLE, None),
    ("Invalid date format: Invalid isoformat string: '2026-13-01'. Use YYYY-MM-DD.", te.BAD_INPUT, te.DATE_DETAIL),
    ("Error: date must be YYYY-MM-DD, got '2026-02-30'", te.BAD_INPUT, te.DATE_DETAIL),
    ("Date range too large (400 days). Maximum is 90 days.", te.BAD_INPUT,
     "the date range is too long: at most 90 days"),
    ("Error retrieving activities: limit cannot exceed 1000", te.BAD_INPUT, "limit is too large: at most 1000"),
    ("Error: text_format must be '' or 'markdown'.", te.BAD_INPUT, te.TEXT_FORMAT_DETAIL),
    ("Error: course 7 not found or missing geoPoints.", te.NO_DATA, None),
    ("Error: REPORT_TZ is not set: set it to your IANA time zone name (for example Europe/London) and restart the "
     "server", te.INTERNAL, None),
    ("Unexpected response format from API", te.INTERNAL, None),
    ("fitparse library is not installed. Install it with: pip install fitparse", te.INTERNAL, None),
    ("ToolError: Unknown tool: no_such_tool", te.BAD_INPUT, te.DETAIL_UNKNOWN_TOOL),
)

# One text per row of the table, top to bottom, plus the anchored bad-input rows.
ROWS = (
    ("Garmin authentication failed: expired. Re-run 'garmin-mcp-auth'", te.LOGIN, None),
    ("GarminConnectAuthenticationError: x", te.LOGIN, None),
    ("HTTP error: API Error 401 - Unauthorized", te.LOGIN, None),
    ("API client error (401): Unauthorized", te.LOGIN, None),
    ("Error: unauthorised", te.LOGIN, None),
    ("OAuth1 token exchange failed", te.LOGIN, None),
    ("Error: refresh token expired", te.LOGIN, None),
    ("MFA required but non-interactive environment", te.LOGIN, None),
    ("GarminConnectTooManyRequestsError: x", te.RATE_LIMITED, None),
    ("429 Too Many Requests", te.RATE_LIMITED, None),
    ("API Error 429 - x", te.RATE_LIMITED, None),
    ("API client error (429): x", te.RATE_LIMITED, None),
    ("Garmin returned 429 to the login", te.RATE_LIMITED, None),
    ("got 429 in JSON body", te.RATE_LIMITED, None),
    ("API Error 404 - Not Found", te.NO_DATA, None),
    ("API client error (404): x", te.NO_DATA, None),
    ("API Error 403 - Forbidden", te.UNAVAILABLE, None),
    ("API Error 500 - x", te.UNAVAILABLE, None),
    ("API client error (502): x", te.UNAVAILABLE, None),
    ("API Error 400 - Bad Request", te.BAD_INPUT, te.GARMIN_400_DETAIL),
    ("API client error (400): x", te.BAD_INPUT, te.GARMIN_400_DETAIL),
    ("Error executing tool x: Unknown tool: y", te.BAD_INPUT, te.DETAIL_UNKNOWN_TOOL),
    ("Error retrieving sleep: Date range too large (1 day). Maximum is 7 days.", te.BAD_INPUT,
     "the date range is too long: at most 7 days"),
    ("Error retrieving activities: startdate cannot be more than one year before enddate", te.BAD_INPUT,
     te.RANGE_DETAIL),
    ("Error retrieving training: end_date must be on or after start_date", te.BAD_INPUT, te.ORDER_DETAIL),
    ("Error retrieving activities: Start date cannot be after end date", te.BAD_INPUT, te.ORDER_DETAIL),
    ("Error retrieving training: Provide both start_date and end_date", te.BAD_INPUT, te.BOTH_DATES_DETAIL),
    ("Error: You must either provide all parameters or no parameters", te.BAD_INPUT, te.BOTH_DATES_DETAIL),
    ("Error retrieving stats: day is out of range for month", te.BAD_INPUT, te.DATE_DETAIL),
    ("Error retrieving stats: month must be in 1..12", te.BAD_INPUT, te.DATE_DETAIL),
    ("Error retrieving stats: Invalid startdate", te.BAD_INPUT, te.DATE_DETAIL),
    ("Error retrieving stats: invalid date format", te.BAD_INPUT, te.DATE_DETAIL),
    ("Error retrieving daily: text_format must be x", te.BAD_INPUT, te.TEXT_FORMAT_DETAIL),
    ("Error retrieving activities: limit cannot exceed 50", te.BAD_INPUT, "limit is too large: at most 50"),
    ("Error retrieving activity: invalid literal for int() with base 10: 'abc'", te.BAD_INPUT, te.NUMBER_DETAIL),
    ("Error: activity_id must be an integer", te.BAD_INPUT, te.NUMBER_DETAIL),
    ("Error: count must be a positive integer", te.BAD_INPUT, te.NUMBER_DETAIL),
    ("Error: weight must be a number", te.BAD_INPUT, te.NUMBER_DETAIL),
    ("Error: days must be positive", te.BAD_INPUT, te.RANGE_VALUE_DETAIL),
    ("Error: start must be non-negative", te.BAD_INPUT, te.RANGE_VALUE_DETAIL),
    ("Error: days must be between 1 and 28", te.BAD_INPUT, te.RANGE_VALUE_DETAIL),
    ("Error: status must be one of: a, b", te.BAD_INPUT, te.RANGE_VALUE_DETAIL),
    ("Error: index must be an int in [0, 3]", te.BAD_INPUT, te.RANGE_VALUE_DETAIL),
    ("Error: invalid input", te.BAD_INPUT, te.GENERIC_DETAIL),
    ("Error retrieving stats: Garmin Connect request failed: No data received from server. " + HINT, te.NO_DATA, None),
    ("Error: course has fewer than two points with elevation", te.NO_DATA, None),
    ("Error: empty FIT download", te.NO_DATA, None),
    ("Error: no data from Garmin", te.NO_DATA, None),
    ("Error: x no data: y", te.NO_DATA, None),
    ("Connection error: x", te.UNAVAILABLE, None),
    ("Garmin did not answer", te.UNAVAILABLE, None),
    ("Read timed out", te.UNAVAILABLE, None),
    ("ReadTimeout", te.UNAVAILABLE, None),
    ("Failed to establish a new connection", te.UNAVAILABLE, None),
    ("Temporary failure in name resolution", te.UNAVAILABLE, None),
    ("nodename nor servname provided, or not known", te.UNAVAILABLE, None),
    ("Name or service not known", te.UNAVAILABLE, None),
    ("[Errno 61] Connection refused", te.UNAVAILABLE, None),
    ("Connection reset by peer", te.UNAVAILABLE, None),
    ("Connection aborted.", te.UNAVAILABLE, None),
    ("Remote end closed connection without response", te.UNAVAILABLE, None),
    ("Network is unreachable", te.UNAVAILABLE, None),
    ("503 Service Unavailable", te.UNAVAILABLE, None),
    ("502 Bad Gateway", te.UNAVAILABLE, None),
    ("504 Gateway Time-out", te.UNAVAILABLE, None),
    ("Error: math domain error", te.INTERNAL, None),
    ("Error retrieving activities: 'list' object has no attribute 'get'", te.INTERNAL, None),
    ("Error retrieving activities: ", te.INTERNAL, None),
    ("", te.INTERNAL, None),
)

# Real answers the R1b probes recorded (critic A, classify-out.txt): (text, group).
REAL = (
    ("Error fetching course details: API Error 401 - Unauthorized", te.LOGIN),
    ("Error downloading FIT data for activity 1: Garmin Connect request failed: Download error: API Error 401 - "
     "Unauthorized. " + HINT + ".", te.LOGIN),
    ("Error retrieving activities: Garmin Connect request failed: HTTP error: API Error 429 - <html><body><h1>429 Too "
     "Many Requests</h1></body></html>. " + HINT + ".", te.RATE_LIMITED),
    ("Error building course profile: API Error 404 - ", te.NO_DATA),
    ("Error retrieving stats: Garmin Connect request failed: HTTP error: API Error 500 - <html><head><title>Error"
     "</title></head><body>Internal error for /usersummary-service/usersummary/daily/MyName</body></html>. " + HINT,
     te.UNAVAILABLE),
    ("Error retrieving stats: Garmin Connect request failed: HTTP error: API Error 503 - {'error': 'Service "
     "Unavailable', 'path': '/usersummary-service/usersummary/daily/MyName', 'traceId': '0123456789abcdef'}. " + HINT,
     te.UNAVAILABLE),
    ("Error fetching course details: HTTPSConnectionPool(host='connectapi.garmin.com', port=443): Max retries "
     "exceeded with url: /course-service/course/1 (Caused by NameResolutionError(\"<urllib3.connection."
     "HTTPSConnection object>: Failed to resolve\"))", te.UNAVAILABLE),
    ("Error retrieving workout: 'list' object has no attribute 'get'", te.INTERNAL),
    ("Error retrieving sleep summary: " + STALL, te.UNAVAILABLE),
    ("Error: course 1 not found or missing geoPoints.", te.NO_DATA),
    ("Error retrieving weekly stress data: Garmin login failed. Run 'garmin-mcp-auth' to authenticate, then restart "
     "the server.", te.LOGIN),
    ("Error retrieving heart-rate zones: Garmin returned an unexpected heart-rate-zone response", te.INTERNAL),
    ("Error executing tool exits: SystemExit: 3", te.INTERNAL),
)

KEYWORDS = ("garmin login", "rate limit", "API Error 404", "timeout", "unauthorized", "(503)", "x' rate limit \"",
            "no data: x", "Unknown tool: y")


class ClassifyText(unittest.TestCase):
    """classify_text(): the table, the anchored rows and the caller echoes."""

    def test_contract_samples(self):
        for text, group, detail in SAMPLES:
            with self.subTest(text[:60]):
                self.assertEqual(te.classify_text(text), (group, detail))

    def test_every_row(self):
        for text, group, detail in ROWS:
            with self.subTest(text[:60]):
                self.assertEqual(te.classify_text(text), (group, detail))

    def test_real_answers(self):
        for text, group in REAL:
            with self.subTest(text[:60]):
                self.assertEqual(te.classify_text(text)[0], group)

    def test_caller_text_never_picks_the_group(self):
        forms = (
            ("Error: date must be YYYY-MM-DD, got {!r}", te.DATE_DETAIL),
            ("Error: end_date must be YYYY-MM-DD, got {!r}", te.DATE_DETAIL),
            ("Error: text_format must be '' or 'markdown', got {!r}", te.TEXT_FORMAT_DETAIL),
            ("Error retrieving activity: invalid literal for int() with base 10: {!r}", te.NUMBER_DETAIL),
            ("Invalid date format: Invalid isoformat string: {!r}. Use YYYY-MM-DD.", te.DATE_DETAIL),
            ("Error retrieving scheduled workouts: Invalid start_date {!r}: expected YYYY-MM-DD", te.DATE_DETAIL),
            ("Invalid date {!r}. Use YYYY-MM-DD.", te.DATE_DETAIL),
            ("Error retrieving stats: time data {!r} does not match format '%Y-%m-%d' (YYYY-MM-DD)", te.DATE_DETAIL),
            ("Error retrieving stats: cdate must be in format 'YYYY-MM-DD', got: {}", te.DATE_DETAIL),
            ("ToolError: Unknown tool: {}", te.DETAIL_UNKNOWN_TOOL),
            ("Error executing tool get_stats: Unknown tool: {}", te.DETAIL_UNKNOWN_TOOL),
        )
        for form, detail in forms:
            for word in KEYWORDS:
                for value in (word, "x\n" + word, "x\r\n" + word + "\n"):  # a line break in the value
                    text = form.format(value)
                    with self.subTest(text):
                        self.assertEqual(te.classify_text(text), (te.BAD_INPUT, detail))

    def test_empty_sleep_summary(self):
        """Live test B3: upstream answers an empty object, or only nulls, for a night Garmin has nothing for."""
        for text in ("{}", " {} ", '{"sleep_seconds": null}', '{\n  "sleep_seconds": null,\n  "sleep_score": null\n}'):
            with self.subTest(text):
                self.assertTrue(te.is_empty_sleep_summary(text))
        for text in ('{"sleep_seconds": 0}', '{"sleep_seconds": null, "nap_seconds": 600}', "[]", "null", "", "{",
                     "No sleep summary found for 2026-11-18", None, 7, b"{}", "[" * 100000,
                     '{"a": null}' + " " * 4000):
            with self.subTest(repr(text)[:40]):
                self.assertFalse(te.is_empty_sleep_summary(text))
        self.assertTrue(te.NO_SLEEP_TEXT.startswith("No data: "))
        self.assertTrue(te.NO_SLEEP_TEXT.isascii())
        self.assertLessEqual(len(te.NO_SLEEP_TEXT), 300)
        self.assertEqual(te.classify_text(te.NO_SLEEP_TEXT), (te.NO_DATA, None))
        self.assertEqual(te.classify_text(te.NO_SLEEP_LOG), (te.NO_DATA, None))

    def test_caller_fit_id_never_picks_the_group(self):
        """Upstream's FIT tools write the caller's id before their colon."""
        for word in KEYWORDS:
            for activity_id in (word, "x\n" + word, "12: " + word, word + " " + "a" * 5000, "1" * 5000 + word):
                text = fit_failure(activity_id)
                with self.subTest(ascii(activity_id[:40])):
                    self.assertTrue(te.is_failure_text(text))
                    self.assertEqual(te.classify_text(text), (te.BAD_INPUT, te.NUMBER_DETAIL))
        self.assertEqual(te.classify_text(fit_failure("1" * 47)), (te.LOGIN, None))  # numeric: Garmin's text decides
        self.assertEqual(te.classify_text(fit_failure("1" * 5000)), (te.BAD_INPUT, te.NUMBER_DETAIL))
        # A numeric id that int() accepts reaches Garmin, whose words then follow the id. With a very long id they
        # fall outside the 4,000-character window (int() takes up to 4,300 digits, and any length before Python
        # 3.10.7): the answer was "internal error". 100 digits or more is bad input on every Python.
        prefix = te.ECHO_FAILURE_PREFIXES[0]
        for digits in (100, 3990, 4100, 4300, 5000):
            for tail in (": API Error 404 - x", ": " + PENDING_LOGIN, ": rate limit"):
                with self.subTest(digits=digits, tail=tail[:12]):
                    self.assertEqual(te.classify_text(prefix + "1" * digits + tail), (te.BAD_INPUT, te.NUMBER_DETAIL))
        self.assertEqual(te.classify_text(prefix + "1" * 99 + ": API Error 404 - x"), (te.NO_DATA, None))
        self.assertEqual(te.classify_text("Error: " + "1" * 200 + " API Error 404 - x"), (te.NO_DATA, None))  # FIT only
        self.assertEqual(te.classify_text("Error downloading FIT data for activity 7: API Error 404 - x"),
                         (te.NO_DATA, None))

    def test_m7_samples(self):
        self.assertEqual(te.classify_text("Error: date must be YYYY-MM-DD, got 'rate limit'"),
                         (te.BAD_INPUT, te.DATE_DETAIL))
        self.assertEqual(te.classify_text("Invalid date format: Invalid isoformat string: 'garmin login'. Use "
                                          "YYYY-MM-DD."), (te.BAD_INPUT, te.DATE_DETAIL))

    def test_hint_and_reading_window(self):
        self.assertEqual(te.classify_text("Garmin Connect request failed: odd. " + HINT.upper()), (te.INTERNAL, None))
        self.assertEqual(te.classify_text("x" * 4000 + " rate limit"), (te.INTERNAL, None))
        self.assertEqual(te.classify_text("rate limit " + "x" * 5000), (te.RATE_LIMITED, None))

    def test_numbers_in_details_are_ascii(self):
        group, detail = te.classify_text("Date range too large (400 days). Maximum is \u0669\u0660 days.")
        self.assertEqual((group, detail), (te.BAD_INPUT, "the date range is too long: at most 90 days"))
        group, detail = te.classify_text("Error: limit cannot exceed \u0661\u0660\u0660\u0660")
        self.assertEqual((group, detail), (te.BAD_INPUT, "limit is too large: at most 1000"))

    def test_non_strings(self):
        for value in (None, 42, b"Error: rate limit", ["rate limit"]):
            with self.subTest(repr(value)):
                self.assertEqual(te.classify_text(value), (te.INTERNAL, None))


class ClassifyException(unittest.TestCase):
    """classify_exception(): only failures from Garmin, the network or known failures get the table."""

    def assertGroup(self, exc, group, detail=None):
        result = te.classify_exception(exc)
        self.assertEqual(len(result), 4)
        self.assertEqual(result[:2], (group, detail), result[3])
        return result

    def test_validation_error(self):
        exc = ValidationError([INT_ERROR])
        group, detail, root, text = te.classify_exception(exc)
        self.assertEqual((group, detail), (te.BAD_INPUT, "activity_id must be a whole number"))
        self.assertIs(root, exc)
        self.assertEqual(text, "ValidationError: activity_id must be a whole number")  # field names only
        wrapped = raised(lambda: raise_it(ToolError("Error executing tool get_activity: 1 validation error"), exc))
        self.assertEqual(self.assertGroup(wrapped, te.BAD_INPUT, "activity_id must be a whole number")[3],
                         "ValidationError: activity_id must be a whole number")
        self.assertGroup(OldValidationError([INT_ERROR]), te.BAD_INPUT, "activity_id must be a whole number")

    def test_validation_values_never_reach_the_chain_text(self):
        """The chain text is the log line's text; pydantic's message holds the rejected value."""
        value = "TEST-ONLY-" + secrets.token_hex(6)

        class LoudValidationError(ValidationError):
            def __str__(self):
                return ("1 validation error for get_activitiesArguments\nlimit\n  Input should be a valid integer "
                        f"[type=int_parsing, input_value={value!r}, input_type=str]")

        LoudValidationError.__name__ = "ValidationError"
        loud = LoudValidationError([{"type": "int_parsing", "loc": ("limit",), "input": value}])
        sdk = raised(lambda: raise_it(ToolError(f"Error executing tool get_activities: {loud}"), loud))
        for exc in (loud, sdk):
            with self.subTest(type(exc).__name__):
                group, detail, _, text = te.classify_exception(exc)
                self.assertEqual((group, detail, text),
                                 (te.BAD_INPUT, "limit must be a whole number",
                                  "ValidationError: limit must be a whole number"))
                self.assertNotIn(value, te.redact(text))

    @unittest.skipUnless(importlib.util.find_spec("pydantic") is not None, "needs pydantic")
    def test_real_pydantic_validation_error(self):
        import pydantic

        class Arguments(pydantic.BaseModel):
            activity_id: int
            start_date: str

        value = "TEST-ONLY-" + secrets.token_hex(6)
        try:
            Arguments(activity_id=value, start_date=7)
        except pydantic.ValidationError as exc:
            self.assertIn(value, str(exc))  # pydantic's own message holds the value...
            group, detail, root, text = te.classify_exception(exc)
        self.assertEqual((group, detail), (te.BAD_INPUT, "activity_id must be a whole number; start_date must be text"))
        self.assertNotIn(value, text)  # ...the chain text never does
        self.assertEqual(text, f"ValidationError: {detail}")

    def test_validation_error_without_usable_errors(self):
        class Broken(ValidationError):
            def errors(self, **kwargs):
                raise RuntimeError("boom")

        Broken.__name__ = "ValidationError"
        self.assertGroup(Broken([]), te.BAD_INPUT, te.GENERIC_DETAIL)

    def test_login_through_wrappers(self):
        login = RuntimeError(PENDING_LOGIN)
        exc = raised(lambda: raise_it(ToolError("Error executing tool get_vo2max_trend: " + PENDING_LOGIN),
                                      raised(lambda: raise_it(ToolError(PENDING_LOGIN), login))))
        group, detail, root, text = te.classify_exception(exc)
        self.assertEqual((group, detail), (te.LOGIN, None))
        self.assertIs(root, login)
        self.assertEqual(text.count(" <- "), 2)
        self.assertTrue(text.startswith("ToolError: Error executing tool get_vo2max_trend: Garmin login failed."))

    def test_unknown_tool(self):
        for name in ("x", "garmin login", "rate limit"):
            with self.subTest(name):
                exc = ToolError(f"Unknown tool: {name}")
                self.assertEqual(te.classify_exception(exc),
                                 (te.BAD_INPUT, te.DETAIL_UNKNOWN_TOOL, exc, f"ToolError: Unknown tool: {name}"))
        from_bug = raised(lambda: raise_it(ToolError("Unknown tool: x"), KeyError("k")))
        self.assertGroup(from_bug, te.INTERNAL)

    def test_chains_end(self):
        loop = RuntimeError("loop")
        loop.__cause__ = loop
        self.assertGroup(loop, te.INTERNAL)
        first, second = RuntimeError("a"), RuntimeError("b")
        first.__cause__, second.__cause__ = second, first
        self.assertEqual(te.classify_exception(first)[3], "RuntimeError: a <- RuntimeError: b")
        long_chain = KeyError("start")
        for i in range(10):
            outer = ToolError(f"wrap {i}")
            outer.__cause__ = long_chain
            long_chain = outer
        self.assertEqual(te.classify_exception(long_chain)[3].count(" <- "), 5)

    def test_suppressed_context_is_not_followed(self):
        def from_none():
            try:
                raise TimeoutError(STALL)
            except TimeoutError:
                raise KeyError("sleep") from None

        group, _, root, text = te.classify_exception(raised(from_none))
        self.assertEqual(group, te.INTERNAL)
        self.assertEqual(text, "KeyError: 'sleep'")
        self.assertIsInstance(root, KeyError)

    def test_crashes_are_internal(self):
        crashes = (
            raised(key_error_while_handling_a_timeout),
            raised(lambda: Cfg().timeout),
            ValueError("day is out of range for month"),
            RuntimeError("timed out"),
            raised(lambda: raise_it(ToolError("Error executing tool x: 'k'"), KeyError("k"))),
            ValueError("rate limit"),
            AttributeError("'NoneType' object has no attribute 'get' (API Error 503)"),
        )
        for exc in crashes:
            with self.subTest(repr(exc)):
                self.assertGroup(exc, te.INTERNAL)
                self.assertGroup(through_wrappers(lambda: raise_it(exc)), te.INTERNAL)
        group, _, root, text = te.classify_exception(through_wrappers(key_error_while_handling_a_timeout))
        self.assertIsInstance(root, TimeoutError)  # root is the last link; the KeyError decides
        self.assertIn("did not return within", text)

    def test_garmin_and_network_failures_use_the_table(self):
        cases = (
            (garmin_error("GarminConnectConnectionError", "API Error 503 - Service Unavailable"), te.UNAVAILABLE),
            (garmin_error("GarminConnectConnectionError", "Garmin Connect request failed: HTTP error: API Error 404 - "
                                                         "Not Found. " + HINT + "."), te.NO_DATA),
            (garmin_error("GarminConnectTooManyRequestsError", "Too many requests"), te.RATE_LIMITED),
            (garmin_error("ReadTimeout", "HTTPSConnectionPool(host='example.invalid'): Read timed out.",
                          "requests.exceptions"), te.UNAVAILABLE),
            (garmin_error("RequestsError", "Failed to perform, curl: (6) Could not resolve host",
                          "curl_cffi.requests.exceptions"), te.INTERNAL),
            (TimeoutError(STALL), te.UNAVAILABLE),
            (type("TimeoutError", (Exception,), {})(), te.UNAVAILABLE),
            (ConnectionRefusedError("[Errno 61] Connection refused"), te.UNAVAILABLE),
            (OSError("Network is unreachable"), te.UNAVAILABLE),
            (ToolError("Garmin rate limit"), te.RATE_LIMITED),
        )
        for exc, group in cases:
            with self.subTest(repr(exc)):
                self.assertGroup(exc, group)
                self.assertGroup(through_wrappers(lambda: raise_it(exc)), group)

    def test_login_wins_whatever_the_type(self):
        for exc in (RuntimeError(PENDING_LOGIN), RuntimeError(LOGIN_WAIT),
                    RuntimeError("MFA required but non-interactive environment"),
                    garmin_error("GarminConnectAuthenticationError", "Garmin authentication failed: 401")):
            with self.subTest(repr(exc)):
                self.assertGroup(exc, te.LOGIN)
                self.assertGroup(through_wrappers(lambda: raise_it(exc)), te.LOGIN)

    def test_known_failures(self):
        cases = (
            (NoData("course has fewer than two points with elevation"), te.NO_DATA, None),
            (BadArgument("date must be YYYY-MM-DD, got 'garmin login'"), te.BAD_INPUT, te.DATE_DETAIL),
            (BadArgument("text_format must be '' or 'markdown', got 'rate limit'"), te.BAD_INPUT,
             te.TEXT_FORMAT_DETAIL),
            (BadArgument("end_date must be on or after start_date"), te.BAD_INPUT, te.ORDER_DETAIL),
            (SystemicFailure("Garmin rate limit"), te.RATE_LIMITED, None),
            (SystemicFailure("Garmin login missing or expired"), te.LOGIN, None),
            (SystemicFailure("Garmin Connect did not answer"), te.UNAVAILABLE, None),
            (BadArgument("REPORT_TZ is not set"), te.INTERNAL, None),
        )
        for exc, group, detail in cases:
            with self.subTest(repr(exc)):
                self.assertGroup(exc, group, detail)
                self.assertGroup(through_wrappers(lambda: raise_it(exc)), group, detail)

    def test_chain_text_is_the_original(self):
        exc = through_wrappers(lambda: raise_it(BadArgument("date must be YYYY-MM-DD, got 'garmin login'")))
        text = te.classify_exception(exc)[3]
        self.assertEqual(text, "ToolError: Error executing tool get_daily_status: date must be YYYY-MM-DD, got "
                               "'garmin login' <- ToolError: date must be YYYY-MM-DD, got 'garmin login' <- "
                               "BadArgument: date must be YYYY-MM-DD, got 'garmin login'")
        sdk = raised(lambda: raise_it(UnexpectedToolError("Error executing tool get_stats"), RuntimeError("timed out")))
        self.assertGroup(sdk, te.INTERNAL)

    def test_odd_inputs_never_raise(self):
        class Unprintable(Exception):
            def __str__(self):
                raise RuntimeError("no")

        group, detail, root, text = te.classify_exception(Unprintable())
        self.assertEqual((group, detail), (te.INTERNAL, None))
        self.assertIn("<unprintable Unprintable>", text)
        self.assertEqual(te.classify_exception("Error: rate limit"),
                         (te.INTERNAL, None, "Error: rate limit", "str: Error: rate limit"))
        self.assertEqual(te.classify_exception(None), (te.INTERNAL, None, None, "NoneType: None"))
        huge = garmin_error("GarminConnectConnectionError", "API Error 503 - " + "x" * 100000)
        self.assertLessEqual(len(te.classify_exception(huge)[3]), 2100)


class ValidationDetail(unittest.TestCase):
    """validation_detail() names the fields, never the values."""

    def test_problem_words(self):
        words = {"missing": "is required", "int_parsing": "must be a whole number",
                 "int_type": "must be a whole number", "int_from_float": "must be a whole number",
                 "float_parsing": "must be a number", "float_type": "must be a number",
                 "bool_parsing": "must be true or false", "bool_type": "must be true or false",
                 "string_type": "must be text", "date_parsing": "must be a date written YYYY-MM-DD",
                 "date_from_datetime_parsing": "must be a date written YYYY-MM-DD",
                 "literal_error": "has an invalid value", None: "has an invalid value"}
        for kind, problem in words.items():
            with self.subTest(kind):
                self.assertEqual(te.validation_detail([{"type": kind, "loc": ("start_date",)}]),
                                 f"start_date {problem}")

    def test_fields(self):
        cases = (
            (("body", 0, "na me\u202e;DROP", "x"), "body.0.nameDROP.x"),
            (("\u05e9\u05dd",), "an argument"),
            ((), "an argument"),
            (None, "an argument"),
            ("activity_id", "activity_id"),
            (("a" * 100,), "a" * 64),
            (("items[0]",), "items[0]"),
        )
        for loc, field in cases:
            with self.subTest(ascii(loc)):
                self.assertEqual(te.validation_detail([{"type": "missing", "loc": loc}]), f"{field} is required")

    def test_at_most_three_and_never_the_input(self):
        errors = [{"type": "missing", "loc": (f"arg{i}",), "input": "garmin login", "ctx": {"x": "rate limit"}}
                  for i in range(5)]
        self.assertEqual(te.validation_detail(errors), "arg0 is required; arg1 is required; arg2 is required")
        self.assertEqual(te.validation_detail(iter(errors)), "arg0 is required; arg1 is required; arg2 is required")
        self.assertEqual(te.validation_detail([INT_ERROR]), "activity_id must be a whole number")

    def test_long_and_odd(self):
        long_errors = [{"type": "date_parsing", "loc": (c * 64,)} for c in "abc"]
        detail = te.validation_detail(long_errors)
        self.assertEqual(len(detail), 200)
        self.assertTrue(detail.endswith("..."))
        for value in ([], None, "x", 42, {"type": "missing"}, [1, "x", None], object()):
            with self.subTest(repr(value)):
                self.assertEqual(te.validation_detail(value), te.GENERIC_DETAIL)

        class OddMapping(dict):
            def get(self, *args):
                raise RuntimeError("no")

        def stops_early():
            yield {"type": "missing", "loc": ("a",)}
            raise RuntimeError("no")

        self.assertEqual(te.validation_detail([OddMapping(), {"type": "missing", "loc": ("a",)}]), "a is required")
        self.assertEqual(te.validation_detail(stops_early()), te.GENERIC_DETAIL)


class ClientText(unittest.TestCase):
    """client_text(): the fixed text of each group."""

    def test_texts(self):
        for group, want in CLIENT_TEXTS.items():
            with self.subTest(group):
                self.assertEqual(te.client_text(group, ref=REF), want)
                self.assertEqual(want.startswith("Error: "), te.IS_ERROR[group])
        self.assertTrue(CLIENT_TEXTS[te.NO_DATA].startswith("No data: "))

    def test_ascii_and_short(self):
        three = te.validation_detail([{"type": "date_parsing", "loc": (c * 64,)} for c in "xyz"])
        details = ALL_DETAILS + (three, "x" * 1000, "caf\u00e9 \u202e x", "")
        for group in te.GROUPS:
            for detail in details:
                text = te.client_text(group, ref=REF, detail=detail, today="2026-11-16")
                with self.subTest((group, ascii(detail[:20]))):
                    self.assertTrue(text.isascii())
                    self.assertTrue(text.isprintable())
                    self.assertLessEqual(len(text), 300)
        self.assertEqual(len(te.client_text(te.BAD_INPUT, detail="x" * 1000)), 266)
        self.assertEqual(te.client_text(te.BAD_INPUT, detail="caf\u00e9 \u202e x"),
                         "Error: Invalid input: caf x. Fix the arguments and call the tool again.")

    def test_dates_and_today(self):
        with_today = te.client_text(te.BAD_INPUT, detail=te.DATE_DETAIL, today="2026-11-16")
        self.assertEqual(with_today, "Error: Invalid input: dates must be real calendar dates written YYYY-MM-DD, for "
                                     "example 2026-11-16 (today). Fix the arguments and call the tool again.")
        plain = "Error: Invalid input: dates must be real calendar dates written YYYY-MM-DD. Fix the arguments and " \
                "call the tool again."
        self.assertEqual(te.client_text(te.BAD_INPUT, detail=te.DATE_DETAIL), plain)
        for bad in ("2026-1-16", "16/11/2026", "\u0662\u0660\u0662\u0666-\u0661\u0661-\u0661\u0666", "2026-11-16\n",
                    " 2026-11-16", 20261116, None):
            with self.subTest(ascii(bad)):
                self.assertEqual(te.client_text(te.BAD_INPUT, detail=te.DATE_DETAIL, today=bad), plain)
        self.assertEqual(te.client_text(te.BAD_INPUT, detail=te.ORDER_DETAIL, today="2026-11-16"),
                         "Error: Invalid input: end_date must be on or after start_date. Fix the arguments and call "
                         "the tool again.")

    def test_programming_errors_raise(self):
        for ref in (None, "ABCDEF12", "0a1b2c3", "0a1b2c3d4", "0a1b2c3d\n", 12345678, "zzzzzzzz"):
            with self.subTest(repr(ref)), self.assertRaises(ValueError):
                te.client_text(te.INTERNAL, ref=ref)
        for group in ("nope", None, ["login"], "LOGIN", 3):
            with self.subTest(repr(group)), self.assertRaises(ValueError):
                te.client_text(group)

    def test_extra_arguments_are_ignored(self):
        self.assertEqual(te.client_text(te.LOGIN, ref=REF, detail="x", today="2026-11-16"), CLIENT_TEXTS[te.LOGIN])
        self.assertEqual(te.client_text(te.BAD_INPUT, ref=REF), CLIENT_TEXTS[te.BAD_INPUT])
        self.assertEqual(te.client_text(te.BAD_INPUT, detail=None), CLIENT_TEXTS[te.BAD_INPUT])
        self.assertEqual(te.client_text(te.BAD_INPUT, detail=42), CLIENT_TEXTS[te.BAD_INPUT])


class RoundTrip(unittest.TestCase):
    """A client text classifies back to its own group and detail."""

    def test_groups_round_trip(self):
        for group in te.GROUPS:
            with self.subTest(group):
                self.assertEqual(te.classify_text(te.client_text(group, ref=REF))[0], group)
                for ref in (REF, None):
                    self.assertEqual(te.classify_text(te.source_phrase(group, ref))[0], group)
                    self.assertEqual(te.classify_text("Error: " + te.source_phrase(group, ref))[0], group)
                self.assertEqual(te.is_failure_text(te.client_text(group, ref=REF)), te.IS_ERROR[group])

    def test_bad_input_details_round_trip(self):
        three = te.validation_detail([{"type": "int_parsing", "loc": ("unauthorized",)},
                                      {"type": "missing", "loc": ("toomanyrequests",)},
                                      {"type": "missing", "loc": ("x" * 64,)}])
        for detail in ALL_DETAILS + (three, "x" * 1000, "rate limit (429) api error 404"):
            for today in (None, "2026-11-16"):
                text = te.client_text(te.BAD_INPUT, detail=detail, today=today)
                with self.subTest((detail[:30], today)):
                    group, back = te.classify_text(text)
                    self.assertEqual(group, te.BAD_INPUT)
                    self.assertEqual(te.client_text(te.BAD_INPUT, detail=back, today=today), text)


class NoInternals(unittest.TestCase):
    """The client text never carries the original failure's text."""

    def test_no_internals(self):
        token = "TEST-ONLY-" + secrets.token_urlsafe(32)
        internals = ("https://connectapi.garmin.com/x", "/usersummary-service/usersummary/daily/MyName", token,
                     "someone@mail.example", "<html>", "GARMIN_MCP_CALL_TIMEOUT")
        original = ("Garmin Connect request failed: HTTP error: API Error 500 - <html><body>Internal error for "
                    f"/usersummary-service/usersummary/daily/MyName?token={token}</body></html> "
                    "https://connectapi.garmin.com/x someone@mail.example GARMIN_MCP_CALL_TIMEOUT=90")
        excs = (garmin_error("GarminConnectConnectionError", original), RuntimeError(original),
                BadArgument(original), ToolError("Unknown tool: " + original))
        for exc in excs:
            group, detail, _, _ = te.classify_exception(exc)
            text_group, text_detail = te.classify_text("Error retrieving stats: " + original)
            for g in te.GROUPS:
                for d in (detail, text_detail):
                    out = te.client_text(g, ref=te.new_ref(), detail=d if g == te.BAD_INPUT else None)
                    with self.subTest((repr(exc)[:30], g)):
                        for secret in internals:
                            self.assertNotIn(secret, out)
                        self.assertNotIn("MyName", out)
                        self.assertIn(te.GROUP_PHRASE[g], out)
            self.assertIn(group, te.GROUPS)
            self.assertIn(text_group, te.GROUPS)


class SourcePhrase(unittest.TestCase):
    def test_phrases(self):
        self.assertEqual([te.source_phrase(g) for g in te.GROUPS],
                         ["Garmin login missing or expired", "Garmin rate limit", "invalid input",
                          "no data from Garmin", "Garmin Connect did not answer", "internal error"])
        self.assertEqual(te.source_phrase(te.INTERNAL, REF), "internal error (ref 0a1b2c3d)")
        self.assertEqual(te.source_phrase(te.LOGIN, REF), "Garmin login missing or expired")
        for group, ref in ((te.INTERNAL, "ABC"), (te.INTERNAL, 5), ("nope", REF), (None, None), ([], REF),
                           (object(), None)):
            with self.subTest((repr(group), ref)):
                self.assertEqual(te.source_phrase(group, ref), "internal error")


class NewRef(unittest.TestCase):
    """new_ref(): 8 lowercase hex characters, new each time."""

    def test_new_ref(self):
        refs = {te.new_ref() for _ in range(100)}
        self.assertEqual(len(refs), 100)
        for ref in refs:
            self.assertRegex(ref, r"^[0-9a-f]{8}$")
            text = te.client_text(te.INTERNAL, ref=ref)
            self.assertTrue(text.startswith(f"Error: Internal server error (ref {ref})"))


class Redact(unittest.TestCase):
    """redact(): secrets, tokens and emails never reach a log line."""

    def test_patterns(self):
        token = "TEST-ONLY-" + secrets.token_urlsafe(32)
        short = secrets.token_hex(4)
        cases = (
            (f"Authorization: Bearer {token}", "Authorization: Bearer <redacted>"),
            (f"auth: bearer {short}.x", "auth: Bearer <redacted>"),
            (f"refresh_token={token}&x=1", "refresh_token=<redacted>&x=1"),
            (f"client_secret: {short}x", "client_secret: <redacted>"),
            ('body {"password": "hunter22x"}', 'body {"password": "<redacted>"}'),  # gitleaks:allow (fake value)
            ("{'access_token': 'abc123def456'}", "{'access_token': '<redacted>'}"),  # gitleaks:allow (fake value)
            ("mail someone@mail.example now", "mail <email> now"),
            (f"id {token} end", "id <redacted> end"),
            ("Garmin rate limit: wait", "Garmin rate limit - wait"),
            ("RATE LIMIT: wait", "rate limit - wait"),
            ("rate  limit: wait", "rate limit - wait"),
            ("rate\u00a0limit\u200b: wait", "rate limit - wait"),
            ("a\nb\r\nc\x00d\U0000202ee\U00002066f\tg", "a b cdef g"),
            ("  padded  ", "padded"),
        )
        for raw, want in cases:
            with self.subTest(ascii(raw[:40])):
                self.assertEqual(te.redact(raw), want)

    def test_key_values_with_any_characters(self):
        """The value runs to a delimiter, and a key may end a longer name."""
        hexes = secrets.token_hex(5)
        odd = "p@ss!" + secrets.token_hex(3) + "#99"
        cases = (
            (f"password={odd}", "password=<redacted>"),
            (f"password=!{hexes}", "password=<redacted>"),
            ('{"password": "%s"}' % odd, '{"password": "<redacted>"}'),
            (f"oauth_token={hexes}", "oauth_token=<redacted>"),
            (f"oauth_token_secret={hexes}&x=1", "oauth_token_secret=<redacted>&x=1"),
            (f"di_refresh_token={hexes}", "di_refresh_token=<redacted>"),
            (f"accessToken={hexes}", "accessToken=<redacted>"),
            ('{\\"access_token\\": \\"%s\\"}' % hexes, '{\\"access_token\\": \\"<redacted>"}'),
            (f"https://connectapi.example.invalid/x?token={hexes}&a=1",
             "https://connectapi.example.invalid/x?token=<redacted>&a=1"),
            ("Error: refresh token expired", "Error: refresh token expired"),
            ("OAuth1 token exchange failed", "OAuth1 token exchange failed"),
        )
        for raw, want in cases:
            with self.subTest(want):
                self.assertEqual(te.redact(raw), want)
                for secret in (hexes, odd[1:]):
                    self.assertNotIn(secret, te.redact(raw))

    def test_linear_time(self):
        """The old address pattern took 32 s for 100,000 word characters (quadratic);
        each of these takes milliseconds. A large limit turns the input cut off, so the patterns alone are timed."""
        runs = ("Error retrieving stats: " + "a" * 200_000, "x " + "a." * 100_000, "x " + "a+" * 100_000,
                "a@" * 100_000, "token=" * 30_000, "bearer " * 30_000, ("a" * 38 + " ") * 5_000)
        for text in runs:
            start = time.perf_counter()
            out = te.redact(text, limit=10 ** 7)
            with self.subTest(ascii(text[:30])):
                self.assertLess(time.perf_counter() - start, 2.0)
                self.assertNotIn("  ", out)
        for text, limit in (("a" * 1_000_000, 2000), ("x" * 1_000_000, 64), (chr(0x5D0) * 1_000_000, 2000),
                            ("a " * 2_000_000, 2000)):
            start = time.perf_counter()
            out = te.redact(text, limit=limit)
            with self.subTest((ascii(text[:3]), limit)):
                self.assertLess(time.perf_counter() - start, 0.5)
                self.assertLessEqual(len(out), limit)

    def test_cut_input_never_keeps_part_of_a_secret(self):
        """The input is cut to 4 x limit + 256 characters before the patterns; the text after the last space, quote
        or & , ; < > is dropped, so a secret the patterns would no longer recognise (part of an address, a short
        start of a token) is gone, with or without spaces in the text."""
        token = "TEST-ONLY-" + secrets.token_urlsafe(48)
        bound = 4 * 2000 + 256
        for secret in (token, "someone.name@mail.example"):
            for keep in (5, 12, 20):  # characters of the secret before the cut
                # zero-width characters vanish, so the cut text is short and the split word would survive the cap
                raw = "Garmin said: " + chr(0x200B) * (bound - 13 - keep) + secret + " tail"
                out = te.redact(raw)
                with self.subTest((secret[:8], keep)):
                    self.assertTrue(out.startswith("Garmin said:"), out)
                    self.assertNotIn(secret[:keep], out)
                    self.assertTrue(out.endswith(th.ELLIPSIS))
                # a minified JSON body has no space: the long blob shrinks to <redacted>, the cut value still goes
                raw = '{"blob":"' + "z" * (bound - 20 - keep) + '","value":"' + secret + '"}'
                out = te.redact(raw)
                with self.subTest(("no space", secret[:8], keep)):
                    self.assertEqual(out, '{"blob":"<redacted>","value":"' + th.ELLIPSIS)
                    self.assertNotIn(secret[:keep], out)
        self.assertEqual(te.redact("x" * 10_000), th.ELLIPSIS)  # one cut run with no stop: all of it goes
        self.assertEqual(te.redact("Error: " + "x" * 10_000), "Error: " + th.ELLIPSIS)

    def test_api_paths_and_urls_stay(self):
        for path in ("/usersummary-service/usersummary/daily/MyName",
                     "/wellness-service/wellness/dailySleepData/MyName",
                     "https://connectapi.garmin.com/activitylist-service/activities/search/activities?limit=20"):
            with self.subTest(path):
                self.assertEqual(te.redact(f"Internal error for {path}"), f"Internal error for {path}")

    def test_cap_and_inputs(self):
        long_line = te.redact("word " * 3000)
        self.assertEqual(len(long_line), 2000)
        self.assertTrue(long_line.endswith(th.ELLIPSIS))
        self.assertEqual(te.redact("abc def", limit=5), "abc" + th.ELLIPSIS)
        self.assertEqual(te.redact("abc def", limit=0), "")
        self.assertEqual(te.redact("abc def", limit="5"), "abc def")
        self.assertEqual(te.redact(ValueError("x\ny")), "x y")
        self.assertEqual(te.redact(None), "None")
        self.assertEqual(te.redact(42), "42")

    def test_one_line_and_no_rate_limit_colon(self):
        rng = random.Random(4242)
        pieces = ("rate", "limit", ":", " ", "  ", "\n", "\u200b", "\u00a0", "\u202e", "RATE", "Limit", "x", "\t",
                  "\x85", "\U00002028", "token=", "Bearer ", "a@b.example", "-")
        for _ in range(3000):
            raw = "".join(rng.choice(pieces) for _ in range(rng.randint(0, 14)))
            out = te.redact(raw)
            self.assertNotIn("rate limit:", out.lower(), ascii(raw))
            self.assertIsNone(re.search(r"[\r\n\x85\u2028\u2029]", out), ascii(raw))
            self.assertNotIn("  ", out, ascii(raw))


KEY_MODE_TEXTS = {
    te.LOGIN: "Error: Garmin login missing or expired. This server cannot read Garmin data until its owner makes a "
              "new Personal API key in Garmin Connect and sets it on the server. Retrying will not help until then.",
    te.NO_DATA: "No data: Garmin has nothing for this request. Check the id or the date (ids come from get_activities "
                "or get_activities_by_date); repeating the same call will not help.",
}


def fake_api_key():
    """CI's fake Personal API key form : "fk!" and 24 hex characters, made at run time."""
    return "fk!" + secrets.token_hex(12)


class KeyModeClientText(unittest.TestCase):
    """Key mode changes the LOGIN and NO_DATA texts only, after their first words."""

    def test_texts(self):
        for group in te.GROUPS:
            with self.subTest(group):
                text = te.client_text(group, ref=REF, key_mode=True)
                if group in KEY_MODE_TEXTS:
                    self.assertEqual(text, KEY_MODE_TEXTS[group])
                    full = CLIENT_TEXTS[group]
                    self.assertEqual(text.split(".")[0], full.split(".")[0])
                else:
                    self.assertEqual(text, te.client_text(group, ref=REF))
                self.assertTrue(text.isascii() and text.isprintable())
                self.assertLessEqual(len(text), 300)
                self.assertIn(te.GROUP_PHRASE[group], text)
                self.assertEqual(text.startswith("Error: "), te.IS_ERROR[group])
                self.assertEqual(te.classify_text(text)[0], group)
                self.assertEqual(te.client_text(group, ref=REF, key_mode=False), CLIENT_TEXTS[group])
        self.assertEqual(len(KEY_MODE_TEXTS[te.LOGIN]), 200)
        self.assertEqual(len(KEY_MODE_TEXTS[te.NO_DATA]), 167)
        for name in ("get_scheduled_workouts", "get_courses", "garmin-mcp-auth"):
            self.assertNotIn(name, KEY_MODE_TEXTS[te.LOGIN] + KEY_MODE_TEXTS[te.NO_DATA])
        self.assertEqual(te.client_text(te.BAD_INPUT, detail=te.DATE_DETAIL, today="2026-11-16", key_mode=True),
                         te.client_text(te.BAD_INPUT, detail=te.DATE_DETAIL, today="2026-11-16"))
        for group in ("nope", None):
            with self.assertRaises(ValueError):
                te.client_text(group, key_mode=True)
        with self.assertRaises(ValueError):
            te.client_text(te.INTERNAL, key_mode=True)

    def test_source_phrases_and_modules(self):
        # Source phrases are the same in both modes (status_common computes its systemic set at import).
        self.assertEqual([te.source_phrase(g) for g in te.GROUPS],
                         ["Garmin login missing or expired", "Garmin rate limit", "invalid input",
                          "no data from Garmin", "Garmin Connect did not answer", "internal error"])
        self.assertIn("garmin_personal_api", te._GARMIN_MODULES)
        # An exception of Garmin's client that slipped past key_mode's mapping is read by the table (eligible).
        slipped = garmin_error("GarminPersonalApiError", "the request timed out", module="garmin_personal_api.errors")
        self.assertEqual(te.classify_exception(slipped)[0], te.UNAVAILABLE)
        self.assertEqual(te.classify_exception(RuntimeError("the request timed out"))[0], te.INTERNAL)


class RegisterSecret(unittest.TestCase):
    """Key mode's redaction: register_secret, the ApiKey row and api_?key."""

    def setUp(self):
        self.saved = te._SECRETS

    def tearDown(self):
        te._SECRETS = self.saved

    def test_forms(self):
        key = fake_api_key()
        self.assertEqual(len(key), 27)
        te.register_secret(key)
        cases = (
            (f"Authorization: ApiKey {key}", "Authorization: ApiKey <redacted>"),
            (f"GARMIN_PERSONAL_API_KEY={key}", "GARMIN_PERSONAL_API_KEY=<redacted>"),
            (f'{{"apiKey": "{key}"}}', '{"apiKey": "<redacted>"}'),
            (f"key {key} again {key}", "key <redacted> again <redacted>"),
            (f"x{key}y", "x<redacted>y"),
            (f"a{chr(0x200B)}{key}", "a<redacted>"),
        )
        for raw, want in cases:
            with self.subTest(raw[:20]):
                self.assertEqual(te.redact(raw), want)

    def test_patterns_without_registration(self):
        key = fake_api_key()
        for raw, want in ((f"Authorization: ApiKey {key}", "Authorization: ApiKey <redacted>"),
                          (f"authorization: apikey   {key}, next", "authorization: ApiKey <redacted>, next"),
                          (f"GARMIN_PERSONAL_API_KEY={key}", "GARMIN_PERSONAL_API_KEY=<redacted>"),
                          (f"api_key: {key}", "api_key: <redacted>"),
                          (f'{{\\"apiKey\\": \\"{key}\\"}}', '{\\"apiKey\\": \\"<redacted>"}')):
            with self.subTest(raw[:20]):
                self.assertEqual(te.redact(raw), want)
        # A bare key is left alone unless registered: no generic pattern catches 27 characters.
        self.assertIn(key, te.redact(f"oops {key}"))

    def test_short_values_and_non_strings_are_ignored(self):
        for value in ("", "short", "1234567", None, 12345678, b"bytes-value-here", ["list-value"]):
            te.register_secret(value)
        self.assertEqual(te._SECRETS, self.saved)
        self.assertEqual(te.redact("short words 1234567 stay"), "short words 1234567 stay")
        te.register_secret("12345678")
        self.assertEqual(te.redact("a 12345678 b"), "a <redacted> b")
        te.register_secret("12345678")
        self.assertEqual(te._SECRETS.count("12345678"), 1)

    def test_longest_first_and_cut_input(self):
        key = fake_api_key()
        te.register_secret(key[:10])
        te.register_secret(key)
        self.assertEqual(te.redact(f"[{key}]"), "[<redacted>]")
        long_text = ("x " * 3000) + key + " tail"
        self.assertNotIn(key, te.redact(long_text, limit=10000))
        self.assertNotIn(key[:10], te.redact(long_text, limit=10000))


@unittest.skipUnless(HAVE_UPSTREAM, "needs upstream garmin_mcp")
class UpstreamTexts(unittest.TestCase):
    """An upstream bump that rewords these texts fails here."""

    @staticmethod
    def source(*names):
        return "\n".join(inspect.getsource(importlib.import_module(name)) for name in names)

    def test_texts_still_exist(self):
        upstream = self.source("garmin_mcp", "garmin_mcp.training", "garmin_mcp.health_wellness",
                               "garmin_mcp.nutrition", "garmin_mcp.activity_analysis")
        for text in te.PLAIN_FAILURE_PREFIXES + te.UPSTREAM_HINTS + te.ECHO_FAILURE_PREFIXES:
            with self.subTest(text):
                self.assertIn(text, upstream)
        # Each function that answers the FIT failure text runs int(activity_id) first, which the FIT rule of
        # classify_text() relies on (a numeric id is written back as digits).
        fit = self.source("garmin_mcp.activity_analysis")
        error = 'f"Error downloading FIT data for activity {activity_id}: {str(e)}"'
        ends = [m.start() for m in re.finditer(re.escape(error), fit)]
        self.assertTrue(ends)
        for end in ends:
            body = fit[fit.rfind("async def ", 0, end):end]
            self.assertLess(body.find("activity_id = int(activity_id)"), body.find("try:") + 40)
            self.assertGreater(body.find("activity_id = int(activity_id)"), body.find("try:"))
        init = self.source("garmin_mcp")
        for text in ("Garmin authentication failed", "Garmin rate limit hit", "Garmin Connect request failed",
                     "Garmin login failed. Run 'garmin-mcp-auth' to ", "Garmin login did not finish within",
                     "MFA required but non-interactive environment"):
            with self.subTest(text):
                self.assertIn(text, init)

    def test_their_groups(self):
        for text, group in (("Garmin authentication failed: x. Re-run 'garmin-mcp-auth' to refresh your tokens and "
                             "restart the server.", te.LOGIN),
                            ("Garmin rate limit hit: x. Wait a few minutes before retrying.", te.RATE_LIMITED),
                            (PENDING_LOGIN, te.LOGIN), (LOGIN_WAIT, te.LOGIN),
                            ("MFA required but non-interactive environment", te.LOGIN)):
            with self.subTest(text[:40]):
                self.assertEqual(te.classify_text(text)[0], group)


if __name__ == "__main__":
    unittest.main()
