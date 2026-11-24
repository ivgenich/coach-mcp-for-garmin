"""Tests for enqueue.py on the in-memory queue.

    python3 -m unittest test_enqueue        (from ext/)
"""

from __future__ import annotations

import contextlib
import datetime as dt
import types
import unittest

import enqueue
from jobqueue import MemoryJobQueue

TP = "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01"
MONDAY = dt.date(2026, 11, 23)


def fake_plan(store, today):
    return [("sweep", f"sweep:{today.isoformat()}", {"late": today.weekday() == 0}),
            ("rebuild", "rebuild:r1.0-t1-n1", {})]


class Run(unittest.TestCase):
    def test_idempotent_and_traced(self):
        q = MemoryJobQueue()
        spans = []

        @contextlib.contextmanager
        def span(name, attrs=None):
            spans.append((name, attrs))
            yield

        tele = types.SimpleNamespace(inject=lambda: TP, span=span)
        self.assertEqual(enqueue.run(q, MONDAY, telemetry=tele, plan=fake_plan), {"planned": 2, "added": 2})
        self.assertEqual(enqueue.run(q, MONDAY, telemetry=tele, plan=fake_plan), {"planned": 2, "added": 0})
        self.assertEqual(sorted(r.key for r in q.rows()), ["rebuild:r1.0-t1-n1", "sweep:2026-11-23"])
        self.assertTrue(all(r.traceparent == TP for r in q.rows()))
        self.assertEqual(spans[0], ("history.enqueue", {"coach.caller": "enqueue"}))
        self.assertEqual([r.payload for r in q.rows() if r.kind == "sweep"], [{"late": True}])

    def test_all_or_nothing(self):
        q = MemoryJobQueue()

        def bad_plan(store, today):
            return [("sweep", "sweep:2026-11-23", {}), ("Bad Kind", "x", {})]

        with self.assertRaises(ValueError):
            enqueue.run(q, MONDAY, plan=bad_plan)
        self.assertEqual(q.rows(), [])

    def test_catch_up_never_raises(self):
        q = MemoryJobQueue()

        def broken(store, today):
            raise ConnectionError("db down: password=hunter2")

        with self.assertLogs("garmin_ext.enqueue", "WARNING") as logs:
            self.assertEqual(enqueue.catch_up(q, MONDAY, plan=broken), {"planned": 0, "added": 0})
        self.assertNotIn("hunter2", "\n".join(logs.output))
        with self.assertLogs("garmin_ext.enqueue", "INFO"):
            self.assertEqual(enqueue.catch_up(q, MONDAY, plan=fake_plan)["added"], 2)

    def test_broken_telemetry_is_ignored(self):
        q = MemoryJobQueue()
        tele = types.SimpleNamespace(inject=lambda: 1 / 0, span=lambda *a: 1 / 0)
        enqueue.run(q, MONDAY, telemetry=tele, plan=fake_plan)
        self.assertIsNone(q.rows()[0].traceparent)

    def test_default_plan_is_history_jobs(self):
        try:
            import history_jobs
            import history_store
        except ModuleNotFoundError as exc:
            self.skipTest(f"needs {exc.name}")
        store = history_store.MemoryHistoryStore()
        store.migrate()
        q = MemoryJobQueue()
        out = enqueue.run(q, MONDAY, store=store)
        self.assertEqual(out["planned"], len(history_jobs.scheduled_jobs(store, MONDAY)))
        self.assertIn("sweep:2026-11-23", [r.key for r in q.rows()])


class Main(unittest.TestCase):
    def test_refuses_the_wrong_role_and_honours_forget(self):
        import io
        from contextlib import redirect_stderr

        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(enqueue.main(environ={"ROLE": "app", "REPORT_TZ": "UTC"}), 1)
            self.assertEqual(enqueue.main(environ={"ROLE": "enqueue", "REPORT_TZ": "UTC"}), 1)
        self.assertIn("config error", err.getvalue())
        env = {"ROLE": "enqueue", "HISTORY_SEARCH": "on", "HISTORY_FORGET": "yes", "REPORT_TZ": "UTC",
               "DATABASE_URL": "postgresql://u:p@db.internal:5432/app", "GARMIN_AUTH": "key"}
        with self.assertLogs("garmin_ext.enqueue", "WARNING"):
            self.assertEqual(enqueue.main(environ=env), 0)

    def test_main_block_runs_the_importable_module_without_tracing_when_off(self):
        """`python enqueue.py` runs the importable module's main() (one module object); with
        tracing off no telemetry module is used; the secrets are registered and taken out of the environment."""
        import os
        import runpy
        import secrets as secrets_mod
        from unittest import mock

        import feature_config
        import tool_errors

        self.assertIsNone(enqueue._setup_telemetry(feature_config.load({})))
        ran = []
        path = os.path.join(os.path.dirname(os.path.abspath(enqueue.__file__)), "enqueue.py")
        with mock.patch.object(enqueue, "main", lambda: ran.append(1) or 0), mock.patch("logging.basicConfig"):
            with self.assertRaises(SystemExit) as cm:
                runpy.run_path(path, run_name="__main__")
        self.assertEqual((cm.exception.code, ran), (0, [1]))

        password = "TEST-ONLY-" + secrets_mod.token_urlsafe(12)
        env = {"ROLE": "enqueue", "HISTORY_SEARCH": "on", "GARMIN_AUTH": "key",
               "DATABASE_URL": f"postgresql://u:{password}@db.internal:5432/app"}
        store = types.ModuleType("history_store")

        def refuse(dsn):
            raise RuntimeError("stop here")
        store.PgHistoryStore = refuse
        with mock.patch.dict("sys.modules", {"history_store": store}), \
                mock.patch.dict(os.environ, {"REPORT_TZ": "UTC"}), \
                mock.patch.object(enqueue.jobqueue, "PgJobQueue", lambda *a, **k: None), \
                self.assertLogs("garmin_ext.enqueue", "ERROR"):
            self.assertEqual(enqueue.main(environ=env), 1)
        self.assertNotIn("DATABASE_URL", env)
        self.assertNotIn(password, tool_errors.redact(f"x {password} y"))


if __name__ == "__main__":
    unittest.main()
