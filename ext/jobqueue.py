"""The history job queue: a small JobQueue interface, a Postgres implementation and an in-memory one.

Plan section 5b. Jobs live in `history.jobs`; a worker claims one with
`UPDATE ... WHERE id = (SELECT ... FOR UPDATE SKIP LOCKED LIMIT 1)`, holds it under a lease it renews
(heartbeat), and finishes it with complete(), fail() or defer(). Each claim carries a fresh claim token, and every
write a worker makes to its job is fenced by that token, so a worker whose lease expired (and whose job another
worker took) can no longer change the job.

- enqueue(kind, key, payload, *, run_at=None, traceparent=None, priority=0, conn=None): while a queued job holds
  the same key, its payload takes in the new one (merge_payload: the new keys win, a flag that was true stays
  true) and None is returned. While a running job holds it, that job takes in the payload the same way and is
  marked to run again (`rerun`): complete() then puts it back in the queue instead of finishing it, so data that
  changed while it ran is fetched again; the running job's id is returned. A job runs again at most MAX_RERUNS
  times in a row (a handler that enqueues its own key cannot loop for ever); after that an enqueue of its key
  while it runs returns None and changes nothing, and the job ends normally. `conn` is an open transaction from
  transaction(): the insert commits or rolls back with the caller's own writes (the outbox pattern); the id
  returned then is provisional in MemoryJobQueue (below zero) and real in PgJobQueue.
- claim(worker_id, kinds) -> Job | None: the ready job with the highest priority, then the oldest next_run_at.
- heartbeat(job) -> bool: renews the lease; False when the job is no longer this claim's (lost lease).
- complete(job, conn=None) -> bool. With `conn`, a lost lease raises LeaseLost so the caller rolls back. A job
  marked `rerun` goes back to `queued` (attempts 0, ready now) instead of `done`.
- fail(job, group, retryable, *, until=None) -> str (the new status): `rate_limited` and `login` never count as an
  attempt, the job waits for the shared cooldown (`until`, else 6 h / 24 h). Otherwise attempts += 1 and the job
  waits backoff_s(attempts) (1 m, 5 m, 30 m, 2 h, 6 h, +-20% jitter), or ends `failed` (the dead-letter state) when
  it was not retryable or used its max_attempts.
- defer(job, seconds, group=None) -> bool: back to the queue without an attempt (a build_week whose days are not
  final yet, a worker the guard blocked).
- reap(now=None) -> dict: running jobs past their lease go back to `queued` (one attempt used, so a job that
  kills its worker every time still ends `failed`); done jobs are deleted after 7 days, failed ones after 30.
- stats(now=None) -> dict: counts by kind and status, queue depth, oldest ready age, what queued jobs wait for.

Error groups stored in `last_error_group` are fixed names (GROUP_RE), never raw error text. Keys and payloads
carry dates and kinds, never health values or names.

Standard library only at import time: psycopg (3) is imported inside PgJobQueue, so the stdlib test leg imports
this module and runs every MemoryJobQueue test.
"""

from __future__ import annotations

import contextlib
import copy
import dataclasses
import itertools
import json
import logging
import random
import re
import secrets
import threading
import time
from typing import Any, Callable, Iterable, Optional

log = logging.getLogger("garmin_ext.jobs")

QUEUED, RUNNING, DONE, FAILED = "queued", "running", "done", "failed"
STATUSES = (QUEUED, RUNNING, DONE, FAILED)
KINDS = ("index_day", "build_week", "embed_doc", "sweep", "rebuild")

LEASE_S = 300                 # a claim holds its job this long ...
HEARTBEAT_S = 60              # ... and the worker renews it this often
BACKOFF_S = (60, 300, 1800, 7200, 21600)   # after attempt 1, 2, 3, 4, 5 (and later: the last value)
JITTER = 0.2                  # each delay is scaled by a factor in [1 - JITTER, 1 + JITTER]
MAX_ATTEMPTS = 6
COOLDOWN_S = {"rate_limited": 6 * 3600, "login": 24 * 3600}   # waits that never count as an attempt
NOT_ATTEMPTS = frozenset(COOLDOWN_S)
LEASE_EXPIRED = "lease_expired"
KEEP_DONE_S = 7 * 86400
KEEP_FAILED_S = 30 * 86400
PAYLOAD_MAX = 4096            # bytes of JSON
MAX_RERUNS = 3                # runs again in a row (enqueued while running), then the job ends normally

