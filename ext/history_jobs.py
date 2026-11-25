"""History search: the job handlers the worker runs (index_day, build_week, sweep, rebuild).

worker.load_handlers() imports this module and calls register_jobs(register). A handler is handler(job, ctx)
(worker.Context: store, queue, guard, garmin, telemetry, conn, prepared). index_day and build_week also have a
prepare step (register(..., prepare=)) that the worker runs before it opens the job's transaction: the Garmin
calls happen there, so no database transaction stays open while a call waits for the guard. The handler then
runs in the job's transaction (ctx.conn) with the answers in ctx.prepared; every write and every follow-up job
(the outbox) uses ctx.conn, so a crash leaves all or nothing. Garmin calls go through ctx.garmin (the worker's
GuardedGarmin: call guard, token bucket, quiet window).

- index_day {"date", "backfill"?, "recheck"?, "empty_run"?, "probe"?}: 4 Garmin calls (stats, sleep, HRV, the
  day's activities) -> the day's numbers (history_extract.day_numbers), upsert, then embed_doc for the day,
  build_week when the week's 7 days are final, and (backfill) the next older missing day. A day already final
  is not fetched again unless "recheck". The backfill stops after EMPTY_STOP_DAYS answered empty days in a row
  (every source answered, none had data: the account start or a long break) and stores that day as the
  backfill floor. A day with a failed source never counts, and the stop is skipped while an older stored day
  has data. The floor is provisional: each sweep probes a few days before it (PROBE_DAYS, each one once), and a
  probe that finds data removes the floor and the backfill goes on from there. A probe stored open (a source
  failed) is probed again once, then only on the late sweep.
- build_week {"start" (a Monday), "cascade"?, "fetch_load"?}: the week's numbers from its stored days, plus one
  get_training_status in full mode (kept in the row, so rebuilds call nothing), then embed_doc for the week and
  build_week for the next week while cascade > 0 (its HRV and volume comparisons read this week).
- sweep {"late"?}: retention, the last 3 days (late syncs), the newest missing day (starts the backfill
  chain), up to REFETCH_PER_SWEEP older days that are still not final (a source failed on every attempt; a day
  already fetched again once only on the late sweep), the floor probes, weeks whose days are all final but
  whose row is missing or not final, and with "late" the empty or partial days of the last 30 days once more.
  Sets the lag gauge.
- rebuild {}: re-derives every week and re-embeds every day and week from stored numbers, no Garmin calls (after
  a rules, text or numbers version change; scheduled_jobs() decides).

A Garmin login or rate-limit failure is re-raised (the worker waits for the shared cooldown and uses no attempt);
Garmin unavailable (a 5xx answer, a network failure, a timeout) is re-raised for a retry with backoff, and from
the third attempt on the source is stored as missing and the day stays non-final, so a later sweep fetches it
again. An HTTP 403 is not retried: the source is stored as missing and the day can be final. Garmin's "no data"
is an answer (the source is not missing). Any other failure marks only that source missing for the day. A different Garmin account stops indexing
(worker.Permanent("account_mismatch")). Logs: counts, dates of jobs, fixed group names.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import re
from typing import Any, Callable, Dict, List, Optional

import history_docs
import history_extract as hx
import history_store as hs
import key_mode
import status_common as sc
import tool_errors

LOG = logging.getLogger("garmin_ext.history")

KINDS = ("index_day", "build_week", "sweep", "rebuild")
LATE_SWEEP_DAYS = 30
RECENT_DAYS = hx.FINAL_AFTER_DAYS
CASCADE_WEEKS = 3                 # the HRV 28-day average reads 3 weeks back
NOT_FINAL_WAIT_S = 6 * 3600       # build_week whose days are not final yet
DEGRADE_FROM_ATTEMPT = 2          # 0-based attempts: from the 3rd try Garmin unavailable is stored as missing
STOP_GROUPS = (tool_errors.LOGIN, tool_errors.RATE_LIMITED)
REFETCH_PER_SWEEP = 5             # older non-final days fetched again per sweep (4 Garmin calls each)
PROBE_DAYS = (7, 21, 49, 98)      # days before the backfill floor each sweep checks once for older data
PREPARE_LOST_WAIT_S = 60          # the day's row changed between the prepare step and the transaction
# tool_errors puts an HTTP 403 in the unavailable group with the 5xx answers. A 403 does not go away on a retry:
# it is a failed source, never retried. These are tool_errors' own 403 / 5xx spellings.
_HTTP_403 = re.compile(r"api error 403(?!\d)|\(403\)", re.IGNORECASE)
_HTTP_5XX = re.compile(r"api error 5\d\d(?!\d)|\(5\d\d\)", re.IGNORECASE)


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


def _forbidden(chain_text: str) -> bool:
    """True when an unavailable failure is Garmin's HTTP 403 (and no 5xx): retrying it will not help."""
    return bool(_HTTP_403.search(chain_text)) and not _HTTP_5XX.search(chain_text)


