"""Tests for worker.py on the in-memory queue: dispatch, failures, leases, the outbox, tracing, stopping, and
three workers sharing one Garmin token bucket.

    python3 -m unittest test_worker        (from ext/)
"""

from __future__ import annotations

import contextlib
import datetime as dt
import random
import sys
import threading
import time
import types
import unittest
from zoneinfo import ZoneInfo

import garmin_guard_pg
import jobqueue
import worker
import worker_garmin
from jobqueue import DONE, FAILED, QUEUED, MemoryJobQueue

TZ = ZoneInfo("Europe/Berlin")
NOON = dt.datetime(2026, 11, 24, 12, tzinfo=TZ).timestamp()
TP = "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01"


class Clock:
    def __init__(self, t=NOON):
        self.t = t
        self._lock = threading.Lock()

    def __call__(self):
        with self._lock:
            return self.t

    def advance(self, s):
        with self._lock:
            self.t += s


class GuardBlocked(Exception):
    """Same name and fields as garmin_guard.GuardBlocked; the worker matches it by name."""

    def __init__(self, group, until):
        super().__init__(group)
        self.group, self.until = group, until


class FakeGuard:
    """GarminGuard's worker side over the shared state rules."""

    def __init__(self, shared):
        self.shared = shared
        self.outcomes = []

    def before_call(self, caller):
        d = self.shared.take_token_decision()
        if not d.ok:
            raise GuardBlocked(d.reason, d.retry_at)

    def after_outcome(self, caller, group):
        self.outcomes.append((caller, group))
        if group in jobqueue.COOLDOWN_S:
            self.shared.set_cooldown(self.shared._clock() + jobqueue.COOLDOWN_S[group], group)


class RateLimited(Exception):
    pass


class FakeGarmin:
    def __init__(self, clock):
        self.clock = clock
        self.calls = []
        self.fail_with = None

    def get_stats(self, cdate):
        self.calls.append(self.clock())
        if self.fail_with is not None:
            raise self.fail_with
        return {"cdate": cdate}


class FakeStore:
    def __init__(self):
        self.rows = {}
        self.writes = 0

    def upsert_day(self, fp, day, numbers, final):
        self.rows[(fp, day)] = (dict(numbers), final)
        self.writes += 1


def handlers(**kinds):
    return {k: worker.Handler(fn, garmin) for k, (fn, garmin) in kinds.items()}