KIND_RE = re.compile(r"[a-z_]{1,32}")
KEY_RE = re.compile(r"[A-Za-z0-9_:.-]{1,128}")
GROUP_RE = re.compile(r"[a-z_]{1,32}")
WORKER_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}")
TRACEPARENT_RE = re.compile(r"[0-9a-f]{2}-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}")


class LeaseLost(Exception):
    """The job is no longer this claim's: its lease expired and it was reaped (or taken by another worker)."""


class Defer(Exception):
    """Raised by a handler (or the worker's Garmin client): put the job back for `seconds`, no attempt used.
    `group` is a fixed name recorded as the job's last_error_group (what it waits for)."""

    def __init__(self, seconds: float, group: Optional[str] = None):
        super().__init__(f"deferred {float(seconds):.0f} s")
        self.seconds = float(seconds)
        self.group = group


@dataclasses.dataclass
class Job:
    id: int
    kind: str
    key: str
    payload: dict
    attempts: int
    max_attempts: int
    traceparent: Optional[str]
    claim_token: str
    worker_id: str
    created_at: float          # epoch seconds
    ready_at: float            # next_run_at when it was claimed
    claimed_at: float

    @property
    def wait_s(self) -> float:
        """How long the job was ready before a worker took it."""
        return max(0.0, self.claimed_at - self.ready_at)


# --- shared rules -----------------------------------------------------------------------------------------------

def backoff_s(attempts: int, rng: Callable[[], float] = random.random) -> float:
    """The wait after the `attempts`-th failed attempt (1-based): BACKOFF_S with +-JITTER."""
    base = BACKOFF_S[min(max(attempts, 1), len(BACKOFF_S)) - 1]
    return base * (1.0 + JITTER * (2.0 * rng() - 1.0))


def clean_group(group) -> str:
    """A fixed group name for last_error_group; anything else becomes 'internal' (never raw text)."""
    return group if isinstance(group, str) and GROUP_RE.fullmatch(group) else "internal"


def merge_payload(old, new) -> dict:
    """The payload of a job enqueued again: `old` updated with the keys of `new`, except that a flag that was
    true in `old` stays true ({"recheck": true} then {"backfill": true} keeps both). One rule for both queues."""
    out = dict(old or {})
    for k, v in (new or {}).items():
        if out.get(k) is True:
            continue
        out[k] = copy.deepcopy(v)
    return out


def clean_traceparent(value) -> Optional[str]:
    return value if isinstance(value, str) and TRACEPARENT_RE.fullmatch(value) else None


def _check_enqueue(kind, key, payload, priority):
    if not isinstance(kind, str) or not KIND_RE.fullmatch(kind):
        raise ValueError("jobqueue: kind must match [a-z_]{1,32}")
    if not isinstance(key, str) or not KEY_RE.fullmatch(key):
        raise ValueError("jobqueue: key must match [A-Za-z0-9_:.-]{1,128}")
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise ValueError("jobqueue: payload must be a dict")
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    if len(text.encode()) > PAYLOAD_MAX:
        raise ValueError("jobqueue: payload is larger than 4096 bytes")
    if not isinstance(priority, int) or isinstance(priority, bool):
        raise ValueError("jobqueue: priority must be an int")
    return json.loads(text), text


def _check_worker(worker_id):
    if not isinstance(worker_id, str) or not WORKER_RE.fullmatch(worker_id):
        raise ValueError("jobqueue: worker_id must match [A-Za-z0-9_.-]{1,64}")


def _kinds(kinds) -> Optional[list]:
    if kinds is None:
        return None
    out = sorted({k for k in kinds if isinstance(k, str)})
    return out


