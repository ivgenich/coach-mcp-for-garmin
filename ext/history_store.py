"""History search: the store of per-day and per-week numbers (schema "history" in Postgres).

HistoryStore is the interface; MemoryHistoryStore backs the unit tests and
PgHistoryStore(dsn) is the real one (psycopg 3, imported only when it is
created, so a server with history off never loads it). Every write is an
idempotent upsert keyed by (account fingerprint, date): running a job twice
writes the same rows. A day written again is merged (merge_day): the fields of
a source that answered take the new values (a null too), and the fields of a
source that failed this time keep their stored values, so a failed recheck
never wipes them. Every numbers dict goes through
history_extract.check_numbers (the field allowlist) before it is stored.

Migrations are plain versioned SQL files in history_sql/ (NNN_name.sql,
applied in order once, recorded in history.schema_migrations, under an
advisory lock so the app and a worker starting together do not race).

Account fingerprint: HMAC-SHA256 of the Garmin profile id with a random salt
kept in history.meta. Only the fingerprint is stored, never the id. On a
mismatch the indexer stops (history_jobs) and the tools say the history
belongs to a different account.

The vectors live in history.doc_vectors (history_retrieval's PGVectorStore
table, created by migration 002), so dropping the schema removes them too.

forget_all() drops the schema "history" (days, weeks, meta, the job tables,
the vector table and anything else in it), also clears the deprecated
langchain_pg_* collection "coach_history" if an older build left one, then
runs VACUUM. retention() deletes old days and weeks and their rows in
history.doc_vectors. Logs carry counts and fixed texts only, never values or the
database URL. psycopg errors are raised as StoreError with a fixed text.
Connections carry the shared statement, lock and idle-in-transaction timeouts
(jobqueue.pg_options).
"""

from __future__ import annotations

import contextlib
import copy
import datetime as dt
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
from typing import Any, Dict, Iterator, List, Mapping, Optional, Protocol

import history_extract as hx
import jobqueue

LOG = logging.getLogger("garmin_ext.history")

SCHEMA = "history"
SQL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "history_sql")
MIGRATION_RE = re.compile(r"^(\d{3})_[a-z0-9_]{1,60}\.sql$")
MIGRATE_LOCK_ID = 0x636F6163  # "coac": pg_advisory_xact_lock key for migrate()
META_KEY_RE = re.compile(r"^[a-z0-9_.:-]{1,64}$")
META_VALUE_MAX = 256
FP_RE = re.compile(r"^[0-9a-f]{64}$")

# The vector table (history_retrieval.DOC_TABLE, migration 002): retention deletes its old rows.
DOC_VECTORS = f"{SCHEMA}.doc_vectors"
# Deprecated: the langchain_pg_* collection layout of early builds; forget_all still clears it if present.
COLLECTION_NAME = "coach_history"
LANGCHAIN_TABLES = ("langchain_pg_embedding", "langchain_pg_collection")

# meta keys
META_SALT = "fp_salt"
META_ACCOUNT = "account_fp"
META_MISMATCH = "account_mismatch"
META_BACKFILL_FLOOR = "backfill_floor"
META_RULES = "rules_version"
META_TEXT = "text_version"
META_NUMBERS = "numbers_version"
META_INDEX_MODE = "index_mode"      # full, key or mixed (the modes the stored days were fetched in)

ACCOUNT_OK, ACCOUNT_NEW, ACCOUNT_MISMATCH = "ok", "new", "mismatch"


class StoreError(RuntimeError):
    """A database failure, with a fixed text (never the SQL, the values or the URL)."""


