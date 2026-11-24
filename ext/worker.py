"""The history worker (ROLE=worker): claims jobs from the queue and runs their handlers (plan section 5b).

Handlers are registered by the modules that own each kind (B3: index_day, build_week, sweep, rebuild; B5:
embed_doc) with register(kind, handler, garmin=True|False). A handler is handler(job, ctx): ctx carries the store,
the queue, the guard, the Garmin client, telemetry, the worker id and `conn`, the open transaction the job's own
writes and follow-up jobs must use (queue.enqueue(..., conn=ctx.conn): the outbox). The worker then completes the
job in that same transaction, so a crash leaves either everything or nothing.

What a handler may raise, and what happens to the job:
- jobqueue.Defer(seconds, group): back to the queue after `seconds`, no attempt used (a week whose days are not
  final, a stored token that is stale);
- garmin_guard.GuardBlocked(group, until): back to the queue until `until`; rate_limited and login use no attempt
  (they wait for the shared cooldown), neither do the guard's other groups; the worker then claims only kinds
  registered with garmin=False until `until` (at most MAX_PAUSE_S);
- Permanent(group): `failed` at once (dead letter);
- anything else: grouped by tool_errors.classify_exception; bad_input and no_data are not retried, every other
  group is retried with backoff until max_attempts, then `failed`.
The log line for a failed job names the kind, the job id, the group and the exception class, never its text.

Leases: a heartbeat thread renews the claim every HEARTBEAT_S; when a renewal finds the job taken (the lease ran
out and the reaper requeued it), ctx.lost is set and the worker drops the job's transaction. Every worker reaps
expired leases every REAP_S. Stop: SIGTERM or SIGINT lets the running job finish, then the loop exits.

Distributed tracing: each job runs inside telemetry.span("history.job", ...) linked to the traceparent stored at
enqueue time; queue wait goes in coach.queue.wait_s.

main() is the service's entry point; it needs feature_config (ROLE=worker, HISTORY_SEARCH=on, DATABASE_URL).
"""

from __future__ import annotations

import contextlib
import dataclasses
import importlib
import inspect
import logging
import os
import secrets
import signal
import sys
import threading
import time
from typing import Any, Callable, Optional

import jobqueue
import tool_errors

log = logging.getLogger("garmin_ext.worker")

POLL_S = 2.0
MAX_POLL_S = 30.0
REAP_S = 60.0
MAX_PAUSE_S = 900.0
NOT_RETRIED = frozenset({tool_errors.BAD_INPUT, tool_errors.NO_DATA})
# Modules that may register job handlers; each one that exists and has register_jobs(register) is called.
HANDLER_MODULES = ("history_jobs", "history_extract", "history_docs", "history_store", "history_embed",
                   "history_retrieval")


class Permanent(Exception):
    """Raised by a handler: this job can never succeed; mark it failed now (`group` is a fixed name)."""

    def __init__(self, group: str = "internal"):
        super().__init__(group)
        self.group = group


@dataclasses.dataclass(frozen=True)
class Handler:
    fn: Callable[[Any, Any], Any]
    garmin: bool


_HANDLERS: dict = {}
_LOCK = threading.Lock()


def register(kind: str, handler: Callable, *, garmin: bool = True) -> None:
    """Run `handler(job, ctx)` for jobs of `kind`. garmin=False marks kinds that never call Garmin (embed_doc,
    rebuild): they keep running while the guard pauses Garmin work."""
    if not isinstance(kind, str) or not jobqueue.KIND_RE.fullmatch(kind):
        raise ValueError("worker.register: kind must match [a-z_]{1,32}")
    if not callable(handler):
        raise TypeError("worker.register: handler must be callable")
    with _LOCK:
        _HANDLERS[kind] = Handler(handler, bool(garmin))


def registered() -> dict:
    with _LOCK:
        return dict(_HANDLERS)


def clear() -> None:
    """Tests only."""
    with _LOCK:
        _HANDLERS.clear()


@dataclasses.dataclass
class Context:
    store: Any
    queue: Any
    guard: Any
    garmin: Any
    telemetry: Any
    worker_id: str
    job: Any = None
    conn: Any = None
    vectors: Any = None          # a LangChain VectorStore, when the worker has one (sweep's retention deletes)
    lost: threading.Event = dataclasses.field(default_factory=threading.Event)