def fail_plan(attempts: int, max_attempts: int, group: str, retryable: bool, now: float,
              until: Optional[float], rng: Callable[[], float], *, rerun: bool = False) -> tuple:
    """(status, attempts, next_run_at) after a failure; one rule for both implementations. A job marked `rerun`
    (enqueued again while it ran) that would end failed is queued again as a new job instead (attempts 0, after
    the first backoff)."""
    if group in NOT_ATTEMPTS:
        wait_until = until if until is not None and until > now else now + COOLDOWN_S[group]
        return QUEUED, attempts, wait_until
    attempts += 1
    if not retryable or attempts >= max_attempts:
        if rerun:
            return QUEUED, 0, now + backoff_s(1, rng)
        return FAILED, attempts, now
    return QUEUED, attempts, now + backoff_s(attempts, rng)


# --- in memory --------------------------------------------------------------------------------------------------

@dataclasses.dataclass
class _Row:
    id: int
    kind: str
    key: str
    payload: dict
    status: str
    attempts: int
    max_attempts: int
    next_run_at: float
    priority: int
    traceparent: Optional[str]
    created_at: float
    updated_at: float
    lease_until: Optional[float] = None
    worker_id: Optional[str] = None
    claim_token: Optional[str] = None
    last_error_group: Optional[str] = None
    finished_at: Optional[float] = None
    rerun: bool = False
    reruns: int = 0             # runs again in a row so far (MAX_RERUNS)


class _MemoryTxn:
    """A transaction on MemoryJobQueue: enqueues and the completion are buffered and applied together on commit,
    or dropped on rollback. Handlers may register their own writes with on_commit(fn) (B3's memory store)."""

    def __init__(self, queue):
        self.queue = queue
        self.ops: list = []

    def on_commit(self, fn: Callable[[], Any]) -> None:
        self.ops.append(("call", fn))


