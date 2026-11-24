"""The Garmin call guard: one rate-limit memory for every caller of Garmin.

Before this module, "stop for 15 minutes" after a Garmin rate limit was only
text for the model, and evals showed models retry anyway. The guard enforces
it in code, and with history search on it also keeps the background worker
(ROLE=worker) from competing with live tool calls:

- a cooldown, set by a rate_limited or login outcome from any caller:
  workers pause WORKER_PAUSE[group] (6 h, 24 h); live tool calls are refused
  for LIVE_PAUSE[group] after it was set (15 minutes for a rate limit, the
  time the client text names; never for login, which a new login fixes and
  a refused call would only hide);
- live calls first: the MCP server writes a heartbeat when a live call
  starts, every HEARTBEAT_EVERY seconds while one runs (a background thread,
  with a shared state only) and when it ends, so a worker in any process
  takes no Garmin call while a live call runs or ran in the last LIVE_QUIET
  seconds;
- one token bucket for all workers (one call every 5 s, at most 300 a local
  day; at the cap the worker waits for the next 12:00 local) and a quiet
  window: workers call Garmin only from 12:00 to 22:00 local time;
- refresh_lock, around full mode's token refresh in each process
  (garminconnect refreshes without one; install_refresh_lock()), and an
  atomic, owner-only write of the token file garminconnect saves after a
  refresh (install_atomic_token_dump()).

The state lives behind the SharedState protocol: MemoryState here (one
process; the default, and the only one with history search off), and the
Postgres one in garmin_guard_pg.py (several processes). The live path never
waits on the shared state for long and never fails because of it: a shared
state error falls back to the in-memory copy and logs one line (the error's
type name only). A worker, on a shared-state error, is refused (it must never
call Garmin without the shared bucket).

Standard library only; thread-safe; nothing here logs a value.
"""

from __future__ import annotations

import contextlib
import datetime
import logging
import os
import tempfile
import threading
import time
from typing import Callable, NamedTuple, Optional, Protocol, Tuple

log = logging.getLogger("garmin_ext.guard")

TOOL, WORKER = "tool", "worker"
CALLERS = (TOOL, WORKER)
# The outcome groups that start a cooldown (tool_errors' names).
RATE_LIMITED, LOGIN = "rate_limited", "login"
WORKER_PAUSE = {RATE_LIMITED: 6 * 3600, LOGIN: 24 * 3600}
LIVE_PAUSE = {RATE_LIMITED: 15 * 60, LOGIN: 0}
LIVE_QUIET = 60          # seconds a worker waits after a live call
HEARTBEAT_EVERY = 20     # a running live call rewrites the heartbeat this often (well within LIVE_QUIET)
SHARED_READ_EVERY = 15   # the live path rereads the shared cooldown at most this often
BUCKET_SPACING = 5.0     # seconds between two worker calls, all workers together
BUCKET_DAILY_MAX = 300
QUIET_START_HOUR, QUIET_END_HOUR = 22, 12   # workers call Garmin only from 12:00 to 22:00 local
# GuardBlocked.group for the refusals that are no Garmin outcome.
COOLDOWN = "cooldown"
LIVE_BUSY = "live_busy"
BUCKET = "bucket"
QUIET_WINDOW = "quiet_window"
GUARD_UNAVAILABLE = "guard_unavailable"
DAILY_CAP = "daily_cap"
# The refusal reasons a state's take_token_decision() may give (garmin_guard_pg uses the same names).
TOKEN_REASONS = frozenset({COOLDOWN, LIVE_BUSY, BUCKET, QUIET_WINDOW, DAILY_CAP})


class GuardBlocked(Exception):
    """The guard refused a Garmin call. `group` is the cooldown's outcome group (rate_limited, login) or one of
    COOLDOWN, LIVE_BUSY, BUCKET, QUIET_WINDOW, DAILY_CAP, GUARD_UNAVAILABLE; `until` is the epoch second it may be tried
    again. The message is fixed text."""

    def __init__(self, group: str, until: float):
        self.group = group
        self.until = float(until)
        super().__init__(f"Garmin call refused by the call guard: {group}")


class TokenDecision(NamedTuple):
    """take_token_decision()'s answer: ok, the reason when not (one of TOKEN_REASONS) and the earliest epoch
    second a new try can succeed (None when ok)."""
    ok: bool
    reason: str
    retry_at: Optional[float]