# --- telemetry, never in the way ----------------------------------------------------------------------------------

def _span_accepts(fn, name) -> bool:
    try:
        return name in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False


# The worker's outcome -> coach.job.status (telemetry's allowed values).
_SPAN_STATUS = {jobqueue.DONE: "done", jobqueue.FAILED: "failed"}


@contextlib.contextmanager
def job_span(telemetry, job):
    """telemetry.span("history.job", ...) as a child of the traceparent stored at enqueue time (a span link when
    the telemetry module takes links but no parent); a null context when telemetry is off, missing or fails.
    Yields the span (or None)."""
    span_fn = getattr(telemetry, "span", None) if telemetry is not None else None
    if not callable(span_fn):
        yield None
        return
    attrs = {"coach.job.kind": job.kind, "coach.job.attempt": job.attempts + 1,
             "coach.queue.wait_s": round(job.wait_s, 3)}
    kwargs = {}
    try:
        if job.traceparent:
            if _span_accepts(span_fn, "parent"):
                kwargs["parent"] = job.traceparent
            else:
                extract = getattr(telemetry, "extract_link", None)
                link = extract(job.traceparent) if callable(extract) else None
                if link is not None and _span_accepts(span_fn, "links"):
                    kwargs["links"] = [link]
        cm = span_fn("history.job", attrs, **kwargs)
        span = cm.__enter__()
    except Exception as exc:  # noqa: BLE001 - tracing never stops a job
        log.debug("worker: span not started (%s)", type(exc).__name__)
        yield None
        return
    try:
        yield span
    except BaseException:
        with contextlib.suppress(Exception):
            cm.__exit__(*sys.exc_info())
        raise
    else:
        with contextlib.suppress(Exception):
            cm.__exit__(None, None, None)


def _set_status(span, outcome: str, group: Optional[str] = None) -> None:
    if span is None:
        return
    with contextlib.suppress(Exception):
        span.set({"coach.job.status": _SPAN_STATUS.get(outcome, "retry")})
        if group and outcome != jobqueue.DONE:
            span.error(group)


def _metric(telemetry, kind: str, name: str, value, attrs: dict) -> None:
    fn = getattr(telemetry, f"metric_{kind}", None) if telemetry is not None else None
    if callable(fn):
        with contextlib.suppress(Exception):
            fn(name, value, attrs=attrs)


def record_queue_stats(telemetry, stats: dict) -> None:
    """Gauges: jobs by status and the oldest ready job's age (Grafana alerts on failed > 0 and age > 1 day)."""
    for status in jobqueue.STATUSES:
        _metric(telemetry, "gauge", "coach.queue.depth", stats.get(status, 0), {"coach.job.status": status})
    _metric(telemetry, "gauge", "coach.queue.oldest_age", stats.get("oldest_ready_age_s") or 0.0, {})


# --- the loop -------------------------------------------------------------------------------------------------------

def _is_guard_block(exc) -> bool:
    return type(exc).__name__ == "GuardBlocked" and hasattr(exc, "group")


def _is_permanent(exc) -> bool:
    """worker.Permanent, also when this file runs as __main__ and the handler modules imported a second copy
    of it as `worker` (then the classes differ): matched by name and its fixed `group`."""
    if isinstance(exc, Permanent):
        return True
    return (any(cls.__name__ == "Permanent" and cls.__module__ in ("worker", "__main__")
                for cls in type(exc).__mro__)
            and isinstance(getattr(exc, "group", None), str))


def _is_defer(exc) -> bool:
    """jobqueue.Defer (TokenStale too), also from a second copy of jobqueue: matched by name and `seconds`."""
    if isinstance(exc, jobqueue.Defer):
        return True
    return (any(cls.__name__ == "Defer" and cls.__module__ == "jobqueue" for cls in type(exc).__mro__)
            and isinstance(getattr(exc, "seconds", None), (int, float)))


def new_worker_id(n: int = 0) -> str:
    return f"w-{secrets.token_hex(4)}-{n}"


