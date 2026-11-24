"""Tests for key_mode (GARMIN_AUTH=key; KEY contract tests K1, K3-K6, K9, K11, K16-K18).

Fakes only, no network. Run from ext/:

    python3 -m unittest test_key_mode -v

The tests that need Garmin's own client (garmin-connect-personal-api-python, Python 3.12) or upstream garmin_mcp
skip on the stdlib leg with a reason starting "needs "; CI's full leg installs both and allows no skip.
"""

import ast
import base64
import contextlib
import copy
import importlib.util
import io
import hashlib
import json
import logging
import os
import re
import secrets
import stat
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import types
import unittest
from unittest import mock

import key_mode as km
import status_common as sc
import tool_errors as te

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
HAVE_CLIENT = importlib.util.find_spec("garmin_personal_api") is not None
HAVE_UPSTREAM = (importlib.util.find_spec("garmin_mcp") is not None
                 and importlib.util.find_spec("garminconnect") is not None)
NEEDS_CLIENT = "needs garmin-connect-personal-api-python (Python 3.12)"
NEEDS_UPSTREAM = "needs upstream garmin_mcp and garminconnect"
KEY_LOG = logging.getLogger("garmin_ext.key_mode")
DAY = "2026-11-10"
NOW = 1_800_000_000.0


def fake_key():
    """CI's fake key form: "fk!" and 24 hex characters (27 characters, passes KEY_RE, no generic pattern)."""
    return "fk!" + secrets.token_hex(12)


def _seg(data):
    return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")


def jwt(exp):
    """A fake JWT with an exp claim, made at run time."""
    return f"{_seg({'alg': 'none', 'typ': 'JWT'})}.{_seg({'exp': exp, 'n': secrets.token_hex(8)})}." \
           f"{secrets.token_urlsafe(24)}"


class Clock:
    def __init__(self, t=NOW):
        self.t = t

    def __call__(self):
        return self.t


# --- fakes of Garmin's client and of requests ----------------------------------------------------------------

class GarminPersonalApiError(Exception):
    pass


class HttpError(GarminPersonalApiError):
    """Same shape as garmin_personal_api.errors.HttpError: keyword arguments, the body in the message."""

    def __init__(self, *, status_code, method, url, response_text):
        self.status_code, self.method, self.url, self.response_text = status_code, method, url, response_text
        super().__init__(f"{method} {url} returned HTTP {status_code}: {response_text}")


def requests_error(name, message, bases=(OSError,)):
    """An exception of a class from requests' module (ConnectionError, Timeout, InvalidHeader, ...)."""
    return type(name, bases, {"__module__": "requests.exceptions"})(message)


class Bearer:
    def __init__(self, access_token, expires_at):
        self.access_token, self.expires_at = access_token, expires_at


class FakeAuth:
    """PersonalApiAuthenticator's behaviour: the cache file first (same key, more than 1 h left), else one token
    request answered by `answers` (a (token, exp) pair or an exception), written to the cache file 0600."""

    def __init__(self, cache, clock, answers=(), delay=0.0):
        self.cache, self.clock, self.answers, self.delay = cache, clock, list(answers), delay
        self.posts = 0
        self.lock = threading.Lock()

    def obtain_token(self, api_key):
        try:
            with open(self.cache, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            data = None
        if data and data.get("key") == api_key and data["expires_at"] - self.clock() > 3600:
            return Bearer(data["access_token"], data["expires_at"])
        with self.lock:
            self.posts += 1
        if self.delay:
            time.sleep(self.delay)
        answer = self.answers.pop(0) if self.answers else (jwt(self.clock() + 86400), self.clock() + 86400)
        if isinstance(answer, BaseException):
            raise answer
        token, exp = answer
        os.makedirs(os.path.dirname(self.cache), exist_ok=True)
        fd = os.open(self.cache, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"key": api_key, "access_token": token, "expires_at": exp}, fh)
        return Bearer(token, exp)


class FakeHttp:
    """KeyModeGarmin's HTTP layer: records ("GET", path, params) and ("DOWNLOAD", id); `answer` gives each result
    (a value, or an exception to raise)."""

    def __init__(self, answer=None):
        self.calls = []
        self.answer = answer or (lambda kind, path, params: {"ok": True})
        self.tokens = []

    def get(self, token, path, params):
        self.calls.append(("GET", path, params))
        self.tokens.append(token)
        return self._result(self.answer("GET", path, params))

    def download(self, token, activity_id):
        self.calls.append(("DOWNLOAD", activity_id, None))
        self.tokens.append(token)
        return self._result(self.answer("DOWNLOAD", activity_id, None))

    @staticmethod
    def _result(value):
        if isinstance(value, BaseException):
            raise value
        return value


class FixedTokens:
    """A TokenManager that always has `token`; records refused() calls."""

    def __init__(self, token=None):
        self.value = token or jwt(NOW + 86400)
        self.refusals = []

    def token(self, *, first=False):
        return self.value

    def refused(self, token):
        self.refusals.append(token)


def adapter(answer=None):
    http = FakeHttp(answer)
    return km.KeyModeGarmin(FixedTokens(), http), http


class Capture(logging.Handler):
    """Rendered records (message and traceback) of the loggers it is attached to."""

    def __init__(self):
        super().__init__(logging.DEBUG)
        self.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
        self.lines = []

    def emit(self, record):
        self.lines.append(self.format(record))


@contextlib.contextmanager
def captured(logger_name=""):
    """Every record of `logger_name` (root by default) rendered with a real Formatter, tracebacks included."""
    logger = logging.getLogger(logger_name)
    handler = Capture()
    saved = (logger.level, logger.propagate)
    key_saved = (KEY_LOG.level, KEY_LOG.propagate, KEY_LOG.handlers[:])
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    if logger_name != "garmin_ext.key_mode":
        KEY_LOG.handlers, KEY_LOG.propagate = [], True
        KEY_LOG.setLevel(logging.DEBUG)
    try:
        yield handler.lines
    finally:
        logger.removeHandler(handler)
        logger.setLevel(saved[0])
        logger.propagate = saved[1]
        KEY_LOG.setLevel(key_saved[0])
        KEY_LOG.propagate = key_saved[1]
        KEY_LOG.handlers = key_saved[2]


class TempDir:
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.cache = os.path.join(self.tmp, "data", "garmin-key", "token.json")

    def tearDown(self):
        for root, dirs, _files in os.walk(self.tmp):
            for d in dirs:
                with contextlib.suppress(OSError):
                    os.chmod(os.path.join(root, d), 0o700)
        self._tmp.cleanup()


def setUpModule():
    # Key mode logs its token lines; keep the run quiet (the tests attach their own handlers).
    global _saved
    _saved = (KEY_LOG.handlers[:], KEY_LOG.propagate)
    KEY_LOG.handlers, KEY_LOG.propagate = [logging.NullHandler()], False


def tearDownModule():
    KEY_LOG.handlers, KEY_LOG.propagate = _saved


# --- K1: the config matrix -----------------------------------------------------------------------------------