class Base(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.q = MemoryJobQueue(clock=self.clock, rng=lambda: 0.5)

    def make(self, hs, **kw):
        kw.setdefault("heartbeat_s", 0)
        return worker.Worker(self.q, handlers=hs, clock=self.clock, **kw)


class Registry(unittest.TestCase):
    def tearDown(self):
        worker.clear()

    def test_register(self):
        worker.register("index_day", lambda job, ctx: None)
        worker.register("embed_doc", lambda job, ctx: None, garmin=False)
        self.assertEqual({k: h.garmin for k, h in worker.registered().items()},
                         {"index_day": True, "embed_doc": False})
        with self.assertRaises(ValueError):
            worker.register("Bad Kind", lambda job, ctx: None)
        with self.assertRaises(TypeError):
            worker.register("index_day", None)

    def test_load_handlers(self):
        mod = types.ModuleType("fake_history_jobs")
        mod.register_jobs = lambda register: register("sweep", lambda job, ctx: None)
        sys.modules["fake_history_jobs"] = mod
        try:
            self.assertEqual(worker.load_handlers(("fake_history_jobs", "no_such_module_here")),
                             ["fake_history_jobs"])
        finally:
            del sys.modules["fake_history_jobs"]
        self.assertIn("sweep", worker.registered())

    def test_main_refuses_the_wrong_role(self):
        import io
        from contextlib import redirect_stderr

        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(worker.main(environ={"ROLE": "app", "REPORT_TZ": "UTC"}), 1)
        self.assertIn("ROLE=worker", err.getvalue())


class LogStreams(unittest.TestCase):
    """configure_logging(): INFO lines on stdout, warnings and errors on stderr (Railway marks stderr as errors)."""

    def test_info_on_stdout_warnings_on_stderr(self):
        import contextlib
        import io
        import logging

        import enqueue

        root = logging.getLogger()
        saved = (list(root.handlers), root.level)

        def restore():
            root.handlers[:] = saved[0]
            root.setLevel(saved[1])

        self.addCleanup(restore)
        for module in (worker, enqueue):
            root.handlers[:] = []
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                module.configure_logging()
                logging.getLogger("garmin_ext.worker").info("job done")
                logging.getLogger("garmin_ext.worker").error("job failed")
            self.assertRegex(out.getvalue(), r"\A\S+ \S+ INFO garmin_ext\.worker: job done\n\Z", module.__name__)
            self.assertRegex(err.getvalue(), r"\A\S+ \S+ ERROR garmin_ext\.worker: job failed\n\Z", module.__name__)


class RunAsMain(Base):
    """entrypoint.sh runs `python worker.py`, so this file loads as __main__ while the handler
    modules `import worker` again; their worker.Permanent must still fail the job with its group."""

    def tearDown(self):
        worker.clear()

    def test_main_block_runs_the_importable_module(self):
        import os
        import runpy
        from unittest import mock

        outcomes = []

        def fake_main():
            # What history_jobs does: the class from `import worker`, not from __main__.
            import worker as handler_view

            def bad(job, ctx):
                raise handler_view.Permanent("bad_input")

            w = worker.Worker(self.q, handlers={"rebuild": worker.Handler(bad, False)}, clock=self.clock,
                              heartbeat_s=0)
            self.q.enqueue("rebuild", "rebuild:all")
            with self.assertLogs("garmin_ext.worker", "WARNING"):
                w.run_once()
            outcomes.extend(w.outcomes)
            return 0

        path = os.path.join(os.path.dirname(os.path.abspath(worker.__file__)), "worker.py")
        with mock.patch.object(worker, "main", fake_main), mock.patch("logging.basicConfig"):
            with self.assertRaises(SystemExit) as cm:
                runpy.run_path(path, run_name="__main__")
        self.assertEqual(cm.exception.code, 0)
        self.assertEqual([o[2] for o in outcomes], [FAILED], "fake_main ran in the importable module")
        row = self.q.rows()[0]
        self.assertEqual((row.status, row.last_error_group), (FAILED, "bad_input"))

    def test_a_permanent_or_defer_from_a_second_module_copy_is_still_caught(self):
        import importlib.util
        import os

        here = os.path.dirname(os.path.abspath(worker.__file__))
        copies = {}
        for name in ("worker", "jobqueue"):
            spec = importlib.util.spec_from_file_location(name, os.path.join(here, f"{name}.py"))
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)    # not put in sys.modules: a second copy, as under __main__
            copies[name] = mod
        self.assertIsNot(copies["worker"].Permanent, worker.Permanent)

        def permanent(job, ctx):
            raise copies["worker"].Permanent("bad_account")

        def defer(job, ctx):
            raise copies["jobqueue"].Defer(600, "not_final")

        w = self.make(handlers(rebuild=(permanent, False), build_week=(defer, False)))
        self.q.enqueue("rebuild", "rebuild:all")
        self.q.enqueue("build_week", "build_week:2026-W48")
        with self.assertLogs("garmin_ext.worker", "WARNING"):
            w.run_once()
            w.run_once()
        rows = {r.kind: r for r in self.q.rows()}
        self.assertEqual((rows["rebuild"].status, rows["rebuild"].last_error_group), (FAILED, "bad_account"))
        self.assertEqual((rows["build_week"].status, rows["build_week"].attempts,
                          rows["build_week"].last_error_group), (QUEUED, 0, "not_final"))


class Startup(unittest.TestCase):
    def test_no_telemetry_with_tracing_off(self):
        """The queue stats gauges never run with tracing off."""
        import feature_config

        self.assertIsNone(worker._setup_telemetry(feature_config.load({})))
        calls = []
        q = MemoryJobQueue()
        q.stats = lambda: calls.append(1) or {}
        worker.Worker(q, telemetry=worker._setup_telemetry(feature_config.load({})), heartbeat_s=0).maybe_reap()
        self.assertEqual(calls, [])

    def test_main_registers_every_secret_form_and_takes_the_secrets(self):
        """As launch does, the DSN password and the token key are redacted, and the secret
        variables leave the environment before anything else starts."""
        import base64
        import secrets as secrets_mod

        import tool_errors

        password = "TEST-ONLY-" + secrets_mod.token_urlsafe(12)
        key = base64.b64encode(secrets_mod.token_bytes(32)).decode()
        environ = {"ROLE": "worker", "HISTORY_SEARCH": "on", "GARMIN_AUTH": "full", "REPORT_TZ": "UTC",
                   "DATABASE_URL": f"postgresql://u:{password}@db.internal:5432/railway", "HISTORY_TOKEN_KEY": key}
        stop = types.ModuleType("history_store")

        class Stop(Exception):
            pass

        def refuse(dsn):
            raise Stop()
        stop.PgHistoryStore = refuse
        saved = sys.modules.get("history_store")
        sys.modules["history_store"] = stop
        try:
            import os
            from unittest import mock

            with mock.patch.dict(os.environ, {"REPORT_TZ": "UTC"}), self.assertRaises(Stop):
                worker.main(environ=environ)
        finally:
            if saved is None:
                sys.modules.pop("history_store", None)
            else:
                sys.modules["history_store"] = saved
        self.assertNotIn("DATABASE_URL", environ)
        self.assertNotIn("HISTORY_TOKEN_KEY", environ)
        line = tool_errors.redact(f"failed: password {password} key {key}")
        self.assertNotIn(password, line)
        self.assertNotIn(key, line)


