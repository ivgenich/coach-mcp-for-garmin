"""The history scheduler (ROLE=enqueue): put the day's jobs in the queue, then exit.

A Railway cron service runs this once a day (UTC). What to enqueue is history_jobs.scheduled_jobs(store, today)
(history_jobs owns the job kinds): the daily sweep, which indexes the last days (late syncs), starts or continues
the backfill chain from the newest missing day (catch-up: newest first, one day at a time, stopping at the account
start after 14 answered empty days), fetches again a few older days that are still not final, queues the weeks
that became complete, and on Mondays re-checks the empty days of the last 30 once; plus a rebuild when the rules, text or numbers version changed. The trigger is "a day is
missing", never the hour, so a DST change or a missed cron run loses nothing.

Enqueueing is idempotent: a key that already has a queued or running job is skipped, so running this twice, or
while the worker is busy, changes nothing. All inserts go in one transaction.

The app calls catch_up() at startup too (from a background thread, so a slow database never delays the server):
the same jobs, so a first deploy starts indexing without waiting for the cron.

Each job stores the W3C traceparent of the "history.enqueue" span (telemetry.inject()), so the worker's span joins
the scheduler's trace.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import logging
import os
import sys
from typing import Callable, Optional

import jobqueue

log = logging.getLogger("garmin_ext.enqueue")


def default_plan(store, today: dt.date) -> list:
    """history_jobs.scheduled_jobs: [(kind, key, payload)]."""
    import history_jobs

    return list(history_jobs.scheduled_jobs(store, today))


def _inject(telemetry) -> Optional[str]:
    fn = getattr(telemetry, "inject", None) if telemetry is not None else None
    if not callable(fn):
        return None
    try:
        return jobqueue.clean_traceparent(fn())
    except Exception:  # noqa: BLE001 - tracing never stops the scheduler
        return None


@contextlib.contextmanager
def _span(telemetry, name):
    fn = getattr(telemetry, "span", None) if telemetry is not None else None
    if not callable(fn):
        yield
        return
    try:
        cm = fn(name, {"coach.caller": "enqueue"})
        cm.__enter__()
    except Exception:  # noqa: BLE001
        yield
        return
    try:
        yield
    finally:
        with contextlib.suppress(Exception):
            cm.__exit__(None, None, None)


def run(queue, today: dt.date, *, store=None, telemetry=None,
        plan: Callable[[object, dt.date], list] = default_plan) -> dict:
    """Enqueue plan(store, today) in one transaction. Returns {"planned": n, "added": n}."""
    with _span(telemetry, "history.enqueue"):
        traceparent = _inject(telemetry)
        jobs = plan(store, today)
        added = 0
        with queue.transaction() as conn:
            for kind, key, payload in jobs:
                if queue.enqueue(kind, key, payload, traceparent=traceparent, conn=conn) is not None:
                    added += 1
    return {"planned": len(jobs), "added": added}


def catch_up(queue, today: dt.date, *, store=None, telemetry=None,
             plan: Callable[[object, dt.date], list] = default_plan) -> dict:
    """The app's startup call. Never raises: a fixed log line names the error class."""
    try:
        out = run(queue, today, store=store, telemetry=telemetry, plan=plan)
    except Exception as exc:  # noqa: BLE001
        log.warning("history: startup enqueue failed (%s); the scheduler will enqueue later", type(exc).__name__)
        return {"planned": 0, "added": 0}
    log.info("history: startup enqueue: %d job(s) planned, %d new", out["planned"], out["added"])
    return out


def _setup_telemetry(cfg):
    """The telemetry module when TRACING is on and its setup worked, else None."""
    if not getattr(cfg, "tracing", False):
        return None
    try:
        import telemetry as telemetry_mod
    except ModuleNotFoundError:
        return None
    try:
        telemetry_mod.setup(cfg)
    except Exception as exc:  # noqa: BLE001
        log.warning("enqueue: tracing setup failed (%s)", type(exc).__name__)
        return None
    return telemetry_mod


def main(argv=None, environ=None) -> int:
    environ = os.environ if environ is None else environ
    import feature_config
    import status_common
    import tool_errors

    problems = feature_config.problems(environ)
    for line in problems:
        print(f"enqueue: config error: {line}", file=sys.stderr)
    if problems:
        return 1
    cfg = feature_config.load(environ)
    if cfg.role != "enqueue" or not cfg.history:
        print("enqueue: config error: runs only with ROLE=enqueue and HISTORY_SEARCH=on", file=sys.stderr)
        return 1
    if cfg.history_forget:
        log.warning("enqueue: HISTORY_FORGET=yes: nothing enqueued until the flag is removed")
        return 0
    try:
        today = dt.datetime.now(status_common.zone()).date()
    except ValueError as exc:
        print(f"enqueue: config error: {exc}", file=sys.stderr)
        return 1
    dsn = cfg.database_url or environ.get("DATABASE_URL", "")
    # As launch._setup_features: every secret form registered for redaction, tracing set up (it reads the
    # OTLP headers), then the secret variables taken out of the environment.
    for value in feature_config.secret_values(cfg):
        tool_errors.register_secret(value)
    tool_errors.register_secret(dsn)
    import history_store

    telemetry = _setup_telemetry(cfg)
    feature_config.take_secrets(environ)
    queue = jobqueue.PgJobQueue(dsn, application_name="coach-mcp-enqueue")
    try:
        store = history_store.PgHistoryStore(dsn)
        store.migrate()
        queue.migrate()
        out = run(queue, today, store=store, telemetry=telemetry)
    except Exception as exc:  # noqa: BLE001 - a fixed line; cron shows the exit status
        log.error("enqueue: failed (%s)", type(exc).__name__)
        return 1
    finally:
        if telemetry is not None:
            with contextlib.suppress(Exception):
                telemetry.shutdown()
    log.info("enqueue: %d job(s) planned, %d new", out["planned"], out["added"])
    return 0


def configure_logging() -> None:
    """As worker.configure_logging: INFO lines, the Garmin libraries' records hidden, secrets redacted."""
    import log_privacy

    logging.basicConfig(level=logging.INFO, handlers=[
        log_privacy.LevelStreamHandler("%(asctime)s %(levelname)s %(name)s: %(message)s")])
    log_privacy.install()


if __name__ == "__main__":
    configure_logging()
    # Run the importable module, not this __main__ copy (one module object, as in worker.py).
    import enqueue as _e
    sys.exit(_e.main())