class MemoryJobQueue:
    """The JobQueue in one process: tests, and the shape every implementation must keep. Thread-safe."""

    def __init__(self, *, clock: Callable[[], float] = time.time, rng: Callable[[], float] = random.random,
                 lease_s: float = LEASE_S, max_attempts: int = MAX_ATTEMPTS):
        self._clock = clock
        self._rng = rng
        self._lease_s = lease_s
        self._max_attempts = max_attempts
        self._lock = threading.RLock()
        self._rows: dict = {}
        self._ids = itertools.count(1)
        self.claims = 0          # for tests: how many claims succeeded

    # the outbox
    @contextlib.contextmanager
    def transaction(self):
        txn = _MemoryTxn(self)
        yield txn
        with self._lock:
            # Check every fence first, so a lost lease applies nothing at all.
            for op in txn.ops:
                if op[0] == "complete" and not self._fenced(op[1]):
                    raise LeaseLost()
            for op in txn.ops:
                if op[0] == "enqueue":
                    self._insert(*op[1:])
                elif op[0] == "complete":
                    self._complete(op[1])
                else:
                    op[1]()

    def migrate(self) -> None:
        return None

    def enqueue(self, kind, key, payload=None, *, run_at=None, traceparent=None, priority=0, conn=None,
                max_attempts=None):
        payload, _ = _check_enqueue(kind, key, payload, priority)
        args = (kind, key, payload, run_at, clean_traceparent(traceparent), priority,
                max_attempts or self._max_attempts)
        if conn is not None:
            # Buffered until commit (where a held key's payload is merged); the answer reflects what commit will
            # do unless another enqueue races it.
            for i, op in enumerate(conn.ops):
                if op[0] == "enqueue" and op[2] == key:
                    conn.ops[i] = (*op[:3], merge_payload(op[3], payload), *op[4:])
                    return None
            with self._lock:
                queued = self._holds(key, QUEUED) is not None
            conn.ops.append(("enqueue", *args))
            return None if queued else -len(conn.ops)      # a provisional id
        with self._lock:
            return self._insert(*args)

    def _holds(self, key, *statuses) -> Optional[_Row]:
        statuses = statuses or (QUEUED, RUNNING)
        for row in self._rows.values():
            if row.key == key and row.status in statuses:
                return row
        return None

    def _insert(self, kind, key, payload, run_at, traceparent, priority, max_attempts):
        held = self._holds(key)
        if held is not None:
            if held.status != RUNNING:
                held.payload = merge_payload(held.payload, payload)
                return None
            if held.reruns >= MAX_RERUNS:
                return None      # it ran again MAX_RERUNS times in a row: this run ends it
            # Running: run it again after it completes, with the payloads merged.
            held.rerun = True
            held.payload = merge_payload(held.payload, payload)
            held.priority = max(held.priority, priority)
            held.traceparent = traceparent or held.traceparent
            return held.id
        now = self._clock()
        row = _Row(id=next(self._ids), kind=kind, key=key, payload=copy.deepcopy(payload), status=QUEUED,
                   attempts=0, max_attempts=max_attempts, next_run_at=now if run_at is None else float(run_at),
                   priority=priority, traceparent=traceparent, created_at=now, updated_at=now)
        self._rows[row.id] = row
        return row.id

    def claim(self, worker_id, kinds=None):
        _check_worker(worker_id)
        kinds = _kinds(kinds)
        with self._lock:
            now = self._clock()
            ready = [r for r in self._rows.values()
                     if r.status == QUEUED and r.next_run_at <= now and (kinds is None or r.kind in kinds)]
            if not ready:
                return None
            row = min(ready, key=lambda r: (-r.priority, r.next_run_at, r.id))
            row.status, row.worker_id = RUNNING, worker_id
            row.claim_token = secrets.token_hex(16)
            row.lease_until = now + self._lease_s
            row.updated_at = now
            self.claims += 1
            return Job(id=row.id, kind=row.kind, key=row.key, payload=copy.deepcopy(row.payload),
                       attempts=row.attempts, max_attempts=row.max_attempts, traceparent=row.traceparent,
                       claim_token=row.claim_token, worker_id=worker_id, created_at=row.created_at,
                       ready_at=row.next_run_at, claimed_at=now)

    def _fenced(self, job) -> Optional[_Row]:
        row = self._rows.get(job.id)
        if row is None or row.status != RUNNING or row.claim_token != job.claim_token:
            return None
        return row

    def heartbeat(self, job) -> bool:
        with self._lock:
            row = self._fenced(job)
            if row is None:
                return False
            now = self._clock()
            row.lease_until, row.updated_at = now + self._lease_s, now
            return True

    def _complete(self, job) -> bool:
        row = self._fenced(job)
        if row is None:
            return False
        now = self._clock()
        if row.rerun:
            row.status, row.attempts, row.next_run_at, row.updated_at = QUEUED, 0, now, now
            row.rerun = False
            row.reruns += 1
            row.lease_until = row.claim_token = row.worker_id = None
            return True
        row.status, row.finished_at, row.updated_at = DONE, now, now
        row.lease_until = row.claim_token = None
        return True

    def complete(self, job, conn=None) -> bool:
        if conn is not None:
            conn.ops.append(("complete", job))
            return True
        with self._lock:
            return self._complete(job)

    def fail(self, job, group, retryable, *, until=None) -> Optional[str]:
        group = clean_group(group)
        with self._lock:
            row = self._fenced(job)
            if row is None:
                return None
            now = self._clock()
            status, attempts, next_run = fail_plan(row.attempts, row.max_attempts, group, retryable, now, until,
                                                   self._rng, rerun=row.rerun)
            row.status, row.attempts, row.next_run_at, row.rerun = status, attempts, next_run, False
            row.last_error_group, row.updated_at = group, now
            row.lease_until = row.claim_token = row.worker_id = None
            if status == FAILED:
                row.finished_at = now
            return status

    def defer(self, job, seconds, group=None) -> bool:
        with self._lock:
            row = self._fenced(job)
            if row is None:
                return False
            now = self._clock()
            row.status, row.next_run_at, row.updated_at = QUEUED, now + max(0.0, float(seconds)), now
            row.lease_until = row.claim_token = row.worker_id = None
            row.rerun = False       # it runs again anyway
            if group is not None:
                row.last_error_group = clean_group(group)
            return True

    def reap(self, now=None) -> dict:
        with self._lock:
            now = self._clock() if now is None else now
            out = {"requeued": 0, "failed": 0, "purged": 0}
            for row in list(self._rows.values()):
                if row.status == RUNNING and row.lease_until is not None and row.lease_until < now:
                    row.attempts += 1
                    row.last_error_group = LEASE_EXPIRED
                    row.lease_until = row.claim_token = row.worker_id = None
                    row.updated_at = now
                    row.rerun = False   # requeued anyway; failed when it used every attempt
                    if row.attempts >= row.max_attempts:
                        row.status, row.finished_at = FAILED, now
                        out["failed"] += 1
                    else:
                        row.status, row.next_run_at = QUEUED, now
                        out["requeued"] += 1
                elif ((row.status == DONE and row.finished_at is not None and row.finished_at < now - KEEP_DONE_S)
                      or (row.status == FAILED and row.finished_at is not None
                          and row.finished_at < now - KEEP_FAILED_S)):
                    del self._rows[row.id]
                    out["purged"] += 1
            return out

    def stats(self, now=None) -> dict:
        with self._lock:
            now = self._clock() if now is None else now
            return _stats(((r.kind, r.status, r.next_run_at, r.last_error_group) for r in self._rows.values()), now)

    # tests only
    def rows(self) -> list:
        with self._lock:
            return [dataclasses.replace(r) for r in self._rows.values()]