class _Calls:
    """Garmin calls of one job: a failed source -> None, an empty answer -> {} / [], a stop group -> raised.

    Garmin's "no data" (a 404 and the like) is an answer: the source gives {} / [], so it is not stored as
    missing. Garmin unavailable (5xx, network, timeout) is raised on the first attempts so the worker retries
    the job with backoff; from DEGRADE_FROM_ATTEMPT on the source is missing and `degraded` keeps the day
    non-final. An HTTP 403 is not retried and does not keep the day open: that source is missing, and the day
    never counts as answered empty. `groups` lists the group of every source that failed or had no data."""

    def __init__(self, ctx: Any, job: Any):
        self.ctx, self.job = ctx, job
        self.degraded = False
        self.groups: List[str] = []

    def call(self, fn: Callable[..., Any]) -> Any:
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - grouped below; the text is never logged
            if _passthrough(exc):
                raise
            group, _detail, _root, chain_text = tool_errors.classify_exception(exc)
            if group in STOP_GROUPS:
                raise
            if group == tool_errors.UNAVAILABLE and not _forbidden(chain_text):
                if getattr(self.job, "attempts", 0) < DEGRADE_FROM_ATTEMPT:
                    raise
                self.degraded = True
            self.groups.append(group)
            if group == tool_errors.NO_DATA:
                return None              # answered with nothing: method() / activities() give {} / []
            return _FAILED

    @property
    def answered(self) -> bool:
        """True when every source answered; Garmin's "no data" for a source counts as an answer."""
        return all(g == tool_errors.NO_DATA for g in self.groups)

    def method(self, name: str, *args: Any) -> Any:
        client = self.ctx.garmin
        data = self.call(lambda: getattr(client, name)(*args))
        return None if data is _FAILED else ({} if data is None else data)

    def activities(self, day: str) -> Any:
        client = self.ctx.garmin
        params = {"startDate": day, "endDate": day, "start": "0", "limit": str(hx.ACTIVITY_LIMIT)}
        data = self.call(lambda: sc.activities_call(client)(params=params))
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
    """(payloads by source, degraded, answered) for one day: 4 Garmin calls. answered: every source answered
    (Garmin's "no data" counts as an answer); degraded: Garmin was unavailable for a source on every attempt."""
    calls = _Calls(ctx, job)
    iso = day.isoformat()
    payloads: Dict[str, Any] = {}
    for source, name in (("stats", "get_stats"), ("sleep", "get_sleep_data"), ("hrv", "get_hrv_data")):
        payloads[source] = calls.method(name, iso)
    payloads["activities"] = calls.activities(iso)
    return payloads, calls.degraded, calls.answered


def _index_target(job: Any, ctx: Any) -> tuple:
    """(day, today, account fingerprint, stored row or None, fetch needed) for an index_day job."""
    p = job.payload or {}
    day = _date(p.get("date"))
    today = _now().date()
    if day >= today:
        raise _permanent(tool_errors.BAD_INPUT)
    fp = _account(ctx)
    rows = ctx.store.days(fp, day, day, conn=ctx.conn) if fp else []
    row = rows[0] if rows else None
    return day, today, fp, row, not (row is not None and row["final"] and not p.get("recheck"))


