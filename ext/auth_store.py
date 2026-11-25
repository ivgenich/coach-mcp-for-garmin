"""Sign-in records in one sqlite file (AUTH_DB, default /data/auth.sqlite).

Standard library only. owner_auth.py is the only caller of the record
methods; `launch.py --check-config` calls check_writable() before Caddy starts.

What is kept, and how:

    clients   registered OAuth clients, with the client secret as issued (the
              SDK compares it in plain text; a client secret alone gets no token)
    pending   consent requests waiting for the owner secret
    codes     authorization codes, single use, 5 minutes
    grants    one per approved sign-in: a token family that is revoked as a whole
    tokens    access tokens (1 hour) and rotating refresh tokens (30 days)
    meta      the schema version and a PBKDF2 fingerprint of OWNER_SECRET

Codes, tokens and pending ids are stored only as SHA-256 hashes
(hash_secret), the owner secret only as the fingerprint. A changed
OWNER_SECRET at startup revokes every grant (owner_secret_changed).

One connection, one lock, one transaction per operation. The file is 0600 in
the default rollback-journal mode (one file, no WAL). Single-use steps are one
guarded statement checked with rowcount (no RETURNING, for older sqlite).
Times come from `clock` (tests pass a fake clock that starts at time.time()).
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
import stat
import threading
import time
from contextlib import contextmanager
from typing import Callable, Dict, Iterator, List, Optional, Tuple

SCHEMA_VERSION = 1
SECRET_FP_ITERATIONS = 600_000            # tests patch it lower
FP_ALGORITHM = "pbkdf2_sha256"
FP_SALT_BYTES = 16
FP_MAX_ITERATIONS = 10_000_000            # bounds the work a damaged meta row can ask for

PURGE_EVERY = 600                         # seconds between purges during writes
CODE_KEEP = 3600                          # used/expired codes stay 1 h past expiry (reuse detection)
ACCESS_KEEP = 3600                        # expired access tokens stay 1 h
REVOKED_KEEP = 30 * 86400                 # revoked grants stay 30 days
EMPTY_GRANT_KEEP = 86400                  # a grant with no tokens left goes after 1 day

_FRESH_START = "; deleting the file signs everyone out and starts fresh"

SCHEMA = (
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    """CREATE TABLE IF NOT EXISTS clients (
  client_id    TEXT PRIMARY KEY,
  info_json    TEXT NOT NULL,
  created_at   REAL NOT NULL,
  last_used_at REAL NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS pending (
  pending_hash          TEXT PRIMARY KEY,
  client_id             TEXT NOT NULL,
  redirect_uri          TEXT NOT NULL,
  redirect_uri_explicit INTEGER NOT NULL,
  state                 TEXT,
  code_challenge        TEXT NOT NULL,
  scopes                TEXT NOT NULL,
  resource              TEXT NOT NULL,
  created_at            REAL NOT NULL,
  expires_at            REAL NOT NULL,
  failures              INTEGER NOT NULL DEFAULT 0)""",
    """CREATE TABLE IF NOT EXISTS codes (
  code_hash             TEXT PRIMARY KEY,
  client_id             TEXT NOT NULL,
  redirect_uri          TEXT NOT NULL,
  redirect_uri_explicit INTEGER NOT NULL,
  code_challenge        TEXT NOT NULL,
  scopes                TEXT NOT NULL,
  resource              TEXT NOT NULL,
  created_at            REAL NOT NULL,
  expires_at            REAL NOT NULL,
  used_at               REAL,
  grant_id              TEXT)""",
    """CREATE TABLE IF NOT EXISTS grants (
  grant_id       TEXT PRIMARY KEY,
  client_id      TEXT NOT NULL,
  scopes         TEXT NOT NULL,
  resource       TEXT NOT NULL,
  created_at     REAL NOT NULL,
  last_used_at   REAL NOT NULL,
  revoked_at     REAL,
  revoked_reason TEXT)""",
    """CREATE TABLE IF NOT EXISTS tokens (
  token_hash    TEXT PRIMARY KEY,
  kind          TEXT NOT NULL CHECK (kind IN ('access', 'refresh')),
  grant_id      TEXT NOT NULL,
  client_id     TEXT NOT NULL,
  scopes        TEXT NOT NULL,
  created_at    REAL NOT NULL,
  expires_at    REAL NOT NULL,
  used_at       REAL,
  parent_hash   TEXT)""",
    # The owner's explicit plan:write consent (the ticked consent-page box): one row per code ("code:<hash>") and
    # per grant ("grant:<id>"). A plan:write in a scopes column without its row is not honoured (get_code,
    # get_token, exchange_code and rotate_refresh drop it), so grants an older page minted with a requested
    # plan:write never write. An additive table: schema version 1 is unchanged and older servers ignore it.
    """CREATE TABLE IF NOT EXISTS write_consent (
  consent_id TEXT PRIMARY KEY,
  created_at REAL NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS tokens_by_grant   ON tokens(grant_id)",
    "CREATE INDEX IF NOT EXISTS tokens_by_parent  ON tokens(parent_hash)",
    "CREATE INDEX IF NOT EXISTS codes_by_client   ON codes(client_id)",
    "CREATE INDEX IF NOT EXISTS pending_by_client ON pending(client_id)",
    "CREATE INDEX IF NOT EXISTS grants_by_client  ON grants(client_id)",
)

WRITE_SCOPE = "plan:write"   # plan_confirm.WRITE_SCOPE (not imported: this module stays standard library only)

# Revocation reasons (grants.revoked_reason).
REASON_REFRESH_REUSE = "refresh token reuse"
REASON_CODE_REUSE = "authorization code reuse"
REASON_CLIENT = "revoked by client"
REASON_SECRET_CHANGED = "owner secret changed"
REASON_EVICTED = "client evicted"


class SchemaVersionError(RuntimeError):
    """The file was written by a newer (or unknown) version of this server."""

    def __init__(self, version: str) -> None:
        self.version = version
        super().__init__(f"schema version {version}; this server knows {SCHEMA_VERSION}")


class ClientLimitError(RuntimeError):
    """MAX_CLIENTS is reached and no client may be evicted."""


class _Rollback(Exception):
    """Raised inside a transaction to undo it and report a refusal."""


def hash_secret(value: str) -> str:
    """The storage key for a code, token or pending id: only this hash is kept."""
    return hashlib.sha256(value.encode("utf-8", "surrogateescape")).hexdigest()


def _one_line(exc: BaseException) -> str:
    return " ".join(str(exc).split())[:300]


def check_writable(path: str) -> List[str]:
    """--check-config: create/open the file (0600) and schema exactly as the server would, then close.

    [] or one A13 line. A rolled-back probe write also proves that the
    directory takes sqlite's journal file."""
    try:
        store = AuthStore(path)
    except SchemaVersionError as exc:
        return [f"AUTH_DB: {path} has schema version {exc.version}; this server knows {SCHEMA_VERSION}{_FRESH_START}"]
    except Exception as exc:  # noqa: BLE001 - one clear line, not a traceback
        return [f"AUTH_DB: cannot open {path}: {type(exc).__name__}: {_one_line(exc)}{_FRESH_START}"]
    try:
        store.probe_write()
    except Exception as exc:  # noqa: BLE001
        return [f"AUTH_DB: cannot open {path}: {type(exc).__name__}: {_one_line(exc)}{_FRESH_START}"]
    finally:
        store.close()
    return []


def _make_parents(directory: str) -> None:
    """Create each missing directory level with mode 0700. An existing
    directory is never chmod-ed (/data is the volume)."""
    missing = []
    current = directory
    while current and not os.path.isdir(current):
        missing.append(current)
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    for path in reversed(missing):
        try:
            os.mkdir(path, 0o700)
        except FileExistsError:
            continue  # made by someone else meanwhile: leave it as it is
        os.chmod(path, 0o700)  # exactly 0700 whatever the umask; this one is ours


def _prepare_file(path: str) -> None:
    """The database file, created 0600; an existing one loses group and other bits."""
    _make_parents(os.path.dirname(path))
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        created = True
    except FileExistsError:
        fd = os.open(path, os.O_RDWR)
        created = False
    try:
        mode = os.fstat(fd).st_mode
        if not stat.S_ISREG(mode):
            raise OSError(f"not a regular file: {path}")
        if (created and stat.S_IMODE(mode) != 0o600) or mode & 0o077:
            os.fchmod(fd, 0o600)
    finally:
        os.close(fd)


@contextmanager
def _transaction(db: sqlite3.Connection) -> Iterator[None]:
    """BEGIN IMMEDIATE ... COMMIT, or ROLLBACK on any exception."""
    db.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        if db.in_transaction:
            db.execute("ROLLBACK")
        raise
    try:
        db.execute("COMMIT")
    except BaseException:
        if db.in_transaction:
            db.execute("ROLLBACK")
        raise


def _fingerprint(secret: str, salt: Optional[bytes] = None, iterations: Optional[int] = None) -> str:
    iterations = SECRET_FP_ITERATIONS if iterations is None else iterations
    salt = secrets.token_bytes(FP_SALT_BYTES) if salt is None else salt
    digest = hashlib.pbkdf2_hmac("sha256", secret.encode("utf-8", "surrogateescape"), salt, iterations)
    return f"{FP_ALGORITHM}${iterations}${salt.hex()}${digest.hex()}"


def _fingerprint_matches(stored: str, secret: str) -> bool:
    """False for a different secret and for a damaged fingerprint."""
    try:
        algorithm, iterations, salt_hex, digest_hex = stored.split("$")
        rounds = int(iterations)
        salt, expected = bytes.fromhex(salt_hex), bytes.fromhex(digest_hex)
    except ValueError:
        return False
    if algorithm != FP_ALGORITHM or not 0 < rounds <= FP_MAX_ITERATIONS or not salt or not expected:
        return False
    actual = hashlib.pbkdf2_hmac("sha256", secret.encode("utf-8", "surrogateescape"), salt, rounds)
    return hmac.compare_digest(actual, expected)


def _row(row: Optional[sqlite3.Row]) -> Optional[Dict]:
    return None if row is None else dict(row)


def _consented(db: sqlite3.Connection, scopes: str, consent_id: Optional[str]) -> str:
    """`scopes` without plan:write unless the write_consent row `consent_id` exists."""
    words = (scopes or "").split()
    if WRITE_SCOPE not in words:
        return scopes
    if consent_id is not None and db.execute("SELECT 1 FROM write_consent WHERE consent_id = ?",
                                             (consent_id,)).fetchone() is not None:
        return scopes
    return " ".join(w for w in words if w != WRITE_SCOPE)


class AuthStore:
    """The sign-in records. Every method is one transaction under one lock,
    so the provider can call it from the event loop or from any thread."""

    def __init__(self, path: str, clock: Callable[[], float] = time.time) -> None:
        self.path = path
        self.clock = clock
        self._lock = threading.Lock()
        self._last_purge: Optional[float] = None
        _prepare_file(path)
        self._db: Optional[sqlite3.Connection] = sqlite3.connect(
            path, timeout=10, isolation_level=None, check_same_thread=False)
        try:
            self._db.row_factory = sqlite3.Row
            self._db.execute("PRAGMA journal_mode=DELETE")  # one file, even if someone switched it to WAL
            self._init_schema()
            self.purge()
        except BaseException:
            self._db.close()
            self._db = None
            raise

    # --- plumbing ------------------------------------------------------------

    def close(self) -> None:
        with self._lock:
            if self._db is not None:
                self._db.close()
                self._db = None

    def __enter__(self) -> "AuthStore":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def _conn(self) -> sqlite3.Connection:
        if self._db is None:
            raise sqlite3.ProgrammingError("the sign-in store is closed")
        return self._db

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            yield self._conn()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        """One write transaction; purges first when the last purge is 10 minutes old."""
        with self._lock:
            db = self._conn()
            with _transaction(db):
                now = self.clock()
                if self._last_purge is not None and now - self._last_purge >= PURGE_EVERY:
                    self._purge(db, now)
                yield db

    def _init_schema(self) -> None:
        with self._lock:
            db = self._conn()
            with _transaction(db):
                db.execute(SCHEMA[0])
                row = db.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
                if row is not None and row["value"] != str(SCHEMA_VERSION):
                    raise SchemaVersionError(" ".join(str(row["value"]).split())[:40])
                for statement in SCHEMA[1:]:
                    db.execute(statement)
                if row is None:
                    db.execute("INSERT INTO meta (key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))

    def probe_write(self) -> None:
        """Write a row and roll it back: proves the file and its directory take writes."""
        with self._lock:
            db = self._conn()
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('write_probe', '1')")
            finally:
                if db.in_transaction:
                    db.execute("ROLLBACK")

    # --- housekeeping ----------------------------------------------------------

    def purge(self) -> None:
        """Delete what can no longer be used (also runs at open, then at most every 10 minutes)."""
        with self._lock:
            db = self._conn()
            with _transaction(db):
                self._purge(db, self.clock())

    def _purge(self, db: sqlite3.Connection, now: float) -> None:
        db.execute("DELETE FROM pending WHERE expires_at <= ?", (now,))
        db.execute("DELETE FROM codes WHERE expires_at <= ?", (now - CODE_KEEP,))
        db.execute("DELETE FROM tokens WHERE kind = 'access' AND expires_at <= ?", (now - ACCESS_KEEP,))
        db.execute("DELETE FROM tokens WHERE kind = 'refresh' AND expires_at <= ?", (now,))
        old_revoked = now - REVOKED_KEEP
        db.execute("DELETE FROM tokens WHERE grant_id IN (SELECT grant_id FROM grants "
                   "WHERE revoked_at IS NOT NULL AND revoked_at <= ?)", (old_revoked,))
        db.execute("DELETE FROM grants WHERE (revoked_at IS NOT NULL AND revoked_at <= ?) "
                   "OR (created_at <= ? AND NOT EXISTS (SELECT 1 FROM tokens t WHERE t.grant_id = grants.grant_id))",
                   (old_revoked, now - EMPTY_GRANT_KEEP))
        db.execute("DELETE FROM write_consent WHERE (consent_id LIKE 'code:%' AND substr(consent_id, 6) NOT IN "
                   "(SELECT code_hash FROM codes)) OR (consent_id LIKE 'grant:%' AND substr(consent_id, 7) NOT IN "
                   "(SELECT grant_id FROM grants))")
        self._last_purge = now

    def counts(self) -> Tuple[int, int]:
        """(stored clients, live grants: unrevoked, with an unused unexpired refresh token)."""
        now = self.clock()
        with self._read() as db:
            clients = db.execute("SELECT COUNT(*) FROM clients").fetchone()[0]
            grants = db.execute(
                "SELECT COUNT(*) FROM grants g WHERE g.revoked_at IS NULL AND EXISTS (SELECT 1 FROM tokens t "
                "WHERE t.grant_id = g.grant_id AND t.kind = 'refresh' AND t.used_at IS NULL AND t.expires_at > ?)",
                (now,)).fetchone()[0]
        return clients, grants

    # --- the owner secret --------------------------------------------------------

    def owner_secret_changed(self, secret: str) -> bool:
        """Compare with meta.owner_secret_fp (PBKDF2-SHA256, SECRET_FP_ITERATIONS, 16-byte salt,
        stored as 'pbkdf2_sha256$<iterations>$<salt hex>$<hash hex>'). Absent: store it, return False.
        Different: revoke every grant ('owner secret changed'), delete all pending rows and unused codes,
        store the new fingerprint, return True."""
        with self._read() as db:
            row = db.execute("SELECT value FROM meta WHERE key = 'owner_secret_fp'").fetchone()
        stored = None if row is None else row["value"]
        if stored is not None and _fingerprint_matches(stored, secret):
            return False
        fingerprint = _fingerprint(secret)  # slow on purpose: outside the lock
        with self._tx() as db:
            if stored is not None:
                db.execute("UPDATE grants SET revoked_at = ?, revoked_reason = ? WHERE revoked_at IS NULL",
                           (self.clock(), REASON_SECRET_CHANGED))
                db.execute("DELETE FROM pending")
                db.execute("DELETE FROM codes WHERE used_at IS NULL")
            db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('owner_secret_fp', ?)", (fingerprint,))
        return stored is not None

    # --- clients -------------------------------------------------------------------

    def get_client(self, client_id: str) -> Optional[str]:
        """The client's info_json, or None."""
        with self._read() as db:
            row = db.execute("SELECT info_json FROM clients WHERE client_id = ?", (client_id,)).fetchone()
        return None if row is None else row["info_json"]

    def add_client(self, client_id: str, info_json: str, *, max_clients: int, evict_idle_days: float,
                   pending_ttl: float = 600.0) -> List[Dict]:
        """Store a client. At max_clients, evict first (tier 1, then tier 2) and return
        what was evicted ({client_id, tier, revoked_grants}); ClientLimitError if nothing may go.
        A client registered less than pending_ttl seconds ago with a live consent
        request counts as a sign-in in flight: tier 1 takes those last, and after
        them a client of any age with a live consent request and a grant row."""
        with self._tx() as db:
            now = self.clock()
            evicted = []
            count = db.execute("SELECT COUNT(*) FROM clients").fetchone()[0]
            while count >= max_clients:
                victim = self._eviction_candidate(db, now, evict_idle_days, pending_ttl)
                if victim is None:
                    raise ClientLimitError("too many registered clients")
                evicted.append(self._evict(db, *victim, now))
                count -= 1
            db.execute("INSERT INTO clients (client_id, info_json, created_at, last_used_at) VALUES (?, ?, ?, ?)",
                       (client_id, info_json, now, now))
        return evicted

    @staticmethod
    def _eviction_candidate(db: sqlite3.Connection, now: float, idle_days: float,
                            pending_ttl: float) -> Optional[Tuple[str, int]]:
        # Tier 1: any client that holds nothing only the owner can create, that is
        # no unused, unexpired refresh token in a grant that is not revoked and no
        # unused, unexpired code. Anyone can register a client and open a consent
        # request without the owner secret, so neither keeps a client out of tier 1:
        # a stranger's clients could otherwise fill the table and push tier 2 onto
        # the owner's working sign-in. Order: clients with no sign-in in flight
        # first, least recently used first; then sign-ins in flight (a live consent
        # request on a client registered less than pending_ttl ago, as claude.ai
        # registers right before it signs in), oldest first, so the newest sign-in
        # goes last of all; last, a live consent request on a client that has a
        # grant row (live, expired or revoked), oldest first. A consent request on
        # an old client alone protects nothing, or a stranger could shield old
        # clients for free; a grant row needs the owner secret, so only the
        # owner's own client reconnecting (after an OWNER_SECRET change, say) is
        # protected, however old it is. The trade-off: an old client that never
        # signed in, or whose grants were purged (30 days after revocation, or a
        # day after its last token expired), is not.
        row = db.execute(
            "SELECT client_id FROM ("
            "  SELECT c.client_id, c.created_at, c.last_used_at, c.rowid AS seq, "
            "    CASE WHEN NOT EXISTS (SELECT 1 FROM pending p "
            "        WHERE p.client_id = c.client_id AND p.expires_at > :now) THEN 0 "
            "      WHEN EXISTS (SELECT 1 FROM grants g WHERE g.client_id = c.client_id) THEN 2 "
            "      WHEN c.created_at > :young THEN 1 ELSE 0 END AS in_flight "
            "  FROM clients c WHERE "
            "  NOT EXISTS (SELECT 1 FROM tokens t JOIN grants g ON g.grant_id = t.grant_id "
            "    WHERE t.client_id = c.client_id AND t.kind = 'refresh' AND t.used_at IS NULL "
            "    AND t.expires_at > :now AND g.revoked_at IS NULL) "
            "  AND NOT EXISTS (SELECT 1 FROM codes k WHERE k.client_id = c.client_id "
            "    AND k.used_at IS NULL AND k.expires_at > :now)) "
            "ORDER BY in_flight, CASE WHEN in_flight THEN created_at ELSE last_used_at END, created_at, seq "
            "LIMIT 1", {"now": now, "young": now - pending_ttl}).fetchone()
        if row is not None:
            return row["client_id"], 1
        # Tier 2: every client now holds something only the owner can create.
        # Take the one whose newest activity (its grants' last use or its own last
        # /authorize or /token) is oldest, if that is more than idle_days ago.
        row = db.execute(
            "SELECT c.client_id, MAX(c.last_used_at, COALESCE((SELECT MAX(g.last_used_at) FROM grants g "
            "  WHERE g.client_id = c.client_id), c.last_used_at)) AS active "
            "FROM clients c ORDER BY active, c.created_at, c.rowid LIMIT 1").fetchone()
        if row is not None and row["active"] < now - idle_days * 86400:
            return row["client_id"], 2
        return None

    @staticmethod
    def _evict(db: sqlite3.Connection, client_id: str, tier: int, now: float) -> Dict:
        revoked = []
        if tier == 2:
            revoked = [r[0] for r in db.execute(
                "SELECT grant_id FROM grants WHERE client_id = ? AND revoked_at IS NULL", (client_id,))]
            db.execute("UPDATE grants SET revoked_at = ?, revoked_reason = ? WHERE client_id = ? AND revoked_at IS NULL",
                       (now, REASON_EVICTED, client_id))
        for table in ("tokens", "codes", "pending", "grants", "clients"):
            db.execute(f"DELETE FROM {table} WHERE client_id = ?", (client_id,))
        return {"client_id": client_id, "tier": tier, "revoked_grants": revoked}

    def touch_client(self, client_id: str) -> None:
        with self._tx() as db:
            db.execute("UPDATE clients SET last_used_at = ? WHERE client_id = ?", (self.clock(), client_id))

    # --- pending consent requests ----------------------------------------------------

    def add_pending(self, *, pending_hash: str, client_id: str, redirect_uri: str, redirect_uri_explicit: bool,
                    state: Optional[str], code_challenge: str, scopes: str, resource: str, ttl: float,
                    max_pending: int, max_per_client: int) -> bool:
        """Store a consent request and touch the client. Past max_per_client live rows,
        this client's own oldest go first. False (nothing stored or deleted) when the
        table already holds max_pending live rows: another client's row is never deleted."""
        try:
            with self._tx() as db:
                now = self.clock()
                own = [r[0] for r in db.execute(
                    "SELECT pending_hash FROM pending WHERE client_id = ? AND expires_at > ? "
                    "ORDER BY created_at, rowid", (client_id, now))]
                surplus = len(own) - max_per_client + 1
                if surplus > 0:
                    db.executemany("DELETE FROM pending WHERE pending_hash = ?", [(h,) for h in own[:surplus]])
                live = db.execute("SELECT COUNT(*) FROM pending WHERE expires_at > ?", (now,)).fetchone()[0]
                if live >= max_pending:
                    raise _Rollback()
                db.execute(
                    "INSERT INTO pending (pending_hash, client_id, redirect_uri, redirect_uri_explicit, state, "
                    "code_challenge, scopes, resource, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (pending_hash, client_id, redirect_uri, int(bool(redirect_uri_explicit)), state,
                     code_challenge, scopes, resource, now, now + ttl))
                db.execute("UPDATE clients SET last_used_at = ? WHERE client_id = ?", (now, client_id))
        except _Rollback:
            return False
        return True

    def get_pending(self, pending_hash: str) -> Optional[Dict]:
        """A live (unexpired) consent request, or None."""
        with self._read() as db:
            row = db.execute("SELECT * FROM pending WHERE pending_hash = ? AND expires_at > ?",
                             (pending_hash, self.clock())).fetchone()
        return _row(row)

    def take_pending(self, pending_hash: str) -> Optional[Dict]:
        """Delete a live consent request and return it; None if it was not there (single use)."""
        with self._tx() as db:
            now = self.clock()
            row = db.execute("SELECT * FROM pending WHERE pending_hash = ? AND expires_at > ?",
                             (pending_hash, now)).fetchone()
            if row is None:
                return None
            cur = db.execute("DELETE FROM pending WHERE pending_hash = ? AND expires_at > ?", (pending_hash, now))
            return dict(row) if cur.rowcount == 1 else None

    def pending_failure(self, pending_hash: str, *, max_failures: int) -> Optional[Tuple[int, Dict, bool]]:
        """Count one wrong secret: (failures now, the row, deleted). The row is deleted
        at max_failures. None if the request is gone."""
        with self._tx() as db:
            now = self.clock()
            row = db.execute("SELECT * FROM pending WHERE pending_hash = ? AND expires_at > ?",
                             (pending_hash, now)).fetchone()
            if row is None:
                return None
            db.execute("UPDATE pending SET failures = failures + 1 WHERE pending_hash = ?", (pending_hash,))
            failures = row["failures"] + 1
            deleted = failures >= max_failures
            if deleted:
                db.execute("DELETE FROM pending WHERE pending_hash = ?", (pending_hash,))
            return failures, dict(row), deleted

    def approve_pending(self, pending_hash: str, *, code_hash: str, code_ttl: float,
                        scopes: Optional[str] = None, write_consent: bool = False) -> Optional[Dict]:
        """Take the consent request and store a code made from it, in one transaction.
        The code gets `scopes` when given (what the owner approved on the consent page),
        else the scopes the request asked for; its plan:write counts only with
        write_consent (the owner ticked the box). Returns the request, or None if it was
        already taken or expired."""
        with self._tx() as db:
            now = self.clock()
            row = db.execute("SELECT * FROM pending WHERE pending_hash = ? AND expires_at > ?",
                             (pending_hash, now)).fetchone()
            if row is None:
                return None
            cur = db.execute("DELETE FROM pending WHERE pending_hash = ? AND expires_at > ?", (pending_hash, now))
            if cur.rowcount != 1:
                return None
            db.execute(
                "INSERT INTO codes (code_hash, client_id, redirect_uri, redirect_uri_explicit, code_challenge, "
                "scopes, resource, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (code_hash, row["client_id"], row["redirect_uri"], row["redirect_uri_explicit"],
                 row["code_challenge"], row["scopes"] if scopes is None else scopes, row["resource"], now,
                 now + code_ttl))
            if write_consent:
                db.execute("INSERT OR REPLACE INTO write_consent (consent_id, created_at) VALUES (?, ?)",
                           ("code:" + code_hash, now))
            return dict(row)

    # --- codes -------------------------------------------------------------------------

    def get_code(self, code_hash: str) -> Optional[Dict]:
        with self._read() as db:
            row = _row(db.execute("SELECT * FROM codes WHERE code_hash = ?", (code_hash,)).fetchone())
            if row is not None:
                row["scopes"] = _consented(db, row["scopes"], "code:" + code_hash)
            return row

    def exchange_code(self, *, code_hash: str, client_id: str, grant_id: str, access_hash: str, refresh_hash: str,
                      access_ttl: float, refresh_ttl: float) -> Dict:
        """Use a code once and create its grant with an access and a refresh token.

        {"ok": True, "scopes", "resource"} or {"ok": False, "reason": "used" | "expired" | "missing",
        "revoked": (grant_id, client_id) or None}. A code that was already used revokes the
        grant it produced."""
        with self._tx() as db:
            now = self.clock()
            cur = db.execute(
                "UPDATE codes SET used_at = ?, grant_id = ? WHERE code_hash = ? AND client_id = ? "
                "AND used_at IS NULL AND expires_at > ?", (now, grant_id, code_hash, client_id, now))
            if cur.rowcount == 1:
                code = dict(db.execute("SELECT scopes, resource FROM codes WHERE code_hash = ?",
                                       (code_hash,)).fetchone())
                code["scopes"] = _consented(db, code["scopes"], "code:" + code_hash)
                if WRITE_SCOPE in code["scopes"].split():
                    db.execute("INSERT OR REPLACE INTO write_consent (consent_id, created_at) VALUES (?, ?)",
                               ("grant:" + grant_id, now))
                db.execute("INSERT INTO grants (grant_id, client_id, scopes, resource, created_at, last_used_at) "
                           "VALUES (?, ?, ?, ?, ?, ?)", (grant_id, client_id, code["scopes"], code["resource"], now, now))
                self._insert_token(db, access_hash, "access", grant_id, client_id, code["scopes"], now, now + access_ttl)
                self._insert_token(db, refresh_hash, "refresh", grant_id, client_id, code["scopes"], now,
                                   now + refresh_ttl)
                db.execute("UPDATE clients SET last_used_at = ? WHERE client_id = ?", (now, client_id))
                return {"ok": True, "scopes": code["scopes"], "resource": code["resource"]}
            row = db.execute("SELECT client_id, used_at, expires_at, grant_id FROM codes WHERE code_hash = ?",
                             (code_hash,)).fetchone()
            if row is None or row["client_id"] != client_id:
                return {"ok": False, "reason": "missing", "revoked": None}
            if row["used_at"] is not None:
                revoked = None
                if row["grant_id"] and self._revoke(db, row["grant_id"], REASON_CODE_REUSE, now):
                    revoked = (row["grant_id"], client_id)
                return {"ok": False, "reason": "used", "revoked": revoked}
            return {"ok": False, "reason": "expired", "revoked": None}

    # --- tokens --------------------------------------------------------------------------

    @staticmethod
    def _insert_token(db: sqlite3.Connection, token_hash: str, kind: str, grant_id: str, client_id: str,
                      scopes: str, now: float, expires_at: float, parent_hash: Optional[str] = None) -> None:
        db.execute("INSERT INTO tokens (token_hash, kind, grant_id, client_id, scopes, created_at, expires_at, "
                   "parent_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                   (token_hash, kind, grant_id, client_id, scopes, now, expires_at, parent_hash))

    def get_token(self, token_hash: str, kind: str) -> Optional[Dict]:
        """A token row of this kind with its grant's resource and revoked_at, or None."""
        with self._read() as db:
            row = db.execute(
                "SELECT t.*, g.resource AS resource, g.revoked_at AS revoked_at FROM tokens t "
                "JOIN grants g ON g.grant_id = t.grant_id WHERE t.token_hash = ? AND t.kind = ?",
                (token_hash, kind)).fetchone()
            out = _row(row)
            if out is not None:
                out["scopes"] = _consented(db, out["scopes"], "grant:" + str(out["grant_id"]))
        return out

    def rotate_refresh(self, *, old_hash: str, client_id: str, access_hash: str, refresh_hash: str,
                       access_scopes: str, access_ttl: float, refresh_ttl: float) -> Optional[Dict]:
        """Use a refresh token once and issue its successors, in one transaction.

        None unless the token is this client's, unused, unexpired and in a live grant.
        The new access token gets access_scopes; the new refresh token keeps the old
        one's scopes and points back to it (parent_hash)."""
        with self._tx() as db:
            now = self.clock()
            cur = db.execute(
                "UPDATE tokens SET used_at = ? WHERE token_hash = ? AND kind = 'refresh' AND client_id = ? "
                "AND used_at IS NULL AND expires_at > ? "
                "AND grant_id IN (SELECT grant_id FROM grants WHERE revoked_at IS NULL)",
                (now, old_hash, client_id, now))
            if cur.rowcount != 1:
                return None
            old = db.execute("SELECT t.grant_id, t.scopes, g.resource FROM tokens t "
                             "JOIN grants g ON g.grant_id = t.grant_id WHERE t.token_hash = ?", (old_hash,)).fetchone()
            self._insert_token(db, access_hash, "access", old["grant_id"], client_id, access_scopes, now,
                               now + access_ttl)
            self._insert_token(db, refresh_hash, "refresh", old["grant_id"], client_id, old["scopes"], now,
                               now + refresh_ttl, parent_hash=old_hash)
            db.execute("UPDATE grants SET last_used_at = ? WHERE grant_id = ?", (now, old["grant_id"]))
            db.execute("UPDATE clients SET last_used_at = ? WHERE client_id = ?", (now, client_id))
            return {"grant_id": old["grant_id"], "resource": old["resource"],
                    "refresh_scopes": _consented(db, old["scopes"], "grant:" + str(old["grant_id"])),
                    "access_expires_at": now + access_ttl, "refresh_expires_at": now + refresh_ttl}

    def refresh_unused(self, token_hash: str) -> bool:
        """True while this refresh token is unused, unexpired and its grant is not revoked
        (the replay condition for the child of a rotated token)."""
        with self._read() as db:
            row = db.execute(
                "SELECT 1 FROM tokens t JOIN grants g ON g.grant_id = t.grant_id WHERE t.token_hash = ? "
                "AND t.kind = 'refresh' AND t.used_at IS NULL AND t.expires_at > ? AND g.revoked_at IS NULL",
                (token_hash, self.clock())).fetchone()
        return row is not None

    @staticmethod
    def _revoke(db: sqlite3.Connection, grant_id: str, reason: str, now: float) -> bool:
        cur = db.execute("UPDATE grants SET revoked_at = ?, revoked_reason = ? WHERE grant_id = ? "
                         "AND revoked_at IS NULL", (now, reason, grant_id))
        return cur.rowcount == 1

    def revoke_grant(self, grant_id: str, reason: str) -> Optional[str]:
        """Revoke a grant (its whole token family). The grant's client id if this call
        revoked it, None if it was already revoked or is unknown."""
        with self._tx() as db:
            if not self._revoke(db, grant_id, reason, self.clock()):
                return None
            row = db.execute("SELECT client_id FROM grants WHERE grant_id = ?", (grant_id,)).fetchone()
            return row["client_id"]

    def revoke_token_grant(self, token_hash: str, reason: str) -> Optional[Tuple[str, str]]:
        """Revoke the grant of a token (either kind): (grant_id, client_id) if this call revoked it."""
        with self._tx() as db:
            row = db.execute("SELECT grant_id, client_id FROM tokens WHERE token_hash = ?", (token_hash,)).fetchone()
            if row is None or not self._revoke(db, row["grant_id"], reason, self.clock()):
                return None
            return row["grant_id"], row["client_id"]