class Config(TempDir, unittest.TestCase):
    def key_env(self, **changes):
        env = {"GARMIN_AUTH": "key", "GARMIN_PERSONAL_API_KEY": fake_key(), "GARMIN_KEY_TOKEN_CACHE": self.cache}
        env.update(changes)
        return {k: v for k, v in env.items() if v is not None}

    def full_env(self, **changes):
        env = {"GARMIN_AUTH": "full"}
        env.update(changes)
        return {k: v for k, v in env.items() if v is not None}

    def lines(self, env):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            lines = km.check(env)
        self.assertEqual((out.getvalue(), err.getvalue()), ("", ""))
        for line in lines:
            for name in ("GARMIN_PERSONAL_API_KEY", "GARMIN_PERSONAL_API_BASE_URL", "GARMIN_AUTH"):
                value = env.get(name, "").strip()
                if len(value) > 4 and value.lower() not in ("full", "key"):
                    self.assertNotIn(value, line)
        return lines

    def test_values_are_never_shown(self):
        sentinel = "S" + secrets.token_hex(10)
        for env in (self.key_env(GARMIN_KEY_TOKEN_CACHE=f"/data/{sentinel}.json"),
                    self.key_env(GARMIN_KEY_TOKEN_CACHE=f"/data/garmin-key/../{sentinel}.json"),
                    self.key_env(GARMIN_AUTH=sentinel), self.key_env(GARMIN_PERSONAL_API_KEY=sentinel + " x"),
                    self.full_env(GARMIN_PERSONAL_API_KEY=sentinel),
                    self.key_env(GARMIN_PERSONAL_API_BASE_URL=f"https://{sentinel}.example")):
            lines = self.lines(env)
            self.assertEqual(len(lines), 1)
            self.assertNotIn(sentinel, lines[0])

    def test_auth_mode(self):
        for value, want in ((None, None), ("", None), ("   ", None), (" KEY ", "key"), ("Full", "full"),
                            ("key", "key"), ("sideways", None), ("full key", None)):
            env = {} if value is None else {"GARMIN_AUTH": value}
            with self.subTest(repr(value)):
                self.assertEqual(km.auth_mode(env), want)

    def test_garmin_auth_lines(self):
        for value in (None, "", "   ", "\t"):
            with self.subTest(repr(value)):
                self.assertEqual(self.lines({} if value is None else {"GARMIN_AUTH": value}), [km.ERR_AUTH_UNSET])
        junk = "sideways-" + secrets.token_hex(4)
        lines = self.lines({"GARMIN_AUTH": junk})
        self.assertEqual(lines, [km.ERR_AUTH_INVALID])
        self.assertNotIn(junk, lines[0])
        self.assertEqual(self.lines(self.key_env(GARMIN_AUTH=" KEY ")), [])
        self.assertEqual(self.lines(self.full_env(GARMIN_AUTH="Full")), [])

    def test_key_lines(self):
        for value in (None, "", "   "):
            with self.subTest(repr(value)):
                self.assertEqual(self.lines(self.key_env(GARMIN_PERSONAL_API_KEY=value)), [km.ERR_KEY_MISSING])
                self.assertEqual(self.lines(self.full_env(GARMIN_PERSONAL_API_KEY=value)), [])
        key = fake_key()
        self.assertEqual(self.lines(self.key_env(GARMIN_PERSONAL_API_KEY="  " + key + "\n")), [])
        self.assertEqual(self.lines(self.key_env(GARMIN_PERSONAL_API_KEY="x" * 512)), [])
        for bad in (key[:5] + " " + key[5:], key[:5] + "\n" + key[5:], key + chr(0xE9), "x" * 513,
                    key + chr(0x200B)):
            with self.subTest(ascii(bad[-3:])):
                self.assertEqual(self.lines(self.key_env(GARMIN_PERSONAL_API_KEY=bad)), [km.ERR_KEY_INVALID])
        self.assertEqual(self.lines(self.full_env(GARMIN_PERSONAL_API_KEY=key)), [km.ERR_KEY_UNUSED])

    def test_a_key_under_20_characters_is_refused_as_too_short(self):
        """Live test B1: a paste that did not land stored a 1-character key; Garmin's 401 came only at start."""
        self.assertEqual(self.lines(self.key_env(GARMIN_PERSONAL_API_KEY="x" * km.KEY_MIN)), [])
        for short in ("x", "v", "x" * (km.KEY_MIN - 1), "  ab!c  "):
            with self.subTest(len(short)):
                self.assertEqual(self.lines(self.key_env(GARMIN_PERSONAL_API_KEY=short)), [km.ERR_KEY_SHORT])
                self.assertFalse(km.key_ok(short.strip()))
                self.assertTrue(km.key_too_short(short.strip()))
        # A short value with a character no key has is "invalid", not "short"; a long one is never "short".
        self.assertEqual(self.lines(self.key_env(GARMIN_PERSONAL_API_KEY="a b")), [km.ERR_KEY_INVALID])
        self.assertFalse(km.key_too_short("x" * km.KEY_MIN))
        self.assertFalse(km.key_too_short("x" * 513))
        self.assertFalse(km.key_too_short(None))
        self.assertEqual((km.KEY_MIN, km.KEY_MAX), (20, 512))
        self.assertIn("shorter than 20 characters", km.ERR_KEY_SHORT)
        with self.assertRaises(ValueError):
            km.install("x" * (km.KEY_MIN - 1), "/tmp/garmin-key/token.json", authenticator=object(), http=object())

    def test_base_url_and_cn(self):
        url = "https://evil-" + secrets.token_hex(4) + ".example"
        self.assertEqual(self.lines(self.key_env(GARMIN_PERSONAL_API_BASE_URL=url)), [km.ERR_BASE_URL])
        self.assertEqual(self.lines(self.full_env(GARMIN_PERSONAL_API_BASE_URL=url)), [km.ERR_BASE_URL])
        self.assertEqual(self.lines({"GARMIN_PERSONAL_API_BASE_URL": url}), [km.ERR_AUTH_UNSET, km.ERR_BASE_URL])
        for empty in ("", "  "):
            self.assertEqual(self.lines(self.key_env(GARMIN_PERSONAL_API_BASE_URL=empty)), [])
            self.assertEqual(self.lines(self.full_env(GARMIN_PERSONAL_API_BASE_URL=empty)), [])
        for true in ("true", " TRUE ", "1", "yes"):
            self.assertEqual(self.lines(self.key_env(GARMIN_IS_CN=true)), [km.ERR_IS_CN])
            self.assertEqual(self.lines(self.full_env(GARMIN_IS_CN=true)), [])
        self.assertEqual(self.lines(self.key_env(GARMIN_IS_CN="false")), [])

    def test_cache_path_rule(self):
        for bad in ("token.json", "garmin-key/token.json", "/x.json", "/tmp/x.json", "/data/x.json",
                    "/data/garmin-key/../x.json", "/data/garmin-key/token.txt", "/data/garmin-key/",
                    "/data//garmin-key/token.json", "/data/./garmin-key/token.json", "/data/garmin-key/.json",
                    "/data/garmin-key/tok\nen.json", " /data/garmin-key/token.json"):
            with self.subTest(ascii(bad)):
                env = self.key_env(GARMIN_KEY_TOKEN_CACHE=bad)
                self.assertIsNone(km.cache_path(env))
                self.assertEqual(self.lines(env), [km.ERR_CACHE_PATH])
                self.assertEqual(self.lines(self.full_env(GARMIN_KEY_TOKEN_CACHE=bad)), [])
        self.assertEqual(km.cache_path(self.key_env()), self.cache)

    def test_cache_folder_is_made_private(self):
        self.assertEqual(self.lines(self.key_env()), [])
        folder = os.path.dirname(self.cache)
        self.assertEqual(stat.S_IMODE(os.stat(folder).st_mode), 0o700)
        self.assertEqual(os.listdir(folder), [])
        os.chmod(folder, 0o755)
        self.assertEqual(self.lines(self.key_env()), [])
        self.assertEqual(stat.S_IMODE(os.stat(folder).st_mode), 0o700)

    def test_default_cache_path(self):
        home = os.path.join(self.tmp, "home")
        with mock.patch.dict(os.environ, {"HOME": home}):
            for env in ({}, {"GARMIN_KEY_TOKEN_CACHE": ""}, {"GARMIN_KEY_TOKEN_CACHE": "  "}):
                self.assertEqual(km.cache_path(env), os.path.join(home, ".config", "garmin-key", "token.json"))

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores file modes")
    def test_cache_folder_not_writable(self):
        parent = os.path.join(self.tmp, "ro")
        os.makedirs(parent)
        os.chmod(parent, 0o500)
        path = os.path.join(parent, "garmin-key", "token.json")
        lines = self.lines(self.key_env(GARMIN_KEY_TOKEN_CACHE=path))
        self.assertEqual(lines, [km.ERR_CACHE_WRITE.format(folder=os.path.dirname(path),
                                                           strerror="Permission denied")])

    def test_order(self):
        env = self.key_env(GARMIN_PERSONAL_API_KEY="bad key", GARMIN_PERSONAL_API_BASE_URL="https://x.example",
                           GARMIN_IS_CN="true", GARMIN_KEY_TOKEN_CACHE="/tmp/x.json")
        self.assertEqual(self.lines(env), [km.ERR_KEY_INVALID, km.ERR_BASE_URL, km.ERR_IS_CN, km.ERR_CACHE_PATH])
        env = self.full_env(GARMIN_PERSONAL_API_KEY=fake_key(), GARMIN_PERSONAL_API_BASE_URL="https://x.example")
        self.assertEqual(self.lines(env), [km.ERR_KEY_UNUSED, km.ERR_BASE_URL])

    def test_texts(self):
        self.assertEqual(km.ERR_AUTH_UNSET, "GARMIN_AUTH is not set: set it to full (log in to Garmin once with your "
                         "email and password; every tool) or key (a Garmin Personal API key; fewer tools and less "
                         "data), then restart the server")
        self.assertTrue(km.ERR_KEY_MISSING.startswith("GARMIN_AUTH=key needs GARMIN_PERSONAL_API_KEY"))
        self.assertEqual(km.ERR_TOOL_NOT_IN_KEY_MODE.format(name="get_training_status"),
                         "GARMIN_ENABLED_TOOLS: get_training_status is not available with GARMIN_AUTH=key; remove "
                         "it, or set GARMIN_AUTH=full")
        for name, value in vars(km).items():
            if name.startswith(("ERR_", "TEXT_", "LOG_")):
                self.assertTrue(value.isascii() and value.isprintable(), name)

    def test_take_key(self):
        key = fake_key()
        env = {"GARMIN_PERSONAL_API_KEY": " " + key + " ", "GARMIN_PERSONAL_API_BASE_URL": "", "OTHER": "x"}
        saved = te._SECRETS
        try:
            self.assertEqual(km.take_key(env), key)
            self.assertEqual(env, {"OTHER": "x"})
            self.assertNotIn(key, te.redact(f"oops {key} and {key}x"))
            self.assertEqual(km.take_key({}), "")
        finally:
            te._SECRETS = saved


# --- K3: the adapter's requests ------------------------------------------------------------------------------