def prepare_index_day(job: Any, ctx: Any) -> Optional[Dict[str, Any]]:
    """Run by the worker before the job's transaction: the day's Garmin calls (none for a day already final)."""
    day, _today, _fp, _row, fetch = _index_target(job, ctx)
    return {"day": day.isoformat(), "fetched": fetch_day(ctx, job, day) if fetch else None}


def _prepared_fetch(ctx: Any, key: str, value: str) -> Optional[tuple]:
    prepared = getattr(ctx, "prepared", None)
    if isinstance(prepared, dict) and prepared.get(key) == value:
        return prepared.get("fetched")
    return None


def index_day(job: Any, ctx: Any) -> None:
    p = job.payload or {}
    store, conn = ctx.store, ctx.conn
    day, today, fp, row, fetch = _index_target(job, ctx)
    if not fetch:
        empty_day = hx.is_answered_empty(row["numbers"])
    else:
        fetched = _prepared_fetch(ctx, "day", day.isoformat())
        if fetched is None:
            if getattr(ctx, "prepared", None) is not None:
                # The prepare step found the day final and fetched nothing, and the row has changed since (it was
                # removed or reopened). Garmin is never called inside the transaction: run the job again shortly.
                raise _defer(PREPARE_LOST_WAIT_S, "prepare_stale")
            fetched = fetch_day(ctx, job, day)   # called directly (no prepare step, no worker transaction)
        payloads, degraded, answered = fetched
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
            if p.get("probe") and not empty_day:
                _probe_found_data(ctx, fp, today)
        else:
            # No account yet and Garmin named none (an empty day): nothing to key the row by. The day stays
            # missing; the sweep comes back to it once a day with data has set the fingerprint.
            empty_day = hx.is_empty_day(hx.day_numbers(day, payloads, sc.zone(), _mode()))
        # Only a day whose every source answered counts toward the backfill stop.
        empty_day = empty_day and answered
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


def _older_data(store: Any, fp: Optional[str], day: dt.date, today: dt.date, conn: Any) -> bool:
    """True when a stored day before `day` (inside the backfill window) has data: a break, not the start."""
    if not fp:
        return False
    first = today - dt.timedelta(days=hx.BACKFILL_DAYS)
    return any(not r["empty"] for r in store.days(fp, first, day - dt.timedelta(days=1), conn=conn))


def _continue_backfill(ctx: Any, fp: Optional[str], day: dt.date, today: dt.date, empty_run: int) -> None:
    store, conn = ctx.store, ctx.conn
    if empty_run >= hx.EMPTY_STOP_DAYS and not _older_data(store, fp, day, today, conn):
        store.meta_set(hs.META_BACKFILL_FLOOR, day.isoformat(), conn=conn)
        LOG.info("history: backfill stopped after %d answered empty days in a row", empty_run)
        return
    floor = _floor(store, today, conn)
    missing = store.missing_days(fp or "", floor, day - dt.timedelta(days=1), conn=conn)
    if missing:
        nxt = missing[-1].isoformat()
        _enqueue(ctx, "index_day", f"index_day:{nxt}", {"date": nxt, "backfill": True, "empty_run": empty_run})


def _probe_found_data(ctx: Any, fp: str, today: dt.date) -> None:
    """A probe before the backfill floor found data: the floor was a long break, not the account start. The
    floor is removed and the backfill goes on from it."""
    store, conn = ctx.store, ctx.conn
    stop = store.meta_get(hs.META_BACKFILL_FLOOR, conn=conn)
    if not stop:
        return
    store.meta_set(hs.META_BACKFILL_FLOOR, "", conn=conn)
    LOG.info("history: data found before the backfill floor; the backfill goes on")
    try:
        old = dt.date.fromisoformat(stop)
    except ValueError:
        return
    _continue_backfill(ctx, fp, old, today, 0)


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