class HistoryStore(Protocol):
    def migrate(self) -> List[int]: ...
    def transaction(self) -> contextlib.AbstractContextManager: ...
    def upsert_day(self, account_fp: str, date: dt.date, numbers: Mapping[str, Any], final: bool, *,
                   rechecked: bool = False, conn: Any = None) -> bool: ...
    def upsert_week(self, account_fp: str, start: dt.date, numbers: Mapping[str, Any], final: bool, *,
                    conn: Any = None) -> bool: ...
    def days(self, account_fp: str, date_from: dt.date, date_to: dt.date, *, conn: Any = None) -> List[dict]: ...
    def weeks(self, account_fp: str, date_from: dt.date, date_to: dt.date, *, conn: Any = None) -> List[dict]: ...
    def missing_days(self, account_fp: str, date_from: dt.date, date_to: dt.date, *,
                     conn: Any = None) -> List[dt.date]: ...
    def open_days(self, account_fp: str, date_from: dt.date, date_to: dt.date, *,
                  conn: Any = None) -> List[dt.date]: ...
    def meta_get(self, key: str, default: Optional[str] = None, *, conn: Any = None) -> Optional[str]: ...
    def meta_set(self, key: str, value: str, *, conn: Any = None) -> None: ...
    def meta_setdefault(self, key: str, value: str, *, conn: Any = None) -> str: ...
    def forget_all(self) -> Dict[str, Any]: ...
    def retention(self, account_fp: str, cutoff: dt.date) -> Dict[str, Any]: ...
    def coverage(self, account_fp: Optional[str] = None) -> Dict[str, Any]: ...


# --- shared helpers ---------------------------------------------------------------------------------------------

def _date(value: Any) -> dt.date:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, str):
        return dt.date.fromisoformat(value)
    raise ValueError("history: a date is required")


def _fp(account_fp: Any) -> str:
    if not isinstance(account_fp, str) or not FP_RE.fullmatch(account_fp):
        raise ValueError("history: account fingerprint must be 64 hex characters")
    return account_fp


def _meta_key(key: Any) -> str:
    if not isinstance(key, str) or not META_KEY_RE.fullmatch(key):
        raise ValueError("history: bad meta key")
    return key


def _meta_value(value: Any) -> str:
    if not isinstance(value, str) or len(value) > META_VALUE_MAX:
        raise ValueError("history: meta value must be a short string")
    return value


def _date_range(date_from: dt.date, date_to: dt.date) -> List[dt.date]:
    out, day = [], date_from
    while day <= date_to:
        out.append(day)
        day += dt.timedelta(days=1)
    return out


def fingerprint(salt_hex: str, profile_id: Any) -> str:
    """HMAC-SHA256(salt, profile id) as 64 hex characters: one-way, so the id is never stored."""
    key = bytes.fromhex(salt_hex)
    return hmac.new(key, str(profile_id).strip().encode("utf-8"), hashlib.sha256).hexdigest()


def account_check(store: HistoryStore, profile_id: Any, *, conn: Any = None) -> tuple:
    """(fingerprint, ACCOUNT_OK | ACCOUNT_NEW | ACCOUNT_MISMATCH) for the profile id Garmin answered with.

    The first call creates the salt and stores the fingerprint (NEW). A different account afterwards sets
    meta account_mismatch=yes and returns MISMATCH; nothing is overwritten (forget is the only way out).
    """
    salt = store.meta_get(META_SALT, conn=conn)
    if not salt:
        salt = store.meta_setdefault(META_SALT, secrets.token_hex(32), conn=conn)
    fp = fingerprint(salt, profile_id)
    stored = store.meta_get(META_ACCOUNT, conn=conn)
    if not stored:
        # Insert-if-absent: two workers seeing the first day at once agree on one salt and one account.
        if hmac.compare_digest(store.meta_setdefault(META_ACCOUNT, fp, conn=conn), fp):
            return fp, ACCOUNT_NEW
        stored = store.meta_get(META_ACCOUNT, conn=conn)
    if hmac.compare_digest(stored, fp):
        return fp, ACCOUNT_OK
    store.meta_set(META_MISMATCH, "yes", conn=conn)
    LOG.warning("history: the Garmin account changed; indexing stopped (set HISTORY_FORGET=yes to start over)")
    return stored, ACCOUNT_MISMATCH


