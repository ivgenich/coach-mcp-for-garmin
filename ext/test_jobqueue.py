"""Tests for jobqueue.py: the in-memory queue always, the Postgres queue only with COACH_TEST_DATABASE_URL.

    python3 -m unittest test_jobqueue        (from ext/)
"""

from __future__ import annotations

import importlib.util
import os
import threading
import time
import unittest

import jobqueue
from jobqueue import DONE, FAILED, QUEUED, RUNNING, MemoryJobQueue

TP = "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01"


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t
        self._lock = threading.Lock()

    def __call__(self):
        with self._lock:
            return self.t

    def advance(self, s):
        with self._lock:
            self.t += s


def no_jitter():
    return 0.5   # backoff factor 1 + 0.2 * (2 * 0.5 - 1) = 1


class Rules(unittest.TestCase):
    def test_backoff_schedule_and_jitter_bounds(self):
        self.assertEqual([jobqueue.backoff_s(a, no_jitter) for a in range(1, 7)],
                         [60, 300, 1800, 7200, 21600, 21600])
        self.assertAlmostEqual(jobqueue.backoff_s(1, lambda: 0.0), 48.0)
        self.assertAlmostEqual(jobqueue.backoff_s(1, lambda: 1.0), 72.0)

    def test_group_and_traceparent_cleaning(self):
        self.assertEqual(jobqueue.clean_group("rate_limited"), "rate_limited")
        self.assertEqual(jobqueue.clean_group("HTTP 500: secret body"), "internal")
        self.assertEqual(jobqueue.clean_group(None), "internal")
        self.assertEqual(jobqueue.clean_traceparent(TP), TP)
        self.assertIsNone(jobqueue.clean_traceparent("00-xyz"))

    def test_one_migration_lock_with_the_history_store(self):
        try:
            import history_store
        except ModuleNotFoundError:
            self.skipTest("needs history_store")
        self.assertEqual(jobqueue.MIGRATE_LOCK_ID, history_store.MIGRATE_LOCK_ID)

    def test_enqueue_checks(self):
        q = MemoryJobQueue()
        for kind, key, payload in (("Index", "k", {}), ("index_day", "bad key", {}),
                                   ("index_day", "k", ["x"]), ("index_day", "k", {"x": "y" * 5000})):
            with self.subTest(kind=kind, key=key):
                with self.assertRaises(ValueError):
                    q.enqueue(kind, key, payload)
        with self.assertRaises(ValueError):
            q.claim("bad id with spaces")