class SharedState(Protocol):
    """Where the guard keeps what every process must see."""

    def get_cooldown(self) -> Tuple[Optional[float], Optional[str]]:
        """(until, group) of the cooldown, or (None, None)."""

    def set_cooldown(self, until: float, group: str) -> None:
        """Start a cooldown; an existing later `until` is kept (never shortened)."""

    def clear_cooldown(self, group: str) -> None:
        """End a cooldown of `group` (a live call succeeded after a login cooldown)."""

    def heartbeat_live(self) -> None:
        """Record that a live call runs now. A state may take `force=True` (the end of a live call: write it
        even when it wrote one a moment ago); the guard falls back to the plain call when it does not."""

    def last_live(self) -> Optional[float]:
        """The epoch second of the last live heartbeat, or None."""

    def take_token(self, now: float) -> bool:
        """Take one worker call from the shared bucket; False when it is empty. A state may also offer
        take_token_decision(now) -> TokenDecision (or garmin_guard_pg.Decision); the guard prefers it, so a
        refusal carries its reason and retry time (the daily cap waits for the next window, not 5 s)."""


class MemoryState:
    """SharedState for one process (the default)."""

    def __init__(self, clock: Callable[[], float] = time.time, zone=None):
        self._clock = clock
        self._zone = zone if zone is not None else datetime.timezone.utc
        self._lock = threading.Lock()
        self._until: Optional[float] = None
        self._group: Optional[str] = None
        self._live: Optional[float] = None
        self._last_token: Optional[float] = None
        self._day: Optional[int] = None
        self._day_count = 0

    def get_cooldown(self):
        with self._lock:
            return self._until, self._group

    def set_cooldown(self, until, group):
        with self._lock:
            if self._until is None or until >= self._until:
                self._until, self._group = float(until), group

    def clear_cooldown(self, group):
        with self._lock:
            if self._group == group:
                self._until, self._group = None, None

    def heartbeat_live(self, force: bool = False):
        with self._lock:
            self._live = self._clock()

    def last_live(self):
        with self._lock:
            return self._live

    def take_token_decision(self, now) -> TokenDecision:
        with self._lock:
            day = datetime.datetime.fromtimestamp(now, self._zone).date().toordinal()   # the local day
            if day != self._day:
                self._day, self._day_count = day, 0
            if self._day_count >= BUCKET_DAILY_MAX:
                return TokenDecision(False, DAILY_CAP, next_window_start(now, self._zone))
            if self._last_token is not None and now - self._last_token < BUCKET_SPACING:
                return TokenDecision(False, BUCKET, self._last_token + BUCKET_SPACING)
            self._last_token = now
            self._day_count += 1
            return TokenDecision(True, "ok", None)

    def take_token(self, now):
        return self.take_token_decision(now).ok


def _zone(name: Optional[str]):
    if not name:
        return datetime.timezone.utc
    try:
        import zoneinfo

        return zoneinfo.ZoneInfo(name)
    except Exception:  # noqa: BLE001 - REPORT_TZ is checked at startup; UTC is the safe fallback here
        return datetime.timezone.utc


def next_window_start(now: float, zone) -> float:
    """The epoch second of the next QUIET_END_HOUR:00 local time at or after `now`."""
    local = datetime.datetime.fromtimestamp(now, zone)
    start = local.replace(hour=QUIET_END_HOUR, minute=0, second=0, microsecond=0)
    if start <= local:
        start += datetime.timedelta(days=1)
        start = start.replace(hour=QUIET_END_HOUR)  # a DST change keeps the wall-clock hour
    return start.timestamp()


def in_quiet_window(now: float, zone) -> bool:
    """True from 22:00 to 12:00 local time, when workers do not call Garmin."""
    hour = datetime.datetime.fromtimestamp(now, zone).hour
    return hour >= QUIET_START_HOUR or hour < QUIET_END_HOUR