def _stats(rows: Iterable[tuple], now: float) -> dict:
    counts: dict = {}
    out = {"counts": counts, "queued": 0, "ready": 0, "running": 0, "done": 0, "failed": 0,
           "oldest_ready_age_s": None, "waiting_by_group": {}}
    oldest = None
    for kind, status, next_run_at, group in rows:
        counts.setdefault(kind, {s: 0 for s in STATUSES})[status] += 1
        out[status] += 1
        if status == QUEUED:
            if next_run_at <= now:
                out["ready"] += 1
                oldest = next_run_at if oldest is None else min(oldest, next_run_at)
            elif group:
                out["waiting_by_group"][group] = out["waiting_by_group"].get(group, 0) + 1
    if oldest is not None:
        out["oldest_ready_age_s"] = round(now - oldest, 3)
    return out


# --- Postgres ---------------------------------------------------------------------------------------------------

QUEUE_DDL = """
CREATE SCHEMA IF NOT EXISTS history;
CREATE TABLE IF NOT EXISTS history.jobs (
    id               bigserial PRIMARY KEY,
    kind             text NOT NULL CHECK (kind ~ '^[a-z_]{1,32}$'),
    key              text NOT NULL CHECK (key ~ '^[A-Za-z0-9_:.-]{1,128}$'),
    payload          jsonb NOT NULL DEFAULT '{}'::jsonb,
    status           text NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'running', 'done', 'failed')),
    priority         integer NOT NULL DEFAULT 0,
    attempts         integer NOT NULL DEFAULT 0,
    max_attempts     integer NOT NULL DEFAULT 6,
    next_run_at      timestamptz NOT NULL DEFAULT now(),
    lease_until      timestamptz,
    worker_id        text,
    claim_token      text,
    last_error_group text CHECK (last_error_group IS NULL OR last_error_group ~ '^[a-z_]{1,32}$'),
    traceparent      text,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    finished_at      timestamptz,
    rerun            boolean NOT NULL DEFAULT false,
    reruns           integer NOT NULL DEFAULT 0
);
ALTER TABLE history.jobs ADD COLUMN IF NOT EXISTS rerun boolean NOT NULL DEFAULT false;
ALTER TABLE history.jobs ADD COLUMN IF NOT EXISTS reruns integer NOT NULL DEFAULT 0;
CREATE UNIQUE INDEX IF NOT EXISTS jobs_key_unfinished ON history.jobs (key) WHERE status IN ('queued', 'running');
CREATE INDEX IF NOT EXISTS jobs_ready ON history.jobs (priority DESC, next_run_at, id) WHERE status = 'queued';
CREATE INDEX IF NOT EXISTS jobs_running_lease ON history.jobs (lease_until) WHERE status = 'running';
CREATE INDEX IF NOT EXISTS jobs_finished ON history.jobs (finished_at) WHERE status IN ('done', 'failed');
"""

