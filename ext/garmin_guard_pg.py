"""The Garmin call guard's shared state in Postgres, and the same rules in memory.

garmin_guard.GarminGuard asks its SharedState for: get_cooldown() -> (until, group) or (None, None),
set_cooldown(until, group) (never shortens), clear_cooldown(group), heartbeat_live(force=False), last_live(),
take_token(now) -> bool and take_token_decision(now) -> Decision (the guard prefers the Decision: its reason and
retry_at become GuardBlocked's group and until, so the daily cap waits for the next 12:00 local, not 5 s). With history on, every process (the MCP server and each worker) uses PgSharedState, so a
rate limit seen anywhere pauses everyone and the workers share one token bucket. Times are epoch seconds.

One row, history.guard_state (id = 1), holds it all. take_token() locks that row (SELECT ... FOR UPDATE), so
however many workers run, these rules hold for all of them together (GarminGuard checks the first three before it
asks for a token; take_token checks them again inside the lock, so a race between two processes cannot slip by):
- the quiet window: tokens only from WINDOW_START to WINDOW_END local time (REPORT_TZ), 12:00-22:00;
- no token while a cooldown runs (rate_limited 6 h, login 24 h, set by any process);
- no token while a live (MCP tool) call ran in the last LIVE_GAP_S seconds: live calls first;
- at most one token every SPACING_S seconds (a bucket of one token refilled every 5 s);
- at most DAY_CAP tokens a local day.
decide() is that rule as a pure function; PgSharedState runs it inside the row lock, LocalSharedState under a
threading.Lock (tests, and the live path's copy).

Two modes:
- live=True (the MCP server): never waits on Postgres and never raises. Reads answer from the in-memory copy;
  writes (set_cooldown, clear_cooldown, the live heartbeat) go to one background thread, which also refreshes the
  copy from the row at most every REFRESH_S. A database error logs one line (the error's class name) and the copy
  stays in charge.
- live=False (a worker): synchronous, and a database error is raised, so GarminGuard refuses the worker
  (GUARD_UNAVAILABLE): a worker never calls Garmin without the shared bucket.

Standard library only at import time (psycopg is imported through jobqueue.connect when used).
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import logging
import queue as queue_mod
import threading
import time
from typing import Callable, Optional
from zoneinfo import ZoneInfo

log = logging.getLogger("garmin_ext.guard")

SPACING_S = 5.0
DAY_CAP = 300
WINDOW_START = 12             # local hour, inclusive
WINDOW_END = 22               # local hour, exclusive
LIVE_GAP_S = 60.0
LIVE_WRITE_S = 10.0           # the live heartbeat reaches the database at most this often (unless forced)
REFRESH_S = 15.0              # the live path's copy of the shared row is at most this old (when asked)

# Why take_token_decision() said no (fixed names, the same as garmin_guard's GuardBlocked groups where they
# overlap).
OK, WINDOW, COOLDOWN, LIVE, SPACING, DAY_CAP_REACHED = (
    "ok", "quiet_window", "cooldown", "live_busy", "bucket", "daily_cap")

GUARD_DDL = """
CREATE SCHEMA IF NOT EXISTS history;
CREATE TABLE IF NOT EXISTS history.guard_state (
    id              smallint PRIMARY KEY CHECK (id = 1),
    cooldown_until  double precision,
    cooldown_group  text CHECK (cooldown_group IS NULL OR cooldown_group ~ '^[a-z_]{1,32}$'),
    last_live_at    double precision,
    last_token_at   double precision,
    day_key         date,
    day_count       integer NOT NULL DEFAULT 0
);
INSERT INTO history.guard_state (id) VALUES (1) ON CONFLICT (id) DO NOTHING;
"""


@dataclasses.dataclass
class State:
    cooldown_until: Optional[float] = None
    cooldown_group: Optional[str] = None
    last_live_at: Optional[float] = None
    last_token_at: Optional[float] = None
    day_key: Optional[dt.date] = None
    day_count: int = 0


@dataclasses.dataclass(frozen=True)
class Decision:
    ok: bool
    reason: str
    retry_at: Optional[float]     # the earliest time a new try can succeed


def _local(now: float, tz: ZoneInfo) -> dt.datetime:
    return dt.datetime.fromtimestamp(now, tz)


def window_open(now: float, tz: ZoneInfo) -> bool:
    return WINDOW_START <= _local(now, tz).hour < WINDOW_END


def next_window_start(now: float, tz: ZoneInfo) -> float:
    """The next WINDOW_START:00 local time after `now` (today's if it is still ahead)."""
    local = _local(now, tz)
    day = local.date() if local.hour < WINDOW_START else local.date() + dt.timedelta(days=1)
    return dt.datetime.combine(day, dt.time(WINDOW_START), tzinfo=tz).timestamp()


def decide(state: State, now: float, tz: ZoneInfo, *, spacing_s: float = SPACING_S, day_cap: int = DAY_CAP,
           live_gap_s: float = LIVE_GAP_S) -> Decision:
    """May a worker make one Garmin call at `now`? On yes, `state` is updated (the token is taken)."""
    if not window_open(now, tz):
        return Decision(False, WINDOW, next_window_start(now, tz))
    if state.cooldown_until is not None and now < state.cooldown_until:
        return Decision(False, COOLDOWN, state.cooldown_until)
    if state.last_live_at is not None and now - state.last_live_at < live_gap_s:
        return Decision(False, LIVE, state.last_live_at + live_gap_s)
    today = _local(now, tz).date()
    count = state.day_count if state.day_key == today else 0
    if count >= day_cap:
        return Decision(False, DAY_CAP_REACHED, next_window_start(now, tz))
    if state.last_token_at is not None and now - state.last_token_at < spacing_s:
        return Decision(False, SPACING, state.last_token_at + spacing_s)
    state.last_token_at = now
    state.day_key, state.day_count = today, count + 1
    return Decision(True, OK, None)


def _merge_cooldown(state: State, until: float, group: str) -> None:
    """A cooldown only ever grows: a longer one already running is kept."""
    if state.cooldown_until is None or until > state.cooldown_until:
        state.cooldown_until, state.cooldown_group = float(until), group


class LocalSharedState:
    """The same rules in one process, under a lock (tests; PgSharedState's copy)."""

    def __init__(self, tz: ZoneInfo, *, clock: Callable[[], float] = time.time, spacing_s: float = SPACING_S,
                 day_cap: int = DAY_CAP, live_gap_s: float = LIVE_GAP_S):
        self.tz = tz
        self._clock = clock
        self._lock = threading.Lock()
        self.state = State()
        self._limits = {"spacing_s": spacing_s, "day_cap": day_cap, "live_gap_s": live_gap_s}
        self.granted: list = []        # tests: the time of every token handed out

    def get_cooldown(self):
        with self._lock:
            s = self.state
            if s.cooldown_until is None or s.cooldown_until <= self._clock():
                return None, None
            return s.cooldown_until, s.cooldown_group

    def set_cooldown(self, until, group) -> None:
        with self._lock:
            _merge_cooldown(self.state, until, group)

    def clear_cooldown(self, group) -> None:
        with self._lock:
            if self.state.cooldown_group == group:
                self.state.cooldown_until = self.state.cooldown_group = None

    def heartbeat_live(self, force: bool = False) -> None:
        with self._lock:
            self.state.last_live_at = self._clock()

    def last_live(self):
        with self._lock:
            return self.state.last_live_at

    def take_token_decision(self, now=None) -> Decision:
        with self._lock:
            now = self._clock() if now is None else now
            d = decide(self.state, now, self.tz, **self._limits)
            if d.ok:
                self.granted.append(now)
            return d

    def take_token(self, now=None) -> bool:
        return self.take_token_decision(now).ok


_SQL_READ = ("SELECT cooldown_until, cooldown_group, last_live_at, last_token_at, day_key, day_count "
             "FROM history.guard_state WHERE id = 1")
_SQL_LOCK = _SQL_READ + " FOR UPDATE"
_SQL_TAKE = ("UPDATE history.guard_state SET last_token_at = %(t)s, day_key = %(d)s, day_count = %(n)s "
             "WHERE id = 1")
_SQL_COOLDOWN = (
    "UPDATE history.guard_state SET cooldown_group = CASE WHEN cooldown_until IS NULL OR cooldown_until < %(u)s "
    "THEN %(g)s ELSE cooldown_group END, cooldown_until = GREATEST(COALESCE(cooldown_until, %(u)s), %(u)s) "
    "WHERE id = 1")
_SQL_CLEAR = ("UPDATE history.guard_state SET cooldown_until = NULL, cooldown_group = NULL "
              "WHERE id = 1 AND cooldown_group = %(g)s")
_SQL_LIVE = ("UPDATE history.guard_state SET last_live_at = GREATEST(COALESCE(last_live_at, %(t)s), %(t)s) "
             "WHERE id = 1")


def _row_state(row) -> State:
    if row is None:
        return State()
    return State(cooldown_until=row[0], cooldown_group=row[1], last_live_at=row[2], last_token_at=row[3],
                 day_key=row[4], day_count=row[5] or 0)


class PgSharedState:
    """SharedState in history.guard_state. `live=True` for the MCP server (never waits on Postgres)."""

    def __init__(self, dsn: str, tz: ZoneInfo, *, live: bool = False, clock: Callable[[], float] = time.time,
                 connect: Optional[Callable] = None, spacing_s: float = SPACING_S, day_cap: int = DAY_CAP):
        if connect is None:
            if not isinstance(dsn, str) or not dsn:
                raise ValueError("garmin_guard_pg: DATABASE_URL is not set")
            import jobqueue

            def connect():
                return jobqueue.connect(dsn, application_name="coach-mcp-guard")
        self.tz = tz
        self._live = live
        self._clock = clock
        self._limits = {"spacing_s": spacing_s, "day_cap": day_cap}
        self._connect = connect
        self.local = LocalSharedState(tz, clock=clock, spacing_s=spacing_s, day_cap=day_cap)
        self._logged = False
        self._last_live_write = None
        self._last_refresh = None
        self._jobs: Optional[queue_mod.Queue] = None
        self._thread: Optional[threading.Thread] = None
        self._start_lock = threading.Lock()

    def __repr__(self):
        return "PgSharedState(<dsn hidden>)"

    # --- database helpers
    def _run(self, fn):
        """fn(conn) in one transaction."""
        c = self._connect()
        try:
            with c.transaction():
                out = fn(c)
        finally:
            c.close()
        if self._logged:
            self._logged = False
            log.info("garmin guard: shared state available again")
        return out

    def _background(self, fn) -> None:
        """Run fn(conn) in the background thread (live path); a full queue drops it."""
        with self._start_lock:
            if self._thread is None or not self._thread.is_alive():
                self._jobs = queue_mod.Queue(maxsize=64)
                self._thread = threading.Thread(target=self._loop, name="garmin-guard-pg", daemon=True)
                self._thread.start()
        try:
            self._jobs.put_nowait(fn)
        except queue_mod.Full:
            pass

    def _loop(self) -> None:
        while True:
            fn = self._jobs.get()
            try:
                self._run(fn)
            except Exception as exc:  # noqa: BLE001 - any DB failure: the copy stays in charge
                if not self._logged:
                    self._logged = True
                    log.warning("garmin guard: shared state unavailable (%s); live calls use this process's copy",
                                type(exc).__name__)

    def _do(self, fn) -> None:
        if self._live:
            self._background(fn)
        else:
            self._run(fn)

    def _pull(self, c) -> State:
        """Copy the row's cooldown and live heartbeat into the local copy (the row is the truth)."""
        shared = _row_state(c.execute(_SQL_READ).fetchone())
        with self.local._lock:
            s = self.local.state
            s.cooldown_until, s.cooldown_group = shared.cooldown_until, shared.cooldown_group
            if shared.last_live_at is not None and (s.last_live_at is None or shared.last_live_at > s.last_live_at):
                s.last_live_at = shared.last_live_at
        return shared

    def _refresh(self) -> None:
        if self._live:
            now = self._clock()
            if self._last_refresh is None or now - self._last_refresh >= REFRESH_S:
                self._last_refresh = now
                self._background(self._pull)
            return
        self._run(self._pull)

    # --- SharedState
    def migrate(self) -> None:
        import jobqueue

        def ddl(c):
            c.execute(jobqueue.MIGRATE_LOCK)
            c.execute(GUARD_DDL)
        self._run(ddl)

    def get_cooldown(self):
        self._refresh()
        return self.local.get_cooldown()

    def set_cooldown(self, until, group) -> None:
        self.local.set_cooldown(until, group)
        self._do(lambda c: c.execute(_SQL_COOLDOWN, {"u": float(until), "g": group}))

    def clear_cooldown(self, group) -> None:
        self.local.clear_cooldown(group)
        self._do(lambda c: c.execute(_SQL_CLEAR, {"g": group}))

    def heartbeat_live(self, force: bool = False) -> None:
        """The live heartbeat; written to the row at most every LIVE_WRITE_S, unless `force` (the end of a live
        call, so the workers' quiet time counts from it)."""
        self.local.heartbeat_live()
        now = self._clock()
        if not force and self._last_live_write is not None and now - self._last_live_write < LIVE_WRITE_S:
            return
        self._last_live_write = now
        self._do(lambda c: c.execute(_SQL_LIVE, {"t": now}))

    def last_live(self):
        self._refresh()
        return self.local.last_live()

    def take_token_decision(self, now=None) -> Decision:
        """decide() inside the row lock. A database error is raised (the guard refuses the worker)."""
        now = self._clock() if now is None else now
        own_live = self.local.last_live()

        def take(c):
            state = _row_state(c.execute(_SQL_LOCK).fetchone())
            if own_live is not None and (state.last_live_at is None or own_live > state.last_live_at):
                state.last_live_at = own_live      # an app+worker process knows its own live calls first
            d = decide(state, now, self.tz, **self._limits)
            if d.ok:
                c.execute(_SQL_TAKE, {"t": state.last_token_at, "d": state.day_key, "n": state.day_count})
            return d

        return self._run(take)

    def take_token(self, now=None) -> bool:
        return self.take_token_decision(now).ok