class Worker:
    """One claim-run-finish loop. Several may share a queue (threads or processes)."""

    def __init__(self, queue, *, store=None, guard=None, garmin=None, telemetry=None, worker_id=None,
                 handlers: Optional[dict] = None, clock: Callable[[], float] = time.time,
                 heartbeat_s: float = jobqueue.HEARTBEAT_S, reap_s: float = REAP_S, poll_s: float = POLL_S,
                 stop: Optional[threading.Event] = None, vectors=None):
        self.queue = queue
        self.vectors = vectors
        self.store, self.guard, self.garmin, self.telemetry = store, guard, garmin, telemetry
        self.worker_id = worker_id or new_worker_id()
        self._handlers = handlers
        self._clock = clock
        self._heartbeat_s = heartbeat_s
        self._reap_s = reap_s
        self._poll_s = poll_s
        self._last_reap = None
        self.garmin_paused_until = 0.0
        self.stop_event = stop or threading.Event()
        self.outcomes: list = []        # (job id, kind, outcome) for tests and logs

    def handlers(self) -> dict:
        return dict(self._handlers) if self._handlers is not None else registered()

    def stop(self) -> None:
        self.stop_event.set()

    def _kinds(self, handlers: dict) -> list:
        if self._clock() < self.garmin_paused_until:
            return sorted(k for k, h in handlers.items() if not h.garmin)
        return sorted(handlers)

    def maybe_reap(self) -> None:
        now = self._clock()
        if self._last_reap is not None and now - self._last_reap < self._reap_s:
            return
        self._last_reap = now
        try:
            out = self.queue.reap()
        except Exception as exc:  # noqa: BLE001
            log.warning("worker: reap failed (%s)", type(exc).__name__)
            return
        if out.get("requeued") or out.get("failed"):
            log.info("worker: reaped expired leases: requeued %d, failed %d", out["requeued"], out["failed"])
        if self.telemetry is not None:
            try:
                record_queue_stats(self.telemetry, self.queue.stats())
            except Exception as exc:  # noqa: BLE001
                log.debug("worker: queue stats failed (%s)", type(exc).__name__)

    def _heartbeat(self, job, ctx, done: threading.Event) -> None:
        while not done.wait(self._heartbeat_s):
            try:
                ok = self.queue.heartbeat(job)
            except Exception as exc:  # noqa: BLE001 - try again next beat; the lease covers 5 beats
                log.warning("worker: heartbeat failed (%s)", type(exc).__name__)
                continue
            if not ok:
                ctx.lost.set()
                return

    def run_once(self) -> bool:
        """Reap if due, claim one job and run it. False when there was nothing to do."""
        self.maybe_reap()
        handlers = self.handlers()
        kinds = self._kinds(handlers)
        if not kinds:
            return False
        job = self.queue.claim(self.worker_id, kinds)
        if job is None:
            return False
        handler = handlers.get(job.kind)
        if handler is None:   # registered when claimed, removed since: someone else's job
            self.queue.defer(job, 60.0, "internal")
            return True
        ctx = Context(store=self.store, queue=self.queue, guard=self.guard, garmin=self.garmin,
                      telemetry=self.telemetry, worker_id=self.worker_id, job=job, vectors=self.vectors)
        bind = getattr(self.garmin, "for_job", None) if self.garmin is not None else None
        if callable(bind):
            # This job's client: its short guard waits stop when the job loses its lease or the worker stops.
            ctx.garmin = bind(lost=ctx.lost, stop=self.stop_event)
        done = threading.Event()
        beat = None
        if self._heartbeat_s and self._heartbeat_s > 0:
            beat = threading.Thread(target=self._heartbeat, args=(job, ctx, done), daemon=True,
                                    name=f"heartbeat-{job.id}")
            beat.start()
        try:
            with job_span(self.telemetry, job) as span:
                outcome, group = self._run(job, handler, ctx)
                _set_status(span, outcome, group)
        finally:
            done.set()
            if beat is not None:
                beat.join(timeout=5)
        self.outcomes.append((job.id, job.kind, outcome))
        _metric(self.telemetry, "count", "coach.jobs", 1,
                {"coach.job.kind": job.kind, "coach.job.status": _SPAN_STATUS.get(outcome, "retry")})
        _metric(self.telemetry, "observe", "coach.queue.wait", round(job.wait_s, 3), {"coach.job.kind": job.kind})
        return True

    def _run(self, job, handler: Handler, ctx: Context) -> tuple:
        try:
            with self.queue.transaction() as conn:
                ctx.conn = conn
                handler.fn(job, ctx)
                if ctx.lost.is_set():
                    raise jobqueue.LeaseLost()
                self.queue.complete(job, conn=conn)
            return jobqueue.DONE, None
        except jobqueue.LeaseLost:
            log.info("worker: job %s (%s) lost its lease; its changes were rolled back", job.id, job.kind)
            return "lost", None
        except Exception as exc:  # noqa: BLE001 - every failure is grouped, the text is never logged
            if _is_defer(exc):
                group = getattr(exc, "group", None)
                self.queue.defer(job, exc.seconds, group)
                return "deferred", group
            if _is_permanent(exc):
                group = jobqueue.clean_group(exc.group)
                self.queue.fail(job, group, retryable=False)
                log.warning("worker: job %s (%s) failed for good: group=%s", job.id, job.kind, group)
                return jobqueue.FAILED, group
            if _is_guard_block(exc):
                return self._blocked(job, exc)
            group = tool_errors.classify_exception(exc)[0]
            status = self.queue.fail(job, group, retryable=group not in NOT_RETRIED)
            level = logging.WARNING if status == jobqueue.FAILED else logging.INFO
            log.log(level, "worker: job %s (%s) attempt %d failed: group=%s type=%s -> %s", job.id, job.kind,
                    job.attempts + 1, group, type(exc).__name__, status)
            return status or "lost", group

    def _blocked(self, job, exc) -> tuple:
        group = jobqueue.clean_group(getattr(exc, "group", None))
        until = getattr(exc, "until", None)
        now = self._clock()
        try:
            until = float(until) if until is not None else None
        except (TypeError, ValueError):
            until = None
        wait_until = until if until is not None and until > now else now + 60.0
        self.garmin_paused_until = min(wait_until, now + MAX_PAUSE_S)
        if group in jobqueue.NOT_ATTEMPTS:
            self.queue.fail(job, group, retryable=True, until=wait_until)
        else:
            self.queue.defer(job, wait_until - now, group)
        return "blocked", group

    def run(self) -> None:
        """Until stop(): run jobs back to back; when idle, poll with a growing pause (POLL_S up to MAX_POLL_S)."""
        idle = self._poll_s
        while not self.stop_event.is_set():
            try:
                worked = self.run_once()
            except Exception as exc:  # noqa: BLE001 - a database blip: wait and try again
                log.warning("worker: loop error (%s)", type(exc).__name__)
                worked = False
            if worked:
                idle = self._poll_s
                continue
            self.stop_event.wait(idle)
            idle = min(idle * 2, MAX_POLL_S)