class Adapter(unittest.TestCase):
    def test_paths_and_params(self):
        cases = (
            (lambda g: g.get_stats(DAY), ("GET", "/usersummary-service/usersummary/daily", {"calendarDate": DAY})),
            (lambda g: g.get_user_summary(" " + DAY),
             ("GET", "/usersummary-service/usersummary/daily", {"calendarDate": DAY})),
            (lambda g: g.get_sleep_data(DAY), ("GET", "/sleep-service/sleep/dailySleep", {"date": DAY})),
            (lambda g: g.get_hrv_data(DAY), ("GET", f"/hrv-service/hrv/{DAY}", None)),
            (lambda g: g.get_body_battery(DAY), ("GET", "/wellness-service/wellness/bodyBattery/reports/daily",
                                                 {"startDate": DAY, "endDate": DAY})),
            (lambda g: g.get_body_battery("2026-11-01", DAY),
             ("GET", "/wellness-service/wellness/bodyBattery/reports/daily", {"startDate": "2026-11-01",
                                                                              "endDate": DAY})),
            (lambda g: g.get_weigh_ins("2026-11-01", DAY),
             ("GET", f"/weight-service/weight/range/2026-11-01/{DAY}", {"includeAll": "true"})),
            (lambda g: g.get_activities(), ("GET", "/activitylist-service/activities/search/activities",
                                            {"start": "0", "limit": "20"})),
            (lambda g: g.get_activities(5, 10, "running"),
             ("GET", "/activitylist-service/activities/search/activities",
              {"start": "5", "limit": "10", "activityType": "running"})),
            (lambda g: g.get_activity(123), ("GET", "/activity-service/activity/123", None)),
            (lambda g: g.get_activity(" 1"), ("GET", "/activity-service/activity/1", None)),
            (lambda g: g.get_activity("1 "), ("GET", "/activity-service/activity/1", None)),
            # int() reads any Unicode digit, as garminconnect's own check does; the path holds int()'s ASCII form.
            (lambda g: g.get_activity(chr(0x661)), ("GET", "/activity-service/activity/1", None)),
            (lambda g: g.download_activity(77), ("DOWNLOAD", 77, None)),
            (lambda g: g.connectapi(g.garmin_connect_activities,
                                    params={"startDate": DAY, "endDate": DAY, "start": "0", "limit": "20",
                                            "activityType": "cycling"}),
             ("GET", "/activitylist-service/activities/search/activities",
              {"startDate": DAY, "endDate": DAY, "start": "0", "limit": "20", "activityType": "cycling"})),
            (lambda g: g.connectapi("activitylist-service/activities/search/activities", params={"start": 0}),
             ("GET", "/activitylist-service/activities/search/activities", {"start": "0"})),
            (lambda g: g.connectapi(f"{g.garmin_connect_metrics_url}/2026-11-01/{DAY}"),
             ("GET", f"/metrics-service/metrics/maxmet/daily/2026-11-01/{DAY}", None)),
            (lambda g: g.connectapi("/biometric-service/heartRateZones"),
             ("GET", "/biometric-service/heartRateZones/v2", None)),
        )
        for i, (fn, want) in enumerate(cases):
            with self.subTest(i):
                g, http = adapter(lambda kind, path, params: b"zip" if kind == "DOWNLOAD" else {"x": 1})
                fn(g)
                self.assertEqual(http.calls, [want])

    def test_answers_are_mapped(self):
        g, _ = adapter(lambda kind, path, params: {"sleepTimeSeconds": 1})
        self.assertEqual(g.get_sleep_data(DAY), {"dailySleepDTO": {"sleepTimeSeconds": 1}})
        g, _ = adapter(lambda kind, path, params: None)
        self.assertEqual(g.get_sleep_data(DAY), {})
        self.assertIsNone(g.get_hrv_data(DAY))
        self.assertEqual(g.get_activities(), [])
        with self.assertRaises(km.KeyModeNoData) as cm:
            g.get_stats(DAY)
        self.assertEqual(str(cm.exception), km.TEXT_EMPTY_ANSWER)
        with self.assertRaises(km.KeyModeNoData) as cm:
            km.KeyModeGarmin(FixedTokens(), FakeHttp(lambda *a: b"")).download_activity(1)
        self.assertEqual(str(cm.exception), km.TEXT_EMPTY_FILE)
        weights = {"dailyWeightSummaries": [
            {"summaryDate": "2026-11-01", "allWeightMetrics": [
                {"calendarDate": "2026-11-01", "weight": 70500.0, "bmi": 22.0},
                {"weight": 70400.0}, "junk"]},
            {"summaryDate": "2026-11-05", "allWeightMetrics": None}, "junk"]}
        g, http = adapter(lambda kind, path, params: weights)
        self.assertEqual(g.get_body_composition("2026-11-01", DAY), {"dateWeightList": [
            {"calendarDate": "2026-11-01", "weight": 70500.0}, {"calendarDate": "2026-11-01", "weight": 70400.0}]})
        self.assertEqual(http.calls, [("GET", f"/weight-service/weight/range/2026-11-01/{DAY}",
                                       {"includeAll": "true"})])
        g, _ = adapter(lambda kind, path, params: None)
        self.assertEqual(g.get_body_composition(DAY), {"dateWeightList": []})

    def test_empty_activity_search_is_an_empty_list(self):
        # R9: Garmin's client gives None for an empty answer (HTTP 204); connectapi's activity search maps it to
        # [] as get_activities does, so a rest day reads "no workouts logged", not "no data".
        g, http = adapter(lambda kind, path, params: None)
        self.assertEqual(g.connectapi(g.garmin_connect_activities, params={"startDate": DAY, "endDate": DAY}), [])
        self.assertEqual(http.calls, [("GET", "/activitylist-service/activities/search/activities",
                                       {"startDate": DAY, "endDate": DAY})])
        self.assertIsNone(g.connectapi("/biometric-service/heartRateZones"))  # the other paths are unchanged
        g, _ = adapter(lambda kind, path, params: [{"activityId": 1}])
        self.assertEqual(g.connectapi(g.garmin_connect_activities), [{"activityId": 1}])

    def test_injection_is_refused_before_any_request(self):
        ids = ("1/../../userprofile-service/userprofile/settings", "1?x=1", "1#x", "%2e%2e", "-1", "0", "1.5", "",
               "1 2", "1\n2")
        dates = ("2026-11-17/../x", "2026-11-17?x=1", "2026-11-17#x", "2026-13-01", "17-11-2026", "", "x",
                 "2026-11-17 /x")
        calls = []
        for bad in ids:
            calls += [("get_activity", (bad,)), ("download_activity", (bad,))]
        for bad in dates:
            calls += [("get_hrv_data", (bad,)), ("get_stats", (bad,)), ("get_user_summary", (bad,)),
                      ("get_sleep_data", (bad,)), ("get_body_battery", (bad,)), ("get_body_battery", (DAY, bad)),
                      ("get_weigh_ins", (bad, DAY)), ("get_weigh_ins", (DAY, bad)),
                      ("get_body_composition", (bad,)), ("get_body_composition", (DAY, bad)),
                      ("connectapi", (f"/metrics-service/metrics/maxmet/daily/{bad}/{DAY}",)),
                      ("connectapi", ("/activitylist-service/activities/search/activities",),
                       {"params": {"startDate": bad}})]
        for spec in calls:
            name, args = spec[0], spec[1]
            kwargs = spec[2] if len(spec) > 2 else {}
            with self.subTest((name, ascii(args))):
                g, http = adapter()
                with self.assertRaises((ValueError, km.NotInKeyMode)):
                    getattr(g, name)(*args, **kwargs)
                self.assertEqual(http.calls, [])

    def test_bad_input_texts(self):
        g, http = adapter()
        for fn, text in ((lambda: g.get_stats("2026-1-1"), "cdate must be in format 'YYYY-MM-DD', got: 2026-1-1"),
                         (lambda: g.get_activity("-1"), "activity_id must be a positive integer, got: -1"),
                         (lambda: g.get_activity("x"), "invalid literal for int() with base 10: 'x'"),
                         (lambda: g.get_activities(limit=1001), "limit cannot exceed 1000"),
                         (lambda: g.get_activities(start=-1), "start must be non-negative, got: -1"),
                         (lambda: g.get_activities(activitytype="run ning"),
                          "activity_type must be one of Garmin's activity type keys (letters, digits and _)"),
                         (lambda: g.get_body_composition(DAY, "2026-11-01"), "startdate cannot be after enddate")):
            with self.subTest(text), self.assertRaises(ValueError) as cm:
                fn()
            self.assertEqual(str(cm.exception), text)
            self.assertEqual(te.classify_text("Error retrieving x: " + text)[0], te.BAD_INPUT)
        self.assertEqual(http.calls, [])

    @unittest.skipUnless(HAVE_UPSTREAM, NEEDS_UPSTREAM)
    def test_validators_are_garminconnects(self):
        import garminconnect as gc

        self.assertIs(km._validators()[0], gc._validate_date_format)
        local, theirs = km._LOCAL_VALIDATORS, km._validators()
        for i, values in ((0, ("2026-11-10", " 2026-11-10", "2026-02-30", "x", 5, "2026-1-1")),
                          (1, (1, 0, -1, True, "1", 1.5)), (2, (0, -1, False, "0", 2.5))):
            for value in values:
                with self.subTest((i, value)):
                    def outcome(fn):
                        try:
                            return ("ok", fn(value, "p"))
                        except Exception as exc:  # noqa: BLE001
                            return (type(exc).__name__, str(exc))
                    self.assertEqual(outcome(local[i]), outcome(theirs[i]))
        self.assertEqual(local[3], theirs[3])

    def test_connectapi_refuses_everything_else(self):
        for path in ("/activitylist-service/activities/search/activities/", "/activity-service/activity/1",
                     "/activitylist-service/activities/../search/activities",
                     "//activitylist-service/activities/search/activities",
                     "/activitylist-service/activities/search/activities?x=1",
                     "/activitylist-service/activities/search/activities#x",
                     "/activitylist-service/activities/search%2Factivities",
                     "/biometric-service/heartRateZones/v2", "/userprofile-service/userprofile/settings",
                     "/course-service/course", "workout-service/fbt-adaptive/abc", "", 42, None,
                     "/metrics-service/metrics/maxmet/daily/2026-11-01", "/biometric-service/heartRateZones\n"):
            with self.subTest(ascii(path)):
                g, http = adapter()
                with self.assertRaises(km.NotInKeyMode):
                    g.connectapi(path)
                self.assertEqual(http.calls, [])
        g, http = adapter()
        for kwargs in ({"method": "POST"}, {"json": {}}, {"headers": {}}, {"params": {"x": "1"}},
                       {"params": "start=0"}):
            with self.subTest(kwargs), self.assertRaises(km.NotInKeyMode):
                g.connectapi(g.garmin_connect_activities, **kwargs)
        with self.assertRaises(km.NotInKeyMode):
            g.connectapi("/biometric-service/heartRateZones", params={"x": "1"})
        with self.assertRaises(km.NotInKeyMode):
            g.download_activity(1, dl_fmt=types.SimpleNamespace(name="TCX"))
        g.download_activity(1, dl_fmt=types.SimpleNamespace(name="ORIGINAL"))
        self.assertEqual(http.calls, [("DOWNLOAD", 1, None)])

    def test_other_names(self):
        g, _ = adapter()
        for name in ("get_training_status", "get_training_readiness", "query_garmin_graphql", "client",
                     "display_name", "get_devices", "get_activity_splits", "garmin_connect_activity"):
            with self.subTest(name):
                self.assertIsNone(getattr(g, name, None))
                with self.assertRaises(km.NotInKeyMode) as cm:
                    getattr(g, name)
                self.assertIsInstance(cm.exception, AttributeError)
                self.assertEqual(str(cm.exception), f"not available with a Garmin Personal API key: {name}")
                self.assertEqual(te.classify_exception(cm.exception)[0], te.INTERNAL)
                self.assertEqual(te.classify_text("Error retrieving x: " + str(cm.exception))[0], te.INTERNAL)
        with self.assertRaises(km.NotInKeyMode) as cm:
            g.connectapi("/x-service/(401)?" + "a" * 100)
        self.assertEqual(te.classify_exception(cm.exception)[0], te.INTERNAL)
        self.assertLessEqual(len(str(cm.exception)), len("not available with a Garmin Personal API key: ") + 60)
        for name in km.KeyModeGarmin._IMPLEMENTED:
            self.assertTrue(callable(getattr(g, name)))
        self.assertEqual(g.garmin_connect_activities, "/activitylist-service/activities/search/activities")
        self.assertEqual(g.garmin_connect_metrics_url, "/metrics-service/metrics/maxmet/daily")
        self.assertIsNone(copy.copy(g).__dict__.get("missing"))


