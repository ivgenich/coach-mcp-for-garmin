"""Tests for launch.py: the optional features in launch.py: their config check, _setup_features(), the
token refresh hook, the history tools and ROLE dispatch (feature_startup.py).

    cd ext && python -m unittest test_launch_features

Shared helpers: test_launch_support.py.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import os
import secrets
import sys
import threading
import types
import unittest
from unittest import mock

import tool_errors  # noqa: E402
# --- The call guard and the optional features (tracing, history search; feature_config) --------------------

import base64  # noqa: E402
import feature_config  # noqa: E402
import garmin_guard  # noqa: E402

from test_launch_support import (captured_records, fake_db_url, fake_key, fake_modules, fake_secret, FakeTelemetry,
    FULL, HAVE_UPSTREAM, keep_tracing_hooks, key_env, KEY_MODE_TOOL_NAMES, load_launch, quiet, serve_key_mode, Stubs,
    UpstreamCase)

FEATURE_VARS = ("TRACING", "HISTORY_SEARCH", "ROLE", "DATABASE_URL", "HISTORY_FORGET", "HISTORY_TOKEN_KEY",
                "WORKER_CONCURRENCY", "OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_HEADERS", "OTEL_SDK_DISABLED")


def fake_token_key():
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()


class FeatureCheckConfig(unittest.TestCase):
    """--check-config with the features: their lines come last, the OK line names what is on, and the worker
    and enqueue roles check only what they use. Standard library only."""

    def setUp(self):
        self.launch = load_launch(self, Stubs().modules)

    def run_cli(self, env, packages=True):
        env = {key: value for key, value in {**FULL, "REPORT_TZ": "UTC", **env}.items() if value is not None}
        with mock.patch.object(self.launch, "allowed_tools", return_value=frozenset({"get_stats"})) as tools, \
                mock.patch.object(self.launch, "_has_module", return_value=packages), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            code = self.launch._check_config_cli(env)
        self.tools = tools
        return code, err.getvalue().splitlines()

    def test_off_prints_what_it_always_printed(self):
        self.assertEqual(self.run_cli({}), (0, ["launch: config OK (1 read-only tools)"]))
        self.assertEqual(self.run_cli({"TRACING": "off", "HISTORY_SEARCH": "off"}),
                         (0, ["launch: config OK (1 read-only tools)"]))

    def test_tracing_on(self):
        env = {"TRACING": "on", "OTEL_EXPORTER_OTLP_ENDPOINT": "https://otlp.example.com/otlp"}
        self.assertEqual(self.run_cli(env), (0, ["launch: config OK (1 read-only tools; tracing on)"]))
        self.assertEqual(self.run_cli(env, packages=False),
                         (1, ["launch: config error: " + feature_config.ERR_T7]))

    def test_history_on_needs_its_packages(self):
        env = {"HISTORY_SEARCH": "on", "DATABASE_URL": fake_db_url(), "HISTORY_TOKEN_KEY": fake_token_key()}
        self.assertEqual(self.run_cli(env), (0, ["launch: config OK (1 read-only tools; history search on)"]))
        self.assertEqual(self.run_cli(env, packages=False),
                         (1, ["launch: config error: " + feature_config.ERR_H11]))

    def test_key_mode_line_names_the_features(self):
        env = {"GARMIN_AUTH": "key", "GARMIN_PERSONAL_API_KEY": fake_key(), "TRACING": "on",
               "OTEL_EXPORTER_OTLP_ENDPOINT": "https://otlp.example.com/otlp"}
        self.assertEqual(self.run_cli(env), (0, [
            "launch: config OK (1 read-only tools, Garmin Personal API key; tracing on)"]))

    def test_feature_lines_come_after_the_others_and_hide_values(self):
        secret = fake_secret()
        code, lines = self.run_cli({"REPORT_TZ": None, "TRACING": "maybe", "DATABASE_URL": secret,
                                    "HISTORY_SEARCH": "on", "HISTORY_TOKEN_KEY": secret})
        self.assertEqual(code, 1)
        self.assertTrue(lines[0].startswith("launch: config error: REPORT_TZ"), lines)
        self.assertEqual(lines[1:], ["launch: config error: " + line
                                     for line in (feature_config.ERR_T1, feature_config.ERR_H3, feature_config.ERR_H7)])
        self.assertNotIn(secret, "\n".join(lines))

    def test_the_worker_checks_only_what_it_uses(self):
        env = {"ROLE": "worker", "HISTORY_SEARCH": "on", "DATABASE_URL": fake_db_url(),
               "HISTORY_TOKEN_KEY": fake_token_key(), "GARMIN_MCP_TRANSPORT": "streamable-http"}  # no OWNER_SECRET
        self.assertEqual(self.run_cli(env), (0, ["launch: config OK (ROLE=worker, history search on)"]))
        self.tools.assert_not_called()
        code, lines = self.run_cli({**env, "GARMIN_AUTH": None})
        self.assertEqual(code, 1)
        self.assertTrue(lines[0].startswith("launch: config error: GARMIN_AUTH"), lines)

    def test_enqueue(self):
        env = {"ROLE": "enqueue", "HISTORY_SEARCH": "on", "DATABASE_URL": fake_db_url(), "GARMIN_AUTH": None}
        self.assertEqual(self.run_cli(env), (0, ["launch: config OK (ROLE=enqueue, history search on)"]))
        self.assertEqual(self.run_cli({"ROLE": "enqueue"}),
                         (1, ["launch: config error: " + feature_config.ERR_H5.format(role="enqueue")]))


class SetupFeatures(unittest.TestCase):
    """_setup_features(): tracing, the secrets, the guard, the forget and the catch-up, with fake feature
    modules. Standard library only."""

    def setUp(self):
        self.launch = load_launch(self, Stubs().modules)
        self.launch._KEY_MODE = True  # no refresh lock on garminconnect here
        keep_tracing_hooks(self)
        root = logging.getLogger()
        saved = (list(root.handlers), root.level)
        self.addCleanup(lambda: (root.handlers.__setitem__(slice(None), saved[0]), root.setLevel(saved[1])))
        # Runs first (cleanups are LIFO): a token thread a test started must not log into the next test.
        self.addCleanup(self.join_token_threads)

    @staticmethod
    def join_token_threads():
        for thread in threading.enumerate():
            if thread.name.startswith("history-token-"):
                thread.join(10)

    def setup_features(self, env, allowed=frozenset({"get_stats"})):
        env = dict(env)
        cfg = feature_config.load(env)
        with captured_records() as records:
            self.launch._setup_features(cfg, env, allowed)
        return cfg, env, records

    def test_off_builds_an_in_memory_guard_and_nothing_else(self):
        cfg, env, records = self.setup_features({"REPORT_TZ": "UTC", "KEEP": "1"})
        self.assertIsNone(self.launch._TELEMETRY)
        self.assertIsInstance(self.launch._GUARD, garmin_guard.GarminGuard)
        self.assertIsInstance(self.launch._GUARD._state, garmin_guard.MemoryState)
        self.assertEqual(env, {"REPORT_TZ": "UTC", "KEEP": "1"})
        self.assertEqual(records, [])

    def test_tracing_is_set_up_then_the_secrets_leave_the_environment(self):
        fake = FakeTelemetry()
        fake_modules(self, telemetry=fake)
        token = "glc_" + secrets.token_urlsafe(30)
        header = "Authorization=Basic%20" + base64.b64encode(f"123456:{token}".encode()).decode()
        env = {"TRACING": "on", "OTEL_EXPORTER_OTLP_ENDPOINT": "https://otlp.example.com/otlp",
               "OTEL_EXPORTER_OTLP_HEADERS": header}
        seen = {}
        fake.setup = lambda cfg: seen.update(headers=env.get("OTEL_EXPORTER_OTLP_HEADERS"))
        cfg, env, _ = self.setup_features(env, allowed=frozenset({"get_stats", "get_daily_status"}))
        self.assertEqual(seen["headers"], header)  # still there while setup() runs
        self.assertNotIn("OTEL_EXPORTER_OTLP_HEADERS", env)
        self.assertIs(self.launch._TELEMETRY, fake)
        self.assertIn(("register_tools", ("get_daily_status", "get_stats")), fake.calls)
        self.assertNotIn(token, tool_errors.redact(f"export failed: 401 for {token}"))

    def test_history_tools_are_tool_labels_when_history_search_is_on(self):
        self.fake_history()
        fake = FakeTelemetry()
        fake_modules(self, telemetry=fake)
        with mock.patch.object(self.launch, "_start_catch_up"):
            self.setup_features(self.history_env(TRACING="on",
                                                 OTEL_EXPORTER_OTLP_ENDPOINT="https://otlp.example.com/otlp"))
        self.assertIn(("register_tools", ("find_similar_weeks", "get_stats", "search_history")), fake.calls)

    def test_a_tracing_setup_that_fails_leaves_tracing_off(self):
        fake_modules(self, telemetry=FakeTelemetry(fail=True))
        _, _, records = self.setup_features({"TRACING": "on",
                                             "OTEL_EXPORTER_OTLP_ENDPOINT": "https://otlp.example.com/otlp"})
        self.assertIsNone(self.launch._TELEMETRY)
        self.assertIn("tracing: setup failed (RuntimeError); tracing stays off", [r.getMessage() for r in records])

    def history_env(self, **changes):
        env = {"REPORT_TZ": "UTC", "HISTORY_SEARCH": "on", "DATABASE_URL": fake_db_url(), "GARMIN_AUTH": "key"}
        env.update(changes)
        return env

    def fake_history(self, forget_result=None, forget_error=None):
        calls = []

        class Store:
            def __init__(self, dsn):
                calls.append(("store", dsn))

            def forget_all(self):
                calls.append(("forget_all",))
                if forget_error:
                    raise forget_error
                return forget_result

        class Shared(garmin_guard.MemoryState):
            def __init__(self, dsn, tz, live=False):
                super().__init__()
                calls.append(("shared", dsn, str(tz), live))

            def migrate(self):
                calls.append(("shared.migrate",))

        history_store = types.ModuleType("history_store")
        history_store.PgHistoryStore = Store
        guard_pg = types.ModuleType("garmin_guard_pg")
        guard_pg.PgSharedState = Shared
        retrieval, embed = types.ModuleType("history_retrieval"), types.ModuleType("history_embed")
        retrieval.forget = lambda cfg: calls.append(("retrieval.forget",)) or "the vector table"
        embed.forget_caches = lambda: calls.append(("embed.forget_caches",)) or "no caches"
        fake_modules(self, history_store=history_store, garmin_guard_pg=guard_pg, history_retrieval=retrieval,
                     history_embed=embed)
        return calls

    def test_history_uses_the_shared_guard_state_and_starts_the_catch_up(self):
        calls = self.fake_history()
        started = []
        with mock.patch.object(self.launch, "_start_catch_up", side_effect=lambda cfg: started.append(cfg)):
            cfg, env, records = self.setup_features(self.history_env())
        dsn = cfg.database_url
        self.assertEqual(calls, [("shared", dsn, "UTC", True)])  # its table: in the startup thread
        self.assertEqual(type(self.launch._GUARD._state).__name__, "Shared")
        self.assertEqual(started, [cfg])
        self.assertNotIn("DATABASE_URL", env)
        self.assertNotIn(dsn, " ".join(r.getMessage() for r in records))
        self.assertNotIn(dsn.split(":")[2].split("@")[0], tool_errors.redact("connect failed: " + dsn))

    def test_forget_runs_before_anything_and_keeps_the_tools_off(self):
        calls = self.fake_history(forget_result={"schema_dropped": True, "days": 3, "weeks": 1})
        with mock.patch.object(self.launch, "_start_catch_up") as catch_up:
            cfg, _, records = self.setup_features(self.history_env(HISTORY_FORGET="yes"))
        self.assertEqual([c for c in calls if "forget" in c[0]],
                         [("forget_all",), ("retrieval.forget",), ("embed.forget_caches",)])
        catch_up.assert_not_called()
        self.assertFalse(self.launch._history_tools_wanted(cfg))
        messages = [r.getMessage() for r in records]
        self.assertIn("history forget: removed days: 3, schema_dropped: True, weeks: 1; the vector table; no caches",
                      messages)
        self.assertTrue(any(m.startswith("history forget: done. Now delete HISTORY_FORGET") for m in messages))
        self.assertIn("history search: the history tools are off while HISTORY_FORGET is set", messages)

    def test_forget_with_history_off(self):
        calls = self.fake_history(forget_result=["history schema"])
        with mock.patch.object(self.launch, "_start_token_cleanup") as cleanup:
            cfg, _, records = self.setup_features({"REPORT_TZ": "UTC", "DATABASE_URL": fake_db_url(),
                                                   "HISTORY_FORGET": "yes"})
        cleanup.assert_called_once_with(cfg)
        self.assertIn(("forget_all",), calls)
        self.assertNotIn(("shared.migrate",), calls)  # history off: the guard stays in memory
        self.assertIn("history forget: removed history schema; the vector table; no caches",
                      [r.getMessage() for r in records])

    def test_a_failed_forget_is_one_error_line_and_no_value(self):
        self.fake_history(forget_error=ConnectionError("could not connect to postgresql://u:pw@h/db"))
        _, _, records = self.setup_features(self.history_env(HISTORY_FORGET="yes"))
        errors = [r.getMessage() for r in records if r.levelno == logging.ERROR]
        self.assertEqual(len(errors), 1)
        self.assertTrue(errors[0].startswith("history forget: FAILED in history_store (ConnectionError)"), errors)
        self.assertNotIn("pw@h", errors[0])

    @staticmethod
    def fake_history_store_migrate(calls):
        sys.modules["history_store"].PgHistoryStore.migrate = lambda self: calls.append(("store.migrate",))

    def test_history_off_with_a_database_starts_the_token_cleanup(self):
        with mock.patch.object(self.launch, "_start_token_cleanup") as cleanup:
            cfg, _, _ = self.setup_features({"REPORT_TZ": "UTC", "DATABASE_URL": fake_db_url()})
        cleanup.assert_called_once_with(cfg)
        for env in ({"REPORT_TZ": "UTC"}, self.history_env(HISTORY_FORGET="yes")):
            self.fake_history()
            with mock.patch.object(self.launch, "_start_token_cleanup") as cleanup, \
                    mock.patch.object(self.launch, "_start_catch_up"):
                self.setup_features(env)
            cleanup.assert_not_called()

    def test_history_on_in_key_mode_deletes_a_stored_full_mode_token(self):
        """With HISTORY_SEARCH=on and GARMIN_AUTH=key, before this fix neither the publisher nor the cleanup ran,
        so a token row an earlier full-mode setup stored stayed in Postgres."""
        self.fake_history()
        with mock.patch.object(self.launch, "_start_token_cleanup") as cleanup, \
                mock.patch.object(self.launch, "_start_token_publisher") as publisher, \
                mock.patch.object(self.launch, "_start_catch_up"):
            cfg, _, _ = self.setup_features(self.history_env())
        cleanup.assert_called_once_with(cfg, reason=self.launch.TOKEN_CLEANUP_KEY_MODE)
        publisher.assert_not_called()
        delete = mock.Mock(return_value=True)
        fake_modules(self, worker_garmin=types.SimpleNamespace(delete_stored_token=delete))
        with mock.patch.object(self.launch, "_has_module", return_value=True), captured_records() as records:
            self.launch._start_token_cleanup(cfg, reason=self.launch.TOKEN_CLEANUP_KEY_MODE).join(10)
        delete.assert_called_once_with(cfg.database_url)
        self.assertEqual([r.getMessage() for r in records],
                         ["history search runs in key mode: the stored Garmin token was deleted from the database"])

    def token_cleanup(self, delete):
        worker_garmin = types.ModuleType("worker_garmin")
        if delete is not None:
            worker_garmin.delete_stored_token = delete
        fake_modules(self, worker_garmin=worker_garmin)
        cfg = feature_config.load({"REPORT_TZ": "UTC", "DATABASE_URL": fake_db_url()})
        with mock.patch.object(self.launch, "_has_module", return_value=True), captured_records() as records:
            thread = self.launch._start_token_cleanup(cfg)
            if thread is not None:
                thread.join(10)
        return cfg, thread, records

    def test_the_token_cleanup_deletes_in_the_background_and_logs_one_line(self):
        delete = mock.Mock(return_value=True)
        cfg, thread, records = self.token_cleanup(delete)
        self.assertIsNotNone(thread)
        self.assertTrue(thread.daemon)
        delete.assert_called_once_with(cfg.database_url)
        self.assertEqual([r.getMessage() for r in records],
                         ["history search is off: the stored Garmin token was deleted from the database"])
        self.assertNotIn(cfg.database_url, " ".join(r.getMessage() for r in records))

    def test_the_token_cleanup_is_quiet_when_nothing_was_deleted_or_it_fails(self):
        for delete in (mock.Mock(return_value=False), mock.Mock(side_effect=RuntimeError("db down"))):
            _, thread, records = self.token_cleanup(delete)
            self.assertIsNotNone(thread)
            delete.assert_called_once()
            self.assertEqual(records, [])

    def test_the_token_cleanup_skips_without_the_function_or_the_packages(self):
        _, thread, records = self.token_cleanup(None)       # worker_garmin without delete_stored_token
        self.assertIsNone(thread)
        self.assertEqual(records, [])
        delete = mock.Mock(return_value=True)
        fake_modules(self, worker_garmin=types.SimpleNamespace(delete_stored_token=delete))
        cfg = feature_config.load({"REPORT_TZ": "UTC", "DATABASE_URL": fake_db_url()})
        with mock.patch.object(self.launch, "_has_module", return_value=False), captured_records() as records:
            self.assertIsNone(self.launch._start_token_cleanup(cfg))
        delete.assert_not_called()
        # Without the database driver the stored token cannot be checked: one fixed line.
        self.assertEqual([(r.levelno, r.getMessage()) for r in records],
                         [(logging.WARNING, self.launch.TOKEN_CLEANUP_NO_DRIVER)])
        self.assertNotIn(cfg.database_url, self.launch.TOKEN_CLEANUP_NO_DRIVER)

    def test_the_catch_up_calls_enqueue_in_a_thread(self):
        calls = self.fake_history()
        jobqueue = types.ModuleType("jobqueue")
        enqueue = types.ModuleType("enqueue")

        class Queue:
            def __init__(self, dsn, application_name=None):
                calls.append(("queue", application_name))

            def migrate(self):
                calls.append(("queue.migrate",))

            def enqueue(self, kind, key, payload):
                calls.append(("enqueue", kind))

        jobqueue.PgJobQueue = Queue
        history_jobs = types.ModuleType("history_jobs")
        history_jobs.scheduled_jobs = lambda store, today: [("sweep", "sweep:x", {})]
        fake_modules(self, history_jobs=history_jobs)
        self.fake_history_store_migrate(calls)
        self.launch._GUARD = garmin_guard.GarminGuard(mock.Mock(migrate=lambda: calls.append(("guard.migrate",))))
        enqueue.catch_up = lambda queue, today, store=None, telemetry=None: calls.append(
            ("catch_up", type(queue).__name__, type(store).__name__)) or {"added": 4}
        fake_modules(self, jobqueue=jobqueue, enqueue=enqueue)
        cfg = feature_config.load(self.history_env())
        with mock.patch.dict(os.environ, {"REPORT_TZ": "UTC"}), captured_records() as records:
            self.launch._start_catch_up(cfg).join(10)
        self.assertIn(("catch_up", "Queue", "Store"), calls)
        order = [c[0] for c in calls if c[0] in ("store.migrate", "guard.migrate", "queue.migrate", "enqueue")]
        self.assertEqual(order, ["store.migrate", "guard.migrate", "queue.migrate", "enqueue"])
        self.assertIn("history search: startup catch-up enqueued 4 jobs", [r.getMessage() for r in records])
        enqueue.catch_up = mock.Mock(side_effect=RuntimeError("db down"))
        with mock.patch.dict(os.environ, {"REPORT_TZ": "UTC"}), captured_records() as records:
            self.launch._start_catch_up(cfg).join(10)
        # Only this thread's lines: a token-cleanup daemon thread left by another test may log here too.
        self.assertEqual([r.getMessage() for r in records if r.threadName != "history-token-cleanup"],
                         ["history search: the startup catch-up could not be enqueued (RuntimeError)"])

    def test_full_mode_publishes_the_token_for_the_worker(self):
        self.fake_history()
        self.launch._KEY_MODE = False
        worker_garmin = types.ModuleType("worker_garmin")
        made, hooks = [], []

        class TokenStore:
            def __init__(self, dsn, key):
                made.append(("store", len(key)))

            def migrate(self):
                made.append(("migrate",))

        class Publisher:
            def __init__(self, store, token_dir, **kw):
                made.append(("publisher", token_dir))
                hooks.append(kw.get("refresh"))

            def start(self):
                made.append(("start",))

        worker_garmin.PgTokenStore, worker_garmin.TokenPublisher = TokenStore, Publisher
        fake_modules(self, worker_garmin=worker_garmin)
        with mock.patch.object(self.launch, "_start_catch_up"), \
                mock.patch.object(garmin_guard, "install_refresh_lock", return_value=True) as lock, \
                mock.patch.object(garmin_guard, "install_atomic_token_dump", return_value=True) as atomic:
            self.setup_features(self.history_env(GARMIN_AUTH="full", HISTORY_TOKEN_KEY=fake_token_key(),
                                                 GARMINTOKENS="/data/tokens"))
            for thread in threading.enumerate():
                if thread.name == "history-token-setup":
                    thread.join(10)
        lock.assert_called_once_with(self.launch._GUARD.refresh_lock)
        atomic.assert_called_once_with()
        self.assertEqual(made, [("store", 32), ("publisher", "/data/tokens"), ("migrate",), ("start",)])
        self.assertEqual(hooks, [self.launch._refresh_app_token])


class RefreshAppToken(unittest.TestCase):
    """_refresh_app_token(), the token publisher's refresh hook: it refreshes the app's own Garmin client,
    under the guard's refresh lock, only when the token expires soon, and never raises. Standard library
    only (a fake client class stands in for garminconnect's)."""

    def setUp(self):
        self.launch = load_launch(self, Stubs().modules)
        self.launch._GUARD = garmin_guard.GarminGuard(zone="UTC")
        lock = self.launch._GUARD.refresh_lock
        calls = []

        class Client:
            is_authenticated = True
            soon = True
            fail = False

            def _token_expires_soon(self):
                return self.soon

            def _refresh_session(self):
                calls.append(lock.locked())
                if self.fail:
                    raise ConnectionError("token text must not be logged")

        self.Client, self.calls = Client, calls

    def wrap_like_install_refresh_lock(self):
        """The method as install_refresh_lock leaves it: the same (non-reentrant) lock, __wrapped__ set."""
        original, lock = self.Client._refresh_session, self.launch._GUARD.refresh_lock

        def wrapped(client):
            with lock:
                return original(client)

        wrapped.__wrapped__ = original
        self.Client._refresh_session = wrapped

    def run_hook(self, client):
        with mock.patch.object(self.launch, "_app_garmin_client", return_value=client), \
                captured_records() as records:
            self.launch._refresh_app_token()
        return records

    def test_a_token_that_expires_soon_is_refreshed_under_the_lock(self):
        self.wrap_like_install_refresh_lock()
        done = []
        thread = threading.Thread(target=lambda: done.append(self.run_hook(self.Client())), daemon=True)
        thread.start()
        thread.join(5)
        self.assertFalse(thread.is_alive(), "the hook deadlocked on the refresh lock")
        self.assertEqual(self.calls, [True])
        self.assertEqual(done, [[]])
        self.assertFalse(self.launch._GUARD.refresh_lock.locked())

    def test_a_fresh_token_is_left_alone(self):
        client = self.Client()
        client.soon = False
        self.run_hook(client)
        self.assertEqual(self.calls, [])

    def test_a_signed_out_client_is_left_alone(self):
        client = self.Client()
        client.is_authenticated = False
        self.run_hook(client)
        self.assertEqual(self.calls, [])

    def test_no_client_or_no_guard_does_nothing(self):
        self.assertEqual(self.run_hook(None), [])
        self.launch._GUARD = None
        self.assertEqual(self.run_hook(self.Client()), [])
        self.assertEqual(self.calls, [])

    def test_a_failed_refresh_is_one_warning_with_the_class_name(self):
        client = self.Client()
        client.fail = True
        records = self.run_hook(client)
        self.assertEqual([(r.levelname, r.getMessage()) for r in records],
                         [("WARNING", "history token publish: the token refresh failed (ConnectionError)")])
        self.assertFalse(self.launch._GUARD.refresh_lock.locked())

    def test_the_client_is_found_behind_the_proxy_without_waiting_on_login(self):
        client = self.Client()

        class Pending:
            def __init__(self, inner):
                self._client = inner

            def __getattr__(self, name):
                raise AssertionError("waited on the login")

        class Garmin:
            def __init__(self, inner):
                self.client = inner

        class Proxy:
            def __init__(self, inner):
                self._client = inner

            def __getattr__(self, name):
                return getattr(self._client, name)

        courses = types.SimpleNamespace(garmin_client=Proxy(Pending(Garmin(client))))
        package = types.ModuleType("garmin_mcp")
        package.courses = courses
        fake_modules(self, garmin_mcp=package)
        self.assertIs(self.launch._app_garmin_client(), client)
        courses.garmin_client = Proxy(Pending(None))  # login still pending
        self.assertIsNone(self.launch._app_garmin_client())
        courses.garmin_client = None
        self.assertIsNone(self.launch._app_garmin_client())


class HistoryToolsRegistration(unittest.TestCase):
    """_register_history_tools(): history_tools.register(adder, cfg); the adder registers through the app's
    tool() decorator, and a failure leaves the server up."""

    def setUp(self):
        self.launch = load_launch(self, Stubs().modules)

    def test_the_adder_registers_through_the_app(self):
        registered = []

        class App:
            def tool(self, **kwargs):
                return lambda fn: registered.append((fn.__name__, kwargs)) or fn

        module = types.ModuleType("history_tools")

        def search_history(query: str = "") -> str:
            return ""

        module.register = lambda adder, cfg: adder(search_history, name="search_history", description="d")
        fake_modules(self, history_tools=module)
        self.launch._register_history_tools(App(), feature_config.FeatureConfig(history=True))
        self.assertEqual(registered, [("search_history", {"name": "search_history", "description": "d"})])

    def test_a_failure_is_one_line(self):
        module = types.ModuleType("history_tools")
        module.register = mock.Mock(side_effect=RuntimeError("no model"))
        fake_modules(self, history_tools=module)
        with captured_records() as records:
            self.launch._register_history_tools(object(), feature_config.FeatureConfig(history=True))
        self.assertEqual([r.getMessage() for r in records],
                         ["history search: the tools could not be registered (RuntimeError)"])

    def test_wanted_only_in_the_app_and_not_while_forgetting(self):
        cfg = feature_config.FeatureConfig
        self.assertTrue(self.launch._history_tools_wanted(cfg(history=True)))
        self.assertFalse(self.launch._history_tools_wanted(cfg()))
        self.assertFalse(self.launch._history_tools_wanted(cfg(history=True, role="worker")))
        self.assertFalse(self.launch._history_tools_wanted(cfg(history=True, history_forget=True)))


class RoleDispatch(unittest.TestCase):
    """main() with ROLE=worker or enqueue runs that module's main() instead of the server."""

    def setUp(self):
        self.launch = load_launch(self, Stubs().modules)

    def run_main(self, env):
        with mock.patch.dict(os.environ, env), contextlib.redirect_stderr(io.StringIO()) as err:
            for name in FEATURE_VARS:
                if name not in env:
                    os.environ.pop(name, None)
            with self.assertRaises(SystemExit) as cm:
                self.launch.main([])
        return cm.exception.code, err.getvalue()

    def test_worker_and_enqueue(self):
        for role in ("worker", "enqueue"):
            module = types.ModuleType(role)
            module.main = mock.Mock(return_value=3)
            fake_modules(self, **{role: module})
            env = {"ROLE": role, "HISTORY_SEARCH": "on", "DATABASE_URL": fake_db_url(), "GARMIN_AUTH": "key"}
            with self.subTest(role=role):
                self.assertEqual(self.run_main(env), (3, ""))
                module.main.assert_called_once_with()

    def test_a_missing_module_is_one_line(self):
        fake_modules(self, worker=None)  # the import fails
        code, err = self.run_main({"ROLE": "worker", "HISTORY_SEARCH": "on", "DATABASE_URL": fake_db_url(),
                                   "GARMIN_AUTH": "key"})
        self.assertEqual(code, 1)
        self.assertEqual(err, "launch: ROLE=worker cannot start: worker.py could not be imported (ModuleNotFoundError); "
                              "build the image with HISTORY=1\n")

    def test_bad_feature_settings_stop_main(self):
        # A ROLE other than worker or enqueue is checked only with history search on (with it off the server
        # before the features ignored ROLE, and still does).
        code, err = self.run_main({"ROLE": "cron", "HISTORY_SEARCH": "on", "DATABASE_URL": fake_db_url()})
        self.assertEqual(code, 1)
        self.assertIn("launch: config error: " + feature_config.ERR_H4 + "\n", err)
        self.assertEqual(feature_config.problems({"ROLE": "cron"}), [])


@unittest.skipUnless(HAVE_UPSTREAM, "needs upstream garmin_mcp on mcp 2.x")
class HistoryToolPolicy(UpstreamCase):
    """allowed_tools() with HISTORY_SEARCH=on: the history tools join the list in both modes; off, they are
    not there; GARMIN_ENABLED_TOOLS narrows them like any tool."""

    def setUp(self):
        super().setUp()
        self.launch = load_launch(self, Stubs().modules)

    def test_on_and_off(self):
        on = {"HISTORY_SEARCH": "on", **FULL}
        self.assertLessEqual(self.launch.HISTORY_TOOLS, self.launch.allowed_tools(on))
        self.assertFalse(self.launch.HISTORY_TOOLS & self.launch.allowed_tools(FULL))
        key = self.launch.allowed_tools({**on, "GARMIN_AUTH": "key"})
        self.assertEqual(key, KEY_MODE_TOOL_NAMES | self.launch.HISTORY_TOOLS)
        self.assertEqual(self.launch.allowed_tools({**on, "GARMIN_ENABLED_TOOLS": "search_history,get_stats"}),
                         {"search_history", "get_stats"})
        worker = self.launch.allowed_tools({**on, "ROLE": "worker"})
        self.assertFalse(self.launch.HISTORY_TOOLS & worker)

    def test_main_registers_them_through_history_tools(self):
        module = types.ModuleType("history_tools")

        def find_similar_weeks(end_date: str = "") -> str:
            """Stub."""
            return "{}"

        def search_history(query: str = "") -> str:
            """Stub."""
            return "{}"

        def register(adder, cfg):
            adder(find_similar_weeks)
            adder(search_history, name="search_history", description="Search the stored history.")

        module.register = register
        guard_pg = types.ModuleType("garmin_guard_pg")
        guard_pg.PgSharedState = lambda dsn, tz, live=False: garmin_guard.MemoryState()
        # The startup catch-up runs in a thread against these stand-ins.
        jobqueue, enqueue, store = (types.ModuleType(n) for n in ("jobqueue", "enqueue", "history_store"))
        jobqueue.PgJobQueue = mock.Mock()
        store.PgHistoryStore = mock.Mock()
        enqueue.catch_up = mock.Mock(return_value={"added": 0})
        fake_modules(self, history_tools=module, garmin_guard_pg=guard_pg, jobqueue=jobqueue, enqueue=enqueue,
                     history_store=store)
        quiet(self, "garmin_ext")
        env = key_env(self, HISTORY_SEARCH="on", DATABASE_URL=fake_db_url(), REPORT_TZ="UTC")
        launch, _, server, _, err, code = serve_key_mode(self, env)
        for thread in threading.enumerate():
            if thread.name == "history-catch-up":
                thread.join(10)
        enqueue.catch_up.assert_called_once()
        self.assertIsNone(code, err)
        tools = {t.name: t for t in asyncio.run(server.list_tools())}
        self.assertEqual(set(tools), KEY_MODE_TOOL_NAMES | launch.HISTORY_TOOLS)
        self.assertEqual(tools["search_history"].description, "Search the stored history.")
        self.assertTrue(tools["find_similar_weeks"].annotations.read_only_hint)


if __name__ == "__main__":
    unittest.main()