class GarminGuard:
    """One per process. `state` (or `shared`) is the shared state (MemoryState when None); `zone` the IANA
    name for the workers' quiet window (default REPORT_TZ, else UTC); `clock` the epoch-seconds clock (tests
    pass a fake)."""

    def __init__(self, state: Optional[SharedState] = None, *, zone: Optional[str] = None,
                 clock: Callable[[], float] = time.time, shared: Optional[SharedState] = None):
        if state is None:
            state = shared  # the keyword garmin_guard_pg's callers use
        if zone is None:
            import os

            zone = (os.environ.get("REPORT_TZ") or "").strip() or None
        self._clock = clock
        self._zone = _zone(zone)
        self._memory = MemoryState(clock, self._zone)
        self._state = state if state is not None else self._memory
        self._shared = state is not None and state is not self._memory
        self._lock = threading.Lock()
        self._live_in_flight = 0
        self._beat_wake = threading.Condition(self._lock)
        self._beat_thread: Optional[threading.Thread] = None
        self._shared_read_at: Optional[float] = None
        self._shared_cooldown: Tuple[Optional[float], Optional[str]] = (None, None)
        self._logged_shared_error = False
        self.refresh_lock = threading.Lock()

    # --- shared-state access ---------------------------------------------------------------------------

    def _shared_failed(self, exc: BaseException) -> None:
        with self._lock:
            first = not self._logged_shared_error
            self._logged_shared_error = True
        if first:
            log.warning("call guard: the shared state failed (%s); live calls use this process's copy",
                        type(exc).__name__)

    def _shared_ok(self) -> None:
        with self._lock:
            again = self._logged_shared_error
            self._logged_shared_error = False
        if again:
            log.info("call guard: the shared state works again")

    def _cooldown_for_live(self, now: float) -> Tuple[Optional[float], Optional[str]]:
        """The later of this process's cooldown and the shared one (reread at most every SHARED_READ_EVERY s)."""
        local = self._memory.get_cooldown()
        if not self._shared:
            return local
        with self._lock:
            fresh = self._shared_read_at is not None and now - self._shared_read_at < SHARED_READ_EVERY
            cached = self._shared_cooldown
        if not fresh:
            try:
                cached = self._state.get_cooldown()
            except Exception as exc:  # noqa: BLE001 - the live path never fails on the shared state
                self._shared_failed(exc)
            else:
                self._shared_ok()
            with self._lock:
                self._shared_read_at, self._shared_cooldown = now, cached
        candidates = [c for c in (local, cached) if c and c[0] is not None]
        if not candidates:
            return None, None
        # Compare by when live calls may resume, not by the worker pause.
        return max(candidates, key=lambda c: _live_until(c[0], c[1]))

    # --- the API callers use ---------------------------------------------------------------------------

    def before_call(self, caller: str) -> None:
        """Raise GuardBlocked when `caller` (TOOL or WORKER) may not call Garmin now. A worker that may call
        has taken one token from the shared bucket."""
        if caller not in CALLERS:
            raise ValueError("caller must be 'tool' or 'worker'")
        now = self._clock()
        if caller == TOOL:
            until, group = self._cooldown_for_live(now)
            if until is not None:
                live_until = _live_until(until, group)
                if now < live_until:
                    raise GuardBlocked(group or COOLDOWN, live_until)
            return
        self._worker_check(now)

    def _worker_check(self, now: float) -> None:
        try:
            # The shared state is the truth for workers (the app may have ended a login cooldown there).
            until, group = self._state.get_cooldown()
            if until is not None and now < until:
                raise GuardBlocked(group or COOLDOWN, until)
            if in_quiet_window(now, self._zone):
                raise GuardBlocked(QUIET_WINDOW, next_window_start(now, self._zone))
            with self._lock:
                busy = self._live_in_flight > 0
            last = self._state.last_live()
            if busy or (last is not None and now - last < LIVE_QUIET):
                raise GuardBlocked(LIVE_BUSY, (now if busy else last) + LIVE_QUIET)
            self._take_token(now)
        except GuardBlocked:
            raise
        except Exception as exc:  # noqa: BLE001 - a worker never calls Garmin without the shared state
            log.warning("call guard: the shared state failed (%s); the worker waits", type(exc).__name__)
            raise GuardBlocked(GUARD_UNAVAILABLE, now + LIVE_QUIET) from None

    def _take_token(self, now: float) -> None:
        """One token from the state's bucket, or GuardBlocked with the state's reason and retry time."""
        decide = getattr(self._state, "take_token_decision", None)
        if not callable(decide):
            if not self._state.take_token(now):
                raise GuardBlocked(BUCKET, now + BUCKET_SPACING)
            return
        decision = decide(now)
        if decision.ok:
            return
        reason = decision.reason if decision.reason in TOKEN_REASONS else BUCKET
        retry_at = decision.retry_at
        if retry_at is None or retry_at <= now:
            retry_at = now + BUCKET_SPACING
        raise GuardBlocked(reason, retry_at)

    def after_outcome(self, caller: str, group: Optional[str]) -> None:
        """Record how a Garmin call ended: `group` is the tool_errors group, or None for success. A
        rate_limited or login outcome starts a cooldown here and in the shared state; a live success ends a
        login cooldown (the login works again). Never raises."""
        try:
            now = self._clock()
            if group in WORKER_PAUSE:
                until = now + WORKER_PAUSE[group]
                self._memory.set_cooldown(until, group)
                if self._shared:
                    try:
                        self._state.set_cooldown(until, group)
                    except Exception as exc:  # noqa: BLE001
                        self._shared_failed(exc)
                    with self._lock:
                        self._shared_read_at = None  # reread next time
                log.warning("call guard: Garmin %s from a %s call; workers pause %d h%s", group,
                            caller if caller in CALLERS else "other", WORKER_PAUSE[group] // 3600,
                            f", live calls {LIVE_PAUSE[group] // 60} min" if LIVE_PAUSE[group] else "")
            elif group is None and caller == TOOL:
                if self._memory.get_cooldown()[1] == LOGIN:
                    self._memory.clear_cooldown(LOGIN)
                if self._shared:
                    with self._lock:
                        cached_group = self._shared_cooldown[1]
                    clear = getattr(self._state, "clear_cooldown", None)
                    if cached_group == LOGIN and clear is not None:
                        try:
                            clear(LOGIN)
                        except Exception as exc:  # noqa: BLE001
                            self._shared_failed(exc)
                        with self._lock:
                            self._shared_read_at = None
        except Exception:  # noqa: BLE001 - bookkeeping never breaks a call
            pass

    def _beat(self, force: bool = False) -> None:
        """Write the live heartbeat here and in the shared state (the shared state may skip a write it made a
        moment ago, unless `force`)."""
        self._memory.heartbeat_live()
        if not self._shared:
            return
        try:
            if force:
                try:
                    self._state.heartbeat_live(force=True)
                except TypeError:   # a state without the keyword
                    self._state.heartbeat_live()
            else:
                self._state.heartbeat_live()
        except Exception as exc:  # noqa: BLE001
            self._shared_failed(exc)

    def live_call_started(self) -> None:
        """A live tool call begins: counted and the heartbeat written; with a shared state a background
        thread rewrites it every HEARTBEAT_EVERY s while live calls run, so a worker in another process
        stays out of a long live call too."""
        with self._lock:
            self._live_in_flight += 1
            start = self._shared and (self._beat_thread is None or not self._beat_thread.is_alive())
            if start:
                self._beat_thread = threading.Thread(target=self._beat_loop, name="garmin-guard-live", daemon=True)
        self._beat()
        if start:
            self._beat_thread.start()

    def live_call_finished(self) -> None:
        """A live tool call ends: the heartbeat is written once more (forced), so the quiet time after it
        counts from its end in every process."""
        with self._lock:
            self._live_in_flight = max(0, self._live_in_flight - 1)
            self._beat_wake.notify_all()
        self._beat(force=True)

    def refresh_live(self) -> bool:
        """Rewrite the heartbeat when a live call runs now (the background thread's step; tests call it)."""
        with self._lock:
            running = self._live_in_flight > 0
        if running:
            self._beat()
        return running

    def _beat_loop(self) -> None:
        while True:
            with self._lock:
                if self._live_in_flight <= 0:
                    self._beat_thread = None
                    return
                self._beat_wake.wait(HEARTBEAT_EVERY)
                running = self._live_in_flight > 0
            if not running:
                continue
            try:
                self.refresh_live()
            except Exception:  # noqa: BLE001 - the heartbeat never breaks a live call
                pass

    @property
    def live_in_flight(self) -> int:
        with self._lock:
            return self._live_in_flight

    def cooldown(self) -> Tuple[Optional[float], Optional[str]]:
        """(until, group) as live calls see it now; for status and metrics."""
        return self._cooldown_for_live(self._clock())


