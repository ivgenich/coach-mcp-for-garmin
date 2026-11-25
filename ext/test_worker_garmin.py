"""Tests for worker_garmin.py: the sealed token, the publisher, the stored-token client and the guard wrapper.

    python3 -m unittest test_worker_garmin        (from ext/)
"""

from __future__ import annotations

import base64
import contextlib
import datetime as dt
import importlib.util
import io
import logging
import os
import re
import tempfile
import threading
import time
import unittest
import unittest.mock
from zoneinfo import ZoneInfo

import garmin_paths
import jobqueue
import tool_errors
import worker_garmin as wg

HAVE_CRYPTO = importlib.util.find_spec("cryptography") is not None
HAVE_GARMINCONNECT = importlib.util.find_spec("garminconnect") is not None
# A fixed key that passes the weak-key rule (bytes(range(32)) is a counting sequence and is refused).
KEY = bytes.fromhex("9f3c51e2a87d06b4c2e95f1a3d7b08c6e41f92a5b37c0d68f1e2a9c4573b6d80")
KEY_TEXT = base64.b64encode(KEY).decode()
TOKEN = '{"di_token": "a.b.c", "di_refresh_token": "r", "di_client_id": "c"}'


class Key(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(wg.parse_token_key(KEY_TEXT), KEY)
        self.assertEqual(wg.parse_token_key(base64.urlsafe_b64encode(KEY).decode().rstrip("=")), KEY)
        for bad in (None, "", "   ", "short", base64.b64encode(b"x" * 16).decode(), "%%%%",
                    base64.b64encode(bytes(32)).decode(), base64.b64encode(b"ab" * 16).decode()):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError) as cm:
                    wg.parse_token_key(bad)
                self.assertEqual(str(cm.exception), wg.ERR_TOKEN_KEY)

    def test_typed_counting_and_repeated_keys_are_refused(self):
        """16 different byte values was the only test, so a typed phrase, the bytes 0..31 and a
        repeated 16-byte block passed. The rule is feature_config.weak_key, shared with --check-config."""
        import feature_config

        for raw in (b"0123456789abcdefghijklmnopqrstuv", b"my-secret-password-is-hunter2!!!", bytes(range(32)),
                    bytes(range(31, -1, -1)), bytes(range(0, 96, 3)), KEY[:16] * 2, KEY[:8] * 4):
            text = base64.b64encode(raw).decode()
            with self.subTest(raw=raw[:4]):
                self.assertEqual(len(raw), 32)
                self.assertTrue(feature_config.weak_key(raw))
                with self.assertRaises(ValueError) as cm:
                    wg.parse_token_key(text)
                self.assertEqual(str(cm.exception), wg.ERR_TOKEN_KEY)
                self.assertNotIn(text, str(cm.exception))
        for _ in range(50):
            raw = os.urandom(32)
            self.assertEqual(wg.parse_token_key(base64.b64encode(raw).decode()), raw)


@unittest.skipUnless(HAVE_CRYPTO, "needs cryptography")
class Seal(unittest.TestCase):
    def test_round_trip_and_tamper(self):
        nonce, sealed = wg.seal(KEY, TOKEN.encode())
        self.assertNotIn(b"di_token", sealed)
        self.assertEqual(wg.unseal(KEY, nonce, sealed), TOKEN.encode())
        self.assertIsNone(wg.unseal(bytes(32), nonce, sealed), "wrong key")
        self.assertIsNone(wg.unseal(KEY, nonce, sealed[:-1] + bytes([sealed[-1] ^ 1])), "changed blob")
        self.assertNotEqual(wg.seal(KEY, TOKEN.encode())[0], nonce, "fresh nonce")


class FakeConn:
    def __init__(self, db):
        self.db = db
        if db.get("fail"):
            raise ConnectionError("connection refused: password=hunter2")

    def transaction(self):
        import contextlib

        return contextlib.nullcontext()

    def close(self):
        pass

    def execute(self, sql, params=None):
        db = self.db
        if sql is wg._SQL_PUT:
            db["put_params"] = sorted(params)
            db["row"] = (params["n"], params["s"])
            row = None
        elif sql is wg._SQL_GET:
            row = db.get("row")
        elif sql is wg._SQL_TOKEN_TABLE:
            row = ("history.garmin_token",) if db.get("table", True) else (None,)
        elif sql is wg._SQL_DELETE:
            row = None
            count = 1 if db.pop("row", None) is not None else 0
            return type("C", (), {"fetchone": lambda self: None, "rowcount": count})()
        else:
            row = None
        return type("C", (), {"fetchone": lambda self: row})()