# --- K4: the token manager -----------------------------------------------------------------------------------

class Tokens(TempDir, unittest.TestCase):
    def manager(self, answers=(), clock=None, delay=0.0, key=None):
        clock = clock or Clock()
        self.key = key or fake_key()
        auth = FakeAuth(self.cache, clock, answers, delay)
        km.prepare_cache(self.cache)
        return km.TokenManager(self.key, self.cache, authenticator=auth, clock=clock), auth, clock

    def assert_clean(self, lines, *secrets_):
        for line in lines:
            for secret in (self.key,) + secrets_:
                self.assertNotIn(secret, line)

    def request_lines(self, lines):
        markers = ("new access token", "access token from the cache", "Garmin refused the Personal API key",
                   "first token request failed", "token request failed")
        return [line for line in lines if any(m in line for m in markers)]

    def test_one_request_for_ten_threads(self):
        tokens, auth, _ = self.manager(delay=0.05)
        got = []
        with captured("garmin_ext.key_mode") as lines:
            threads = [threading.Thread(target=lambda: got.append(tokens.token())) for _ in range(10)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual(auth.posts, 1)
        self.assertEqual(len(set(got)), 1)
        self.assertEqual(len(got), 10)
        self.assertEqual(len(self.request_lines(lines)), 1)
        self.assertIn("INFO garmin_ext.key_mode garmin key mode: new access token, valid until 2027-01-16T", lines[0])
        self.assert_clean(lines, got[0])

    def test_cache_modes_and_reuse(self):
        tokens, auth, clock = self.manager()
        first = tokens.token()
        folder = os.path.dirname(self.cache)
        self.assertEqual(stat.S_IMODE(os.stat(folder).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(self.cache).st_mode), 0o600)
        # A restart (a new manager on the same file) reuses the cached token: no request.
        again = km.TokenManager(self.key, self.cache, authenticator=auth, clock=clock)
        with captured("garmin_ext.key_mode") as lines:
            self.assertEqual(again.token(first=True), first)
        self.assertEqual(auth.posts, 1)
        self.assertEqual(len(lines), 1)
        self.assertIn("garmin key mode: access token from the cache, valid until ", lines[0])
        # A wider file is fixed at start, with one line.
        os.chmod(self.cache, 0o644)
        os.chmod(folder, 0o755)
        with captured("garmin_ext.key_mode") as lines:
            km.prepare_cache(self.cache)
        self.assertEqual(stat.S_IMODE(os.stat(self.cache).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(folder).st_mode), 0o700)
        self.assertEqual(lines, ["INFO garmin_ext.key_mode garmin key mode: token cache file mode fixed to 0600"])
        # Less than an hour left: a new request.
        clock.t += 86400 - 3000
        self.assertNotEqual(tokens.token(), first)
        self.assertEqual(auth.posts, 2)

    def test_data_401_drops_the_token_and_retries_once(self):
        tokens, auth, _ = self.manager()
        statuses = [401]

        def answer(kind, path, params):
            if statuses:
                return HttpError(status_code=statuses.pop(0), method="GET", url="u", response_text="b")
            return {"ok": True}

        http = FakeHttp(answer)
        g = km.KeyModeGarmin(tokens, http)
        with captured("garmin_ext.key_mode") as lines:
            self.assertEqual(g.get_stats(DAY), {"ok": True})
        self.assertEqual(auth.posts, 2)
        self.assertEqual(len(http.calls), 2)
        self.assertNotEqual(http.tokens[0], http.tokens[1])
        self.assertIn("garmin key mode: Garmin refused the access token (HTTP 401); asking for a new one",
                      "\n".join(lines))
        self.assertEqual(len(self.request_lines(lines)), 2)
        self.assert_clean(lines, *http.tokens)
        # Two 401s in a row: the login error, after one new token.
        statuses[:] = [401, 401]
        with self.assertRaises(km.KeyModeLoginError) as cm:
            g.get_stats(DAY)
        self.assertEqual(str(cm.exception), km.TEXT_TOKEN_REFUSED)
        self.assertEqual(auth.posts, 3)

    def test_401_deletes_the_cache_file(self):
        tokens, auth, _ = self.manager()
        token = tokens.token()
        self.assertTrue(os.path.exists(self.cache))
        tokens.refused("some-other-token")
        self.assertTrue(os.path.exists(self.cache))
        tokens.refused(token)
        self.assertFalse(os.path.exists(self.cache))
        self.assertNotEqual(tokens.token(), token)
        self.assertEqual(auth.posts, 2)

    def test_refused_key_latch(self):
        body = "BODY-" + secrets.token_hex(8)
        for code in (400, 401, 403):
            with self.subTest(code):
                refusal = HttpError(status_code=code, method="POST", url="u", response_text=body)
                tokens, auth, clock = self.manager([refusal])
                with captured("garmin_ext.key_mode") as lines:
                    with self.assertRaises(km.KeyModeLoginError) as cm:
                        tokens.token(first=True)
                    self.assertEqual(str(cm.exception), km.TEXT_KEY_REFUSED.format(code=code))
                    clock.t += 899
                    with self.assertRaises(km.KeyModeLoginError) as cm2:
                        tokens.token()
                self.assertEqual(str(cm2.exception), str(cm.exception))
                self.assertEqual(auth.posts, 1)
                self.assertEqual(lines, [f"WARNING garmin_ext.key_mode garmin key mode: Garmin refused the Personal "
                                         f"API key (HTTP {code}); make a new key in Garmin Connect, set "
                                         "GARMIN_PERSONAL_API_KEY and restart"])
                clock.t += 2
                tokens.token()
                self.assertEqual(auth.posts, 2)
                self.assert_clean(lines, body)

    def test_after_a_restart_a_failed_refresh_keeps_the_cached_token(self):
        # Review: a restart in the token's last hour leaves nothing held in memory; the token Garmin's client
        # cached for this key (same fingerprint) is used while it is still good, a cache of another key never.
        cached = jwt(NOW + 3000)  # 50 minutes left
        for same_key, expected in ((True, cached), (False, None)):
            with self.subTest(same_key=same_key):
                tokens, auth, clock = self.manager([HttpError(status_code=503, method="POST", url="u",
                                                              response_text="x")])
                auth.base_url = "https://garmin.example"
                key = self.key if same_key else self.key + "x"
                with open(self.cache, "w", encoding="utf-8") as fh:
                    json.dump({"fingerprint": hashlib.sha256(f"{auth.base_url}\0{key}".encode()).hexdigest(),
                               "access_token": cached, "expires_at": NOW + 3000}, fh)
                with captured("garmin_ext.key_mode"):
                    if expected:
                        self.assertEqual(tokens.token(), expected)
                    else:
                        with self.assertRaises(km.KeyModeGarminError):
                            tokens.token()

    def test_failed_refresh_keeps_the_held_token(self):
        # R5: with under 1 h left a failed refresh (429, 5xx, network, an unusable answer) no longer fails the
        # call while the held token is still good; Garmin is asked again only after a short back-off.
        body = "BODY-" + secrets.token_hex(8)
        held = jwt(NOW + 86400)
        conn = requests_error("ConnectionError", "HTTPSConnectionPool(host='x', port=443): Max retries exceeded")
        tokens, auth, clock = self.manager([
            (held, NOW + 86400),
            HttpError(status_code=503, method="POST", url="u", response_text=body),
            HttpError(status_code=429, method="POST", url="u", response_text=body),
            conn,
            ("x" * 40, NOW + 86400),
        ])
        self.assertEqual(tokens.token(first=True), held)
        clock.t += 86400 - 3000  # 50 minutes left
        with captured("garmin_ext.key_mode") as lines:
            self.assertEqual(tokens.token(), held)
            self.assertEqual(auth.posts, 2)
            clock.t += km.HELD_RETRY_S - 1
            self.assertEqual(tokens.token(), held)  # within the back-off: no request
            self.assertEqual(auth.posts, 2)
            for _ in range(3):  # 429, ConnectionError, an unusable token answer
                clock.t += km.HELD_RETRY_S
                self.assertEqual(tokens.token(), held)
            self.assertEqual(auth.posts, 5)
        self.assertEqual(lines, [
            "WARNING garmin_ext.key_mode garmin key mode: token request failed (unavailable)",
            "WARNING garmin_ext.key_mode garmin key mode: using the held access token, valid until "
            f"{km._when(NOW + 86400)}, until a token request works",
            "WARNING garmin_ext.key_mode garmin key mode: token request failed (rate_limited)",
            "WARNING garmin_ext.key_mode garmin key mode: token request failed (unavailable)",
            "WARNING garmin_ext.key_mode garmin key mode: token request failed (unavailable)",
        ])
        self.assert_clean(lines, held, body)
        # The next request that works replaces it, and a later fallback logs its line again.
        clock.t += km.HELD_RETRY_S
        fresh = tokens.token()
        self.assertNotEqual(fresh, held)
        self.assertEqual(auth.posts, 6)
        # A refused key keeps today's behaviour: the login error and the 15-minute latch, held token or not.
        for code in (400, 401, 403):
            with self.subTest(code):
                tokens, auth, clock = self.manager([
                    (held, NOW + 86400), HttpError(status_code=code, method="POST", url="u", response_text=body)])
                tokens.token()
                clock.t += 86400 - 3000
                with self.assertRaises(km.KeyModeLoginError):
                    tokens.token()
                clock.t += 1
                with self.assertRaises(km.KeyModeLoginError):
                    tokens.token()
                self.assertEqual(auth.posts, 2)
        # Too little left (HELD_MARGIN_S or less): the failure is raised as before.
        tokens, auth, clock = self.manager([
            (held, NOW + 86400), HttpError(status_code=503, method="POST", url="u", response_text=body)])
        tokens.token()
        clock.t += 86400 - km.HELD_MARGIN_S
        with captured("garmin_ext.key_mode") as lines, self.assertRaises(km.KeyModeGarminError) as cm:
            tokens.token()
        self.assertEqual(str(cm.exception), km.TEXT_REQUEST_FAILED.format(code=503))
        self.assertEqual(lines, ["WARNING garmin_ext.key_mode garmin key mode: token request failed (unavailable)"])
        # A token dropped after a data 401 is never handed back.
        tokens, auth, clock = self.manager([
            (held, NOW + 86400), HttpError(status_code=503, method="POST", url="u", response_text=body)])
        tokens.token()
        tokens.refused(held)
        with self.assertRaises(km.KeyModeGarminError):
            tokens.token()

    def test_short_token_warns(self):
        tokens, _, clock = self.manager([(jwt(NOW + 7200), NOW + 7200)])
        with captured("garmin_ext.key_mode") as lines:
            tokens.token()
        self.assertEqual(len(lines), 2)
        self.assertRegex(lines[1], r"^WARNING garmin_ext\.key_mode garmin key mode: the Personal API key expires at "
                                   r"\d{4}-\d\d-\d\dT\d\d:\d\dZ; make a new key in Garmin Connect before then$")

    def test_unusable_tokens(self):
        good = jwt(NOW + 86400)
        head, payload, sig = good.split(".")
        bad_tokens = (good[:20] + "\n" + good[20:], good[:20] + " " + good[20:], good + ".x", "a.b.c",
                      head + "." + payload + "." + "s" * (8193 - len(head) - len(payload) - 2), "x" * 40, "")
        self.assertEqual(len(bad_tokens[4]), 8193)
        for bad in bad_tokens:
            with self.subTest(len(bad)):
                tokens, auth, _ = self.manager([(bad, NOW + 86400)])
                with captured("garmin_ext.key_mode") as lines:
                    with self.assertRaises(km.KeyModeGarminError) as cm:
                        tokens.token(first=True)
                self.assertEqual(str(cm.exception), km.TEXT_BAD_TOKEN)
                self.assertTrue(cm.exception.__suppress_context__)
                self.assertFalse(os.path.exists(self.cache))
                self.assertEqual(lines, ["WARNING garmin_ext.key_mode garmin key mode: the first token request failed "
                                         "(unavailable); tool calls will try again"])
        # From the cache file too.
        tokens, auth, _ = self.manager()
        with open(self.cache, "w", encoding="utf-8") as fh:
            json.dump({"key": self.key, "access_token": good + "\nX: y", "expires_at": NOW + 86400}, fh)
        with self.assertRaises(km.KeyModeGarminError):
            tokens.token()
        self.assertEqual(auth.posts, 0)
        self.assertFalse(os.path.exists(self.cache))
        for exp in (float("nan"), float("inf"), -1, "soon", None, 1e20):
            tokens, _, _ = self.manager([(jwt(NOW + 86400), exp)])
            with self.subTest(exp), self.assertRaises(km.KeyModeGarminError):
                tokens.token()
        tokens, _, _ = self.manager([GarminPersonalApiError("The token endpoint did not return a JWT with an exp "
                                                            "claim.")])
        with self.assertRaises(km.KeyModeGarminError) as cm:
            tokens.token()
        self.assertEqual(str(cm.exception), km.TEXT_BAD_TOKEN)

    def test_other_token_failures(self):
        body = "BODY-" + secrets.token_hex(8)
        cases = ((HttpError(status_code=429, method="POST", url="u", response_text=body), km.KeyModeRateLimited,
                  km.TEXT_RATE_LIMITED, "rate_limited"),
                 (HttpError(status_code=503, method="POST", url="u", response_text=body), km.KeyModeGarminError,
                  km.TEXT_REQUEST_FAILED.format(code=503), "unavailable"),
                 (HttpError(status_code=418, method="POST", url="u", response_text=body), km.KeyModeGarminError,
                  km.TEXT_NO_ANSWER.format(code=418), "unavailable"),
                 (requests_error("InvalidHeader", "Invalid leading whitespace in header value " + body,
                                 (OSError, ValueError)), km.KeyModeGarminError,
                  km.TEXT_FAILED_TYPE.format(type="InvalidHeader"), "unavailable"))
        for first in (True, False):
            for exc, cls, text, group in cases:
                with self.subTest((first, text)):
                    tokens, auth, _ = self.manager([exc])
                    with captured("garmin_ext.key_mode") as lines:
                        with self.assertRaises(cls) as cm:
                            tokens.token(first=first)
                    self.assertEqual(str(cm.exception), text)
                    self.assertIsNone(cm.exception.__cause__)
                    self.assertTrue(cm.exception.__suppress_context__)
                    want = (f"the first token request failed ({group}); tool calls will try again" if first
                            else f"token request failed ({group})")
                    self.assertEqual(lines, [f"WARNING garmin_ext.key_mode garmin key mode: {want}"])
                    self.assert_clean(lines + [str(cm.exception)], body)
        # requests' ConnectionError passes through unchanged (its text holds the URL only).
        conn = requests_error("ConnectionError", "HTTPSConnectionPool(host='x', port=443): Max retries exceeded")
        tokens, _, _ = self.manager([conn])
        with captured("garmin_ext.key_mode") as lines, self.assertRaises(OSError) as cm:
            tokens.token()
        self.assertIs(cm.exception, conn)
        self.assertEqual(lines, ["WARNING garmin_ext.key_mode garmin key mode: token request failed (unavailable)"])


# --- K5 (fake parts) and install -----------------------------------------------------------------------------

def _hooked_main():
    email = password = None
    return init_api(email, password)  # noqa: F821 - read as text only


def _moved_main():
    return None


class Install(TempDir, unittest.TestCase):
    def fake_upstream(self, main=_hooked_main):
        module = types.ModuleType("garmin_mcp")
        module.main = main
        module.init_api = lambda email, password: None
        return module

    def test_install_replaces_the_login(self):
        module = self.fake_upstream()
        key = fake_key()
        clock = Clock()
        auth = FakeAuth(self.cache, clock)
        http = FakeHttp()
        with mock.patch.dict(sys.modules, {"garmin_mcp": module}), captured("garmin_ext.key_mode") as lines:
            tokens = km.install(key, self.cache, authenticator=auth, http=http, clock=clock)
            garmin = module.init_api("ignored@example.invalid", "ignored")
        self.assertIsInstance(tokens, km.TokenManager)
        self.assertIsInstance(garmin, km.KeyModeGarmin)
        self.assertEqual(auth.posts, 1)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(self.cache)).st_mode), 0o700)
        self.assertEqual(lines[0], f"INFO garmin_ext.key_mode garmin key mode: on, token cache {self.cache}")
        self.assertIn("new access token", lines[1])
        self.assertEqual(logging.getLogger("garmin_personal_api").level, logging.WARNING)
        # The default cache path is cache_path(os.environ).
        with mock.patch.dict(sys.modules, {"garmin_mcp": self.fake_upstream()}), \
                mock.patch.dict(os.environ, {"GARMIN_KEY_TOKEN_CACHE": self.cache}):
            km.install(key, authenticator=auth, http=http, clock=clock)

    def test_login_outcomes(self):
        body = "BODY-" + secrets.token_hex(8)
        key = fake_key()
        clock = Clock()
        for answer in (HttpError(status_code=503, method="POST", url="u", response_text=body),
                       HttpError(status_code=429, method="POST", url="u", response_text=body),
                       requests_error("ConnectionError", "connection refused"), ("bad token", NOW + 86400)):
            with self.subTest(repr(answer)[:40]):
                if os.path.exists(self.cache):
                    os.unlink(self.cache)
                auth = FakeAuth(self.cache, clock, [answer])
                tokens = km.TokenManager(key, self.cache, authenticator=auth, clock=clock)
                self.assertIsInstance(km.login(tokens, FakeHttp()), km.KeyModeGarmin)
        auth = FakeAuth(self.cache, clock, [HttpError(status_code=401, method="POST", url="u", response_text=body)])
        if os.path.exists(self.cache):
            os.unlink(self.cache)
        tokens = km.TokenManager(key, self.cache, authenticator=auth, clock=clock)
        with self.assertRaises(km.KeyModeLoginError):
            km.login(tokens, FakeHttp())
        auth = FakeAuth(self.cache, clock, [RuntimeError("a bug")])
        tokens = km.TokenManager(key, self.cache, authenticator=auth, clock=clock)
        with self.assertRaises(RuntimeError):
            km.login(tokens, FakeHttp())

    def test_install_refuses(self):
        with mock.patch.dict(sys.modules, {"garmin_mcp": self.fake_upstream(_moved_main)}):
            with self.assertRaises(RuntimeError) as cm:
                km.install(fake_key(), self.cache, authenticator=object(), http=object())
            self.assertIn("init_api(email, password)", str(cm.exception))
            for key in ("", "bad key", None):
                with self.assertRaises(ValueError):
                    km.install(key, self.cache)
            with self.assertRaises(ValueError):
                km.install(fake_key(), "/tmp/x.json")