def _live_until(until: Optional[float], group: Optional[str]) -> float:
    """When live calls may resume after a cooldown that keeps workers out until `until`: LIVE_PAUSE[group]
    after it was set (set time = until - WORKER_PAUSE[group]); an unknown group blocks until `until`."""
    if until is None:
        return 0.0
    if group in WORKER_PAUSE:
        return until - WORKER_PAUSE[group] + LIVE_PAUSE[group]
    return until


# --- full mode's token refresh -------------------------------------------------------------------------

_INSTALLED_REFRESH = {}


def install_refresh_lock(lock, on_refreshed: Optional[Callable[[object], None]] = None) -> bool:
    """Serialise garminconnect's token refresh (Client._refresh_session) in this process with `lock`, so two
    tool threads never refresh at once. `on_refreshed(client)` runs after each refresh (inside the lock;
    its errors are swallowed), for example to publish the new token for the worker. True when installed (or
    already installed), False when garminconnect or the method is missing. Idempotent."""
    try:
        from garminconnect import client as gc_client
    except Exception:  # noqa: BLE001 - key mode images or the stdlib leg
        return False
    cls = getattr(gc_client, "Client", None)
    original = getattr(cls, "_refresh_session", None) if cls is not None else None
    if original is None:
        return False
    if getattr(original, "_coach_refresh_lock", False):
        _INSTALLED_REFRESH["on_refreshed"] = on_refreshed
        _INSTALLED_REFRESH["lock"] = lock
        return True
    _INSTALLED_REFRESH.update(lock=lock, on_refreshed=on_refreshed)

    def _refresh_session(self, *args, **kwargs):
        with _INSTALLED_REFRESH["lock"]:
            result = original(self, *args, **kwargs)
            hook = _INSTALLED_REFRESH.get("on_refreshed")
            if hook is not None:
                try:
                    hook(self)
                except Exception:  # noqa: BLE001
                    log.warning("call guard: the token-refresh hook failed")
            return result

    _refresh_session._coach_refresh_lock = True
    _refresh_session.__wrapped__ = original
    cls._refresh_session = _refresh_session
    return True