class MemoryQueue(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.q = MemoryJobQueue(clock=self.clock, rng=no_jitter)

    def test_unique_key_among_unfinished_jobs(self):
        first = self.q.enqueue("index_day", "index_day:2026-11-23", {"date": "2026-11-23"})
        self.assertIsNotNone(first)
        self.assertIsNone(self.q.enqueue("index_day", "index_day:2026-11-23", {"date": "2026-11-23"}))
        job = self.q.claim("w1")
        self.assertTrue(self.q.complete(job))
        self.assertIsNotNone(self.q.enqueue("index_day", "index_day:2026-11-23"), "a done job frees the key")

    def test_enqueue_while_running_runs_it_again(self):
        """Review fix: an enqueue while the same key runs is not lost; the job runs again after it completes."""
        key = "index_day:2026-11-23"
        first = self.q.enqueue("index_day", key, {"date": "2026-11-23"})
        job = self.q.claim("w1")
        self.assertEqual(self.q.enqueue("index_day", key, {"date": "2026-11-23", "recheck": True}), first)
        self.assertEqual(self.q.enqueue("index_day", key, {"date": "2026-11-23", "recheck": True}), first,
                         "twice while running: still one rerun")
        self.assertEqual(len(self.q.rows()), 1)
        self.assertTrue(self.q.complete(job))
        row = self.q.rows()[0]
        self.assertEqual((row.status, row.attempts, row.rerun, row.next_run_at), (QUEUED, 0, False, self.clock()))
        again = self.q.claim("w2")
        self.assertEqual(again.payload, {"date": "2026-11-23", "recheck": True}, "the newest payload")
        self.assertTrue(self.q.complete(again))
        self.assertEqual([r.status for r in self.q.rows() if r.key == key], [DONE])

    def test_rerun_through_the_outbox_and_after_failures(self):
        key = "index_day:2026-11-23"
        self.q.enqueue("index_day", key)
        job = self.q.claim("w1")
        # Another job's transaction enqueues the running key (the outbox).
        self.q.enqueue("sweep", "sweep:1")
        sweep = self.q.claim("w2", ["sweep"])
        with self.q.transaction() as conn:
            self.assertIsNotNone(self.q.enqueue("index_day", key, {"late": True}, conn=conn))
            self.q.complete(sweep, conn=conn)
        self.assertTrue(next(r for r in self.q.rows() if r.key == key).rerun)
        # A failure that would end it for good queues it as a new run instead.
        self.assertEqual(self.q.fail(job, "bad_input", retryable=False), QUEUED)
        row = next(r for r in self.q.rows() if r.key == key)
        self.assertEqual((row.status, row.attempts, row.rerun), (QUEUED, 0, False))
        # A deferred job runs again anyway: the flag is cleared.
        self.clock.advance(3600)
        job = self.q.claim("w1", ["index_day"])
        self.q.enqueue("index_day", key)
        self.assertTrue(self.q.defer(job, 10))
        self.clock.advance(10)
        job = self.q.claim("w1", ["index_day"])
        self.assertTrue(self.q.complete(job))
        self.assertEqual(next(r for r in self.q.rows() if r.key == key).status, DONE)

    def test_payloads_merge_for_queued_and_running_keys(self):
        """Review fix S3: a running key took the newest payload (enqueue {"recheck": true} then
        {"backfill": true} lost recheck) and a queued key kept the first. Both now merge: the new keys win,
        a flag that was true stays true. A queued key still returns None."""
        key = "index_day:2026-11-23"
        self.q.enqueue("index_day", key, {"date": "2026-11-23", "recheck": True})
        self.assertIsNone(self.q.enqueue("index_day", key, {"date": "2026-11-23", "backfill": True, "empty_run": 2}))
        self.assertIsNone(self.q.enqueue("index_day", key, {"recheck": False, "empty_run": 0}))
        self.assertEqual(self.q.rows()[0].payload,
                         {"date": "2026-11-23", "recheck": True, "backfill": True, "empty_run": 0})
        job = self.q.claim("w1")
        self.assertEqual(self.q.complete(job), True)
        first = self.q.enqueue("index_day", key, {"date": "2026-11-23"})
        job = self.q.claim("w1")
        self.assertEqual(self.q.enqueue("index_day", key, {"date": "2026-11-23", "recheck": True}), first)
        self.assertEqual(self.q.enqueue("index_day", key, {"date": "2026-11-23", "backfill": True}), first)
        self.assertTrue(self.q.complete(job))
        again = self.q.claim("w2")
        self.assertEqual(again.payload, {"date": "2026-11-23", "recheck": True, "backfill": True})

    def test_payloads_merge_through_the_outbox(self):
        key = "index_day:2026-11-23"
        self.q.enqueue("index_day", key, {"date": "2026-11-23", "recheck": True})
        with self.q.transaction() as conn:
            self.assertIsNone(self.q.enqueue("index_day", key, {"backfill": True}, conn=conn), "a queued key")
            self.assertIsNotNone(self.q.enqueue("build_week", "build_week:2026-11-16", {"cascade": 3}, conn=conn))
            self.assertIsNone(self.q.enqueue("build_week", "build_week:2026-11-16", {"fetch_load": True},
                                             conn=conn))
        payloads = {r.key: r.payload for r in self.q.rows()}
        self.assertEqual(payloads, {key: {"date": "2026-11-23", "recheck": True, "backfill": True},
                                    "build_week:2026-11-16": {"cascade": 3, "fetch_load": True}})

    def test_merge_payload_rule(self):
        self.assertEqual(jobqueue.merge_payload({"a": True, "b": 1, "c": False}, {"a": False, "b": 2, "c": True}),
                         {"a": True, "b": 2, "c": True})
        self.assertEqual(jobqueue.merge_payload(None, {"a": 1}), {"a": 1})

    def test_a_handler_that_enqueues_its_own_key_ends(self):
        """Review fix S3: a handler that enqueues its own running key ran again for ever; now at most
        MAX_RERUNS times in a row, then the enqueue returns None and the job ends done."""
        import worker

        runs, answers = [], []

        def handler(job, ctx):
            runs.append(job.id)
            answers.append(ctx.queue.enqueue(job.kind, job.key, job.payload))

        w = worker.Worker(self.q, handlers={"sweep": worker.Handler(handler, False)}, clock=self.clock,
                          heartbeat_s=0)
        self.q.enqueue("sweep", "sweep:self")
        for _ in range(20):
            if not w.run_once():
                break
        self.assertEqual(len(runs), 1 + jobqueue.MAX_RERUNS)
        self.assertEqual(answers[-1], None)
        self.assertEqual([r.status for r in self.q.rows()], [DONE])

    def test_claim_order_kinds_and_run_at(self):
        self.q.enqueue("index_day", "index_day:2026-01-01", priority=20260101)
        self.q.enqueue("index_day", "index_day:2026-11-23", priority=20261123)
        self.q.enqueue("embed_doc", "embed_doc:w1", priority=0)
        self.q.enqueue("index_day", "index_day:2026-11-24", priority=20261124, run_at=self.clock() + 60)
        self.assertEqual(self.q.claim("w1", ["embed_doc"]).key, "embed_doc:w1")
        self.assertEqual(self.q.claim("w1").key, "index_day:2026-11-23", "newest ready first; future not ready")
        self.assertEqual(self.q.claim("w1").key, "index_day:2026-01-01")
        self.assertIsNone(self.q.claim("w1"))
        self.clock.advance(61)
        self.assertEqual(self.q.claim("w1").key, "index_day:2026-11-24")

    def test_traceparent_and_wait_time(self):
        self.q.enqueue("index_day", "index_day:2026-11-23", traceparent=TP)
        self.q.enqueue("index_day", "index_day:2026-11-22", traceparent="not a traceparent")
        self.clock.advance(42)
        a, b = self.q.claim("w1"), self.q.claim("w1")
        self.assertEqual(a.traceparent, TP)
        self.assertIsNone(b.traceparent)
        self.assertEqual(a.wait_s, 42)

    def test_crash_lease_expiry_reaper_and_fencing(self):
        """A worker dies mid-job: the lease runs out, the reaper requeues it, another worker finishes it, and the
        dead worker's late writes change nothing."""
        self.q.enqueue("index_day", "index_day:2026-11-23")
        dead = self.q.claim("w-dead")
        self.clock.advance(jobqueue.LEASE_S - 1)
        self.assertEqual(self.q.reap(), {"requeued": 0, "failed": 0, "purged": 0}, "lease still valid")
        self.clock.advance(2)
        self.assertEqual(self.q.reap()["requeued"], 1)
        row = self.q.rows()[0]
        self.assertEqual((row.status, row.attempts, row.last_error_group), (QUEUED, 1, "lease_expired"))
        alive = self.q.claim("w-alive")
        self.assertIsNotNone(alive)
        self.assertFalse(self.q.heartbeat(dead))
        self.assertFalse(self.q.complete(dead))
        self.assertIsNone(self.q.fail(dead, "internal", True))
        self.assertFalse(self.q.defer(dead, 10))
        self.assertTrue(self.q.heartbeat(alive))
        self.assertTrue(self.q.complete(alive))
        self.assertEqual(self.q.rows()[0].status, DONE)

    def test_heartbeat_keeps_the_lease(self):
        self.q.enqueue("index_day", "index_day:2026-11-23")
        job = self.q.claim("w1")
        for _ in range(10):
            self.clock.advance(jobqueue.HEARTBEAT_S)
            self.assertTrue(self.q.heartbeat(job))
            self.assertEqual(self.q.reap()["requeued"], 0)
        self.assertTrue(self.q.complete(job))

    def test_two_workers_race_one_wins(self):
        for round_ in range(50):
            q = MemoryJobQueue()
            q.enqueue("index_day", f"index_day:r{round_}")
            barrier = threading.Barrier(8)
            won = []

            def go(i, q=q, barrier=barrier, won=won):
                barrier.wait()
                job = q.claim(f"w{i}")
                if job is not None:
                    won.append(i)

            threads = [threading.Thread(target=go, args=(i,)) for i in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(len(won), 1)

    def test_backoff_sequence_ends_done_with_the_right_times(self):
        """unavailable, unavailable, rate_limited (no attempt), login (no attempt), then success."""
        t0 = self.clock()
        self.q.enqueue("index_day", "index_day:2026-11-23")
        job = self.q.claim("w1")
        self.assertEqual(self.q.fail(job, "unavailable", True), QUEUED)
        row = self.q.rows()[0]
        self.assertEqual((row.attempts, row.next_run_at), (1, t0 + 60))
        self.assertIsNone(self.q.claim("w1"), "waits its backoff")
        self.clock.advance(60)
        job = self.q.claim("w1")
        self.q.fail(job, "unavailable", True)
        row = self.q.rows()[0]
        self.assertEqual((row.attempts, row.next_run_at), (2, t0 + 60 + 300))
        self.clock.advance(300)
        job = self.q.claim("w1")
        until = self.clock() + 1234
        self.q.fail(job, "rate_limited", True, until=until)
        row = self.q.rows()[0]
        self.assertEqual((row.attempts, row.next_run_at, row.last_error_group), (2, until, "rate_limited"))
        self.clock.advance(1234)
        job = self.q.claim("w1")
        self.q.fail(job, "login", True)        # no until: the default 24 h
        row = self.q.rows()[0]
        self.assertEqual((row.attempts, row.next_run_at), (2, self.clock() + 86400))
        self.clock.advance(86400)
        job = self.q.claim("w1")
        self.assertEqual(job.attempts, 2)
        self.assertTrue(self.q.complete(job))
        self.assertEqual(self.q.rows()[0].status, DONE)

    def test_poisoned_job_ends_failed_and_shows_in_stats(self):
        self.q.enqueue("index_day", "index_day:2026-11-23")
        statuses = []
        for _ in range(jobqueue.MAX_ATTEMPTS):
            self.clock.advance(86400)
            job = self.q.claim("w1")
            statuses.append(self.q.fail(job, "internal", True))
        self.assertEqual(statuses, [QUEUED] * (jobqueue.MAX_ATTEMPTS - 1) + [FAILED])
        self.clock.advance(86400)
        self.assertIsNone(self.q.claim("w1"))
        stats = self.q.stats()
        self.assertEqual(stats["failed"], 1)
        self.assertEqual(stats["counts"]["index_day"][FAILED], 1)

    def test_not_retryable_fails_at_once(self):
        self.q.enqueue("index_day", "index_day:2026-11-23")
        job = self.q.claim("w1")
        self.assertEqual(self.q.fail(job, "bad_input", False), FAILED)
        self.assertIsNotNone(self.q.enqueue("index_day", "index_day:2026-11-23"), "a failed job frees the key")

    def test_reaped_crash_loop_ends_failed(self):
        self.q.enqueue("index_day", "index_day:2026-11-23")
        for _ in range(jobqueue.MAX_ATTEMPTS):
            self.assertIsNotNone(self.q.claim("w1"))
            self.clock.advance(jobqueue.LEASE_S + 1)
            self.q.reap()
        self.assertEqual(self.q.rows()[0].status, FAILED)

    def test_retention(self):
        self.q.enqueue("index_day", "a:1")
        self.q.enqueue("index_day", "a:2")
        self.q.complete(self.q.claim("w1"))
        self.q.fail(self.q.claim("w1"), "bad_input", False)
        self.clock.advance(jobqueue.KEEP_DONE_S + 1)
        self.assertEqual(self.q.reap()["purged"], 1)
        self.assertEqual([r.status for r in self.q.rows()], [FAILED], "failed kept 30 days")
        self.clock.advance(jobqueue.KEEP_FAILED_S)
        self.assertEqual(self.q.reap()["purged"], 1)
        self.assertEqual(self.q.rows(), [])

    def test_defer_uses_no_attempt(self):
        self.q.enqueue("build_week", "build_week:2026-W47")
        job = self.q.claim("w1")
        self.assertTrue(self.q.defer(job, 3600, "not_final"))
        row = self.q.rows()[0]
        self.assertEqual((row.status, row.attempts, row.last_error_group), (QUEUED, 0, "not_final"))
        self.assertEqual(self.q.stats()["waiting_by_group"], {"not_final": 1})

    def test_outbox_commits_together_or_not_at_all(self):
        self.q.enqueue("index_day", "index_day:2026-11-23")
        job = self.q.claim("w1")
        with self.assertRaises(RuntimeError):
            with self.q.transaction() as conn:
                self.q.enqueue("build_week", "build_week:2026-W47", conn=conn)
                self.q.complete(job, conn=conn)
                raise RuntimeError("crash between steps")
        self.assertEqual([(r.kind, r.status) for r in self.q.rows()], [("index_day", RUNNING)])
        with self.q.transaction() as conn:
            self.q.enqueue("build_week", "build_week:2026-W47", conn=conn)
            self.q.enqueue("embed_doc", "embed_doc:2026-11-23", conn=conn)
            self.q.complete(job, conn=conn)
        self.assertEqual(sorted((r.kind, r.status) for r in self.q.rows()),
                         [("build_week", QUEUED), ("embed_doc", QUEUED), ("index_day", DONE)])

    def test_outbox_with_a_lost_lease_applies_nothing(self):
        self.q.enqueue("index_day", "index_day:2026-11-23")
        job = self.q.claim("w1")
        self.clock.advance(jobqueue.LEASE_S + 1)
        self.q.reap()
        with self.assertRaises(jobqueue.LeaseLost):
            with self.q.transaction() as conn:
                self.q.enqueue("build_week", "build_week:2026-W47", conn=conn)
                self.q.complete(job, conn=conn)
        self.assertEqual([r.kind for r in self.q.rows()], ["index_day"])

    def test_stats(self):
        self.q.enqueue("index_day", "a:1")
        self.q.enqueue("index_day", "a:2", run_at=self.clock() + 100)
        self.clock.advance(30)
        stats = self.q.stats()
        self.assertEqual((stats["queued"], stats["ready"], stats["oldest_ready_age_s"]), (2, 1, 30.0))


# --- Postgres (CI: the pgvector service container) -----------------------------------------------------------------

DSN = os.environ.get("COACH_TEST_DATABASE_URL", "")
HAVE_PSYCOPG = importlib.util.find_spec("psycopg") is not None


@unittest.skipUnless(DSN, "needs COACH_TEST_DATABASE_URL (a Postgres with pgvector)")
@unittest.skipUnless(HAVE_PSYCOPG, "needs psycopg")
class PgQueue(unittest.TestCase):
    def setUp(self):
        self.q = jobqueue.PgJobQueue(DSN, rng=no_jitter)
        self.q.migrate()
        with jobqueue.connect(DSN, autocommit=True) as c:
            c.execute("DELETE FROM history.jobs")

    def _row(self, key):
        with jobqueue.connect(DSN, autocommit=True) as c:
            return c.execute("SELECT status, attempts, last_error_group, "
                             "extract(epoch from next_run_at - now())::float8 FROM history.jobs WHERE key = %s "
                             "ORDER BY id DESC LIMIT 1", (key,)).fetchone()

    def test_migrate_twice(self):
        self.q.migrate()

    def test_enqueue_unique_claim_complete(self):
        self.assertIsNotNone(self.q.enqueue("index_day", "index_day:2026-11-23", {"date": "2026-11-23"},
                                            traceparent=TP, priority=20261123))
        self.assertIsNone(self.q.enqueue("index_day", "index_day:2026-11-23"))
        self.q.enqueue("index_day", "index_day:2026-11-01", priority=20261101)
        job = self.q.claim("w1", ["index_day"])
        self.assertEqual((job.key, job.payload, job.traceparent), ("index_day:2026-11-23", {"date": "2026-11-23"},
                                                                  TP))
        self.assertTrue(self.q.heartbeat(job))
        self.assertTrue(self.q.complete(job))
        self.assertIsNotNone(self.q.enqueue("index_day", "index_day:2026-11-23"))

    def test_enqueue_while_running_runs_it_again(self):
        key = "index_day:2026-11-20"
        first = self.q.enqueue("index_day", key, {"date": "2026-11-20"})
        job = self.q.claim("w1", ["index_day"])
        self.assertEqual(self.q.enqueue("index_day", key, {"date": "2026-11-20", "recheck": True}), first)
        self.assertTrue(self.q.complete(job))
        self.assertEqual(self._row(key)[:2], (QUEUED, 0))
        again = self.q.claim("w2", ["index_day"])
        self.assertEqual(again.payload, {"date": "2026-11-20", "recheck": True})
        self.assertTrue(self.q.complete(again))
        self.assertEqual(self._row(key)[0], DONE)
        self.q.enqueue("index_day", "index_day:2026-11-19")
        self.assertIsNone(self.q.enqueue("index_day", "index_day:2026-11-19"), "a queued job still holds its key")

    def test_payloads_merge_and_reruns_end(self):
        """Review fix S3: the SQL merges payloads as merge_payload() does (queued and running keys), a queued key
        returns None, and a job reruns at most MAX_RERUNS times in a row."""
        key = "index_day:2026-11-18"
        first = self.q.enqueue("index_day", key, {"date": "2026-11-18", "recheck": True})
        self.assertIsNotNone(first)
        self.assertIsNone(self.q.enqueue("index_day", key, {"backfill": True, "recheck": False, "empty_run": 1}))
        job = self.q.claim("w1", ["index_day"])
        self.assertEqual(job.payload, {"date": "2026-11-18", "recheck": True, "backfill": True, "empty_run": 1})
        self.assertEqual(self.q.enqueue("index_day", key, {"empty_run": 2, "fetch_load": True}), first)
        self.assertTrue(self.q.complete(job))
        job = self.q.claim("w1", ["index_day"])
        self.assertEqual(job.payload, {"date": "2026-11-18", "recheck": True, "backfill": True, "empty_run": 2,
                                       "fetch_load": True})
        runs = 1
        while self.q.enqueue("index_day", key, job.payload) is not None:
            self.assertTrue(self.q.complete(job))
            job = self.q.claim("w1", ["index_day"])
            runs += 1
        self.assertEqual(runs, jobqueue.MAX_RERUNS)
        self.assertTrue(self.q.complete(job))
        self.assertEqual(self._row(key)[0], DONE)

    def test_skip_locked_race(self):
        for i in range(20):
            self.q.enqueue("index_day", f"race:{i}")
        got, lock = [], threading.Lock()

        def go(n):
            q = jobqueue.PgJobQueue(DSN)
            while True:
                job = q.claim(f"w{n}")
                if job is None:
                    return
                with lock:
                    got.append(job.key)
                q.complete(job)

        threads = [threading.Thread(target=go, args=(n,)) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(got), sorted(f"race:{i}" for i in range(20)))

    def test_lease_expiry_and_fencing(self):
        q = jobqueue.PgJobQueue(DSN, lease_s=1)
        q.enqueue("index_day", "lease:1")
        dead = q.claim("w-dead")
        time.sleep(1.5)
        self.assertEqual(q.reap()["requeued"], 1)
        alive = q.claim("w-alive")
        self.assertFalse(q.complete(dead))
        self.assertTrue(q.complete(alive))
        self.assertEqual(self._row("lease:1")[:2], (DONE, 1))

    def test_fail_backoff_and_cooldown(self):
        self.q.enqueue("index_day", "fail:1")
        job = self.q.claim("w1")
        self.assertEqual(self.q.fail(job, "unavailable", True), QUEUED)
        status, attempts, group, wait = self._row("fail:1")
        self.assertEqual((status, attempts, group), (QUEUED, 1, "unavailable"))
        self.assertAlmostEqual(wait, 60, delta=5)
        with jobqueue.connect(DSN, autocommit=True) as c:
            c.execute("UPDATE history.jobs SET next_run_at = now() WHERE key = 'fail:1'")
        job = self.q.claim("w1")
        self.q.fail(job, "rate_limited", True)
        status, attempts, group, wait = self._row("fail:1")
        self.assertEqual((attempts, group), (1, "rate_limited"))
        self.assertAlmostEqual(wait, 6 * 3600, delta=5)

    def test_outbox_rollback(self):
        self.q.enqueue("index_day", "outbox:1")
        job = self.q.claim("w1")
        with self.assertRaises(RuntimeError):
            with self.q.transaction() as conn:
                self.q.enqueue("build_week", "outbox:week", conn=conn)
                self.q.complete(job, conn=conn)
                raise RuntimeError("crash")
        self.assertIsNone(self._row("outbox:week"))
        self.assertEqual(self._row("outbox:1")[0], RUNNING)
        with self.q.transaction() as conn:
            self.q.enqueue("build_week", "outbox:week", conn=conn)
            self.q.complete(job, conn=conn)
        self.assertEqual(self._row("outbox:week")[0], QUEUED)
        self.assertEqual(self._row("outbox:1")[0], DONE)

    def test_stats(self):
        self.q.enqueue("index_day", "stats:1")
        self.q.fail(self.q.claim("w1"), "bad_input", False)
        self.q.enqueue("index_day", "stats:2")
        stats = self.q.stats()
        self.assertEqual((stats["failed"], stats["ready"]), (1, 1))


if __name__ == "__main__":
    unittest.main()