class DeleteStoredToken(unittest.TestCase):
    def test_deletes_the_row_when_there_is_one(self):
        db = {"row": (b"n", b"s")}
        self.assertTrue(wg.delete_stored_token("", connect=lambda: FakeConn(db)))
        self.assertNotIn("row", db)
        self.assertFalse(wg.delete_stored_token("", connect=lambda: FakeConn(db)), "nothing left to delete")

    def test_no_table_no_dsn(self):
        db = {"row": (b"n", b"s"), "table": False}
        self.assertFalse(wg.delete_stored_token("", connect=lambda: FakeConn(db)))
        self.assertIn("row", db)
        self.assertFalse(wg.delete_stored_token(""))
        self.assertFalse(wg.delete_stored_token(None))

    def test_never_raises_and_logs_the_class_only(self):
        with self.assertLogs("garmin_ext.worker", "WARNING") as logs:
            self.assertFalse(wg.delete_stored_token("", connect=lambda: FakeConn({"fail": True})))
        self.assertIn("ConnectionError", logs.output[0])
        self.assertNotIn("hunter2", "".join(logs.output))


@unittest.skipUnless(HAVE_CRYPTO, "needs cryptography")
class StoreAndPublisher(unittest.TestCase):
    def setUp(self):
        self.db = {}
        self.store = wg.PgTokenStore("", KEY, connect=lambda: FakeConn(self.db))

    def test_store_round_trip_and_wrong_key(self):
        self.store.put(TOKEN)
        self.assertNotIn(b"di_token", bytes(self.db["row"][1]))
        self.assertEqual(self.store.get(), TOKEN)
        other = wg.PgTokenStore("", bytes(32), connect=lambda: FakeConn(self.db))
        self.assertIsNone(other.get())
        self.assertNotIn("di_token", repr(self.store))

    def test_no_digest_is_written_and_the_old_column_only_becomes_nullable(self):
        self.store.put(TOKEN)
        self.assertEqual(self.db["put_params"], ["n", "s"])
        self.assertNotIn("digest", wg._SQL_PUT)
        # An older build still running in a rolling deploy (or a rollback) writes digest: keep the column.
        self.assertNotIn("DROP COLUMN", wg.TOKEN_DDL)
        self.assertIn("    digest      text,\n", wg.TOKEN_DDL)
        self.assertIn("ALTER TABLE history.garmin_token ALTER COLUMN digest DROP NOT NULL;", wg.TOKEN_DDL)
        # Guarded: no lock and no error when the column is already nullable or absent.
        self.assertIn("column_name = 'digest' AND is_nullable = 'NO'", wg.TOKEN_DDL)
        self.assertNotIn("%", wg.TOKEN_DDL)

    def test_publisher_stores_only_changes_and_calls_refresh(self):
        refreshed = []
        with tempfile.TemporaryDirectory() as d:
            pub = wg.TokenPublisher(self.store, d, refresh=lambda: refreshed.append(1))
            self.assertFalse(pub.publish_once(), "no token file yet")
            with open(os.path.join(d, wg.TOKEN_FILE), "w") as fh:
                fh.write(TOKEN)
            with self.assertLogs("garmin_ext.worker", "INFO"):
                self.assertTrue(pub.publish_once())
            self.assertFalse(pub.publish_once(), "unchanged")
            with open(os.path.join(d, wg.TOKEN_FILE), "w") as fh:
                fh.write(TOKEN.replace('"r"', '"r2"'))
            with self.assertLogs("garmin_ext.worker", "INFO"):
                self.assertTrue(pub.publish_once())
        self.assertEqual(len(refreshed), 4)
        self.assertIn('"r2"', self.store.get())

    def test_publisher_store_failure_logs_the_class_only(self):
        class Broken:
            def put(self, text):
                raise RuntimeError(f"cannot store {text}")

        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, wg.TOKEN_FILE), "w") as fh:
                fh.write(TOKEN)
            with self.assertLogs("garmin_ext.worker", "WARNING") as logs:
                self.assertFalse(wg.TokenPublisher(Broken(), d).publish_once())
        self.assertNotIn("di_token", "\n".join(logs.output))