def doc_ids_for(kind: str, starts: List[dt.date]) -> List[str]:
    import history_docs  # local: history_docs imports this module

    return [history_docs.doc_id(kind, d) for d in starts]


def merge_day(old: Optional[Mapping[str, Any]], new: Mapping[str, Any]) -> Dict[str, Any]:
    """The numbers to store when a day already has a row. A field of a source that answered this time (not in
    the new "missing") takes the new value, null included: Garmin's answer now is the truth. A field of a source
    that failed this time keeps its stored value (a failed recheck must not wipe it). A field with no source
    (mode), a field the new numbers leave out, or any field when they carry no "missing", keeps the old value
    only where the new one is null. A source stays in "missing" only when both fetches missed it."""
    if not old:
        return dict(new)
    out = dict(new)
    failed = set(new.get("missing") or []) if "missing" in new else None
    for key, value in old.items():
        if key == "missing":
            continue
        source = hx.FIELD_SOURCE.get(key)
        if failed is not None and source is not None and source not in failed and key in new:
            continue
        if out.get(key) is None and value is not None:
            out[key] = value
    if "missing" in new or "missing" in old:
        before = set(old.get("missing") or [])
        out["missing"] = [s for s in (new.get("missing") or []) if s in before]
    return out


def _coverage(days: List[dict], weeks: List[dict]) -> Dict[str, Any]:
    with_data = [d for d in days if not d["empty"]]
    final = [d for d in days if d["final"]]
    return {
        "days": len(days),
        "days_with_data": len(with_data),
        "final_days": len(final),
        "empty_days": len(days) - len(with_data),
        "weeks": len(weeks),
        "final_weeks": sum(1 for w in weeks if w["final"]),
        "from": min(d["date"] for d in days).isoformat() if days else None,
        "to": max(d["date"] for d in days).isoformat() if days else None,
        "last_final_day": max(d["date"] for d in final).isoformat() if final else None,
        "weeks_from": min(w["start"] for w in weeks).isoformat() if weeks else None,
        "weeks_to": max(w["end"] for w in weeks).isoformat() if weeks else None,
    }


# --- in memory --------------------------------------------------------------------------------------------------