_RETURNING = ("id, kind, key, payload, attempts, max_attempts, traceparent, claim_token, worker_id, "
              "extract(epoch from created_at)::float8, extract(epoch from next_run_at)::float8, "
              "extract(epoch from now())::float8")

# A held key (queued, or running and not yet rerun MAX_RERUNS times) takes in the new payload as merge_payload()
# does: the old payload || the new one without the keys that are true in the old one. A running job is also
# marked rerun. RETURNING says whether the row was inserted (xmax = 0) and whether it is a running job now
# marked rerun: the id is returned for those two only (a queued key returns None).
_SQL_ENQUEUE = (
    "INSERT INTO history.jobs (kind, key, payload, next_run_at, traceparent, priority, max_attempts) "
    "VALUES (%(kind)s, %(key)s, %(payload)s::jsonb, "
    "COALESCE(to_timestamp(%(run_at)s::float8), now()), %(tp)s, %(priority)s, %(max)s) "
    "ON CONFLICT (key) WHERE status IN ('queued', 'running') DO UPDATE SET "
    "payload = history.jobs.payload || (EXCLUDED.payload - ARRAY(SELECT e.key FROM "
    "jsonb_each(history.jobs.payload) AS e WHERE e.value = 'true'::jsonb)), "
    "rerun = history.jobs.rerun OR history.jobs.status = 'running', "
    "priority = CASE WHEN history.jobs.status = 'running' "
    "THEN GREATEST(history.jobs.priority, EXCLUDED.priority) ELSE history.jobs.priority END, "
    "traceparent = CASE WHEN history.jobs.status = 'running' "
    "THEN COALESCE(EXCLUDED.traceparent, history.jobs.traceparent) ELSE history.jobs.traceparent END, "
    "updated_at = now() "
    "WHERE history.jobs.status = 'queued' OR history.jobs.reruns < %(max_reruns)s "
    "RETURNING id, (xmax = 0) AS inserted, status, rerun")

_SQL_CLAIM = (
    "UPDATE history.jobs SET status = 'running', worker_id = %(w)s, claim_token = %(t)s, "
    "lease_until = now() + make_interval(secs => %(lease)s), updated_at = now() "
    "WHERE id = (SELECT id FROM history.jobs WHERE status = 'queued' AND next_run_at <= now() "
    "AND (%(kinds)s::text[] IS NULL OR kind = ANY(%(kinds)s::text[])) "
    "ORDER BY priority DESC, next_run_at, id FOR UPDATE SKIP LOCKED LIMIT 1) "
    "RETURNING " + _RETURNING)

_FENCE = "WHERE id = %(id)s AND claim_token = %(t)s AND status = 'running'"

_SQL_HEARTBEAT = ("UPDATE history.jobs SET lease_until = now() + make_interval(secs => %(lease)s), "
                  "updated_at = now() " + _FENCE + " RETURNING id")
# A job marked rerun goes back to the queue (a new run, ready now) instead of done.
_SQL_COMPLETE = ("UPDATE history.jobs SET status = CASE WHEN rerun THEN 'queued' ELSE 'done' END, "
                 "finished_at = CASE WHEN rerun THEN NULL ELSE now() END, "
                 "attempts = CASE WHEN rerun THEN 0 ELSE attempts END, "
                 "reruns = CASE WHEN rerun THEN reruns + 1 ELSE reruns END, "
                 "next_run_at = CASE WHEN rerun THEN now() ELSE next_run_at END, "
                 "worker_id = CASE WHEN rerun THEN NULL ELSE worker_id END, rerun = false, updated_at = now(), "
                 "lease_until = NULL, claim_token = NULL " + _FENCE + " RETURNING id")
