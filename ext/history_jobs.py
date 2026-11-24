"""History search: the job handlers the worker runs (index_day, build_week, sweep, rebuild).

worker.load_handlers() imports this module and calls register_jobs(register). A handler is handler(job, ctx)
(worker.Context: store, queue, guard, garmin, telemetry, conn). Every write uses ctx.conn, the job's own
transaction, and follow-up jobs are enqueued in it too (the outbox), so a crash leaves all or nothing. Garmin
calls go through ctx.garmin (the worker's GuardedGarmin: call guard, token bucket, quiet window).

- index_day {"date", "backfill"?, "recheck"?, "empty_run"?}: 4 Garmin calls (stats, sleep, HRV, the day's
  activities) -> the day's numbers (history_extract.day_numbers), upsert, then embed_doc for the day, build_week
  when the week's 7 days are final, and (backfill) the next older missing day. A day already final is not
  fetched again unless "recheck". The backfill stops after 14 empty days in a row (account start).
- build_week {"start" (a Monday), "cascade"?, "fetch_load"?}: the week's numbers from its stored days, plus one
  get_training_status in full mode (kept in the row, so rebuilds call nothing), then embed_doc for the week and
  build_week for the next week while cascade > 0 (its HRV and volume comparisons read this week).
- sweep {"late"?}: retention, the last 3 days (late syncs), the newest missing day (starts the backfill
  chain), weeks whose days are all final but whose row is missing or not final, and with "late" the empty or
  partial days of the last 30 days once more. Sets the lag gauge.
- rebuild {}: re-derives every week and re-embeds every day and week from stored numbers, no Garmin calls (after
  a rules, text or numbers version change; scheduled_jobs() decides).

A Garmin login or rate-limit failure is re-raised (the worker waits for the shared cooldown and uses no attempt);
a network failure is re-raised for a retry, and from the third attempt on the source is stored as missing and
the day stays non-final. Any other failure marks only that source missing for the day. A different Garmin
account stops indexing (worker.Permanent("account_mismatch")). Logs: counts, dates of jobs, fixed group names.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
from typing import Any, Callable, Dict, List, Optional

import history_docs
import history_extract as hx
import history_store as hs
import key_mode
import status_common as sc
import tool_errors
import weekly_status as ws

LOG = logging.getLogger("garmin_ext.history")

KINDS = ("index_day", "build_week", "sweep", "rebuild")
LATE_SWEEP_DAYS = 30
RECENT_DAYS = hx.FINAL_AFTER_DAYS
CASCADE_WEEKS = 3                 # the HRV 28-day average reads 3 weeks back
NOT_FINAL_WAIT_S = 6 * 3600       # build_week whose days are not final yet
DEGRADE_FROM_ATTEMPT = 2          # 0-based attempts: from the 3rd try a network failure is stored as missing
STOP_GROUPS = (tool_errors.LOGIN, tool_errors.RATE_LIMITED)


def _now() -> dt.datetime:
    """Local now in REPORT_TZ (tests replace this)."""
    return sc.now_local(sc.zone())


def _mode() -> str:
    return key_mode.auth_mode(os.environ) or "full"


# --- worker glue ------------------------------------------------------------------------------------------------

def _permanent(group: str) -> Exception:
    import worker

    return worker.Permanent(group)


def _defer(seconds: float, group: str) -> Exception:
    import jobqueue

    return jobqueue.Defer(seconds, group)


def _passthrough(exc: BaseException) -> bool:
    """GuardBlocked and jobqueue.Defer (TokenStale) belong to the worker loop."""
    if type(exc).__name__ == "GuardBlocked" and hasattr(exc, "group"):
        return True
    try:
        import jobqueue
    except ImportError:  # pragma: no cover
        return False
    return isinstance(exc, jobqueue.Defer)


def _traceparent(ctx: Any) -> Optional[str]:
    inject = getattr(getattr(ctx, "telemetry", None), "inject", None)
    if not callable(inject):
        return None
    try:
        return inject()
    except Exception:  # noqa: BLE001 - tracing never stops a job
        return None


def _enqueue(ctx: Any, kind: str, key: str, payload: Dict[str, Any]) -> None:
    ctx.queue.enqueue(kind, key, payload, traceparent=_traceparent(ctx), conn=ctx.conn)


def _gauge(ctx: Any, name: str, value: Any) -> None:
    fn = getattr(getattr(ctx, "telemetry", None), "metric_gauge", None)
    if callable(fn):
        try:
            fn(name, value)
        except Exception:  # noqa: BLE001
            pass


def _date(value: Any) -> dt.date:
    try:
        return hs._date(value)
    except (TypeError, ValueError):
        raise _permanent(tool_errors.BAD_INPUT) from None


def _account(ctx: Any) -> Optional[str]:
    """The stored account fingerprint; worker.Permanent when the account changed."""
    if ctx.store.meta_get(hs.META_MISMATCH, conn=ctx.conn) == "yes":
        raise _permanent("account_mismatch")
    return ctx.store.meta_get(hs.META_ACCOUNT, conn=ctx.conn)


_FAILED = object()


class _Calls:
    """Garmin calls of one job: a failed source -> None, an empty answer -> {} / [], a stop group -> raised."""

    def __init__(self, ctx: Any, job: Any):
        self.ctx, self.job = ctx, job
        self.degraded = False

    def call(self, fn: Callable[..., Any]) -> Any:
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - grouped below; the text is never logged
            if _passthrough(exc):
                raise
            group, _detail, _root, full = tool_errors.classify_exception(exc)
            if group in STOP_GROUPS:
                raise
            if group == tool_errors.UNAVAILABLE and sc.is_systemic_error(full):
                if getattr(self.job, "attempts", 0) < DEGRADE_FROM_ATTEMPT:
                    raise
                self.degraded = True
            return _FAILED

    def method(self, name: str, *args: Any) -> Any:
        client = self.ctx.garmin
        data = self.call(lambda: getattr(client, name)(*args))
        return None if data is _FAILED else ({} if data is None else data)

    def activities(self, day: str) -> Any:
        client = self.ctx.garmin
        params = {"startDate": day, "endDate": day, "start": "0", "limit": str(hx.ACTIVITY_LIMIT)}
        data = self.call(lambda: ws._activities_call(client)(params=params))
        return None if data is _FAILED else ([] if data is None else data)


# --- index_day --------------------------------------------------------------------------------------------------

def _floor(store: Any, today: dt.date, conn: Any) -> dt.date:
    floor = today - dt.timedelta(days=hx.BACKFILL_DAYS)
    stop = store.meta_get(hs.META_BACKFILL_FLOOR, conn=conn)
    if stop:
        try:
            floor = max(floor, dt.date.fromisoformat(stop))
        except ValueError:
            pass
    return floor


def fetch_day(ctx: Any, job: Any, day: dt.date) -> tuple:
    """(payloads by source, degraded) for one day: 4 Garmin calls."""
    calls = _Calls(ctx, job)
    iso = day.isoformat()
    payloads: Dict[str, Any] = {}
    for source, name in (("stats", "get_stats"), ("sleep", "get_sleep_data"), ("hrv", "get_hrv_data")):
        payloads[source] = calls.method(name, iso)
    payloads["activities"] = calls.activities(iso)
    return payloads, calls.degraded


def index_day(job: Any, ctx: Any) -> None:
    p = job.payload or {}
    day = _date(p.get("date"))
    today = _now().date()
    if day >= today:
        raise _permanent(tool_errors.BAD_INPUT)
    store, conn = ctx.store, ctx.conn
    fp = _account(ctx)
    rows = store.days(fp, day, day, conn=conn) if fp else []
    row = rows[0] if rows else None
    empty_day = True
    if row is not None and row["final"] and not p.get("recheck"):
        empty_day = row["empty"]
    else:
        payloads, degraded = fetch_day(ctx, job, day)
        pid = hx.profile_id(payloads)
        if pid is not None:
            fp, status = hs.account_check(store, pid)      # its own transaction: a mismatch flag must stay
            if status == hs.ACCOUNT_MISMATCH:
                raise _permanent("account_mismatch")
        if fp:
            mode = _mode()
            numbers = hx.day_numbers(day, payloads, sc.zone(), mode)
            final = hx.is_final_day(day, today) and not degraded
            store.upsert_day(fp, day, numbers, final, rechecked=bool(p.get("recheck")), conn=conn)
            _note_mode(store, mode, conn)
            empty_day = hx.is_empty_day(numbers)
            _after_day(ctx, fp, day, empty_day)
        else:
            # No account yet and Garmin named none (an empty day): nothing to key the row by. The day stays
            # missing; the sweep comes back to it once a day with data has set the fingerprint.
            empty_day = hx.is_empty_day(hx.day_numbers(day, payloads, sc.zone(), _mode()))
    if p.get("backfill"):
        _continue_backfill(ctx, fp, day, today, int(p.get("empty_run") or 0) + 1 if empty_day else 0)


def index_mode(stored: Optional[str], mode: str) -> str:
    """meta index_mode after a day fetched in `mode`: that mode, or "mixed" once both modes were indexed."""
    if not stored or stored == mode:
        return mode
    return "mixed"


def _note_mode(store: Any, mode: str, conn: Any) -> None:
    stored = store.meta_get(hs.META_INDEX_MODE, conn=conn)
    new = index_mode(stored, mode)
    if new != stored:
        store.meta_set(hs.META_INDEX_MODE, new, conn=conn)


def _after_day(ctx: Any, fp: str, day: dt.date, empty_day: bool) -> None:
    store, conn = ctx.store, ctx.conn
    if not empty_day:
        _enqueue(ctx, "embed_doc", f"embed_doc:day:{day.isoformat()}", {"kind": "day", "start": day.isoformat()})
    # The next two days' documents read this day (sleep debt, the 3-day HRV trend).
    for later in store.days(fp, day + dt.timedelta(days=1), day + dt.timedelta(days=2), conn=conn):
        if not later["empty"]:
            iso = later["date"].isoformat()
            _enqueue(ctx, "embed_doc", f"embed_doc:day:{iso}", {"kind": "day", "start": iso})
    monday = hx.week_start(day)
    rows = store.days(fp, monday, monday + dt.timedelta(days=6), conn=conn)
    if len(rows) == 7 and all(r["final"] for r in rows):
        _enqueue(ctx, "build_week", f"build_week:{monday.isoformat()}",
                 {"start": monday.isoformat(), "cascade": CASCADE_WEEKS, "fetch_load": True})


def _continue_backfill(ctx: Any, fp: Optional[str], day: dt.date, today: dt.date, empty_run: int) -> None:
    store, conn = ctx.store, ctx.conn
    if empty_run >= hx.EMPTY_STOP_DAYS:
        store.meta_set(hs.META_BACKFILL_FLOOR, day.isoformat(), conn=conn)
        LOG.info("history: backfill stopped after %d empty days in a row", empty_run)
        return
    floor = _floor(store, today, conn)
    missing = store.missing_days(fp or "", floor, day - dt.timedelta(days=1), conn=conn)
    if missing:
        nxt = missing[-1].isoformat()
        _enqueue(ctx, "index_day", f"index_day:{nxt}", {"date": nxt, "backfill": True, "empty_run": empty_run})


# --- build_week -------------------------------------------------------------------------------------------------

def _stored_load(numbers: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not numbers or numbers.get("mode") != "full":
        return None
    load = {k: numbers.get(k) for k in hx.STORED_LOAD_FIELDS}
    return load if any(v is not None for v in load.values()) else None


def compute_week(store: Any, fp: str, start: dt.date, mode: str, load: Optional[Dict[str, Any]],
                 today: dt.date, conn: Any = None) -> tuple:
    """(numbers, final) of the week starting `start` from its stored days."""
    end = start + dt.timedelta(days=6)
    rows = store.days(fp, start - dt.timedelta(days=28), end, conn=conn)
    days = {r["date"]: r["numbers"] for r in rows}
    prev = store.weeks(fp, start - dt.timedelta(days=7), start - dt.timedelta(days=7), conn=conn)
    chronic = prev[0]["numbers"].get("load_chronic") if prev else None
    numbers = hx.week_numbers(start, days, mode=mode, load=load, chronic_week_ago=chronic)
    week_rows = [r for r in rows if start <= r["date"] <= end]
    final = len(week_rows) == 7 and all(r["final"] for r in week_rows) and hx.is_final_day(end, today)
    return numbers, final


def build_week(job: Any, ctx: Any) -> None:
    p = job.payload or {}
    start = _date(p.get("start"))
    if start.weekday() != 0:
        raise _permanent(tool_errors.BAD_INPUT)
    store, conn = ctx.store, ctx.conn
    fp = _account(ctx)
    if not fp:
        return
    today = _now().date()
    end = start + dt.timedelta(days=6)
    week_rows = store.days(fp, start, end, conn=conn)
    complete = len(week_rows) == 7 and all(r["final"] for r in week_rows)
    if not complete and not (hx.is_final_day(end, today) and all(r["final"] for r in week_rows)):
        raise _defer(NOT_FINAL_WAIT_S, "not_final")
    mode = _mode()
    existing = store.weeks(fp, start, start, conn=conn)
    load = _stored_load(existing[0]["numbers"]) if existing else None
    if mode == "full" and load is None and p.get("fetch_load", True):
        raw = _Calls(ctx, job).method("get_training_status", end.isoformat())
        load = hx.load_numbers(raw, mode) if raw is not None else None
    numbers, final = compute_week(store, fp, start, mode, load, today, conn)
    store.upsert_week(fp, start, numbers, final, conn=conn)
    _enqueue(ctx, "embed_doc", f"embed_doc:week:{start.isoformat()}", {"kind": "week", "start": start.isoformat()})
    cascade = int(p.get("cascade") or 0)
    nxt = start + dt.timedelta(days=7)
    if cascade > 0 and store.weeks(fp, nxt, nxt, conn=conn):
        _enqueue(ctx, "build_week", f"build_week:{nxt.isoformat()}",
                 {"start": nxt.isoformat(), "cascade": cascade - 1, "fetch_load": False})


# --- sweep ------------------------------------------------------------------------------------------------------

def _vectors_delete(ctx: Any, ids: List[str]) -> None:
    store = getattr(ctx, "vectors", None)
    delete = getattr(store, "delete", None)
    if ids and callable(delete):
        try:
            delete(ids=ids)
        except Exception as exc:  # noqa: BLE001 - the Postgres store deleted its own copy already
            LOG.warning("history: vector delete failed (%s)", type(exc).__name__)


def sweep(job: Any, ctx: Any) -> None:
    p = job.payload or {}
    store, conn = ctx.store, ctx.conn
    if store.meta_get(hs.META_MISMATCH, conn=conn) == "yes":
        LOG.info("history: sweep skipped: the Garmin account changed")
        return
    today = _now().date()
    yesterday = today - dt.timedelta(days=1)
    fp = store.meta_get(hs.META_ACCOUNT, conn=conn)
    removed = store.retention(fp or "", today - dt.timedelta(days=hx.RETENTION_DAYS))
    _vectors_delete(ctx, removed.get("doc_ids") or [])
    if not fp:
        _enqueue(ctx, "index_day", f"index_day:{yesterday.isoformat()}",
                 {"date": yesterday.isoformat(), "backfill": True})
        return
    floor = _floor(store, today, conn)
    recent_from = today - dt.timedelta(days=RECENT_DAYS)
    rows = {r["date"]: r for r in store.days(fp, min(floor, recent_from), yesterday, conn=conn)}
    queued = 0
    day = recent_from
    while day <= yesterday:
        r = rows.get(day)
        if r is None or not r["final"]:
            _enqueue(ctx, "index_day", f"index_day:{day.isoformat()}", {"date": day.isoformat()})
            queued += 1
        day += dt.timedelta(days=1)
    missing = store.missing_days(fp, floor, recent_from - dt.timedelta(days=1), conn=conn)
    if missing:
        iso = missing[-1].isoformat()
        _enqueue(ctx, "index_day", f"index_day:{iso}", {"date": iso, "backfill": True})
        queued += 1
    if p.get("late"):
        late_from = today - dt.timedelta(days=LATE_SWEEP_DAYS)
        for d, r in sorted(rows.items()):
            if late_from <= d < recent_from and not r["rechecked"] and (
                    r["empty"] or not r["final"] or r["numbers"].get("missing")):
                _enqueue(ctx, "index_day", f"index_day:{d.isoformat()}", {"date": d.isoformat(), "recheck": True})
                queued += 1
    weeks = {w["start"]: w for w in store.weeks(fp, hx.week_start(floor), today, conn=conn)}
    monday = hx.week_start(floor)
    while monday + dt.timedelta(days=6) < today:
        week_days = [rows.get(monday + dt.timedelta(days=i)) for i in range(7)]
        if all(r is not None and r["final"] for r in week_days):
            w = weeks.get(monday)
            if w is None or not w["final"]:
                _enqueue(ctx, "build_week", f"build_week:{monday.isoformat()}",
                         {"start": monday.isoformat(), "cascade": CASCADE_WEEKS, "fetch_load": True})
                queued += 1
        monday += dt.timedelta(days=7)
    finals = [d for d, r in rows.items() if r["final"]]
    if finals:
        _gauge(ctx, "coach.history.lag_days", max(0, (yesterday - max(finals)).days))
    LOG.info("history: sweep queued %d job(s), retention removed %d day(s) and %d week(s)", queued,
             removed.get("days", 0), removed.get("weeks", 0))


# --- rebuild ----------------------------------------------------------------------------------------------------

def versions() -> Dict[str, str]:
    return {hs.META_RULES: f"{hx.RULES_VERSION[0]}.{hx.RULES_VERSION[1]}",
            hs.META_TEXT: str(history_docs.TEXT_VERSION), hs.META_NUMBERS: str(hx.NUMBERS_VERSION)}


def needs_rebuild(store: Any) -> bool:
    """True when the stored rules, text or numbers versions differ from the code (and there is history)."""
    if not store.meta_get(hs.META_ACCOUNT):
        return False
    return any(store.meta_get(k) != v for k, v in versions().items())


def rebuild(job: Any, ctx: Any) -> None:
    store, conn = ctx.store, ctx.conn
    fp = _account(ctx)
    today = _now().date()
    if fp:
        first = today - dt.timedelta(days=hx.RETENTION_DAYS + 7)
        mode = _mode()
        for w in store.weeks(fp, first, today, conn=conn):
            load = _stored_load(w["numbers"])
            numbers, final = compute_week(store, fp, w["start"], mode, load, today, conn)
            store.upsert_week(fp, w["start"], numbers, final, conn=conn)
            iso = w["start"].isoformat()
            _enqueue(ctx, "embed_doc", f"embed_doc:week:{iso}", {"kind": "week", "start": iso})
        for d in store.days(fp, first, today, conn=conn):
            if not d["empty"]:
                iso = d["date"].isoformat()
                _enqueue(ctx, "embed_doc", f"embed_doc:day:{iso}", {"kind": "day", "start": iso})
    for key, value in versions().items():
        store.meta_set(key, value, conn=conn)


# --- scheduling -------------------------------------------------------------------------------------------------

def scheduled_jobs(store: Any, today: Optional[dt.date] = None) -> List[tuple]:
    """[(kind, key, payload)] the scheduler (ROLE=enqueue) and the app's startup enqueue: the daily sweep (a
    late sweep on Mondays) and a rebuild when a version changed."""
    today = today or _now().date()
    jobs = [("sweep", f"sweep:{today.isoformat()}", {"late": today.weekday() == 0})]
    if needs_rebuild(store):
        v = versions()
        key = f"rebuild:r{v[hs.META_RULES]}-t{v[hs.META_TEXT]}-n{v[hs.META_NUMBERS]}"
        jobs.append(("rebuild", key, {}))
    return jobs


def register_jobs(register: Callable[..., Any]) -> None:
    """Called by worker.load_handlers()."""
    register("index_day", index_day, garmin=True)
    register("build_week", build_week, garmin=True)
    register("sweep", sweep, garmin=False)
    register("rebuild", rebuild, garmin=False)