class MemoryHistoryStore:
    """The interface over dicts (tests, and the evals' fixtures). transaction() rolls back on an exception."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._days: Dict[tuple, dict] = {}
        self._weeks: Dict[tuple, dict] = {}
        self._meta: Dict[str, str] = {}
        self.migrated: List[int] = []
        self.vacuumed = 0

    def migrate(self) -> List[int]:
        with self._lock:
            new = [v for v, _name, _path in migration_files() if v not in self.migrated]
            self.migrated += new
            return new

    @contextlib.contextmanager
    def transaction(self) -> Iterator[Any]:
        with self._lock:
            saved = copy.deepcopy((self._days, self._weeks, self._meta))
            try:
                yield self
            except BaseException:
                self._days, self._weeks, self._meta = saved
                raise

    def upsert_day(self, account_fp, date, numbers, final, *, rechecked=False, conn=None) -> bool:
        fp, day = _fp(account_fp), _date(date)
        clean = hx.check_numbers("day", numbers)
        with self._lock:
            old = self._days.get((fp, day))
            clean = merge_day(old["numbers"] if old else None, clean)
            row = {
                "date": day, "numbers": clean, "final": bool(final) or bool(old and old["final"]),
                "empty": hx.is_empty_day(clean), "rechecked": bool(rechecked) or bool(old and old["rechecked"]),
            }
            if old and all(old[k] == row[k] for k in row):
                return False
            row["updated_at"] = dt.datetime.now(dt.timezone.utc)
            self._days[(fp, day)] = row
            return True

    def upsert_week(self, account_fp, start, numbers, final, *, conn=None) -> bool:
        fp, start = _fp(account_fp), _date(start)
        if start.weekday() != 0:
            raise ValueError("history: a week starts on a Monday")
        clean = hx.check_numbers("week", numbers)
        with self._lock:
            old = self._weeks.get((fp, start))
            row = {"start": start, "end": start + dt.timedelta(days=6), "numbers": clean, "final": bool(final),
                   "rules_version": int(clean.get("rules_version") or 1)}
            if old and all(old[k] == row[k] for k in row):
                return False
            row["updated_at"] = dt.datetime.now(dt.timezone.utc)
            self._weeks[(fp, start)] = row
            return True

    def days(self, account_fp, date_from, date_to, *, conn=None) -> List[dict]:
        a, b = _date(date_from), _date(date_to)
        with self._lock:
            rows = [copy.deepcopy(r) for (fp, d), r in self._days.items() if fp == account_fp and a <= d <= b]
        return sorted(rows, key=lambda r: r["date"])

    def weeks(self, account_fp, date_from, date_to, *, conn=None) -> List[dict]:
        a, b = _date(date_from), _date(date_to)
        with self._lock:
            rows = [copy.deepcopy(r) for (fp, s), r in self._weeks.items() if fp == account_fp and a <= s <= b]
        return sorted(rows, key=lambda r: r["start"])

    def missing_days(self, account_fp, date_from, date_to, *, conn=None) -> List[dt.date]:
        """Days in date_from .. date_to with no row, oldest first."""
        a, b = _date(date_from), _date(date_to)
        with self._lock:
            have = {d for (fp, d) in self._days if fp == account_fp}
        return [d for d in _date_range(a, b) if d not in have]

    def open_days(self, account_fp, date_from, date_to, *, conn=None) -> List[dt.date]:
        """Days in date_from .. date_to that have a row that is not final, oldest first."""
        a, b = _date(date_from), _date(date_to)
        with self._lock:
            return sorted(d for (fp, d), r in self._days.items() if fp == account_fp and a <= d <= b
                          and not r["final"])

    def meta_get(self, key, default=None, *, conn=None):
        with self._lock:
            return self._meta.get(_meta_key(key), default)

    def meta_set(self, key, value, *, conn=None) -> None:
        with self._lock:
            self._meta[_meta_key(key)] = _meta_value(value)

    def meta_setdefault(self, key, value, *, conn=None) -> str:
        with self._lock:
            return self._meta.setdefault(_meta_key(key), _meta_value(value))

    def forget_all(self) -> Dict[str, Any]:
        with self._lock:
            out = {"schema_dropped": True, "days": len(self._days), "weeks": len(self._weeks),
                   "collection_rows": 0, "vacuum": True}
            self._days, self._weeks, self._meta = {}, {}, {}
            self.migrated = []
            self.vacuumed += 1
        LOG.info("history: forget removed %d days, %d weeks and the meta table", out["days"], out["weeks"])
        return out

    def retention(self, account_fp, cutoff) -> Dict[str, Any]:
        cut = _date(cutoff)
        with self._lock:
            days = [k for k in self._days if k[1] < cut]
            weeks = [k for k in self._weeks if k[1] < cut]
            for k in days:
                del self._days[k]
            for k in weeks:
                del self._weeks[k]
        ids = doc_ids_for("day", [k[1] for k in days]) + doc_ids_for("week", [k[1] for k in weeks])
        return {"days": len(days), "weeks": len(weeks), "doc_ids": ids, "vectors": 0}

    def coverage(self, account_fp=None) -> Dict[str, Any]:
        fp = account_fp or self.meta_get(META_ACCOUNT)
        with self._lock:
            days = [r for (f, _d), r in self._days.items() if f == fp]
            weeks = [r for (f, _s), r in self._weeks.items() if f == fp]
        return _coverage(days, weeks)


# --- migrations -------------------------------------------------------------------------------------------------

def migration_files(folder: str = SQL_DIR) -> List[tuple]:
    """[(version, name, path)] of NNN_name.sql files, by version; a duplicate version is an error."""
    out = []
    for name in sorted(os.listdir(folder)) if os.path.isdir(folder) else []:
        m = MIGRATION_RE.fullmatch(name)
        if m:
            out.append((int(m.group(1)), name, os.path.join(folder, name)))
    versions = [v for v, _n, _p in out]
    if len(versions) != len(set(versions)):
        raise StoreError("history: two migration files share a version")
    return sorted(out)


# --- Postgres ---------------------------------------------------------------------------------------------------

def _psycopg():
    try:
        import psycopg
        from psycopg.types.json import Jsonb
    except ImportError as exc:  # pragma: no cover - only with HISTORY_SEARCH=on and no history deps
        raise StoreError("history: psycopg is not installed (build the image with HISTORY=1)") from exc
    return psycopg, Jsonb


class PgHistoryStore:
    """HistoryStore on Postgres (psycopg 3). One short connection per call, or the caller's `conn` (the job's
    transaction, so a job's rows and its follow-up jobs commit together)."""

    def __init__(self, dsn: str, *, connect_timeout: int = 10) -> None:
        if not isinstance(dsn, str) or not dsn.strip():
            raise ValueError("history: DATABASE_URL is empty")
        self._dsn = dsn
        self._timeout = connect_timeout
        self._psycopg, self._Jsonb = _psycopg()

    def __repr__(self) -> str:  # never the URL
        return "PgHistoryStore()"

    def _connect(self, autocommit: bool = False, statement_timeout_ms: int = jobqueue.STATEMENT_TIMEOUT_MS):
        try:
            return self._psycopg.connect(self._dsn, autocommit=autocommit, connect_timeout=self._timeout,
                                         options=jobqueue.pg_options(statement_timeout_ms))
        except self._psycopg.Error:
            raise StoreError("history: cannot connect to the database") from None

    @contextlib.contextmanager
    def transaction(self) -> Iterator[Any]:
        conn = self._connect()
        try:
            with conn:
                yield conn
        except self._psycopg.Error:
            raise StoreError("history: database error") from None
        finally:
            conn.close()

    @contextlib.contextmanager
    def _use(self, conn: Any) -> Iterator[Any]:
        if conn is not None:
            yield conn
            return
        with self.transaction() as own:
            yield own

    def _rows(self, conn, sql: str, params: Optional[tuple] = None) -> List[tuple]:
        """Run one statement (or, with no params, a whole migration file) and return its rows."""
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return cur.fetchall() if cur.description else []
        except self._psycopg.Error:
            raise StoreError("history: database error") from None

    # -- schema

    def migrate(self) -> List[int]:
        applied: List[int] = []
        with self.transaction() as conn:
            # Migrations are rare and may build indexes, and a second service waits here for the first one:
            # longer limits than the per-connection defaults, for this transaction only.
            self._rows(conn, "SET LOCAL statement_timeout = '300s'")
            self._rows(conn, "SET LOCAL lock_timeout = '120s'")
            self._rows(conn, "SELECT pg_advisory_xact_lock(%s)", (MIGRATE_LOCK_ID,))
            self._rows(conn, f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
            self._rows(conn, f"CREATE TABLE IF NOT EXISTS {SCHEMA}.schema_migrations ("
                             "version integer PRIMARY KEY, name text NOT NULL, "
                             "applied_at timestamptz NOT NULL DEFAULT now())")
            done = {r[0] for r in self._rows(conn, f"SELECT version FROM {SCHEMA}.schema_migrations")}
            for version, name, path in migration_files():
                if version in done:
                    continue
                with open(path, encoding="utf-8") as fh:
                    sql = fh.read()
                self._rows(conn, sql)
                self._rows(conn, f"INSERT INTO {SCHEMA}.schema_migrations (version, name) VALUES (%s, %s)",
                           (version, name))
                applied.append(version)
        if applied:
            LOG.info("history: applied %d migration(s)", len(applied))
        return applied

    # -- writes

    def upsert_day(self, account_fp, date, numbers, final, *, rechecked=False, conn=None) -> bool:
        fp, day = _fp(account_fp), _date(date)
        clean = hx.check_numbers("day", numbers)
        # Read the stored row under a row lock and merge (merge_day), so a source that failed on a recheck
        # keeps its stored values instead of wiping them.
        lock = f"SELECT numbers FROM {SCHEMA}.days WHERE account_fp = %s AND day = %s FOR UPDATE"
        sql = (f"INSERT INTO {SCHEMA}.days AS d (account_fp, day, numbers, final, empty, rechecked) "
               "VALUES (%s, %s, %s, %s, %s, %s) "
               "ON CONFLICT (account_fp, day) DO UPDATE SET numbers = EXCLUDED.numbers, "
               "final = d.final OR EXCLUDED.final, empty = EXCLUDED.empty, "
               "rechecked = d.rechecked OR EXCLUDED.rechecked, fetched_at = now(), updated_at = now() "
               "WHERE d.numbers IS DISTINCT FROM EXCLUDED.numbers OR d.empty IS DISTINCT FROM EXCLUDED.empty "
               "OR (EXCLUDED.final AND NOT d.final) OR (EXCLUDED.rechecked AND NOT d.rechecked) "
               "RETURNING 1")
        with self._use(conn) as c:
            stored = self._rows(c, lock, (fp, day))
            if stored:
                clean = merge_day(self._json(stored[0][0]), clean)
            empty = hx.is_empty_day(clean)
            return bool(self._rows(c, sql, (fp, day, self._Jsonb(clean), bool(final), empty, bool(rechecked))))

    def upsert_week(self, account_fp, start, numbers, final, *, conn=None) -> bool:
        fp, start = _fp(account_fp), _date(start)
        if start.weekday() != 0:
            raise ValueError("history: a week starts on a Monday")
        clean = hx.check_numbers("week", numbers)
        sql = (f"INSERT INTO {SCHEMA}.weeks AS w (account_fp, start_day, end_day, numbers, final, rules_version) "
               "VALUES (%s, %s, %s, %s, %s, %s) "
               "ON CONFLICT (account_fp, start_day) DO UPDATE SET numbers = EXCLUDED.numbers, "
               "final = EXCLUDED.final, rules_version = EXCLUDED.rules_version, updated_at = now() "
               "WHERE (w.numbers, w.final, w.rules_version) IS DISTINCT FROM "
               "(EXCLUDED.numbers, EXCLUDED.final, EXCLUDED.rules_version) RETURNING 1")
        params = (fp, start, start + dt.timedelta(days=6), self._Jsonb(clean), bool(final),
                  int(clean.get("rules_version") or 1))
        with self._use(conn) as c:
            return bool(self._rows(c, sql, params))

    # -- reads

    @staticmethod
    def _json(value: Any) -> dict:
        return json.loads(value) if isinstance(value, (str, bytes)) else dict(value)

    def days(self, account_fp, date_from, date_to, *, conn=None) -> List[dict]:
        sql = (f"SELECT day, numbers, final, empty, rechecked, updated_at FROM {SCHEMA}.days "
               "WHERE account_fp = %s AND day BETWEEN %s AND %s ORDER BY day")
        with self._use(conn) as c:
            rows = self._rows(c, sql, (account_fp, _date(date_from), _date(date_to)))
        return [{"date": r[0], "numbers": self._json(r[1]), "final": r[2], "empty": r[3], "rechecked": r[4],
                 "updated_at": r[5]} for r in rows]

    def weeks(self, account_fp, date_from, date_to, *, conn=None) -> List[dict]:
        sql = (f"SELECT start_day, end_day, numbers, final, rules_version, updated_at FROM {SCHEMA}.weeks "
               "WHERE account_fp = %s AND start_day BETWEEN %s AND %s ORDER BY start_day")
        with self._use(conn) as c:
            rows = self._rows(c, sql, (account_fp, _date(date_from), _date(date_to)))
        return [{"start": r[0], "end": r[1], "numbers": self._json(r[2]), "final": r[3], "rules_version": r[4],
                 "updated_at": r[5]} for r in rows]

    def missing_days(self, account_fp, date_from, date_to, *, conn=None) -> List[dt.date]:
        sql = (f"SELECT g::date FROM generate_series(%s::date, %s::date, interval '1 day') AS g "
               f"WHERE NOT EXISTS (SELECT 1 FROM {SCHEMA}.days d WHERE d.account_fp = %s AND d.day = g::date) "
               "ORDER BY 1")
        with self._use(conn) as c:
            return [r[0] for r in self._rows(c, sql, (_date(date_from), _date(date_to), account_fp))]

    def open_days(self, account_fp, date_from, date_to, *, conn=None) -> List[dt.date]:
        sql = (f"SELECT day FROM {SCHEMA}.days WHERE account_fp = %s AND day BETWEEN %s AND %s AND NOT final "
               "ORDER BY day")
        with self._use(conn) as c:
            return [r[0] for r in self._rows(c, sql, (account_fp, _date(date_from), _date(date_to)))]

    def meta_get(self, key, default=None, *, conn=None):
        with self._use(conn) as c:
            rows = self._rows(c, f"SELECT value FROM {SCHEMA}.meta WHERE key = %s", (_meta_key(key),))
        return rows[0][0] if rows else default

    def meta_set(self, key, value, *, conn=None) -> None:
        sql = (f"INSERT INTO {SCHEMA}.meta (key, value) VALUES (%s, %s) "
               "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()")
        with self._use(conn) as c:
            self._rows(c, sql, (_meta_key(key), _meta_value(value)))

    def meta_setdefault(self, key, value, *, conn=None) -> str:
        """Store value unless the key exists; returns the stored value (one round trip, race-free)."""
        sql = (f"WITH ins AS (INSERT INTO {SCHEMA}.meta (key, value) VALUES (%s, %s) ON CONFLICT (key) DO NOTHING "
               f"RETURNING value) SELECT value FROM ins UNION ALL SELECT value FROM {SCHEMA}.meta WHERE key = %s "
               "LIMIT 1")
        k = _meta_key(key)
        with self._use(conn) as c:
            rows = self._rows(c, sql, (k, _meta_value(value), k))
            if not rows:   # another transaction inserted it after this statement's snapshot: read it now
                rows = self._rows(c, f"SELECT value FROM {SCHEMA}.meta WHERE key = %s", (k,))
            return rows[0][0]

    def coverage(self, account_fp=None) -> Dict[str, Any]:
        with self.transaction() as c:
            fp = account_fp or self.meta_get(META_ACCOUNT, conn=c)
            d = self._rows(c, f"SELECT count(*), count(*) FILTER (WHERE NOT empty), count(*) FILTER (WHERE final), "
                              f"min(day), max(day), max(day) FILTER (WHERE final) FROM {SCHEMA}.days "
                              "WHERE account_fp = %s", (fp,))[0]
            w = self._rows(c, f"SELECT count(*), count(*) FILTER (WHERE final), min(start_day), max(end_day) "
                              f"FROM {SCHEMA}.weeks WHERE account_fp = %s", (fp,))[0]

        def iso(v):
            return v.isoformat() if v is not None else None

        return {"days": d[0], "days_with_data": d[1], "final_days": d[2], "empty_days": d[0] - d[1],
                "weeks": w[0], "final_weeks": w[1], "from": iso(d[3]), "to": iso(d[4]),
                "last_final_day": iso(d[5]), "weeks_from": iso(w[2]), "weeks_to": iso(w[3])}

    # -- deletion

    def retention(self, account_fp, cutoff) -> Dict[str, Any]:
        """Deletes days and weeks before cutoff (all accounts), then their vectors in history.doc_vectors (own
        transaction, only when that table exists) and in the deprecated langchain_pg_* collection."""
        cut = _date(cutoff)
        with self.transaction() as c:
            days = [r[0] for r in self._rows(c, f"DELETE FROM {SCHEMA}.days WHERE day < %s RETURNING day", (cut,))]
            weeks = [r[0] for r in self._rows(
                c, f"DELETE FROM {SCHEMA}.weeks WHERE start_day < %s RETURNING start_day", (cut,))]
            ids = doc_ids_for("day", days) + doc_ids_for("week", weeks)
            if ids and self._langchain_tables(c):
                self._rows(c, "DELETE FROM langchain_pg_embedding WHERE id = ANY(%s) AND collection_id IN "
                              "(SELECT uuid FROM langchain_pg_collection WHERE name = %s)", (ids, COLLECTION_NAME))
        vectors = 0
        if ids:
            with self.transaction() as c:
                if self._rows(c, "SELECT to_regclass(%s)", (DOC_VECTORS,))[0][0] is not None:
                    vectors = len(self._rows(c, f"DELETE FROM {DOC_VECTORS} WHERE langchain_id = ANY(%s::uuid[]) "
                                                "RETURNING 1", (ids,)))
        if days or weeks:
            LOG.info("history: retention removed %d days, %d weeks and %d vectors", len(days), len(weeks), vectors)
        return {"days": len(days), "weeks": len(weeks), "doc_ids": ids, "vectors": vectors}

    def _langchain_tables(self, conn) -> bool:
        rows = self._rows(conn, "SELECT to_regclass(%s), to_regclass(%s)", LANGCHAIN_TABLES)
        return all(v is not None for v in rows[0])

    def forget_all(self) -> Dict[str, Any]:
        """Drop schema history (data, meta, jobs, doc_vectors), the deprecated langchain_pg_* collection if any,
        then VACUUM."""
        out: Dict[str, Any] = {"schema_dropped": False, "days": 0, "weeks": 0, "collection_rows": 0,
                               "vacuum": False}
        with self.transaction() as c:
            exists = self._rows(c, "SELECT 1 FROM pg_namespace WHERE nspname = %s", (SCHEMA,))
            if exists:
                for table, key in (("days", "days"), ("weeks", "weeks")):
                    if self._rows(c, "SELECT to_regclass(%s)", (f"{SCHEMA}.{table}",))[0][0] is not None:
                        out[key] = self._rows(c, f"SELECT count(*) FROM {SCHEMA}.{table}")[0][0]
                self._rows(c, f"DROP SCHEMA {SCHEMA} CASCADE")
                out["schema_dropped"] = True
            if self._langchain_tables(c):
                out["collection_rows"] = self._rows(
                    c, "WITH gone AS (DELETE FROM langchain_pg_embedding WHERE collection_id IN "
                       "(SELECT uuid FROM langchain_pg_collection WHERE name = %s) RETURNING 1) "
                       "SELECT count(*) FROM gone", (COLLECTION_NAME,))[0][0]
                self._rows(c, "DELETE FROM langchain_pg_collection WHERE name = %s", (COLLECTION_NAME,))
        conn = self._connect(autocommit=True, statement_timeout_ms=0)    # VACUUM may take a while: no limit
        try:
            self._rows(conn, "VACUUM")
            out["vacuum"] = True
        finally:
            conn.close()
        LOG.info("history: forget removed schema %s (%d days, %d weeks), %d vector rows; VACUUM done",
                 SCHEMA, out["days"], out["weeks"], out["collection_rows"])
        return out
