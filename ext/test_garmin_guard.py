"""Tests for garmin_guard.py: the cooldown, live calls first, the shared bucket, the quiet window and the
refresh lock.

    python -m unittest discover -s ext -p "test_garmin_guard*.py" -v

Standard library only: a fake clock, MemoryState, and a fake shared state that two guards (two "processes")
use together. garminconnect's refresh is replaced by a stand-in class when garminconnect is missing.
"""

from __future__ import annotations

import datetime
import logging
import sys
import threading
import time
import types
import unittest
import zoneinfo

import garmin_guard as gg

ZONE = "Europe/London"


def at(hour, minute=0, day=24):
    """Epoch seconds of 2026-11-<day> hour:minute in ZONE (inside the workers' window from 12 to 22)."""
    return datetime.datetime(2026, 11, day, hour, minute, tzinfo=zoneinfo.ZoneInfo(ZONE)).timestamp()


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now

    def tick(self, seconds):
        self.now += seconds


class SharedFake(gg.MemoryState):
    """A shared state two guards use, like Postgres for two processes; `broken` makes every call raise."""

    def __init__(self, clock):
        super().__init__(clock)
        self.broken = False
        self.calls = []

    def _check(self, name):
        self.calls.append(name)
        if self.broken:
            raise ConnectionError("db down")

    def get_cooldown(self):
        self._check("get_cooldown")
        return super().get_cooldown()

    def set_cooldown(self, until, group):
        self._check("set_cooldown")
        return super().set_cooldown(until, group)

    def clear_cooldown(self, group):
        self._check("clear_cooldown")
        return super().clear_cooldown(group)

    def heartbeat_live(self):
        self._check("heartbeat_live")
        return super().heartbeat_live()

    def last_live(self):
        self._check("last_live")
        return super().last_live()

    def take_token(self, now):
        self._check("take_token")
        return super().take_token(now)


def quiet_logs(test):
    logger = logging.getLogger("garmin_ext.guard")
    saved = logger.disabled
    logger.disabled = True
    test.addCleanup(setattr, logger, "disabled", saved)


class Cooldown(unittest.TestCase):
    def setUp(self):
        quiet_logs(self)
        self.clock = Clock(at(13))
        self.guard = gg.GarminGuard(zone=ZONE, clock=self.clock)

    def test_nothing_blocks_at_first(self):
        self.guard.before_call("tool")
        self.guard.before_call("worker")

    def test_a_live_rate_limit_stops_live_retries_for_15_minutes(self):
        self.guard.after_outcome("tool", "rate_limited")
        with self.assertRaises(gg.GuardBlocked) as cm:
            self.guard.before_call("tool")
        self.assertEqual(cm.exception.group, "rate_limited")
        self.assertEqual(cm.exception.until, self.clock.now + 15 * 60)
        self.assertEqual(str(cm.exception), "Garmin call refused by the call guard: rate_limited")
        self.clock.tick(15 * 60 - 1)
        with self.assertRaises(gg.GuardBlocked):
            self.guard.before_call("tool")
        self.clock.tick(1)
        self.guard.before_call("tool")

    def test_a_rate_limit_pauses_workers_6_hours(self):
        self.guard.after_outcome("tool", "rate_limited")
        self.clock.tick(5 * 3600)
        with self.assertRaises(gg.GuardBlocked) as cm:
            self.guard.before_call("worker")
        self.assertEqual(cm.exception.group, "rate_limited")
        self.clock.tick(3600)
        self.guard.before_call("worker")

    def test_login_pauses_workers_a_day_but_never_live_calls(self):
        self.guard.after_outcome("worker", "login")
        self.guard.before_call("tool")
        with self.assertRaises(gg.GuardBlocked) as cm:
            self.guard.before_call("worker")
        self.assertEqual((cm.exception.group, cm.exception.until), ("login", self.clock.now + 24 * 3600))

    def test_a_live_success_ends_a_login_cooldown(self):
        self.guard.after_outcome("tool", "login")
        self.guard.after_outcome("tool", None)
        self.clock.tick(gg.LIVE_QUIET)
        self.guard.before_call("worker")

    def test_a_live_success_leaves_a_rate_limit(self):
        self.guard.after_outcome("worker", "rate_limited")
        self.guard.after_outcome("tool", None)
        with self.assertRaises(gg.GuardBlocked):
            self.guard.before_call("worker")

    def test_other_groups_change_nothing(self):
        for group in ("bad_input", "no_data", "unavailable", "internal", "something"):
            self.guard.after_outcome("tool", group)
        self.guard.before_call("tool")
        self.guard.before_call("worker")

    def test_a_cooldown_is_never_shortened(self):
        self.guard.after_outcome("tool", "login")
        self.guard.after_outcome("tool", "rate_limited")  # ends earlier for workers: login's day is kept
        until, group = self.guard.cooldown()
        self.assertEqual(group, "login")

    def test_unknown_caller(self):
        with self.assertRaises(ValueError):
            self.guard.before_call("cron")