class Dispatch(Base):
    def test_success_and_outbox(self):
        store = FakeStore()

        def index_day(job, ctx):
            ctx.store.upsert_day("fp", job.payload["date"], {"rhr": 50}, True)
            ctx.queue.enqueue("build_week", "build_week:2026-W48", conn=ctx.conn)

        w = self.make(handlers(index_day=(index_day, True)), store=store)
        self.q.enqueue("index_day", "index_day:2026-11-23", {"date": "2026-11-23"})
        self.assertTrue(w.run_once())
        self.assertFalse(w.run_once(), "build_week has no handler here")
        self.assertEqual(sorted((r.kind, r.status) for r in self.q.rows()),
                         [("build_week", QUEUED), ("index_day", DONE)])

    def test_double_run_changes_nothing(self):
        store = FakeStore()

        def index_day(job, ctx):
            ctx.store.upsert_day("fp", job.payload["date"], {"rhr": 50}, True)
            ctx.queue.enqueue("build_week", "build_week:2026-W48", conn=ctx.conn)

        w = self.make(handlers(index_day=(index_day, True)), store=store)
        self.q.enqueue("index_day", "index_day:2026-11-23", {"date": "2026-11-23"})
        w.run_once()
        before = dict(store.rows)
        self.q.enqueue("index_day", "index_day:2026-11-23", {"date": "2026-11-23"})
        w.run_once()
        self.assertEqual(store.rows, before)
        self.assertEqual(sum(1 for r in self.q.rows() if r.kind == "build_week"), 1, "follow-up deduped")

    def test_handler_failure_rolls_back_its_outbox(self):
        def index_day(job, ctx):
            ctx.queue.enqueue("build_week", "build_week:2026-W48", conn=ctx.conn)
            raise TimeoutError("Garmin did not answer")

        w = self.make(handlers(index_day=(index_day, True)))
        self.q.enqueue("index_day", "index_day:2026-11-23")
        with self.assertLogs("garmin_ext.worker", "INFO") as logs:
            w.run_once()
        self.assertEqual([(r.kind, r.status, r.attempts, r.last_error_group) for r in self.q.rows()],
                         [("index_day", QUEUED, 1, "unavailable")])
        self.assertNotIn("did not answer", "\n".join(logs.output), "never the exception text")

    def test_poisoned_job_ends_failed(self):
        def boom(job, ctx):
            raise ValueError("unexpected shape")

        w = self.make(handlers(index_day=(boom, True)))
        self.q.enqueue("index_day", "index_day:2026-11-23")
        with self.assertLogs("garmin_ext.worker", "INFO"):
            for _ in range(jobqueue.MAX_ATTEMPTS):
                self.clock.advance(86400)
                self.assertTrue(w.run_once())
        self.clock.advance(86400)
        self.assertFalse(w.run_once())
        row = self.q.rows()[0]
        self.assertEqual((row.status, row.attempts, row.last_error_group), (FAILED, 6, "internal"))
        self.assertEqual(self.q.stats()["failed"], 1)

    def test_no_data_and_permanent_are_not_retried(self):
        def nodata(job, ctx):
            raise OSError("no data from Garmin (HTTP 404)")

        def permanent(job, ctx):
            raise worker.Permanent("bad_account")

        w = self.make(handlers(index_day=(nodata, True), rebuild=(permanent, False)))
        self.q.enqueue("index_day", "index_day:2026-11-23")
        self.q.enqueue("rebuild", "rebuild:all")
        with self.assertLogs("garmin_ext.worker", "WARNING"):
            w.run_once()
            w.run_once()
        self.assertEqual(sorted((r.kind, r.status, r.last_error_group) for r in self.q.rows()),
                         [("index_day", FAILED, "no_data"), ("rebuild", FAILED, "bad_account")])

    def test_prepare_runs_before_the_transaction_and_feeds_the_handler(self):
        state = {"open": False}
        seen = []
        real = self.q.transaction

        @contextlib.contextmanager
        def tracked():
            state["open"] = True
            try:
                with real() as conn:
                    yield conn
            finally:
                state["open"] = False

        self.q.transaction = tracked

        def prepare(job, ctx):
            seen.append(("prepare", state["open"], ctx.conn))
            return {"answer": 42}

        def handler(job, ctx):
            seen.append(("handler", state["open"], ctx.prepared))

        w = self.make({"index_day": worker.Handler(handler, True, prepare)})
        self.q.enqueue("index_day", "index_day:2026-11-20")
        w.run_once()
        self.assertEqual(seen, [("prepare", False, None), ("handler", True, {"answer": 42})])
        self.assertEqual(self.q.rows()[0].status, DONE)

    def test_a_prepare_failure_is_handled_like_a_handler_failure(self):
        def prepare(job, ctx):
            raise jobqueue.Defer(600, "not_final")

        ran = []
        w = self.make({"build_week": worker.Handler(lambda job, ctx: ran.append(1), True, prepare)})
        self.q.enqueue("build_week", "build_week:2026-11-16")
        w.run_once()
        row = self.q.rows()[0]
        self.assertEqual((row.status, row.attempts, row.last_error_group), (QUEUED, 0, "not_final"))
        self.assertEqual(ran, [])
        with self.assertRaises(TypeError):
            worker.register("index_day", lambda job, ctx: None, prepare="not callable")

    def test_token_stale_logs_one_warning_an_hour(self):
        worker.clear()
        self.addCleanup(worker.clear)

        def stale(job, ctx):
            raise worker_garmin.TokenStale()

        w = self.make(handlers(index_day=(stale, True)))
        with self.assertLogs("garmin_ext.worker", "INFO") as logs:
            for i in range(3):
                self.q.enqueue("index_day", f"index_day:2026-11-{10 + i}")
                w.run_once()
                self.clock.advance(60)
            self.clock.advance(worker.STALE_LOG_S)
            self.q.enqueue("index_day", "index_day:2026-11-19")
            w.run_once()
        warnings = [r for r in logs.records if r.levelname == "WARNING"]
        self.assertEqual(len(warnings), 2)
        self.assertIn("token_stale", warnings[0].getMessage())
        self.assertEqual(worker.TOKEN_STALE, worker_garmin.TOKEN_STALE)
        self.assertGreaterEqual(sum(r.last_error_group == "token_stale" for r in self.q.rows()), 3)

    def test_defer(self):
        def not_final(job, ctx):
            raise jobqueue.Defer(3600, "not_final")

        w = self.make(handlers(build_week=(not_final, False)))
        self.q.enqueue("build_week", "build_week:2026-W48")
        w.run_once()
        row = self.q.rows()[0]
        self.assertEqual((row.status, row.attempts, row.next_run_at), (QUEUED, 0, self.clock() + 3600))

    def test_guard_block_pauses_garmin_kinds_only(self):
        until = self.clock() + 6 * 3600

        def blocked(job, ctx):
            raise GuardBlocked("rate_limited", until)

        ran = []
        w = self.make(handlers(index_day=(blocked, True), embed_doc=(lambda j, c: ran.append(j.key), False)))
        self.q.enqueue("index_day", "index_day:2026-11-23", priority=10)
        self.q.enqueue("index_day", "index_day:2026-11-22", priority=9)
        self.q.enqueue("embed_doc", "embed_doc:2026-11-21", priority=0)
        w.run_once()
        row = [r for r in self.q.rows() if r.key == "index_day:2026-11-23"][0]
        self.assertEqual((row.status, row.attempts, row.next_run_at), (QUEUED, 0, until))
        self.assertTrue(w.run_once())
        self.assertEqual(ran, ["embed_doc:2026-11-21"], "the other index_day waits; embed_doc runs")
        self.assertEqual(w.garmin_paused_until, self.clock() + worker.MAX_PAUSE_S)

    def test_crashed_worker_job_finishes_elsewhere(self):
        done = []
        self.q.enqueue("index_day", "index_day:2026-11-23")
        self.assertIsNotNone(self.q.claim("w-crashed"))      # claimed, then the process died
        w = self.make(handlers(index_day=(lambda j, c: done.append(j.key), True)))
        self.assertFalse(w.run_once(), "lease still valid")
        self.clock.advance(jobqueue.LEASE_S + 1)
        w._last_reap = None
        self.assertTrue(w.run_once())
        self.assertEqual(done, ["index_day:2026-11-23"])
        self.assertEqual(self.q.rows()[0].status, DONE)

    def test_lease_lost_mid_job_rolls_back(self):
        def slow(job, ctx):
            ctx.queue.enqueue("build_week", "build_week:2026-W48", conn=ctx.conn)
            self.clock.advance(jobqueue.LEASE_S + 1)
            self.q.reap()
            self.q.claim("w-other")

        w = self.make(handlers(index_day=(slow, True)))
        self.q.enqueue("index_day", "index_day:2026-11-23")
        w.run_once()
        self.assertEqual(w.outcomes[-1][2], "lost")
        self.assertEqual([r.kind for r in self.q.rows()], ["index_day"])

    def test_heartbeat_thread_detects_a_lost_lease(self):
        class LosingQueue(MemoryJobQueue):
            def heartbeat(self, job):
                return False

        q = LosingQueue()
        q.enqueue("index_day", "index_day:2026-11-23")
        w = worker.Worker(q, handlers=handlers(index_day=(lambda j, c: time.sleep(0.2), True)), heartbeat_s=0.01)
        w.run_once()
        self.assertEqual(w.outcomes[-1][2], "lost")

    def test_heartbeat_thread_renews(self):
        q = MemoryJobQueue(lease_s=0.15)
        q.enqueue("index_day", "index_day:2026-11-23")
        w = worker.Worker(q, handlers=handlers(index_day=(lambda j, c: time.sleep(0.4), True)), heartbeat_s=0.03)
        w.run_once()
        self.assertEqual(w.outcomes[-1][2], DONE)

    def test_graceful_stop(self):
        w = self.make(handlers(index_day=(lambda j, c: None, True)), poll_s=0.01)
        t = threading.Thread(target=w.run)
        t.start()
        time.sleep(0.05)
        w.stop()
        t.join(timeout=2)
        self.assertFalse(t.is_alive())