class FakeClient:
    def __init__(self, expiring=False):
        self.expiring = expiring
        self.loaded = None
        self.paths = []

    def loads(self, text):
        if text == "bad":
            raise ValueError(f"cannot parse {text}")
        self.loaded = text

    def _token_expires_soon(self):
        return self.expiring

    def _refresh_session(self):
        raise AssertionError("the worker must never refresh")

    def connectapi(self, path):
        self.paths.append(path)
        return {"displayName": "someone"}


class FakeGarmin:
    def __init__(self, expiring=False):
        self.client = FakeClient(expiring)
        self.display_name = None

    def get_stats(self, cdate):
        return {"for": self.display_name, "cdate": cdate}


class SelfCheck(unittest.TestCase):
    def test_passes_with_the_installed_garminconnect(self):
        if importlib.util.find_spec("garminconnect") is None:
            with self.assertRaises(RuntimeError):
                wg.selfcheck()
            return
        wg.selfcheck()

    def test_fails_when_a_private_name_is_gone(self):
        import sys
        import types

        for missing in ("_token_expires_soon", "_refresh_session", "_run_request"):
            names = {n: (lambda self: None) for n in wg.CLIENT_NAMES if n != missing}
            pkg = types.ModuleType("garminconnect")
            mod = types.ModuleType("garminconnect.client")
            mod.Client = type("Client", (), names)
            pkg.client = mod
            with self.subTest(missing=missing), \
                    unittest.mock.patch.dict(sys.modules, {"garminconnect": pkg, "garminconnect.client": mod}):
                with self.assertRaises(RuntimeError) as cm:
                    wg.selfcheck()
                self.assertIn(missing, str(cm.exception))

    def test_a_client_without_the_expiry_check_warns_once_and_defers(self):
        class OldClient(FakeClient):
            _token_expires_soon = None

        class OldGarmin(FakeGarmin):
            def __init__(self):
                self.client = OldClient()
                self.display_name = None

        wg._WARNED.clear()
        self.addCleanup(wg._WARNED.clear)
        client = wg.StoredTokenGarmin(lambda: TOKEN, factory=OldGarmin)
        with self.assertLogs("garmin_ext.worker", "WARNING") as logs:
            for _ in range(3):
                with self.assertRaises(wg.TokenStale):
                    client.ready()
        self.assertEqual(len(logs.records), 1)
        self.assertIn("_token_expires_soon", logs.output[0])


# A worker job's value that bent a Garmin path (the same traversal the app's path guard tests use).
TRAVERSAL = "x-/../../../userprofile-service/userprofile/personal-information"