class LiveFirst(unittest.TestCase):
    def setUp(self):
        quiet_logs(self)
        self.clock = Clock(at(13))
        self.guard = gg.GarminGuard(zone=ZONE, clock=self.clock)

    def test_a_running_live_call_keeps_workers_out(self):
        self.guard.live_call_started()
        self.assertEqual(self.guard.live_in_flight, 1)
        with self.assertRaises(gg.GuardBlocked) as cm:
            self.guard.before_call("worker")
        self.assertEqual(cm.exception.group, gg.LIVE_BUSY)
        self.guard.live_call_finished()
        self.clock.tick(gg.LIVE_QUIET - 1)
        with self.assertRaises(gg.GuardBlocked):
            self.guard.before_call("worker")
        self.clock.tick(1)
        self.guard.before_call("worker")

    def test_finished_never_goes_negative(self):
        self.guard.live_call_finished()
        self.assertEqual(self.guard.live_in_flight, 0)


class SharedAcrossProcesses(unittest.TestCase):
    """Two guards on one shared state: the app (live calls) and a worker."""

    def setUp(self):
        quiet_logs(self)
        self.clock = Clock(at(13))
        self.shared = SharedFake(self.clock)
        self.app = gg.GarminGuard(self.shared, zone=ZONE, clock=self.clock)
        self.worker = gg.GarminGuard(self.shared, zone=ZONE, clock=self.clock)

    def test_a_live_429_stops_the_workers(self):
        self.app.after_outcome("tool", "rate_limited")
        with self.assertRaises(gg.GuardBlocked) as cm:
            self.worker.before_call("worker")
        self.assertEqual(cm.exception.group, "rate_limited")

    def test_a_worker_429_stops_live_retries(self):
        self.worker.after_outcome("worker", "rate_limited")
        with self.assertRaises(gg.GuardBlocked) as cm:
            self.app.before_call("tool")
        self.assertEqual(cm.exception.group, "rate_limited")
        self.clock.tick(15 * 60)
        self.app.before_call("tool")

    def test_the_live_path_rereads_the_shared_cooldown_at_most_every_15_s(self):
        self.app.before_call("tool")
        reads = self.shared.calls.count("get_cooldown")
        self.app.before_call("tool")
        self.assertEqual(self.shared.calls.count("get_cooldown"), reads)
        self.clock.tick(gg.SHARED_READ_EVERY)
        self.app.before_call("tool")
        self.assertEqual(self.shared.calls.count("get_cooldown"), reads + 1)

    def test_a_live_call_in_the_app_keeps_the_worker_out(self):
        self.app.live_call_started()
        self.app.live_call_finished()
        with self.assertRaises(gg.GuardBlocked) as cm:
            self.worker.before_call("worker")
        self.assertEqual(cm.exception.group, gg.LIVE_BUSY)

    def test_a_long_live_call_keeps_the_other_process_worker_out(self):
        """Review fix: the heartbeat is rewritten while a live call runs and at its end, so a worker in another
        process takes no token during a long live call or in the LIVE_QUIET seconds after it."""
        self.app.live_call_started()
        self.clock.tick(90)                   # a long live call: the start heartbeat alone is too old now
        self.assertTrue(self.app.refresh_live())
        with self.assertRaises(gg.GuardBlocked) as cm:
            self.worker.before_call("worker")
        self.assertEqual(cm.exception.group, gg.LIVE_BUSY)
        self.clock.tick(30)
        self.app.live_call_finished()         # written at the end too
        self.assertFalse(self.app.refresh_live(), "nothing runs: no heartbeat")
        self.clock.tick(gg.LIVE_QUIET - 1)
        with self.assertRaises(gg.GuardBlocked):
            self.worker.before_call("worker")
        self.clock.tick(1)
        self.worker.before_call("worker")
        self.assertGreaterEqual(self.shared.calls.count("heartbeat_live"), 3)

    def test_the_background_heartbeat_thread_runs_only_while_live_calls_do(self):
        self.app.live_call_started()
        thread = self.app._beat_thread
        self.assertTrue(thread is not None and thread.is_alive())
        self.app.live_call_finished()
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        # Without a shared state nothing runs in the background (the worker sees this process's count).
        alone = gg.GarminGuard(zone=ZONE, clock=self.clock)
        alone.live_call_started()
        self.assertIsNone(alone._beat_thread)
        alone.live_call_finished()

    def test_two_workers_never_exceed_the_bucket(self):
        other = gg.GarminGuard(self.shared, zone=ZONE, clock=self.clock)
        taken = 0
        for _ in range(100):  # 100 tries each, one second apart
            for guard in (self.worker, other):
                try:
                    guard.before_call("worker")
                    taken += 1
                except gg.GuardBlocked as exc:
                    self.assertEqual(exc.group, gg.BUCKET)
            self.clock.tick(1)
        self.assertEqual(taken, 20)  # one every 5 s over 100 s

    def test_the_daily_cap(self):
        taken = 0
        start = self.clock.now
        while self.clock.now < start + 8 * 3600:  # 13:00 to 21:00
            try:
                self.worker.before_call("worker")
                taken += 1
            except gg.GuardBlocked:
                pass
            self.clock.tick(gg.BUCKET_SPACING)
        self.assertEqual(taken, gg.BUCKET_DAILY_MAX)

    def test_the_daily_cap_waits_for_the_next_window(self):
        """Review fix: at the cap the refusal says daily_cap until the next 12:00 local, not 5 s later."""
        for _ in range(gg.BUCKET_DAILY_MAX):
            self.worker.before_call("worker")
            self.clock.tick(gg.BUCKET_SPACING)
        with self.assertRaises(gg.GuardBlocked) as cm:
            self.worker.before_call("worker")
        self.assertEqual((cm.exception.group, cm.exception.until), (gg.DAILY_CAP, at(12, day=25)))
        self.clock.now = at(12, day=25)
        self.worker.before_call("worker")       # a new local day

    def test_a_state_with_take_token_only_still_works(self):
        class Old:
            def get_cooldown(self):
                return None, None

            def last_live(self):
                return None

            def take_token(self, now):
                return False

        with self.assertRaises(gg.GuardBlocked) as cm:
            gg.GarminGuard(Old(), zone=ZONE, clock=self.clock).before_call("worker")
        self.assertEqual((cm.exception.group, cm.exception.until), (gg.BUCKET, self.clock.now + gg.BUCKET_SPACING))

    def test_threads_never_exceed_the_bucket(self):
        taken = []
        lock = threading.Lock()

        def run():
            for _ in range(50):
                try:
                    self.worker.before_call("worker")
                except gg.GuardBlocked:
                    continue
                with lock:
                    taken.append(1)

        threads = [threading.Thread(target=run) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(taken), 1)  # the clock never moves: one token

    def test_a_broken_shared_state_never_blocks_live_calls(self):
        self.shared.broken = True
        with self.assertLogs("garmin_ext.guard", "WARNING") as logs:
            logging.getLogger("garmin_ext.guard").disabled = False
            self.app.before_call("tool")
            self.app.live_call_started()
            self.app.after_outcome("tool", "rate_limited")
        self.assertTrue(any("ConnectionError" in line for line in logs.output))
        self.assertFalse(any("db down" in line for line in logs.output))
        # The in-memory copy still holds the cooldown.
        self.clock.tick(gg.SHARED_READ_EVERY)
        with self.assertRaises(gg.GuardBlocked):
            self.app.before_call("tool")

    def test_the_shared_error_is_logged_once(self):
        self.shared.broken = True
        logging.getLogger("garmin_ext.guard").disabled = False
        with self.assertLogs("garmin_ext.guard", "WARNING") as logs:
            for _ in range(5):
                self.app.before_call("tool")
                self.clock.tick(gg.SHARED_READ_EVERY)
            logging.getLogger("garmin_ext.guard").warning("end")
        self.assertEqual(sum("shared state failed" in line for line in logs.output), 1)

    def test_a_broken_shared_state_stops_the_worker(self):
        self.shared.broken = True
        with self.assertRaises(gg.GuardBlocked) as cm:
            self.worker.before_call("worker")
        self.assertEqual(cm.exception.group, gg.GUARD_UNAVAILABLE)

    def test_a_live_success_clears_a_shared_login_cooldown(self):
        self.worker.after_outcome("worker", "login")
        self.app.before_call("tool")  # reads the shared cooldown: login
        self.app.after_outcome("tool", None)
        self.clock.tick(gg.LIVE_QUIET)
        self.worker.before_call("worker")