def _week_target(job: Any, ctx: Any) -> tuple:
    """(start, today, account fingerprint or None, stored load or None, training status needed) for a build_week
    job; jobqueue.Defer while its days are not final yet."""
    p = job.payload or {}
    start = _date(p.get("start"))
    if start.weekday() != 0:
        raise _permanent(tool_errors.BAD_INPUT)
    store, conn = ctx.store, ctx.conn
    fp = _account(ctx)
    if not fp:
        return start, None, None, None, False
    today = _now().date()
    end = start + dt.timedelta(days=6)
    week_rows = store.days(fp, start, end, conn=conn)
    complete = len(week_rows) == 7 and all(r["final"] for r in week_rows)
    if not complete and not (hx.is_final_day(end, today) and all(r["final"] for r in week_rows)):
        raise _defer(NOT_FINAL_WAIT_S, "not_final")
    existing = store.weeks(fp, start, start, conn=conn)
    load = _stored_load(existing[0]["numbers"]) if existing else None
    need = _mode() == "full" and load is None and bool(p.get("fetch_load", True))
    return start, today, fp, load, need


def prepare_build_week(job: Any, ctx: Any) -> Optional[Dict[str, Any]]:
    """Run by the worker before the job's transaction: the week's training status call, when it is needed."""
    start, _today, _fp, _load, need = _week_target(job, ctx)
    if not need:
        return None
    end = start + dt.timedelta(days=6)
    return {"start": start.isoformat(), "fetched": (_Calls(ctx, job).method("get_training_status", end.isoformat()),)}


def build_week(job: Any, ctx: Any) -> None:
    p = job.payload or {}
    store, conn = ctx.store, ctx.conn
    start, today, fp, load, need = _week_target(job, ctx)
    if not fp:
        return
    end = start + dt.timedelta(days=6)
    mode = _mode()
    if need:
        got = _prepared_fetch(ctx, "start", start.isoformat())
        raw = got[0] if got is not None else _Calls(ctx, job).method("get_training_status", end.isoformat())
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
    # Older days still not final (Garmin was unavailable for a source on every attempt): a few each day, the
    # ones never fetched again first, then the newest; a day already fetched again only on the late sweep, so a
    # day Garmin keeps failing costs a few calls a week, not a day.
    still_open = sorted((d for d, r in rows.items() if d < recent_from and not r["final"]
                         and (p.get("late") or not r["rechecked"])),
                        key=lambda d: (rows[d]["rechecked"], -d.toordinal()))
    for d in still_open[:REFETCH_PER_SWEEP]:
        _enqueue(ctx, "index_day", f"index_day:{d.isoformat()}", {"date": d.isoformat(), "recheck": True})
        queued += 1
    queued += _probe_floor(ctx, fp, today, bool(p.get("late")))
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


def _probe_floor(ctx: Any, fp: str, today: dt.date, late: bool = False) -> int:
    """index_day probes for the PROBE_DAYS before the backfill floor that have no row yet (each is fetched
    once, then stored); a probe with data removes the floor (_probe_found_data). A probe stored open (a Garmin
    source failed) is probed again, as a recheck, once; after that only on the late sweep, like the open days
    after the floor. The sweep's own recheck never reaches these days: they lie before the floor. Returns how
    many it queued."""
    store, conn = ctx.store, ctx.conn
    stop = store.meta_get(hs.META_BACKFILL_FLOOR, conn=conn)
    if not stop:
        return 0
    try:
        floor = dt.date.fromisoformat(stop)
    except ValueError:
        return 0
    first = today - dt.timedelta(days=hx.BACKFILL_DAYS)
    days = [floor - dt.timedelta(days=n) for n in PROBE_DAYS if floor - dt.timedelta(days=n) >= first]
    queued = 0
    for d in days:
        rows = store.days(fp, d, d, conn=conn)
        if not rows:
            _enqueue(ctx, "index_day", f"index_day:{d.isoformat()}", {"date": d.isoformat(), "probe": True})
            queued += 1
        elif not rows[0]["final"] and (late or not rows[0]["rechecked"]):
            _enqueue(ctx, "index_day", f"index_day:{d.isoformat()}",
                     {"date": d.isoformat(), "probe": True, "recheck": True})
            queued += 1
    return queued


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
    register("index_day", index_day, garmin=True, prepare=prepare_index_day)
    register("build_week", build_week, garmin=True, prepare=prepare_build_week)
    register("sweep", sweep, garmin=False)
    register("rebuild", rebuild, garmin=False)
