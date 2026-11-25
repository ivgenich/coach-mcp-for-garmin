"""Tests for garmin_guard_pg.py: the rules (decide), the in-memory state, PgSharedState on a fake connection, and
the real Postgres row only with COACH_TEST_DATABASE_URL.

    python3 -m unittest test_garmin_guard_pg        (from ext/)
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import os
import random
import threading
import time
import unittest
import unittest.mock
from zoneinfo import ZoneInfo

import garmin_guard_pg as g
from garmin_guard_pg import Decision, LocalSharedState, State, decide

TZ = ZoneInfo("Europe/Berlin")


def at(y, mo, d, h, mi=0, s=0, tz=TZ):
    return dt.datetime(y, mo, d, h, mi, s, tzinfo=tz).timestamp()


NOON = at(2026, 11, 24, 12)


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


class Rules(unittest.TestCase):
    def test_quiet_window(self):
        s = State()
        d = decide(s, at(2026, 11, 24, 11, 59, 59), TZ)
        self.assertEqual((d.ok, d.reason, d.retry_at), (False, g.WINDOW, NOON))
        self.assertTrue(decide(s, NOON, TZ).ok)
        d = decide(State(), at(2026, 11, 24, 22), TZ)
        self.assertEqual((d.ok, d.reason, d.retry_at), (False, g.WINDOW, at(2026, 11, 25, 12)))
        self.assertTrue(decide(State(), at(2026, 11, 24, 21, 59, 59), TZ).ok)

    def test_window_is_local_time_across_dst(self):
        ny = ZoneInfo("America/New_York")
        # 2026-11-01 is the US DST end: 12:00 local is 17:00 UTC (it was 16:00 UTC the day before).
        self.assertFalse(decide(State(), dt.datetime(2026, 11, 1, 16, 30, tzinfo=dt.timezone.utc).timestamp(),
                                ny).ok)
        self.assertTrue(decide(State(), dt.datetime(2026, 11, 1, 17, 0, tzinfo=dt.timezone.utc).timestamp(),
                               ny).ok)
        d = decide(State(), dt.datetime(2026, 10, 31, 2, 30, tzinfo=dt.timezone.utc).timestamp(), ny)
        self.assertEqual(d.retry_at, dt.datetime(2026, 10, 31, 12, tzinfo=ny).timestamp())

    def test_cooldown_live_spacing(self):
        s = State(cooldown_until=NOON + 100, cooldown_group="rate_limited")
        self.assertEqual(decide(s, NOON + 50, TZ), Decision(False, g.COOLDOWN, NOON + 100))
        s = State(last_live_at=NOON + 10)
        self.assertEqual(decide(s, NOON + 30, TZ), Decision(False, g.LIVE, NOON + 70))
        self.assertTrue(decide(s, NOON + 70, TZ).ok)
        self.assertEqual(decide(s, NOON + 74, TZ), Decision(False, g.SPACING, NOON + 75))
        self.assertTrue(decide(s, NOON + 75, TZ).ok)

    def test_day_cap_resets_on_the_local_day(self):
        s = State()
        t = NOON
        for _ in range(g.DAY_CAP):
            self.assertTrue(decide(s, t, TZ).ok)
            t += g.SPACING_S
        d = decide(s, t, TZ)
        self.assertEqual((d.ok, d.reason, d.retry_at), (False, g.DAY_CAP_REACHED, at(2026, 11, 25, 12)))
        self.assertTrue(decide(s, at(2026, 11, 25, 12), TZ).ok)
        self.assertEqual(s.day_count, 1)


class Local(unittest.TestCase):
    def test_cooldown_only_grows(self):
        clock = Clock(NOON)
        s = LocalSharedState(TZ, clock=clock)
        self.assertEqual(s.get_cooldown(), (None, None))
        s.set_cooldown(NOON + 86400, "login")
        s.set_cooldown(NOON + 3600, "rate_limited")
        self.assertEqual(s.get_cooldown(), (NOON + 86400, "login"))
        s.clear_cooldown("rate_limited")
        self.assertEqual(s.get_cooldown(), (NOON + 86400, "login"), "only the named group is cleared")
        clock.advance(86401)
        self.assertEqual(s.get_cooldown(), (None, None))

    def test_threads_never_exceed_the_bucket(self):
        """8 threads hammer take_token on a moving clock: tokens are >= 5 s apart and <= 300 a day."""
        clock = Clock(NOON)
        s = LocalSharedState(TZ, clock=clock)
        stop = threading.Event()

        def hammer():
            rnd = random.Random()
            while not stop.is_set():
                s.take_token()
                if rnd.random() < 0.3:
                    clock.advance(rnd.random() * 3)

        threads = [threading.Thread(target=hammer) for _ in range(8)]
        for t in threads:
            t.start()
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            time.sleep(0.01)
        stop.set()
        for t in threads:
            t.join()
        granted = s.granted
        self.assertGreater(len(granted), 3)
        self.assertTrue(all(b - a >= g.SPACING_S for a, b in zip(granted, granted[1:])))
        days = {}
        for t in granted:
            day = dt.datetime.fromtimestamp(t, TZ).date()
            days[day] = days.get(day, 0) + 1
        self.assertTrue(all(n <= g.DAY_CAP for n in days.values()))

    def test_live_call_blocks_workers_for_a_minute(self):
        clock = Clock(NOON)
        s = LocalSharedState(TZ, clock=clock)
        s.heartbeat_live()
        self.assertEqual(s.take_token_decision().reason, g.LIVE)
        clock.advance(60)
        self.assertTrue(s.take_token())


# --- PgSharedState on a fake connection ----------------------------------------------------------------------------

class FakeDB:
    """One guard_state row; execute() understands the module's own statements."""

    def __init__(self):
        self.row = [None, None, None, None, None, 0]
        self.lock = threading.Lock()
        self.calls = []
        self.fail = False
        self.gate = None

    def connect(self):
        if self.gate is not None:
            self.gate.wait()
        if self.fail:
            raise ConnectionError("db down: password=hunter2")
        return FakeConn(self)