class QuietWindow(unittest.TestCase):
    def setUp(self):
        quiet_logs(self)

    def test_workers_call_only_from_12_to_22_local(self):
        for hour, open_ in ((11, False), (12, True), (21, True), (22, False), (3, False)):
            clock = Clock(at(hour, 30 if hour == 21 else 0))
            guard = gg.GarminGuard(zone=ZONE, clock=clock)
            with self.subTest(hour=hour):
                if open_:
                    guard.before_call("worker")
                else:
                    with self.assertRaises(gg.GuardBlocked) as cm:
                        guard.before_call("worker")
                    self.assertEqual(cm.exception.group, gg.QUIET_WINDOW)
                    local = datetime.datetime.fromtimestamp(cm.exception.until, zoneinfo.ZoneInfo(ZONE))
                    self.assertEqual((local.hour, local.minute), (12, 0))
                guard.before_call("tool")  # live calls are never in a window

    def test_next_window_over_a_dst_change(self):
        zone = zoneinfo.ZoneInfo(ZONE)
        night = datetime.datetime(2026, 3, 28, 23, 0, tzinfo=zone).timestamp()  # the clocks go forward on the 29th
        start = datetime.datetime.fromtimestamp(gg.next_window_start(night, zone), zone)
        self.assertEqual((start.day, start.hour), (29, 12))

    def test_an_unknown_zone_is_utc(self):
        guard = gg.GarminGuard(zone="Mars/Olympus", clock=Clock(datetime.datetime(
            2026, 11, 24, 13, tzinfo=datetime.timezone.utc).timestamp()))
        guard.before_call("worker")