_SQL_FAIL_READ = ("SELECT attempts, max_attempts, extract(epoch from now())::float8, rerun FROM history.jobs "
                  + _FENCE + " FOR UPDATE")
_SQL_FAIL_WRITE = (
    "UPDATE history.jobs SET status = %(status)s, attempts = %(attempts)s, rerun = false, "
    "next_run_at = to_timestamp(%(next)s::float8), last_error_group = %(group)s, updated_at = now(), "
    "lease_until = NULL, claim_token = NULL, worker_id = NULL, "
    "finished_at = CASE WHEN %(status)s = 'failed' THEN now() ELSE NULL END " + _FENCE)
_SQL_DEFER = (
    "UPDATE history.jobs SET status = 'queued', next_run_at = now() + make_interval(secs => %(secs)s), "
    "last_error_group = COALESCE(%(group)s, last_error_group), updated_at = now(), rerun = false, "
    "lease_until = NULL, claim_token = NULL, worker_id = NULL " + _FENCE + " RETURNING id")
_SQL_REAP = (
    "WITH expired AS (SELECT id FROM history.jobs WHERE status = 'running' "
    "AND lease_until < COALESCE(to_timestamp(%(now)s::float8), now()) FOR UPDATE SKIP LOCKED) "
    "UPDATE history.jobs j SET attempts = j.attempts + 1, "
    "status = CASE WHEN j.attempts + 1 >= j.max_attempts THEN 'failed' ELSE 'queued' END, "
    "finished_at = CASE WHEN j.attempts + 1 >= j.max_attempts THEN now() ELSE NULL END, "
    "next_run_at = now(), last_error_group = 'lease_expired', updated_at = now(), rerun = false, "
    "lease_until = NULL, claim_token = NULL, worker_id = NULL "
    "FROM expired WHERE j.id = expired.id RETURNING j.status")
_SQL_PURGE = (
    "DELETE FROM history.jobs WHERE (status = 'done' AND finished_at < now() - make_interval(secs => %(done)s)) "
    "OR (status = 'failed' AND finished_at < now() - make_interval(secs => %(failed)s))")
_SQL_STATS = "SELECT kind, status, extract(epoch from next_run_at)::float8, last_error_group FROM history.jobs"
# Two services may start at once: one migrates, the other waits (CREATE ... IF NOT EXISTS races otherwise).
# The same key as history_store.MIGRATE_LOCK_ID ("coac"), so every history migration takes one lock.
MIGRATE_LOCK_ID = 0x636F6163
MIGRATE_LOCK = f"SELECT pg_advisory_xact_lock({MIGRATE_LOCK_ID})"


def connect(dsn: str, *, autocommit: bool = True, application_name: str = "coach-mcp"):
    """A psycopg 3 connection with short timeouts. The DSN is never logged. Autocommit by default: callers
    group statements with `with conn.transaction():` (psycopg's recommended pattern)."""
    import psycopg

    return psycopg.connect(dsn, autocommit=autocommit, connect_timeout=5, application_name=application_name,
                           options="-c statement_timeout=30000 -c lock_timeout=10000")