class Tracing(Base):
    def fake_telemetry(self, with_parent=True):
        seen, metrics = [], []

        class Span:
            def __init__(self):
                self.attrs, self.errors = {}, []

            def set(self, attrs=None, **kw):
                self.attrs.update(attrs or {})

            def error(self, group=None):
                self.errors.append(group)

        if with_parent:
            @contextlib.contextmanager
            def span(name, attrs=None, *, parent=None, links=None, **kw):
                s = Span()
                seen.append((name, parent, links, dict(attrs or {}), s))
                yield s
        else:
            @contextlib.contextmanager
            def span(name, attrs=None, *, links=None, **kw):
                s = Span()
                seen.append((name, None, links, dict(attrs or {}), s))
                yield s

        tele = types.SimpleNamespace(
            span=span, extract_link=lambda tp: ("LINK", tp),
            metric_count=lambda n, v, attrs=None: metrics.append(("count", n, v, attrs)),
            metric_observe=lambda n, v, attrs=None: metrics.append(("observe", n, v, attrs)),
            metric_gauge=lambda n, v, attrs=None: metrics.append(("gauge", n, v, attrs)))
        return tele, seen, metrics

    def test_span_is_a_child_of_the_enqueuer_with_attributes_status_and_metrics(self):
        tele, seen, metrics = self.fake_telemetry()
        w = self.make(handlers(index_day=(lambda j, c: None, True)), telemetry=tele)
        self.q.enqueue("index_day", "index_day:2026-11-23", traceparent=TP)
        self.clock.advance(7)
        w.run_once()
        name, parent, links, attrs, s = seen[0]
        self.assertEqual((name, parent, links), ("history.job", TP, None))
        self.assertEqual(attrs, {"coach.job.kind": "index_day", "coach.job.attempt": 1, "coach.queue.wait_s": 7})
        self.assertEqual((s.attrs, s.errors), ({"coach.job.status": "done"}, []))
        self.assertIn(("count", "coach.jobs", 1, {"coach.job.kind": "index_day", "coach.job.status": "done"}),
                      metrics)
        self.assertIn(("observe", "coach.queue.wait", 7, {"coach.job.kind": "index_day"}), metrics)
        self.assertIn(("gauge", "coach.queue.depth", 1, {"coach.job.status": "queued"}), metrics,
                      "stats sent after the reap, before the claim")

    def test_link_when_the_module_takes_no_parent(self):
        tele, seen, _ = self.fake_telemetry(with_parent=False)
        w = self.make(handlers(index_day=(lambda j, c: None, True)), telemetry=tele)
        self.q.enqueue("index_day", "index_day:2026-11-23", traceparent=TP)
        w.run_once()
        self.assertEqual(seen[0][2], [("LINK", TP)])

    def test_failure_marks_the_span(self):
        tele, seen, metrics = self.fake_telemetry()

        def boom(job, ctx):
            raise TimeoutError("slow")

        w = self.make(handlers(index_day=(boom, True)), telemetry=tele)
        self.q.enqueue("index_day", "index_day:2026-11-23")
        with self.assertLogs("garmin_ext.worker", "INFO"):
            w.run_once()
        s = seen[0][4]
        self.assertEqual((s.attrs, s.errors), ({"coach.job.status": "retry"}, ["unavailable"]))

    def test_broken_telemetry_never_stops_a_job(self):
        tele = types.SimpleNamespace(span=lambda *a, **k: 1 / 0, extract_link=lambda tp: 1 / 0,
                                     metric_count=lambda *a, **k: 1 / 0, metric_gauge=lambda *a, **k: 1 / 0)
        w = self.make(handlers(index_day=(lambda j, c: None, True)), telemetry=tele)
        self.q.enqueue("index_day", "index_day:2026-11-23", traceparent=TP)
        w.run_once()
        self.assertEqual(self.q.rows()[0].status, DONE)