class RefreshLock(unittest.TestCase):
    """install_refresh_lock() on a stand-in garminconnect.client.Client."""

    def setUp(self):
        saved = {name: sys.modules.get(name) for name in ("garminconnect", "garminconnect.client")}

        def restore():
            for name, mod in saved.items():
                if mod is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = mod
            gg._INSTALLED_REFRESH.clear()

        self.addCleanup(restore)
        self.active = 0
        self.max_active = 0
        test = self

        class Client:
            def _refresh_session(self):
                test.active += 1
                test.max_active = max(test.max_active, test.active)
                time.sleep(0.02)
                test.active -= 1
                return "refreshed"

        package = types.ModuleType("garminconnect")
        client = types.ModuleType("garminconnect.client")
        client.Client = Client
        package.client = client
        sys.modules["garminconnect"], sys.modules["garminconnect.client"] = package, client
        self.Client = Client

    def test_concurrent_refresh_is_serialised(self):
        lock = threading.Lock()
        refreshed = []
        self.assertTrue(gg.install_refresh_lock(lock, on_refreshed=refreshed.append))
        self.assertTrue(gg.install_refresh_lock(lock))  # twice: no double wrap
        clients = [self.Client() for _ in range(5)]
        threads = [threading.Thread(target=c._refresh_session) for c in clients]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(self.max_active, 1)
        self.assertEqual(self.Client._refresh_session.__wrapped__.__name__, "_refresh_session")
        self.assertEqual(self.Client()._refresh_session(), "refreshed")

    def test_a_failing_hook_never_breaks_the_refresh(self):
        def hook(client):
            raise RuntimeError("publish failed")

        gg.install_refresh_lock(threading.Lock(), on_refreshed=hook)
        with self.assertLogs("garmin_ext.guard", "WARNING"):
            self.assertEqual(self.Client()._refresh_session(), "refreshed")

    def test_without_garminconnect(self):
        sys.modules["garminconnect"] = None  # import fails
        sys.modules["garminconnect.client"] = None
        self.assertFalse(gg.install_refresh_lock(threading.Lock()))


if __name__ == "__main__":
    unittest.main()