@unittest.skipUnless(HAVE_GARMINCONNECT, "needs garminconnect")
class PathGuard(unittest.TestCase):
    """The app's Garmin request path guard (garmin_paths) on the worker's full-mode client: installed before
    the first client, a bent path refused as bad input before any request leaves, and the build check."""

    def setUp(self):
        import socket

        from garminconnect.client import Client

        self.Client = Client
        # garminconnect logs the refused call (log_privacy would turn it into one ERROR line): not in test output.
        library = logging.getLogger("garminconnect")
        self.addCleanup(setattr, library, "disabled", library.disabled)
        library.disabled = True
        saved = Client.__dict__["_run_request"]
        self.addCleanup(setattr, Client, "_run_request", saved)
        original = saved
        while getattr(original, "__wrapped__", None) is not None:  # an earlier test's guard or tracing wrapper
            original = original.__wrapped__
        Client._run_request = original
        self.sent = []
        sent = self.sent

        class Response:
            status_code = 200
            text = "{}"
            content = b"{}"

            def json(self):
                return {}

        class Session:
            def request(self, method, url, headers=None, **kwargs):
                sent.append((method, url))
                return Response()

        def refuse(*args, **kwargs):
            raise AssertionError("a test tried to reach the network")

        for patcher in (unittest.mock.patch.object(Client, "_fresh_api_session", lambda client: Session()),
                        unittest.mock.patch.object(Client, "_refresh_session", lambda client: None),
                        unittest.mock.patch.object(socket.socket, "connect", refuse),
                        unittest.mock.patch.object(socket, "getaddrinfo", refuse)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def garmin(self):
        garmin = wg._garminconnect_factory()
        garmin.client.di_token = "TEST-ONLY-" + "x" * 24
        garmin.display_name = "someone"
        return garmin

    def test_the_factory_installs_the_guard_before_the_first_client(self):
        self.assertFalse(garmin_paths.path_guard_installed(self.Client._run_request))
        garmin = self.garmin()
        self.assertTrue(garmin_paths.path_guard_installed(self.Client._run_request))
        guarded = self.Client._run_request
        self.garmin()
        self.assertIs(self.Client._run_request, guarded)  # installed once
        with self.assertRaises(garmin_paths.GarminPathRefused):
            garmin.client.connectapi(TRAVERSAL)
        self.assertEqual(self.sent, [])
        garmin.client.connectapi(wg.StoredTokenGarmin.PROFILE_PATH)
        self.assertEqual(len(self.sent), 1)
        self.assertTrue(self.sent[0][1].endswith(wg.StoredTokenGarmin.PROFILE_PATH))

    def test_a_bent_path_through_the_guarded_client_is_bad_input_and_never_sent(self):
        guard = Guard()
        client = wg.GuardedGarmin(self.garmin(), guard)
        # garminconnect.Garmin.connectapi re-raises the refusal as its connection error; still bad input.
        with self.assertRaisesRegex(Exception, re.escape(garmin_paths.PATH_REFUSED_TEXT)) as cm:
            client.connectapi(TRAVERSAL, params={"start": "0"})
        self.assertEqual(tool_errors.classify_exception(cm.exception)[0], tool_errors.BAD_INPUT)
        # Bad input: never a Garmin failure, so the shared guard starts no cooldown for it.
        self.assertEqual(guard.events, [("before", "worker"), ("after", "worker", tool_errors.BAD_INPUT)])
        self.assertEqual(self.sent, [])

    def test_selfcheck_fails_when_garminconnect_moves_the_hook(self):
        def _run_request(self, method, url, **kwargs):  # a bump that renamed the parameter
            return None

        wg.selfcheck()
        with unittest.mock.patch.object(self.Client, "_run_request", _run_request):
            with self.assertRaisesRegex(RuntimeError, "path guard needs a new hook"):
                wg.selfcheck()
            with self.assertRaisesRegex(RuntimeError, "path guard needs a new hook"):
                wg._garminconnect_factory()   # no unguarded client either


class StoredToken(unittest.TestCase):
    def test_missing_bad_or_expiring_token_defers(self):
        for text, expiring in ((None, False), ("", False), ("bad", False), (TOKEN, True)):
            with self.subTest(text=text, expiring=expiring):
                client = wg.StoredTokenGarmin(lambda t=text: t, factory=lambda e=expiring: FakeGarmin(e))
                with self.assertRaises(wg.TokenStale) as cm:
                    client.ready()
                self.assertIsInstance(cm.exception, jobqueue.Defer)
                self.assertEqual((cm.exception.seconds, cm.exception.group), (wg.STALE_WAIT_S, wg.TOKEN_STALE))

    def test_reading_the_store_fails(self):
        def broken():
            raise ConnectionError("db down")

        with self.assertLogs("garmin_ext.worker", "WARNING"):
            with self.assertRaises(wg.TokenStale):
                wg.StoredTokenGarmin(broken, factory=FakeGarmin).ready()

    def test_loads_profile_once_and_disables_refresh(self):
        made = []

        def factory():
            made.append(FakeGarmin())
            return made[-1]

        client = wg.StoredTokenGarmin(lambda: TOKEN, factory=factory)
        self.assertEqual(client.call("get_stats", "2026-11-23"), {"for": "someone", "cdate": "2026-11-23"})
        client.call("get_stats", "2026-11-22")
        self.assertEqual(len(made), 1)
        self.assertEqual(made[0].client.paths, [wg.StoredTokenGarmin.PROFILE_PATH])
        with self.assertRaises(wg.TokenStale):
            made[0].client._refresh_session()

    def test_reloads_when_the_held_token_nears_expiry(self):
        """The new client built for a fresh token used to keep display_name None (the profile
        counted as loaded), so get_stats failed and the sleep URL read .../dailySleepData/None. Now its
        profile loads again, through the guard as a call of its own."""
        made = []

        def factory():
            made.append(UrlGarmin())
            return made[-1]

        guard = Guard()
        inner = wg.StoredTokenGarmin(lambda: TOKEN, factory=factory)
        client = wg.GuardedGarmin(inner, guard)
        self.assertEqual(client.get_sleep_data("2026-11-22"), "/dailySleepData/someone?date=2026-11-22")
        made[0].client.expiring = True
        self.assertEqual(client.get_sleep_data("2026-11-23"), "/dailySleepData/someone?date=2026-11-23")
        self.assertEqual(len(made), 2)
        self.assertEqual(made[1].display_name, "someone")
        self.assertEqual(made[1].client.paths, [wg.StoredTokenGarmin.PROFILE_PATH])
        self.assertFalse(any("None" in url for g in made for url in g.urls), [g.urls for g in made])
        self.assertEqual(len(guard.events), 8, "two profile calls and two data calls, each guarded")

    def test_no_display_name_never_calls_garmin_and_marks_nothing_final(self):
        """A profile without a display name raises ProfileMissing (a Defer: the job waits, no
        day is stored), and a client without one never makes a data call."""
        made = []

        def factory():
            made.append(UrlGarmin())
            made[-1].client.connectapi = lambda path: made[-1].client.paths.append(path) or {"displayName": None}
            return made[-1]

        client = wg.GuardedGarmin(wg.StoredTokenGarmin(lambda: TOKEN, factory=factory), Guard())
        with self.assertRaises(wg.ProfileMissing) as cm:
            client.get_sleep_data("2026-11-22")
        self.assertIsInstance(cm.exception, jobqueue.Defer)
        self.assertEqual(cm.exception.group, wg.PROFILE_MISSING)
        self.assertEqual(made[0].urls, [])
        inner = wg.StoredTokenGarmin(lambda: TOKEN, factory=UrlGarmin)
        inner.ready()
        inner._profile_loaded = True        # as if loaded, but the client still has no display name
        with self.assertRaises(wg.ProfileMissing):
            inner.call("get_sleep_data", "2026-11-22")


class UrlGarmin(FakeGarmin):
    """Builds its URLs from display_name as garminconnect does."""

    def __init__(self, expiring=False):
        super().__init__(expiring)
        self.urls = []

    def get_sleep_data(self, cdate):
        url = f"/dailySleepData/{self.display_name}?date={cdate}"
        self.urls.append(url)
        return url


class Guard:
    def __init__(self, block=None):
        self.block = block
        self.events = []

    def before_call(self, caller):
        self.events.append(("before", caller))
        if self.block is not None:
            raise self.block

    def after_outcome(self, caller, group):
        self.events.append(("after", caller, group))


class GuardBlocked(Exception):
    def __init__(self, group, until):
        super().__init__(group)
        self.group, self.until = group, until


class Guarded(unittest.TestCase):
    def test_order_and_outcomes(self):
        class Inner:
            def get_stats(self, cdate):
                return 1

            def get_sleep_data(self, cdate):
                raise OSError("Garmin rate limit hit (HTTP 429)")

        guard = Guard()
        client = wg.GuardedGarmin(Inner(), guard)
        self.assertEqual(client.get_stats("2026-11-23"), 1)
        with self.assertRaises(OSError):
            client.get_sleep_data("2026-11-23")
        self.assertEqual(guard.events, [("before", "worker"), ("after", "worker", None),
                                         ("before", "worker"), ("after", "worker", "rate_limited")])
        with self.assertRaises(AttributeError):
            client._private  # noqa: B018 - the attribute access itself must raise

    def test_blocked_call_never_reaches_garmin(self):
        class Inner:
            def get_stats(self, cdate):
                raise AssertionError("called")

        guard = Guard(block=GuardBlocked("cooldown", 123.0))
        with self.assertRaises(GuardBlocked):
            wg.GuardedGarmin(Inner(), guard).get_stats("2026-11-23")
        self.assertEqual(guard.events, [("before", "worker")])

    def test_only_reads_pass(self):
        """An allowlist (get_*, download_*, connectapi GET), not every public method."""
        class Inner:
            def __getattr__(self, name):
                raise AssertionError(f"reached Garmin: {name}")

        guard = Guard()
        client = wg.GuardedGarmin(Inner(), guard)
        for name in ("upload_activity", "delete_activity", "add_weigh_in", "set_gear_default", "logout",
                     "garth", "client", "login"):
            with self.subTest(name=name):
                with self.assertRaises(AttributeError):
                    getattr(client, name)
                self.assertIsNone(getattr(client, name, None))
        for kwargs in ({"method": "POST"}, {"method": "delete"}, {"json": {}}, {"data": "x"}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(PermissionError):
                    client.connectapi("/activity-service/activity/1", **kwargs)
        with self.assertRaises(PermissionError):
            client.connectapi("/x", "POST")
        # Connectapi takes only params=; headers= (X-HTTP-Method-Override) and every other
        # keyword are refused, and get_* / download_* refuse keywords that carry a body, headers or a method.
        for kwargs in ({"headers": {"X-HTTP-Method-Override": "DELETE"}}, {"method": "GET"}, {"api": True},
                       {"params": {}, "headers": {}}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(PermissionError):
                    client.connectapi("/activity-service/activity/1", **kwargs)
        for name in ("get_stats", "download_activity"):
            for kw in ("data", "json", "files", "headers", "method"):
                with self.subTest(name=name, kw=kw):
                    with self.assertRaises(PermissionError):
                        getattr(client, name)("2026-11-23", **{kw: "x"})
        self.assertEqual(guard.events, [], "a refused call takes no token")

        class Reads:
            def connectapi(self, path, **kwargs):
                return ("GET", path, kwargs)

            def download_activity(self, activity_id):
                return b"fit"

        client = wg.GuardedGarmin(Reads(), Guard())
        self.assertEqual(client.connectapi("/a", params={"start": "0"}), ("GET", "/a", {"params": {"start": "0"}}))
        self.assertEqual(client.download_activity(1), b"fit")

    def test_private_names_are_not_reachable(self):
        """gc._inner (or any name starting with "_") used to hand out the unguarded client."""
        class Inner:
            def get_stats(self, cdate):
                return 1

        client = wg.GuardedGarmin(Inner(), Guard())
        for name in ("_inner", "_guard", "_core", "_wait", "_admit", "_guarded", "_private", "_GuardedGarmin__x"):
            with self.subTest(name=name):
                with self.assertRaises(AttributeError):
                    getattr(client, name)
        with self.assertRaises(AttributeError):
            client._inner = Inner()
        self.assertEqual(client.get_stats("2026-11-23"), 1)
        self.assertEqual(client.for_job(lost=threading.Event()).get_stats("2026-11-23"), 1)
        with self.assertRaises(AttributeError):
            client.__dict__   # noqa: B018 - no instance dict to reach the state through

    def test_the_profile_call_is_guarded_as_its_own_call(self):
        """The stored-token client's first call loads the profile; that call takes its own token."""
        made = []

        def factory():
            made.append(FakeGarmin())
            return made[-1]

        guard = Guard()
        client = wg.GuardedGarmin(wg.StoredTokenGarmin(lambda: TOKEN, factory=factory), guard)
        self.assertEqual(client.get_stats("2026-11-23"), {"for": "someone", "cdate": "2026-11-23"})
        self.assertEqual(guard.events, [("before", "worker"), ("after", "worker", None),
                                        ("before", "worker"), ("after", "worker", None)])
        client.get_stats("2026-11-22")
        self.assertEqual(len(guard.events), 6, "the profile loads once")
        self.assertEqual(made[0].client.paths, [wg.StoredTokenGarmin.PROFILE_PATH])

    def test_stale_token_takes_no_bucket_token(self):
        guard = Guard()
        client = wg.GuardedGarmin(wg.StoredTokenGarmin(lambda: None, factory=FakeGarmin), guard)
        with self.assertRaises(wg.TokenStale):
            client.get_stats("2026-11-23")
        self.assertEqual(guard.events, [])

    def test_build_full_mode_and_bad_mode(self):
        class Store:
            def get(self):
                return TOKEN

        client = wg.build({"GARMIN_AUTH": "full"}, Guard(), token_store=Store())
        self.assertIsInstance(wg.inner_client(client), wg.StoredTokenGarmin)
        with self.assertRaises(ValueError):
            wg.build({"GARMIN_AUTH": "full", wg.TOKEN_KEY_ENV: "short"}, Guard(), dsn="postgresql://x")
        for env in ({}, {"GARMIN_AUTH": "other"}):
            with self.assertRaises(ValueError):
                wg.build(env, Guard())

    def test_build_key_mode_checks_the_key(self):
        with tempfile.TemporaryDirectory() as d:
            cache = os.path.join(d, "garmin-key", "token.json")
            with self.assertRaises(ValueError) as cm:
                wg.build({"GARMIN_AUTH": "key", "GARMIN_KEY_TOKEN_CACHE": cache}, Guard())
        self.assertIn("GARMIN_PERSONAL_API_KEY", str(cm.exception))


@contextlib.contextmanager
def logging_state():
    """Saves and restores the root logger and the Garmin library loggers around a logging setup."""
    names = ("", "garminconnect", "garminconnect.client", "garth", "garmin_ext.garmin_library")
    saved = {n: (list(logging.getLogger(n).handlers), logging.getLogger(n).level, logging.getLogger(n).propagate)
             for n in names}
    root = logging.getLogger()
    root.handlers[:] = []
    try:
        yield
    finally:
        for n, (handlers, level, propagate) in saved.items():
            lg = logging.getLogger(n)
            lg.handlers[:] = handlers
            lg.setLevel(level)
            lg.propagate = propagate


class WorkerLogPrivacy(unittest.TestCase):
    """Garminconnect logs a failed call as "API call failed for path
    '/usersummary-service/usersummary/daily/<display name>': API Error 500 - {<Garmin's answer>}" with a
    traceback, and the worker's plain logging.basicConfig printed it. worker.configure_logging() hides it."""

    NAME = "canary-display-name"
    BODY = '{"restingHeartRate": 4217, "note": "canary garmin body"}'

    def test_a_garmin_error_through_get_stats_logs_no_name_and_no_body(self):
        import tool_errors
        import worker

        secret = "canary-registered-secret-value"
        tool_errors.register_secret(secret)
        name, body = self.NAME, self.BODY

        class Inner:
            def get_stats(self, cdate):
                path = f"/usersummary-service/usersummary/daily/{name}"
                try:
                    raise ConnectionError(f"API Error 500 - {body}")
                except ConnectionError as exc:
                    logging.getLogger("garminconnect").exception("API call failed for path '%s': %s", path, exc)
                    logging.getLogger("garminconnect.client").warning("%s failed: %s", path, exc)
                    logging.getLogger("garth").error("GET %s -> %s", path, body)
                    raise

        err = io.StringIO()
        with logging_state(), contextlib.redirect_stderr(err):
            worker.configure_logging()
            with self.assertRaises(ConnectionError):
                wg.GuardedGarmin(Inner(), Guard()).get_stats("2026-11-23")
            logging.getLogger("garmin_ext.worker").warning("worker: a line with %s in it", secret)
        out = err.getvalue()
        for leak in (name, "4217", "canary garmin body", "usersummary", "Traceback", secret):
            self.assertNotIn(leak, out)
        self.assertEqual(out.count("garmin library: one "), 3, out)
        self.assertIn("exception ConnectionError", out)
        self.assertIn("worker: a line with <redacted> in it", out)


class Clock:
    def __init__(self, t):
        self.t = t
        self._lock = threading.Lock()

    def __call__(self):
        with self._lock:
            return self.t

    def advance(self, s):
        with self._lock:
            self.t += s


class MultiCallJobWithTheRealGuard(unittest.TestCase):
    """A job of several Garmin calls used to be deferred at call 2 (the 5 s bucket) and restarted
    from call 1 each time, so it never finished. Now the short wait happens inside the job."""

    def setUp(self):
        import garmin_guard
        import garmin_guard_pg
        import worker

        self.gg, self.worker = garmin_guard, worker
        tz = ZoneInfo("Europe/Berlin")
        self.clock = Clock(dt.datetime(2026, 11, 24, 13, tzinfo=tz).timestamp())
        self.shared = garmin_guard_pg.LocalSharedState(tz, clock=self.clock)
        self.guard = garmin_guard.GarminGuard(shared=self.shared, zone="Europe/Berlin", clock=self.clock)
        self.calls = []
        test = self

        class Inner:
            def get_stats(self, day):
                test.calls.append(("stats", test.clock()))
                return {}

            def get_sleep_data(self, day):
                test.calls.append(("sleep", test.clock()))
                return {}

            def get_hrv_data(self, day):
                test.calls.append(("hrv", test.clock()))
                return {}

            def connectapi(self, path, **kwargs):
                test.calls.append(("activities", test.clock()))
                return []

        self.client = wg.GuardedGarmin(Inner(), self.guard, clock=self.clock, sleep=self.clock.advance)
        self.q = jobqueue.MemoryJobQueue(clock=self.clock)

    def handler(self, job, ctx):
        for name in ("get_stats", "get_sleep_data", "get_hrv_data"):
            getattr(ctx.garmin, name)("2026-11-23")
        ctx.garmin.connectapi("/activitylist-service/activities/search/activities", params={"start": "0"})

    def run_worker(self):
        w = self.worker.Worker(self.q, handlers={"index_day": self.worker.Handler(self.handler, True)},
                               guard=self.guard, garmin=self.client, clock=self.clock, heartbeat_s=0)
        self.q.enqueue("index_day", "index_day:2026-11-23", {"date": "2026-11-23"})
        self.assertTrue(w.run_once())
        return w

    def test_four_calls_end_done_with_exactly_four_tokens(self):
        w = self.run_worker()
        self.assertEqual(w.outcomes[0][2], jobqueue.DONE)
        self.assertEqual(len(self.shared.granted), 4)
        self.assertEqual([c[0] for c in self.calls], ["stats", "sleep", "hrv", "activities"])
        times = [c[1] for c in self.calls]
        self.assertTrue(all(b - a >= 5 for a, b in zip(times, times[1:])))

    def test_a_live_call_that_ends_soon_is_waited_out_a_long_wait_is_not(self):
        self.shared.heartbeat_live()               # a live call just ran: workers wait 60 s
        self.clock.advance(40)                     # 20 s left: waited out inside the job
        w = self.run_worker()
        self.assertEqual(w.outcomes[0][2], jobqueue.DONE)
        self.assertEqual(len(self.shared.granted), 4)
        self.shared.heartbeat_live()               # 60 s left: more than INLINE_WAIT_MAX_S, the job is deferred
        self.calls.clear()
        self.q.enqueue("index_day", "index_day:2026-11-22", {"date": "2026-11-22"})
        self.assertTrue(w.run_once())
        self.assertEqual(w.outcomes[-1][2], "blocked")
        self.assertEqual(self.calls, [])

    def test_a_lost_lease_cuts_a_real_wait_short(self):
        """_wait waited on the stop event only, so a lost lease ran out a wait of up to 30 s.
        A real (not simulated) 20 s bucket wait now ends within about a second of the lease loss."""
        class Blocked(Exception):
            pass

        Blocked.__name__ = "GuardBlocked"

        class Busy:
            def before_call(self, caller):
                exc = Blocked("bucket")
                exc.group, exc.until = "bucket", time.time() + 20
                raise exc

            def after_outcome(self, caller, group):
                pass

        class Inner:
            def get_stats(self, cdate):
                raise AssertionError("called")

        lost, stop = threading.Event(), threading.Event()
        client = wg.GuardedGarmin(Inner(), Busy()).for_job(lost=lost, stop=stop)
        threading.Timer(0.3, lost.set).start()
        started = time.monotonic()
        with self.assertRaises(Blocked):
            client.get_stats("2026-11-23")
        self.assertLess(time.monotonic() - started, 0.3 + wg.WAIT_SLICE_S + 0.7)

    def test_a_lost_lease_or_stop_ends_the_wait(self):
        lost = threading.Event()
        client = self.client.for_job(lost=lost, stop=threading.Event())
        client.get_stats("2026-11-23")
        lost.set()
        with self.assertRaises(self.gg.GuardBlocked) as cm:
            client.get_sleep_data("2026-11-23")
        self.assertEqual(cm.exception.group, self.gg.BUCKET)
        self.assertEqual(len(self.shared.granted), 1)


if __name__ == "__main__":
    unittest.main()