# --- K6: the error table -------------------------------------------------------------------------------------

def systemic(exc):
    group = te.classify_exception(exc)[0]
    return group in (te.LOGIN, te.RATE_LIMITED) or (group == te.UNAVAILABLE
                                                    and sc.is_systemic_error(te.classify_exception(exc)[3]))


class ErrorTable(unittest.TestCase):
    TABLE = (  # exception, group, systemic (D11)
        (km.KeyModeLoginError(km.TEXT_KEY_REFUSED.format(code=400)), te.LOGIN, True),
        (km.KeyModeLoginError(km.TEXT_KEY_REFUSED.format(code=401)), te.LOGIN, True),
        (km.KeyModeLoginError(km.TEXT_KEY_REFUSED.format(code=403)), te.LOGIN, True),
        (km.KeyModeLoginError(km.TEXT_TOKEN_REFUSED), te.LOGIN, True),
        (km.KeyModeRateLimited(km.TEXT_RATE_LIMITED), te.RATE_LIMITED, True),
        (km.KeyModeNoData(km.TEXT_NOT_FOUND), te.NO_DATA, False),
        (km.KeyModeNoData(km.TEXT_ACTIVITY_FORBIDDEN), te.NO_DATA, False),
        (km.KeyModeNoData(km.TEXT_EMPTY_FILE), te.NO_DATA, False),
        (km.KeyModeNoData(km.TEXT_EMPTY_ANSWER), te.NO_DATA, False),
        (km.KeyModeBadRequest(km.TEXT_BAD_REQUEST), te.BAD_INPUT, False),
        (km.KeyModeGarminError(km.TEXT_REQUEST_FAILED.format(code=403)), te.UNAVAILABLE, True),
        (km.KeyModeGarminError(km.TEXT_REQUEST_FAILED.format(code=500)), te.UNAVAILABLE, False),
        (km.KeyModeGarminError(km.TEXT_REQUEST_FAILED.format(code=503)), te.UNAVAILABLE, False),
        (km.KeyModeGarminError(km.TEXT_NO_ANSWER.format(code=418)), te.UNAVAILABLE, False),
        (km.KeyModeGarminError(km.TEXT_BAD_TOKEN), te.UNAVAILABLE, False),
        (km.KeyModeGarminError(km.TEXT_FAILED_TYPE.format(type="InvalidHeader")), te.UNAVAILABLE, False),
        (km.NotInKeyMode(km.TEXT_NOT_IN_KEY_MODE.format(name="get_training_status")), te.INTERNAL, False),
    )

    def test_groups(self):
        for exc, group, is_systemic in self.TABLE:
            with self.subTest(str(exc)):
                got, detail, _, _ = te.classify_exception(exc)
                self.assertEqual(got, group)
                self.assertEqual(te.classify_text("Error retrieving x: " + str(exc))[0], group)
                if group == te.BAD_INPUT:
                    self.assertEqual(detail, te.GARMIN_400_DETAIL)
                if group != te.INTERNAL:
                    self.assertEqual(systemic(exc), is_systemic)
                twin = copy.copy(exc)
                self.assertIs(type(twin), type(exc))
                self.assertEqual(str(twin), str(exc))
                self.assertTrue(str(exc).isascii())

    def test_raised_from_none(self):
        body = "BODY-" + secrets.token_hex(8)
        cases = ((401, False, km.KeyModeLoginError, km.TEXT_TOKEN_REFUSED),
                 (429, False, km.KeyModeRateLimited, km.TEXT_RATE_LIMITED),
                 (404, False, km.KeyModeNoData, km.TEXT_NOT_FOUND),
                 (403, True, km.KeyModeNoData, km.TEXT_ACTIVITY_FORBIDDEN),
                 (403, False, km.KeyModeGarminError, km.TEXT_REQUEST_FAILED.format(code=403)),
                 (400, False, km.KeyModeBadRequest, km.TEXT_BAD_REQUEST),
                 (500, False, km.KeyModeGarminError, km.TEXT_REQUEST_FAILED.format(code=500)),
                 (503, True, km.KeyModeGarminError, km.TEXT_REQUEST_FAILED.format(code=503)),
                 (409, False, km.KeyModeGarminError, km.TEXT_NO_ANSWER.format(code=409)),
                 (302, False, km.KeyModeGarminError, km.TEXT_NO_ANSWER.format(code=302)))
        for status, activity, cls, text in cases:
            with self.subTest((status, activity)):
                g, http = adapter(lambda kind, path, params: HttpError(
                    status_code=status, method="GET", url="https://x/" + body, response_text=body))
                with self.assertRaises(cls) as cm:
                    g.get_activity(5) if activity else g.get_stats(DAY)
                exc = cm.exception
                self.assertEqual(str(exc), text)
                self.assertIsNone(exc.__cause__)
                self.assertIsNone(exc.__context__)
                self.assertTrue(exc.__suppress_context__)
                self.assertNotIn(body, "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
        library = ((GarminPersonalApiError("The original activity file response was empty"), km.KeyModeNoData,
                    km.TEXT_EMPTY_FILE),
                   (GarminPersonalApiError("something " + body), km.KeyModeGarminError,
                    km.TEXT_FAILED_TYPE.format(type="GarminPersonalApiError")),
                   (requests_error("JSONDecodeError", "Expecting value " + body, (OSError, ValueError)),
                    km.KeyModeGarminError, km.TEXT_FAILED_TYPE.format(type="JSONDecodeError")),
                   (requests_error("InvalidURL", body + "x9_y"), km.KeyModeGarminError,
                    km.TEXT_FAILED_TYPE.format(type="InvalidURL")))
        for raised, cls, text in library:
            with self.subTest(text):
                g, _ = adapter(lambda kind, path, params: raised)
                with self.assertRaises(cls) as cm:
                    g.download_activity(5)
                self.assertEqual(str(cm.exception), text)
                self.assertTrue(cm.exception.__suppress_context__)
                self.assertIsNone(cm.exception.__context__)
        timeout = type("Timeout", (OSError,), {"__module__": "requests.exceptions"})("Read timed out.")
        g, _ = adapter(lambda kind, path, params: timeout)
        with self.assertRaises(OSError) as cm:
            g.get_stats(DAY)
        self.assertIs(cm.exception, timeout)
        self.assertEqual(te.classify_exception(timeout)[0], te.UNAVAILABLE)

    def test_bug_keeps_its_type_but_loses_its_chain(self):
        body = "BODY-" + secrets.token_hex(8)

        def answer(kind, path, params):
            try:
                raise HttpError(status_code=500, method="GET", url="u", response_text=body)
            except HttpError:
                raise KeyError("missing")  # noqa: B904 - its context is the HttpError, on purpose

        g, _ = adapter(answer)
        with self.assertRaises(KeyError) as cm:
            g.get_stats(DAY)
        exc = cm.exception
        self.assertIsNone(exc.__context__)
        self.assertNotIn(body, "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
        self.assertEqual(te.classify_exception(exc)[0], te.INTERNAL)


# --- K16: no key, token or Garmin body in any rendered log record or client text -----------------------------

class Leaks(TempDir, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.key = fake_key()
        self.body = "BODY-" + secrets.token_hex(12)
        self.token_sentinel = jwt(NOW + 86400)
        self.saved_secrets = te._SECRETS
        te.register_secret(self.key)

    def tearDown(self):
        te._SECRETS = self.saved_secrets
        super().tearDown()

    def sentinels(self):
        return (self.key, self.body, self.token_sentinel)

    def check_clean(self, texts):
        for text in texts:
            for sentinel in self.sentinels():
                self.assertNotIn(sentinel, text)

    def run_source(self, tokens, http, call=lambda g: g.get_stats(DAY)):
        """One status-report source (status_common.fetch_sync: one log line, a traceback for internal errors) and
        one tool-call style failure (classify, client text in both modes, the redacted original)."""
        g = km.KeyModeGarmin(tokens, http)
        texts = []
        with captured() as lines:
            result = sc.fetch_sync(lambda: call(g))
            try:
                call(g)
            except Exception as exc:  # noqa: BLE001
                group, detail, _root, original = te.classify_exception(exc)
                logging.getLogger("garmin_ext.tools").warning("tool error: tool=get_stats group=%s: %s", group,
                                                              te.redact(original), exc_info=exc)
                ref = te.new_ref() if group == te.INTERNAL else None
                texts += [te.client_text(group, ref=ref, detail=detail, key_mode=True),
                          te.client_text(group, ref=ref, detail=detail),
                          "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))]
        self.assertFalse(result["ok"])
        texts += [json.dumps(result, default=str)]
        self.check_clean(lines + texts)
        return result, lines

    def test_login(self):
        auth = FakeAuth(self.cache, Clock(), [HttpError(status_code=401, method="POST", url="u " + self.key,
                                                        response_text=self.body + " " + self.key)])
        tokens = km.TokenManager(self.key, self.cache, authenticator=auth, clock=Clock())
        result, _ = self.run_source(tokens, FakeHttp())
        self.assertEqual(result["error"], "Garmin login missing or expired")

    def test_unavailable(self):
        tokens = FixedTokens(self.token_sentinel)
        http = FakeHttp(lambda *a: HttpError(status_code=503, method="GET", url="u", response_text=self.body))
        result, _ = self.run_source(tokens, http)
        self.assertEqual(result["error"], "Garmin Connect did not answer")
        header = requests_error("InvalidHeader", f"Invalid return character or leading space in header: "
                                f"Authorization: Bearer {self.token_sentinel}", (OSError, ValueError))
        result, _ = self.run_source(FixedTokens(self.token_sentinel), FakeHttp(lambda *a: header))
        self.assertEqual(result["error"], "Garmin Connect did not answer")
        auth = FakeAuth(self.cache, Clock(), [(self.token_sentinel[:30] + "\n" + self.token_sentinel[30:],
                                               NOW + 86400)])
        tokens = km.TokenManager(self.key, self.cache, authenticator=auth, clock=Clock())
        result, _ = self.run_source(tokens, FakeHttp())
        self.assertEqual(result["error"], "Garmin Connect did not answer")

    def test_internal(self):
        http = FakeHttp(lambda *a: HttpError(status_code=500, method="GET", url="u", response_text=self.body))

        def broken(failure, activity):
            raise RuntimeError("mapper bug")

        with mock.patch.object(km, "_data_failure", broken):
            result, lines = self.run_source(FixedTokens(self.token_sentinel), http)
        self.assertTrue(result["error"].startswith("internal error"))
        self.assertTrue(any("RuntimeError: mapper bug" in line for line in lines))

    @unittest.skipUnless(HAVE_UPSTREAM, NEEDS_UPSTREAM)
    def test_through_upstreams_login_proxy(self):
        import garmin_mcp

        auth = FakeAuth(self.cache, Clock(), [HttpError(status_code=401, method="POST", url="u",
                                                        response_text=self.body)])
        tokens = km.TokenManager(self.key, self.cache, authenticator=auth, clock=Clock())
        pending = garmin_mcp._PendingGarminClient(timeout=10)
        with captured() as lines:
            pending.start(lambda: km.login(tokens, FakeHttp()))
            proxy = garmin_mcp._GarminProxy(pending, timeout=10)
            with self.assertRaises(km.KeyModeLoginError) as cm:
                proxy.get_stats(DAY)
        self.assertEqual(pending.login_state(), "failed")
        exc = cm.exception
        self.assertEqual(str(exc), km.TEXT_KEY_REFUSED.format(code=401))
        self.assertEqual(te.client_text(te.classify_exception(exc)[0], key_mode=True),
                         te._KEY_MODE_TEXTS[te.LOGIN])
        self.check_clean(lines + ["".join(traceback.format_exception(type(exc), exc, exc.__traceback__))])


# --- K11: import hygiene -------------------------------------------------------------------------------------

class Imports(unittest.TestCase):
    def test_no_top_level_client_import(self):
        with open(os.path.join(HERE, "key_mode.py"), encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        top = set()
        for node in tree.body:
            if isinstance(node, ast.Import):
                top |= {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                top.add((node.module or "").split(".")[0])
        self.assertLessEqual(top, {"datetime", "hashlib", "json", "logging", "math", "os", "re", "stat", "tempfile",
                                   "threading", "time", "tool_errors"})

    def test_full_mode_never_loads_the_client(self):
        code = ("import sys, key_mode\n"
                "key_mode.check({'GARMIN_AUTH': 'full'}); key_mode.auth_mode({'GARMIN_AUTH': 'full'})\n"
                "import importlib.util\n"
                "if importlib.util.find_spec('garmin_mcp') and importlib.util.find_spec('mcp'):\n"
                "    import launch\n"
                "print(sorted(m for m in sys.modules if m.split('.')[0] == 'garmin_personal_api'))\n")
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        out = subprocess.run([sys.executable, "-B", "-c", code], cwd=HERE, env=env, capture_output=True, text=True,
                             timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr[-2000:])
        self.assertEqual(out.stdout.strip(), "[]")


# --- K17, K18: the list file and the hash pin ----------------------------------------------------------------

def _names(path):
    names = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.split("#", 1)[0].strip()
            if line:
                names.append(line)
    return names


class Files(unittest.TestCase):
    def test_enabled_tools_key_file(self):
        names = _names(os.path.join(ROOT, "config", "enabled-tools-key.txt"))
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(set(names), km.KEY_MODE_TOOLS)
        self.assertEqual(len(names), 13)
        self.assertLessEqual(set(names), set(_names(os.path.join(ROOT, "config", "enabled-tools.txt"))))

    def test_hash_pin(self):
        with open(os.path.join(ROOT, "config", "garmin-key-client.txt"), encoding="utf-8") as fh:
            reqs = [line.strip() for line in fh if line.strip() and not line.lstrip().startswith("#")]
        self.assertEqual(len(reqs), 1)
        m = re.fullmatch(r"garmin-connect-personal-api-python==(\d+\.\d+\.\d+) --hash=sha256:[0-9a-f]{64}", reqs[0])
        self.assertIsNotNone(m, reqs[0])
        with open(os.path.join(ROOT, "constraints.txt"), encoding="utf-8") as fh:
            constraints = fh.read()
        self.assertRegex(constraints, rf"(?m)^garmin-connect-personal-api-python=={re.escape(m.group(1))}$")
        with open(os.path.join(ROOT, "Dockerfile"), encoding="utf-8") as fh:
            dockerfile = fh.read()
        self.assertIn("COPY config/garmin-key-client.txt /tmp/garmin-key-client.txt", dockerfile)
        self.assertIn("pip install --require-hashes --no-deps -r /tmp/garmin-key-client.txt", dockerfile)
        self.assertNotRegex(dockerfile, r"(?m)^\s*HEALTHCHECK")
        joined = dockerfile.replace("\\\n", " ")
        for line in joined.splitlines():
            if re.match(r"\s*RUN\b", line) and "pip install -c" in line:
                c_part = line.split("pip install -c", 1)[1].split("&&", 1)[0]
                self.assertNotIn("garmin-connect-personal-api-python", c_part)
        m = re.search(r"(?m)^# hadolint ignore=DL3064\n(ENV (?:.*\\\n)*.*)$", dockerfile)
        self.assertIsNotNone(m)
        env = m.group(1)
        self.assertIn("GARMINTOKENS_BASE64=/data/tokens/garmin_tokens.base64", env)
        self.assertIn("GARMIN_KEY_TOKEN_CACHE=/data/garmin-key/token.json", env)


# --- the real client, on fake sessions (Python 3.12) ---------------------------------------------------------

class FakeResponse:
    def __init__(self, status, text="", content=None):
        self.status_code, self.text = status, text
        self.content = text.encode() if content is None else content
        self.ok = 200 <= status < 300

    def json(self):
        try:
            return json.loads(self.text)
        except ValueError as exc:  # as requests.Response.json() does
            import requests

            raise requests.exceptions.JSONDecodeError(exc.msg, exc.doc, exc.pos) from None


class FakeSession:
    def __init__(self, posts=(), gets=()):
        self.posts, self.gets, self.log = list(posts), list(gets), []

    def post(self, url, headers=None, timeout=None):
        self.log.append(("POST", url, dict(headers or {})))
        answer = self.posts.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def get(self, url, headers=None, params=None, timeout=None):
        self.log.append(("GET", url, params, dict(headers or {})))
        answer = self.gets.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def close(self):
        pass


@unittest.skipUnless(HAVE_CLIENT, NEEDS_CLIENT)
class RealClient(TempDir, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.key = fake_key()
        self.body = "BODY-" + secrets.token_hex(12)
        km.prepare_cache(self.cache)

    def test_token_cache_and_base_url(self):
        token = jwt(time.time() + 86400)
        session = FakeSession(posts=[FakeResponse(200, token + "\n")])
        with mock.patch.dict(os.environ, {km.BASE_URL_ENV: "https://evil.example"}):
            auth = km.library_authenticator(self.cache, session=session)
            http = km._LibraryHttp(session=FakeSession(gets=[FakeResponse(200, '{"a": 1}'), FakeResponse(204, "")]))
        self.assertEqual(auth.base_url, km.OFFICIAL_BASE_URL)
        tokens = km.TokenManager(self.key, self.cache, authenticator=auth)
        with captured() as lines:
            got = tokens.token(first=True)
            again = km.TokenManager(self.key, self.cache, authenticator=auth).token()
        self.assertEqual(got, token)
        self.assertEqual(again, token)
        self.assertEqual(len(session.log), 1)
        self.assertEqual(session.log[0][1], km.OFFICIAL_BASE_URL
                         + "/personalapitoken-service/personal-api/personal-api-key/token")
        self.assertEqual(session.log[0][2]["Authorization"], "ApiKey " + self.key)
        self.assertEqual(stat.S_IMODE(os.stat(self.cache).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(self.cache)).st_mode), 0o700)
        for line in lines:
            self.assertNotIn(self.key, line)
            self.assertNotIn(token, line)
        g = km.KeyModeGarmin(tokens, http)
        with mock.patch.dict(os.environ, {km.BASE_URL_ENV: "https://evil.example"}):
            self.assertEqual(g.get_stats(DAY), {"a": 1})
            self.assertIsNone(g.get_hrv_data(DAY))
        get_log = http._session.log
        self.assertEqual(get_log[0][1], km.OFFICIAL_BASE_URL + "/usersummary-service/usersummary/daily")
        self.assertEqual(get_log[0][2], {"calendarDate": DAY})
        self.assertEqual(get_log[0][3]["Authorization"], "Bearer " + token)
        self.assertEqual(get_log[1][1], km.OFFICIAL_BASE_URL + f"/hrv-service/hrv/{DAY}")

    def test_refused_key_and_errors(self):
        session = FakeSession(posts=[FakeResponse(401, self.body)])
        auth = km.library_authenticator(self.cache, session=session)
        tokens = km.TokenManager(self.key, self.cache, authenticator=auth)
        with captured() as lines, self.assertRaises(km.KeyModeLoginError) as cm:
            tokens.token(first=True)
        exc = cm.exception
        rendered = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        for text in lines + [rendered]:
            self.assertNotIn(self.body, text)
            self.assertNotIn(self.key, text)
        token = jwt(time.time() + 86400)
        gets = [FakeResponse(503, self.body), FakeResponse(404, self.body), FakeResponse(200, "", content=b""),
                FakeResponse(200, "not json " + self.body), FakeResponse(401, self.body),
                FakeResponse(200, '{"b": 2}')]
        session = FakeSession(posts=[FakeResponse(200, token), FakeResponse(200, jwt(time.time() + 86400))])
        os.unlink(self.cache) if os.path.exists(self.cache) else None
        auth = km.library_authenticator(self.cache, session=session)
        tokens = km.TokenManager(self.key, self.cache, authenticator=auth)
        http = km._LibraryHttp(session=FakeSession(gets=gets))
        g = km.KeyModeGarmin(tokens, http)
        for cls, text, call in ((km.KeyModeGarminError, km.TEXT_REQUEST_FAILED.format(code=503),
                                 lambda: g.get_stats(DAY)),
                                (km.KeyModeNoData, km.TEXT_NOT_FOUND, lambda: g.get_activity(1)),
                                (km.KeyModeNoData, km.TEXT_EMPTY_FILE, lambda: g.download_activity(1)),
                                (km.KeyModeGarminError, km.TEXT_FAILED_TYPE.format(type="JSONDecodeError"),
                                 lambda: g.get_stats(DAY))):
            with self.subTest(text), self.assertRaises(cls) as cm:
                call()
            self.assertEqual(str(cm.exception), text)
            rendered = "".join(traceback.format_exception(type(cm.exception), cm.exception,
                                                          cm.exception.__traceback__))
            self.assertNotIn(self.body, rendered)
        self.assertEqual(g.get_stats(DAY), {"b": 2})  # a 401, a new token, the retry
        self.assertEqual(len(session.log), 2)

    @unittest.skipUnless(HAVE_UPSTREAM, NEEDS_UPSTREAM)
    def test_selfcheck(self):
        km.selfcheck()
        import garmin_personal_api

        with mock.patch.object(garmin_personal_api, "DEFAULT_BASE_URL", "https://other.example"):
            with self.assertRaises(RuntimeError):
                km.selfcheck()


# --- K9: parity with garminconnect on upstream's tools -------------------------------------------------------

@unittest.skipUnless(HAVE_UPSTREAM, NEEDS_UPSTREAM)
class Parity(unittest.TestCase):
    """Upstream's key-mode tools give the same output through KeyModeGarmin (official answers) as through a fake
    garminconnect client fed the same data in garminconnect's shape."""

    SUMMARY = {"calendarDate": DAY, "totalSteps": 9000, "restingHeartRate": 48, "averageStressLevel": 25,
               "bodyBatteryMostRecentValue": 60, "totalKilocalories": 2400.0, "activeKilocalories": 600.0}
    SLEEP = {"calendarDate": DAY, "sleepTimeSeconds": 27000, "deepSleepSeconds": 5400, "lightSleepSeconds": 14400,
             "remSleepSeconds": 6000, "awakeSleepSeconds": 1200, "sleepScores": {"overall": {"value": 81}}}
    HRV = {"hrvSummary": {"calendarDate": DAY, "weeklyAvg": 60, "lastNightAvg": 58, "status": "BALANCED"}}
    BB = [{"date": DAY, "charged": 40, "drained": 35}]
    ACTS = [{"activityId": 111, "activityName": "Easy run", "activityType": {"typeKey": "running"},
             "startTimeLocal": DAY + " 07:00:00", "duration": 2400.0, "distance": 6500.0, "averageHR": 140.0}]
    ACT = {"activityId": 111, "activityName": "Easy run", "activityTypeDTO": {"typeKey": "running"},
           "summaryDTO": {"startTimeLocal": DAY + "T07:00:00.0", "duration": 2400.0, "distance": 6500.0}}
    VO2 = [{"generic": {"calendarDate": "2026-11-05", "vo2MaxPreciseValue": 52.3, "vo2MaxValue": 52.0}}]
    ZONES = [{"trainingMethod": "HR_MAX", "zone1Floor": 100, "zone2Floor": 120, "zone3Floor": 140,
              "zone4Floor": 155, "zone5Floor": 170, "maxHeartRateUsed": 185, "sport": "DEFAULT"}]

    def official(self, kind, path, params):
        table = {"/usersummary-service/usersummary/daily": self.SUMMARY, "/sleep-service/sleep/dailySleep": self.SLEEP,
                 "/wellness-service/wellness/bodyBattery/reports/daily": self.BB,
                 "/activitylist-service/activities/search/activities": self.ACTS,
                 "/activity-service/activity/111": self.ACT, "/biometric-service/heartRateZones/v2": self.ZONES}
        if path in table:
            return table[path]
        if path.startswith("/hrv-service/hrv/"):
            return self.HRV
        if path.startswith("/metrics-service/metrics/maxmet/daily/"):
            return self.VO2
        raise AssertionError(path)

    def garminconnect(self):
        outer = self

        class FakeGC:
            garmin_connect_activities = "/activitylist-service/activities/search/activities"
            garmin_connect_metrics_url = "/metrics-service/metrics/maxmet/daily"

            def get_stats(self, cdate):
                return outer.SUMMARY

            get_user_summary = get_stats

            def get_sleep_data(self, cdate):
                return {"dailySleepDTO": outer.SLEEP}

            def get_hrv_data(self, cdate):
                return outer.HRV

            def get_body_battery(self, startdate, enddate=None):
                return outer.BB

            def get_activities(self, start=0, limit=20, activitytype=None):
                return outer.ACTS

            def get_activity(self, activity_id):
                return outer.ACT

            def connectapi(self, path, **kwargs):
                if path.startswith("/activitylist-service"):
                    return outer.ACTS
                if path.startswith("/metrics-service"):
                    return outer.VO2
                if path == "/biometric-service/heartRateZones":
                    return outer.ZONES
                raise AssertionError(path)

        return FakeGC()

    def run_tools(self, client):
        import asyncio

        from garmin_mcp import activity_management, health_wellness, training, user_profile

        class App:
            def __init__(self):
                self.tools = {}

            def tool(self, *a, **k):
                def deco(fn):
                    self.tools[k.get("name") or fn.__name__] = fn
                    return fn
                return deco

            def __getattr__(self, name):
                return lambda *a, **k: (lambda fn: fn)

        modules = (health_wellness, training, activity_management, user_profile)
        saved = [getattr(m, "garmin_client", None) for m in modules]
        app = App()
        try:
            for module in modules:
                module.configure(client)
                module.register_tools(app)
            cases = (("get_stats", {"date": DAY}), ("get_sleep_summary", {"date": DAY}),
                     ("get_sleep_summary_range", {"start_date": "2026-11-09", "end_date": DAY}),
                     ("get_hrv_trend", {"start_date": "2026-11-09", "end_date": DAY}),
                     ("get_body_battery", {"start_date": DAY, "end_date": DAY}),
                     ("get_activities", {"start": 0, "limit": 5}),
                     ("get_activities_by_date", {"start_date": DAY, "end_date": DAY}),
                     ("get_activity", {"activity_id": 111}),
                     ("get_vo2max_trend", {"start_date": "2026-11-01", "end_date": DAY}),
                     ("get_heart_rate_zones", {}))
            out = {}
            for name, kwargs in cases:
                fn = app.tools.get(name)
                self.assertIsNotNone(fn, name)
                out[name] = asyncio.run(fn(**kwargs))
            return out
        finally:
            for module, value in zip(modules, saved):
                module.garmin_client = value

    def test_same_output(self):
        keyed = self.run_tools(km.KeyModeGarmin(FixedTokens(), FakeHttp(self.official)))
        full = self.run_tools(self.garminconnect())
        for name in full:
            with self.subTest(name):
                self.assertEqual(keyed[name], full[name])
                self.assertFalse(str(keyed[name]).startswith("Error"), keyed[name])


if __name__ == "__main__":
    unittest.main()