class _Txn:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        self.conn.db.lock.acquire()

    def __exit__(self, *exc):
        self.conn.db.lock.release()


class _Cursor:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class FakeConn:
    def __init__(self, db):
        self.db = db

    def transaction(self):
        return _Txn(self)

    def close(self):
        pass

    def execute(self, sql, params=None):
        db = self.db
        db.calls.append(sql.split()[0])
        if sql.startswith("SELECT cooldown_until"):
            return _Cursor(tuple(db.row))
        if sql is g._SQL_TAKE:
            db.row[3], db.row[4], db.row[5] = params["t"], params["d"], params["n"]
        elif sql is g._SQL_COOLDOWN:
            if db.row[0] is None or db.row[0] < params["u"]:
                db.row[0], db.row[1] = params["u"], params["g"]
        elif sql is g._SQL_CLEAR:
            if db.row[1] == params["g"]:
                db.row[0] = db.row[1] = None
        elif sql is g._SQL_LIVE:
            db.row[2] = max(db.row[2] or params["t"], params["t"])
        return _Cursor(None)


def stop_background(state, db):
    """End a live-mode PgSharedState's background thread (close()), so no thread outlives the test; a database
    the test left failing or blocked is let go first."""
    db.fail = False
    if db.gate is not None:
        db.gate.set()
    state.close()