class SharedBucket(Base):
    def test_three_workers_fifty_jobs_bucket_never_exceeded(self):
        shared = garmin_guard_pg.LocalSharedState(TZ, clock=self.clock)
        guard = FakeGuard(shared)
        garmin = FakeGarmin(self.clock)
        # A short bucket wait is waited out inside the job (on the fake clock).
        client = worker_garmin.GuardedGarmin(garmin, guard, clock=self.clock, sleep=self.clock.advance)
        runs = {}

        def index_day(job, ctx):
            ctx.garmin.get_stats(job.payload["date"])
            runs[job.key] = runs.get(job.key, 0) + 1

        hs = handlers(index_day=(index_day, True))
        workers = [self.make(hs, garmin=client, worker_id=f"w{i}") for i in range(3)]
        start = dt.date(2026, 11, 23)
        for i in range(50):
            day = start - dt.timedelta(days=i)
            self.q.enqueue("index_day", f"index_day:{day}", {"date": day.isoformat()})
        rnd = random.Random(7)
        for _ in range(2000):
            for w in rnd.sample(workers, 3):
                w.run_once()
            if all(r.status == DONE for r in self.q.rows()):
                break
            self.clock.advance(1)
        self.assertTrue(all(r.status == DONE for r in self.q.rows()))
        self.assertEqual(len(runs), 50)
        self.assertTrue(all(n == 1 for n in runs.values()), "each job ran to completion once")
        self.assertEqual(len(garmin.calls), 50)
        self.assertTrue(all(b - a >= 5 for a, b in zip(garmin.calls, garmin.calls[1:])))
        self.assertTrue(all(r.attempts == 0 for r in self.q.rows()), "waiting for a token is not an attempt")

    def test_worker_429_sets_the_shared_cooldown(self):
        shared = garmin_guard_pg.LocalSharedState(TZ, clock=self.clock)
        guard = FakeGuard(shared)
        garmin = FakeGarmin(self.clock)
        garmin.fail_with = OSError("Garmin rate limit hit (HTTP 429)")
        client = worker_garmin.GuardedGarmin(garmin, guard)
        w = self.make(handlers(index_day=(lambda j, c: c.garmin.get_stats("2026-11-23"), True)), garmin=client)
        self.q.enqueue("index_day", "index_day:2026-11-23")
        self.q.enqueue("index_day", "index_day:2026-11-22")
        w.run_once()
        self.assertEqual(guard.outcomes, [("worker", "rate_limited")])
        self.assertEqual(shared.get_cooldown(), (self.clock() + 6 * 3600, "rate_limited"))
        row = self.q.rows()[0]
        self.assertEqual((row.status, row.attempts, row.next_run_at), (QUEUED, 0, self.clock() + 6 * 3600))
        self.clock.advance(10)
        garmin.fail_with = None
        w.garmin_paused_until = 0
        w.run_once()
        self.assertEqual(len(garmin.calls), 1, "no Garmin call during the cooldown")


if __name__ == "__main__":
    unittest.main()