# --- the service ------------------------------------------------------------------------------------------------------

def load_handlers(modules=HANDLER_MODULES) -> list:
    """Import each handler module that exists and call its register_jobs(register). Returns the names loaded."""
    loaded = []
    for name in modules:
        try:
            mod = importlib.import_module(name)
        except ModuleNotFoundError as exc:
            if exc.name == name:
                continue
            raise
        hook = getattr(mod, "register_jobs", None)
        if callable(hook):
            hook(register)
            loaded.append(name)
    return loaded


def _build_guard(shared):
    import garmin_guard

    cls = garmin_guard.GarminGuard
    try:
        return cls(shared=shared)
    except TypeError:
        return cls(shared)


def _setup_telemetry(cfg):
    """The telemetry module when TRACING is on and its setup worked, else None (then nothing calls it)."""
    if not getattr(cfg, "tracing", False):
        return None
    try:
        import telemetry
    except ModuleNotFoundError:
        return None
    try:
        telemetry.setup(cfg)
    except Exception as exc:  # noqa: BLE001
        log.warning("worker: tracing setup failed (%s); running without it", type(exc).__name__)
        return None
    return telemetry


def main(argv=None, environ=None) -> int:
    environ = os.environ if environ is None else environ
    import feature_config
    import status_common

    problems = feature_config.problems(environ)
    for line in problems:
        print(f"worker: config error: {line}", file=sys.stderr)
    if problems:
        return 1
    cfg = feature_config.load(environ)
    if cfg.role != "worker" or not cfg.history:
        print("worker: config error: the worker runs only with ROLE=worker and HISTORY_SEARCH=on", file=sys.stderr)
        return 1
    if cfg.history_forget:
        # The app deletes the history at its startup; a worker must not recreate the tables meanwhile.
        log.warning("worker: HISTORY_FORGET=yes: idle until the flag is removed (the app deletes the history)")
        idle = threading.Event()
        signal.signal(signal.SIGTERM, lambda signum, frame: idle.set())
        idle.wait()
        return 0
    n = cfg.worker_concurrency
    try:
        tz = status_common.zone()
    except ValueError as exc:
        print(f"worker: config error: {exc}", file=sys.stderr)
        return 1
    dsn = cfg.database_url or environ.get("DATABASE_URL", "")
    # As launch._setup_features: every secret form registered for redaction, tracing set up (it reads the
    # OTLP headers), then the secret variables taken out of the environment.
    for value in feature_config.secret_values(cfg):
        tool_errors.register_secret(value)
    tool_errors.register_secret(dsn)
    tele = _setup_telemetry(cfg)
    feature_config.take_secrets(environ)

    import garmin_guard_pg
    import history_store
    import worker_garmin

    # The store's migrations first (B3's versioned files), then the queue's, the guard's and the token's
    # tables; all take the one advisory lock (jobqueue.MIGRATE_LOCK_ID), all in schema history.
    store = history_store.PgHistoryStore(dsn)
    store.migrate()
    queue = jobqueue.PgJobQueue(dsn)
    queue.migrate()
    shared = garmin_guard_pg.PgSharedState(dsn, tz)
    shared.migrate()
    guard = _build_guard(shared)
    try:
        token_store = None
        if cfg.garmin_auth == "full":
            key = cfg.history_token_key
            if key is None:
                raise ValueError(worker_garmin.ERR_TOKEN_KEY)
            token_store = worker_garmin.PgTokenStore(dsn, key)
            token_store.migrate()
        garmin = worker_garmin.build(environ, guard, dsn=dsn, token_store=token_store)
    except ValueError as exc:
        print(f"worker: config error: {exc}", file=sys.stderr)
        _shutdown(tele)
        return 1
    vectors = None
    try:
        import history_retrieval

        vectors = history_retrieval.worker_vectors(dsn)   # deletes vector rows by id; no model needed
    except Exception as exc:  # noqa: BLE001 - the sweep then skips vector deletes (history_jobs logs it)
        log.warning("worker: no vector store for retention deletes (%s)", type(exc).__name__)
    loaded = load_handlers()
    kinds = sorted(registered())
    log.info("worker: on, %d thread(s), handlers from %s, kinds %s", n, ",".join(loaded) or "none",
             ",".join(kinds) or "none")
    if not kinds:
        print("worker: config error: no job handlers are registered", file=sys.stderr)
        _shutdown(tele)
        return 1

    stop = threading.Event()

    def on_signal(signum, frame):
        log.info("worker: stop requested; finishing the running job")
        stop.set()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    workers = [Worker(queue, store=store, guard=guard, garmin=garmin, telemetry=tele, worker_id=new_worker_id(i),
                      stop=stop, vectors=vectors) for i in range(n)]
    threads = [threading.Thread(target=w.run, name=f"worker-{i}") for i, w in enumerate(workers)]
    for t in threads:
        t.start()
    while any(t.is_alive() for t in threads):
        for t in threads:
            t.join(timeout=1.0)
    _shutdown(tele)
    log.info("worker: stopped")
    return 0


def _shutdown(tele) -> None:
    if tele is not None:
        with contextlib.suppress(Exception):
            tele.shutdown()


def configure_logging() -> None:
    """INFO lines on stderr; the Garmin libraries' records hidden and registered secrets redacted
    (log_privacy: garminconnect logs a failed call with the display name in the path and Garmin's answer)."""
    import log_privacy

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    log_privacy.install()


if __name__ == "__main__":
    configure_logging()
    # Run the importable module, not this __main__ copy: the handler modules `import worker`, and their
    # worker.Permanent must be the class the loop catches.
    import worker as _w
    sys.exit(_w.main())