class PgOnFake(unittest.TestCase):
    def make(self, live=False):
        self.db = FakeDB()
        self.clock = Clock(NOON)
        s = g.PgSharedState("postgresql://fake", TZ, live=live, clock=self.clock, connect=self.db.connect)
        if live:
            self.addCleanup(stop_background, s, self.db)
        return s

    def test_take_token_uses_the_shared_row(self):
        a, db = self.make(), None
        db = self.db
        b = g.PgSharedState("postgresql://fake", TZ, clock=self.clock, connect=db.connect)
        self.assertTrue(a.take_token())
        self.assertFalse(b.take_token(), "the other worker sees the token just taken")
        self.clock.advance(5)
        self.assertTrue(b.take_token())
        self.assertEqual(db.row[5], 2)

    def test_cooldown_from_one_process_stops_the_other(self):
        a = self.make()
        b = g.PgSharedState("postgresql://fake", TZ, clock=self.clock, connect=self.db.connect)
        a.set_cooldown(NOON + 6 * 3600, "rate_limited")
        self.assertEqual(b.get_cooldown(), (NOON + 6 * 3600, "rate_limited"))
        self.assertEqual(b.take_token_decision().reason, g.COOLDOWN)

    def test_worker_mode_raises_so_the_guard_refuses(self):
        s = self.make()
        self.db.fail = True
        for call in (s.take_token, s.get_cooldown, s.last_live, lambda: s.set_cooldown(NOON + 10, "login")):
            with self.assertRaises(ConnectionError):
                call()

    def test_clear_cooldown_reaches_every_process(self):
        a = self.make()
        b = g.PgSharedState("postgresql://fake", TZ, clock=self.clock, connect=self.db.connect)
        a.set_cooldown(NOON + 86400, "login")
        self.assertEqual(b.get_cooldown(), (NOON + 86400, "login"))
        a.clear_cooldown("login")
        self.assertEqual(b.get_cooldown(), (None, None), "the row is the truth, not b's old copy")

    def test_live_mode_logs_once_without_the_error_text(self):
        s = self.make(live=True)
        self.db.fail = True
        with self.assertLogs("garmin_ext.guard", "WARNING") as logs:
            s.set_cooldown(NOON + 10, "rate_limited")
            s.clear_cooldown("login")
            deadline = time.monotonic() + 2
            while not s._logged and time.monotonic() < deadline:
                time.sleep(0.01)
            time.sleep(0.05)
        self.assertEqual(len(logs.output), 1)
        self.assertNotIn("hunter2", logs.output[0])
        self.assertIn("ConnectionError", logs.output[0])
        self.assertEqual(s.get_cooldown(), (NOON + 10, "rate_limited"), "the local copy still knows it")

    def test_with_the_real_guard(self):
        """GarminGuard on PgSharedState: a worker 429 pauses the other worker; the bucket is shared."""
        try:
            import garmin_guard
        except ModuleNotFoundError:
            self.skipTest("needs garmin_guard")
        a = self.make()
        b = g.PgSharedState("postgresql://fake", TZ, clock=self.clock, connect=self.db.connect)
        ga = garmin_guard.GarminGuard(shared=a, zone="Europe/Berlin", clock=self.clock)
        gb = garmin_guard.GarminGuard(shared=b, zone="Europe/Berlin", clock=self.clock)
        ga.before_call("worker")
        with self.assertRaises(garmin_guard.GuardBlocked) as cm:
            gb.before_call("worker")
        self.assertEqual(cm.exception.group, garmin_guard.BUCKET)
        self.clock.advance(5)
        with self.assertLogs("garmin_ext.guard", "WARNING"):
            ga.after_outcome("worker", "rate_limited")
        with self.assertRaises(garmin_guard.GuardBlocked) as cm:
            gb.before_call("worker")
        self.assertEqual((cm.exception.group, cm.exception.until), ("rate_limited", NOON + 5 + 6 * 3600))

    def test_a_long_live_call_blocks_a_worker_on_another_handle(self):
        """Two handles on one row (the app and a worker service). The app's heartbeat is rewritten
        while its live call runs and forced at its end, so no worker token while it runs or for 60 s after."""
        import garmin_guard

        app_state = self.make()
        worker_state = g.PgSharedState("postgresql://fake", TZ, clock=self.clock, connect=self.db.connect)
        app = garmin_guard.GarminGuard(shared=app_state, zone="Europe/Berlin", clock=self.clock)
        wk = garmin_guard.GarminGuard(shared=worker_state, zone="Europe/Berlin", clock=self.clock)
        app.live_call_started()
        self.clock.advance(90)
        app.refresh_live()                    # the background thread's step
        with self.assertRaises(garmin_guard.GuardBlocked) as cm:
            wk.before_call("worker")
        self.assertEqual(cm.exception.group, garmin_guard.LIVE_BUSY)
        self.clock.advance(5)
        app.live_call_finished()              # 5 s after the last write: written anyway (forced)
        self.assertEqual(self.db.row[2], NOON + 95)
        self.clock.advance(59)
        with self.assertRaises(garmin_guard.GuardBlocked):
            wk.before_call("worker")
        self.clock.advance(1)
        wk.before_call("worker")

    def test_the_daily_cap_defers_to_the_next_noon_through_the_guard(self):
        """The Decision's reason and retry time reach GuardBlocked (not 'bucket' 5 s later)."""
        import garmin_guard

        for state in (LocalSharedState(TZ, clock=Clock(NOON), day_cap=3),
                      g.PgSharedState("postgresql://fake", TZ, connect=FakeDB().connect, day_cap=3,
                                      clock=Clock(NOON))):
            clock = state._clock
            guard = garmin_guard.GarminGuard(shared=state, zone="Europe/Berlin", clock=clock)
            with self.subTest(state=type(state).__name__):
                for _ in range(3):
                    guard.before_call("worker")
                    clock.advance(g.SPACING_S)
                with self.assertRaises(garmin_guard.GuardBlocked) as cm:
                    guard.before_call("worker")
                self.assertEqual((cm.exception.group, cm.exception.until),
                                 (garmin_guard.DAILY_CAP, at(2026, 11, 25, 12)))
                clock.advance(1)
                with self.assertRaises(garmin_guard.GuardBlocked) as cm:
                    guard.before_call("worker")
                self.assertEqual(cm.exception.group, garmin_guard.DAILY_CAP)

    def test_live_path_never_waits_on_the_database(self):
        s = self.make(live=True)
        self.db.gate = threading.Event()     # every connect blocks until set
        t0 = time.monotonic()
        s.set_cooldown(NOON + 100, "rate_limited")
        s.heartbeat_live()
        self.assertEqual(s.get_cooldown(), (NOON + 100, "rate_limited"))
        self.assertEqual(s.last_live(), NOON)
        self.assertLess(time.monotonic() - t0, 0.5)
        self.db.gate.set()
        deadline = time.monotonic() + 2
        while self.db.row[0] is None and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(self.db.row[0], NOON + 100, "the write arrived in the background")

    def test_live_heartbeat_written_at_most_once_a_minute(self):
        s = self.make()
        for _ in range(10):
            s.heartbeat_live()
            self.clock.advance(0.5)
        self.assertEqual(self.db.calls.count("UPDATE"), 1)
        self.clock.advance(g.LIVE_WRITE_S)
        s.heartbeat_live()
        self.assertEqual(self.db.calls.count("UPDATE"), 2)

    def test_close_ends_the_background_thread_after_the_queued_writes(self):
        s = self.make(live=True)
        s.close()                             # no thread yet: nothing to stop
        s = self.make(live=True)
        self.db.gate = threading.Event()      # the first write blocks until set
        s.set_cooldown(NOON + 100, "rate_limited")
        thread = s._thread
        self.assertTrue(thread.is_alive())
        self.db.gate.set()
        s.close()
        self.assertFalse(thread.is_alive())
        self.assertEqual(self.db.row[0], NOON + 100, "the queued write ran before the thread ended")
        s.close()                             # idempotent
        s.clear_cooldown("rate_limited")      # after close: the local copy only, no new thread
        self.assertFalse(s._thread.is_alive())
        self.assertEqual(s.get_cooldown(), (None, None))
        self.assertEqual(self.db.row[0], NOON + 100, "nothing is written after close()")

    def test_close_never_raises(self):
        s = self.make(live=True)
        s.heartbeat_live(force=True)
        with unittest.mock.patch.object(s._thread, "join", side_effect=RuntimeError("boom")):
            s.close()
        s.close()

    def test_repr_hides_the_dsn(self):
        self.assertNotIn("fake", repr(self.make()))