# --- full mode's token file ----------------------------------------------------------------------------

TOKEN_FILE_NAME = "garmin_tokens.json"   # the name garminconnect gives the file inside a token folder
TOKEN_FILE_MODE = 0o600


def token_file(path):
    """The file garminconnect's Client.dump(path) writes: `path` itself when it names a .json file that is not
    a folder, else <path>/garmin_tokens.json (the same rule as garminconnect)."""
    from pathlib import Path

    p = Path(path).expanduser()
    if p.is_dir() or not p.name.endswith(".json"):
        p = p / TOKEN_FILE_NAME
    return p


def write_private_atomic(path, text: str) -> None:
    """Write `text` to `path` so a reader (or a restart after a kill) sees the old file or the new one, never
    a part: a temporary file in the same folder, mode 0600, flushed and fsynced, then os.replace(). The
    temporary file is removed when anything fails; the error propagates."""
    folder = os.path.dirname(os.fspath(path)) or "."
    fd, tmp = tempfile.mkstemp(dir=folder, prefix="." + os.path.basename(os.fspath(path)) + ".", suffix=".tmp")
    try:
        os.fchmod(fd, TOKEN_FILE_MODE)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = None
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    # The rename itself survives a power loss only once the folder is synced; best effort.
    with contextlib.suppress(OSError):
        dir_fd = os.open(folder, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)


def install_atomic_token_dump() -> bool:
    """Make garminconnect's Client.dump(path) write the token file with write_private_atomic(): garminconnect
    writes it with Path.write_text() after each token refresh, so a kill in the middle of that write could
    leave a cut file and force a new login (with MFA). The file and folder rules stay garminconnect's. True
    when installed (or already installed), False when garminconnect, Client.dump or Client.dumps is missing.
    Idempotent."""
    try:
        from garminconnect import client as gc_client
    except Exception:  # noqa: BLE001 - key mode images or the stdlib leg
        return False
    cls = getattr(gc_client, "Client", None)
    original = getattr(cls, "dump", None) if cls is not None else None
    if original is None or not callable(getattr(cls, "dumps", None)):
        return False
    if getattr(original, "_coach_atomic_dump", False):
        return True

    def dump(self, path):
        target = token_file(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        write_private_atomic(target, self.dumps())

    dump._coach_atomic_dump = True
    dump.__wrapped__ = original
    dump.__doc__ = original.__doc__
    cls.dump = dump
    return True