class PgJobQueue:
    """The JobQueue on Postgres (history.jobs). Every call opens a short connection unless given `conn`."""

    def __init__(self, dsn: str, *, lease_s: float = LEASE_S, max_attempts: int = MAX_ATTEMPTS,
                 rng: Callable[[], float] = random.random, application_name: str = "coach-mcp-jobs"):
        if not isinstance(dsn, str) or not dsn:
            raise ValueError("jobqueue: DATABASE_URL is not set")
        self._dsn = dsn
        self._lease_s = float(lease_s)
        self._max_attempts = max_attempts
        self._rng = rng
        self._app = application_name

    def __repr__(self):
        return "PgJobQueue(<dsn hidden>)"

    @contextlib.contextmanager
    def _conn(self, conn=None):
        if conn is not None:
            yield conn
            return
        c = connect(self._dsn, application_name=self._app)
        try:
            with c.transaction():
                yield c
        finally:
            c.close()

    @contextlib.contextmanager
    def transaction(self):
        """One Postgres transaction: the handler's writes, its follow-up jobs and complete() commit together."""
        with self._conn() as c:
            yield c

    def migrate(self) -> None:
        with self._conn() as c:
            c.execute(MIGRATE_LOCK)
            c.execute(QUEUE_DDL)

    def enqueue(self, kind, key, payload=None, *, run_at=None, traceparent=None, priority=0, conn=None,
                max_attempts=None):
        _, text = _check_enqueue(kind, key, payload, priority)
        params = {"kind": kind, "key": key, "payload": text, "run_at": None if run_at is None else float(run_at),
                  "tp": clean_traceparent(traceparent), "priority": priority,
                  "max": max_attempts or self._max_attempts, "max_reruns": MAX_RERUNS}
        with self._conn(conn) as c:
            row = c.execute(_SQL_ENQUEUE, params).fetchone()
        if row is None:
            return None                  # running and rerun MAX_RERUNS times: nothing changed
        if row[1] or (row[2] == RUNNING and row[3]):
            return row[0]
        return None                      # a queued key: its payload was merged

    def claim(self, worker_id, kinds=None):
        _check_worker(worker_id)
        params = {"w": worker_id, "t": secrets.token_hex(16), "lease": self._lease_s, "kinds": _kinds(kinds)}
        with self._conn() as c:
            row = c.execute(_SQL_CLAIM, params).fetchone()
        if row is None:
            return None
        payload = row[3] if isinstance(row[3], dict) else json.loads(row[3] or "{}")
        return Job(id=row[0], kind=row[1], key=row[2], payload=payload, attempts=row[4], max_attempts=row[5],
                   traceparent=row[6], claim_token=row[7], worker_id=row[8], created_at=row[9], ready_at=row[10],
                   claimed_at=row[11])

    def heartbeat(self, job) -> bool:
        with self._conn() as c:
            row = c.execute(_SQL_HEARTBEAT, {"id": job.id, "t": job.claim_token, "lease": self._lease_s}).fetchone()
        return row is not None

    def complete(self, job, conn=None) -> bool:
        with self._conn(conn) as c:
            row = c.execute(_SQL_COMPLETE, {"id": job.id, "t": job.claim_token}).fetchone()
        if row is None and conn is not None:
            raise LeaseLost()
        return row is not None

    def fail(self, job, group, retryable, *, until=None) -> Optional[str]:
        group = clean_group(group)
        fence = {"id": job.id, "t": job.claim_token}
        with self._conn() as c:
            row = c.execute(_SQL_FAIL_READ, fence).fetchone()
            if row is None:
                return None
            status, attempts, next_run = fail_plan(row[0], row[1], group, retryable, row[2], until, self._rng,
                                                   rerun=bool(row[3]))
            c.execute(_SQL_FAIL_WRITE, {**fence, "status": status, "attempts": attempts, "next": next_run,
                                        "group": group})
        return status

    def defer(self, job, seconds, group=None) -> bool:
        params = {"id": job.id, "t": job.claim_token, "secs": max(0.0, float(seconds)),
                  "group": None if group is None else clean_group(group)}
        with self._conn() as c:
            row = c.execute(_SQL_DEFER, params).fetchone()
        return row is not None

    def reap(self, now=None) -> dict:
        out = {"requeued": 0, "failed": 0, "purged": 0}
        with self._conn() as c:
            for (status,) in c.execute(_SQL_REAP, {"now": None if now is None else float(now)}).fetchall():
                out["requeued" if status == QUEUED else "failed"] += 1
            cur = c.execute(_SQL_PURGE, {"done": float(KEEP_DONE_S), "failed": float(KEEP_FAILED_S)})
            out["purged"] = max(0, cur.rowcount or 0)
        return out

    def stats(self, now=None) -> dict:
        with self._conn() as c:
            rows = c.execute(_SQL_STATS).fetchall()
            db_now = c.execute("SELECT extract(epoch from now())::float8").fetchone()[0]
        return _stats(((r[0], r[1], r[2], r[3]) for r in rows), db_now if now is None else now)