DSN = os.environ.get("COACH_TEST_DATABASE_URL", "")
HAVE_PSYCOPG = importlib.util.find_spec("psycopg") is not None


@unittest.skipUnless(DSN, "needs COACH_TEST_DATABASE_URL (a Postgres with pgvector)")
@unittest.skipUnless(HAVE_PSYCOPG, "needs psycopg")
class PgReal(unittest.TestCase):
    def setUp(self):
        import jobqueue

        self.clock = Clock(NOON)
        self.s = g.PgSharedState(DSN, TZ, clock=self.clock)
        self.s.migrate()
        with jobqueue.connect(DSN, autocommit=True) as c:
            c.execute("UPDATE history.guard_state SET cooldown_until = NULL, cooldown_group = NULL, "
                      "last_live_at = NULL, last_token_at = NULL, day_key = NULL, day_count = 0 WHERE id = 1")

    def test_bucket_across_threads(self):
        granted, lock = [], threading.Lock()

        def go():
            s = g.PgSharedState(DSN, TZ, clock=self.clock)
            for _ in range(5):
                if s.take_token():
                    with lock:
                        granted.append(self.clock())

        threads = [threading.Thread(target=go) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(granted), 1, "one token in the same 5 s, whatever the number of workers")

    def test_cooldown_shared(self):
        other = g.PgSharedState(DSN, TZ, clock=self.clock)
        self.s.set_cooldown(NOON + 3600, "rate_limited")
        self.assertEqual(other.get_cooldown(), (NOON + 3600, "rate_limited"))
        self.assertFalse(other.take_token())


if __name__ == "__main__":
    unittest.main()
