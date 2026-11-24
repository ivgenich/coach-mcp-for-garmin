"""The worker's Garmin client (plan section 5b, "Garmin credentials for the worker").

Railway volumes attach to one service, so the worker cannot read the app's /data/tokens.

- Key mode (GARMIN_AUTH=key): the worker reads GARMIN_PERSONAL_API_KEY like the app (key_mode.take_key removes it
  from the environment and registers it for redaction) and gets its own 24-hour access token. Its token cache is
  GARMIN_KEY_TOKEN_CACHE when that folder is writable, else <tmp>/garmin-key/token.json (the worker has no volume).
- Full mode (GARMIN_AUTH=full): the app publishes its Garmin token file (GARMINTOKENS/garmin_tokens.json) to
  Postgres, sealed with AES-256-GCM under HISTORY_TOKEN_KEY (32 random bytes, base64, set on both services):
  TokenPublisher, started by the app. The worker opens it (StoredTokenGarmin) and never refreshes it: refresh stays
  with the app, under its refresh lock, so the login has one owner. A missing, unreadable or nearly expired stored
  token raises TokenStale (a jobqueue.Defer): the job waits 15 minutes without using an attempt, and the queue
  stats show it waiting for "token_stale".

Every Garmin call goes through GuardedGarmin: guard.before_call("worker") first (cooldown, live calls first, the
shared token bucket, the quiet window), then the call, then guard.after_outcome("worker", group) with the
tool_errors group of a failure (None on success), so a worker's 429 pauses the live path too. A refusal for the
bucket or a live call that ends within INLINE_WAIT_MAX_S is waited out inside the job (a job makes several calls;
deferring it at call 2 would roll it back and restart it from call 1 for ever); any other GuardBlocked passes
through to the worker loop. Only reads pass: get_*, download_* (no data/json/files/headers/method keyword) and
connectapi(path, params=...) (READ_PREFIXES, _get_only); names starting with "_" are not reachable.

A new client (a fresh stored token) loads its profile again before a data call; a client without a display name
makes no data call (ProfileMissing, a Defer: nothing stored, no day marked final).

Nothing here logs a token, the key, the sealed blob or a Garmin answer. Standard library only at import time;
cryptography, psycopg, garminconnect and Garmin's Personal API client are imported where used.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import logging
import os
import tempfile
import threading
import time
from typing import Callable, Optional

import feature_config
import jobqueue
import tool_errors

log = logging.getLogger("garmin_ext.worker")

TOKEN_KEY_ENV = "HISTORY_TOKEN_KEY"
TOKEN_FILE = "garmin_tokens.json"
AAD = b"coach-mcp-for-garmin garmin token v1"
NONCE_BYTES = 12
STALE_WAIT_S = 900.0
TOKEN_STALE = "token_stale"
PUBLISH_EVERY_S = 300.0
TOKEN_MAX = 64 * 1024

ERR_TOKEN_KEY = ("HISTORY_TOKEN_KEY must be 32 random bytes in base64 (make one with: "
                 "python3 -c \"import os,base64;print(base64.b64encode(os.urandom(32)).decode())\"); "
                 "the value set is not that (it is not shown)")

TOKEN_DDL = """
CREATE SCHEMA IF NOT EXISTS history;
CREATE TABLE IF NOT EXISTS history.garmin_token (
    id          smallint PRIMARY KEY CHECK (id = 1),
    nonce       bytea NOT NULL,
    sealed      bytea NOT NULL,
    digest      text NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now()
);
"""
_SQL_PUT = ("INSERT INTO history.garmin_token (id, nonce, sealed, digest, updated_at) "
            "VALUES (1, %(n)s, %(s)s, %(d)s, now()) ON CONFLICT (id) DO UPDATE SET nonce = EXCLUDED.nonce, "
            "sealed = EXCLUDED.sealed, digest = EXCLUDED.digest, updated_at = now()")
_SQL_GET = "SELECT nonce, sealed FROM history.garmin_token WHERE id = 1"
_SQL_TOKEN_TABLE = "SELECT to_regclass('history.garmin_token')"
_SQL_DELETE = "DELETE FROM history.garmin_token WHERE id = 1"
PROFILE_WAIT_S = 900.0         # the profile named no display name: the job waits, nothing is stored
PROFILE_MISSING = "profile_missing"

# GuardedGarmin: the guard refusals waited out inside a job, and for how long at most.
INLINE_WAIT_GROUPS = ("bucket", "live_busy")
INLINE_WAIT_MAX_S = 30.0       # one wait
INLINE_WAIT_TOTAL_S = 300.0    # all the waits of one call (then the refusal goes to the worker loop)
READ_PREFIXES = ("get_", "download_")
READ_METHODS = ("connectapi",)
# connectapi takes only these keywords (the handlers pass params=); get_* / download_* never take these.
CONNECTAPI_KWARGS = ("params",)
WRITE_KWARGS = ("data", "json", "files", "headers", "method")
WAIT_SLICE_S = 0.5             # a guard wait checks the lost-lease and stop flags this often


class TokenStale(jobqueue.Defer):
    """The stored full-mode token is missing, unreadable or about to expire: wait for the app to publish one."""

    def __init__(self):
        super().__init__(STALE_WAIT_S, TOKEN_STALE)


class ProfileMissing(jobqueue.Defer):
    """The client has no display name (the profile call named none): garminconnect would build URLs with
    "None" in them, so no data call is made and the job waits (nothing is stored, no day is marked final)."""

    def __init__(self):
        super().__init__(PROFILE_WAIT_S, PROFILE_MISSING)


# --- the sealed token ---------------------------------------------------------------------------------------------

def parse_token_key(text) -> bytes:
    """HISTORY_TOKEN_KEY as 32 bytes. ValueError(ERR_TOKEN_KEY) for anything else (the value is never shown)."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError(ERR_TOKEN_KEY)
    raw = text.strip()
    for decode in (base64.b64decode, base64.urlsafe_b64decode):
        try:
            key = decode(raw + "=" * (-len(raw) % 4))
        except (binascii.Error, ValueError):
            continue
        if len(key) == 32:
            if feature_config.weak_key(key):
                break           # not random (see feature_config.weak_key): refused like a wrong length
            return key
    raise ValueError(ERR_TOKEN_KEY)


def seal(key: bytes, plaintext: bytes) -> tuple:
    """(nonce, ciphertext) with AES-256-GCM; a fresh random nonce each time."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = os.urandom(NONCE_BYTES)
    return nonce, AESGCM(key).encrypt(nonce, plaintext, AAD)


def unseal(key: bytes, nonce: bytes, sealed: bytes) -> Optional[bytes]:
    """The plaintext, or None when the key is wrong or the blob was changed (never raises on bad data)."""
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    try:
        return AESGCM(key).decrypt(bytes(nonce), bytes(sealed), AAD)
    except (InvalidTag, ValueError, TypeError):
        return None


class PgTokenStore:
    """history.garmin_token: one sealed blob. `connect` is a zero-argument connection factory (tests pass a fake)."""

    def __init__(self, dsn: str, key: bytes, *, connect: Optional[Callable] = None):
        self._key = key
        if connect is None:
            if not isinstance(dsn, str) or not dsn:
                raise ValueError("worker_garmin: DATABASE_URL is not set")

            def connect():
                return jobqueue.connect(dsn, application_name="coach-mcp-token")
        self._connect = connect

    def __repr__(self):
        return "PgTokenStore(<hidden>)"

    def _run(self, fn):
        c = self._connect()
        try:
            with c.transaction():
                return fn(c)
        finally:
            c.close()

    def migrate(self) -> None:
        def ddl(c):
            c.execute(jobqueue.MIGRATE_LOCK)
            c.execute(TOKEN_DDL)
        self._run(ddl)

    def put(self, token_text: str) -> None:
        data = token_text.encode()
        nonce, sealed = seal(self._key, data)
        digest = hashlib.sha256(data).hexdigest()[:16]
        self._run(lambda c: c.execute(_SQL_PUT, {"n": nonce, "s": sealed, "d": digest}))

    def get(self) -> Optional[str]:
        row = self._run(lambda c: c.execute(_SQL_GET).fetchone())
        if row is None:
            return None
        plain = unseal(self._key, row[0], row[1])
        if plain is None:
            return None
        try:
            return plain.decode()
        except UnicodeDecodeError:
            return None


def delete_stored_token(dsn, *, connect: Optional[Callable] = None) -> bool:
    """Delete the sealed token row (history.garmin_token) when the table exists: the app calls this when it starts
    with HISTORY_SEARCH off, or on in key mode, so a Garmin login is never left in the database. True when a row was deleted. Never
    raises: an error logs one line with its class name."""
    try:
        if connect is None:
            if not isinstance(dsn, str) or not dsn:
                return False

            def connect():
                return jobqueue.connect(dsn, application_name="coach-mcp-token")
        c = connect()
        try:
            with c.transaction():
                row = c.execute(_SQL_TOKEN_TABLE).fetchone()
                if row is None or row[0] is None:
                    return False
                cur = c.execute(_SQL_DELETE)
                deleted = (getattr(cur, "rowcount", 0) or 0) > 0
        finally:
            c.close()
    except Exception as exc:  # noqa: BLE001 - logged by class name only (the error may quote the DSN)
        log.warning("history token: the stored Garmin token could not be deleted (%s)", type(exc).__name__)
        return False
    if deleted:
        log.info("history token: deleted the stored Garmin token (history search is off)")
    return deleted


# --- app side: publish the token ----------------------------------------------------------------------------------

class TokenPublisher:
    """In the app (full mode, history on): copies the Garmin token file to the store when it changes.

    publish_once() reads <token_dir>/garmin_tokens.json; it calls `refresh` first when given (the app's refresh
    of its own client, under its refresh lock, so an idle app still keeps the stored token fresh). start() runs it
    every PUBLISH_EVERY_S in a daemon thread. Errors log one fixed line (the class name), never the token."""

    def __init__(self, store, token_dir: str, *, refresh: Optional[Callable[[], None]] = None,
                 every_s: float = PUBLISH_EVERY_S):
        self._store = store
        self._path = os.path.join(token_dir, TOKEN_FILE)
        self._refresh = refresh
        self._every = every_s
        self._last_digest = None
        self._stop = threading.Event()
        self._thread = None

    def publish_once(self) -> bool:
        """True when a new token was stored."""
        if self._refresh is not None:
            try:
                self._refresh()
            except Exception as exc:  # noqa: BLE001 - logged by class name only
                log.warning("history token publish: refresh failed (%s)", type(exc).__name__)
        try:
            with open(self._path, encoding="utf-8") as fh:
                text = fh.read(TOKEN_MAX + 1)
        except OSError:
            return False
        if not text or len(text) > TOKEN_MAX:
            return False
        digest = hashlib.sha256(text.encode()).hexdigest()
        if digest == self._last_digest:
            return False
        try:
            self._store.put(text)
        except Exception as exc:  # noqa: BLE001
            log.warning("history token publish: store failed (%s)", type(exc).__name__)
            return False
        self._last_digest = digest
        log.info("history token publish: stored a new Garmin token for the worker")
        return True

    def start(self) -> None:
        if self._thread is not None:
            return

        def loop():
            while not self._stop.is_set():
                self.publish_once()
                self._stop.wait(self._every)
        self._thread = threading.Thread(target=loop, name="history-token-publish", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()


# --- worker side: full mode -----------------------------------------------------------------------------------------

def _no_refresh():
    raise TokenStale()


def _garminconnect_factory():
    import garminconnect

    return garminconnect.Garmin()


class StoredTokenGarmin:
    """A garminconnect.Garmin built from the stored token, with its own refresh turned into TokenStale.

    ready() (no Garmin call) loads or reloads the token when needed; the first data call of each new client also
    loads the profile (one Garmin call) because garminconnect builds most URLs from the display name."""

    PROFILE_PATH = "/userprofile-service/socialProfile"

    def __init__(self, load_text: Callable[[], Optional[str]], *, factory: Callable = _garminconnect_factory):
        self._load = load_text
        self._factory = factory
        self._garmin = None
        self._profile_loaded = False
        self._lock = threading.Lock()

    def __repr__(self):
        return "StoredTokenGarmin(<hidden>)"

    def _expiring(self, garmin) -> bool:
        try:
            return bool(garmin.client._token_expires_soon())
        except Exception:  # noqa: BLE001 - an unreadable token counts as expiring
            return True

    def ready(self):
        """The client, ready for a call. Raises TokenStale when no usable token is stored."""
        with self._lock:
            if self._garmin is not None and not self._expiring(self._garmin):
                return self._garmin
            # A new client has no display name yet: its profile is loaded again (as its own guarded call).
            self._garmin = None
            self._profile_loaded = False
            try:
                text = self._load()
            except Exception as exc:  # noqa: BLE001
                log.warning("worker garmin: cannot read the stored token (%s)", type(exc).__name__)
                raise TokenStale() from None
            if not text:
                raise TokenStale()
            garmin = self._factory()
            try:
                garmin.client.loads(text)
            except Exception:  # noqa: BLE001 - the library's message may quote the token
                raise TokenStale() from None
            if self._expiring(garmin):
                raise TokenStale()
            garmin.client._refresh_session = _no_refresh   # the app owns the login
            self._garmin = garmin
            return garmin

    def profile_pending(self) -> bool:
        with self._lock:
            return not self._profile_loaded or self._garmin is None

    def load_profile(self) -> None:
        """The profile call (one Garmin call); GuardedGarmin guards it as a call of its own. ProfileMissing when
        the answer names no display name (the profile stays pending, so the next job tries again)."""
        garmin = self.ready()
        prof = garmin.client.connectapi(self.PROFILE_PATH)
        if not (isinstance(prof, dict) and isinstance(prof.get("displayName"), str) and prof["displayName"]):
            raise ProfileMissing()
        garmin.display_name = prof["displayName"]
        garmin.full_name = ""
        with self._lock:
            if self._garmin is garmin:
                self._profile_loaded = True

    def call(self, name, *args, **kwargs):
        garmin = self.ready()
        if self.profile_pending():
            self.load_profile()
            garmin = self.ready()
        if not getattr(garmin, "display_name", None):
            raise ProfileMissing()   # never call with display_name None (URLs would read .../None)
        return getattr(garmin, name)(*args, **kwargs)


# --- the guard around every call ------------------------------------------------------------------------------------

def _is_guard_block(exc) -> bool:
    return type(exc).__name__ == "GuardBlocked"


def is_read_method(name) -> bool:
    """The Garmin methods a worker may call: reads only (no upload, delete, add or set)."""
    return isinstance(name, str) and (name in READ_METHODS or name.startswith(READ_PREFIXES))


def _get_only(name, args, kwargs) -> None:
    """Refused before the guard (PermissionError): connectapi with anything but (path, params=...), so no other
    method, body or header (X-HTTP-Method-Override) can pass; and get_* / download_* with a keyword that would
    carry a body, headers or a method (WRITE_KWARGS)."""
    if name == "connectapi":
        if len(args) > 1 or any(k not in CONNECTAPI_KWARGS for k in kwargs):
            raise PermissionError("the history worker only reads from Garmin (GET)")
        return
    if any(k in WRITE_KWARGS for k in kwargs):
        raise PermissionError("the history worker only reads from Garmin (GET)")


class _Core:
    """GuardedGarmin's state and its guard loop, kept off the public surface (GuardedGarmin hides every name
    that starts with "_", so a handler cannot reach the inner client)."""

    def __init__(self, inner, guard, caller, clock, sleep, lost, stop):
        self.inner = inner
        self.guard = guard
        self.caller = caller
        self.clock = clock
        self.sleep = sleep
        self.lost = lost
        self.stop = stop

    def ending(self) -> bool:
        return any(ev is not None and ev.is_set() for ev in (self.lost, self.stop))

    def wait(self, seconds: float) -> None:
        """Wait `seconds`, cut short within WAIT_SLICE_S when the job loses its lease or the worker stops."""
        if self.sleep is not None:
            self.sleep(seconds)
            return
        end = time.monotonic() + seconds
        while not self.ending():
            left = end - time.monotonic()
            if left <= 0:
                return
            step = min(left, WAIT_SLICE_S)
            if self.stop is not None:
                self.stop.wait(step)
            elif self.lost is not None:
                self.lost.wait(step)
            else:
                time.sleep(step)

    def admit(self) -> None:
        """guard.before_call, waiting out a short bucket or live-call refusal (at most INLINE_WAIT_MAX_S each,
        INLINE_WAIT_TOTAL_S in all) unless the job lost its lease or the worker stops; else GuardBlocked."""
        waited = 0.0
        while True:
            try:
                self.guard.before_call(self.caller)
                return
            except Exception as exc:
                if not _is_guard_block(exc) or getattr(exc, "group", None) not in INLINE_WAIT_GROUPS:
                    raise
                try:
                    wait = float(exc.until) - self.clock()
                except (AttributeError, TypeError, ValueError):
                    raise exc from None
                if wait > INLINE_WAIT_MAX_S or waited + max(wait, 0.0) > INLINE_WAIT_TOTAL_S or self.ending():
                    raise
                blocked = exc
            wait = max(wait, 0.05)
            self.wait(wait)
            waited += wait
            if self.ending():
                raise blocked    # the job lost its lease or the worker stops: the worker loop takes it

    def guarded(self, fn):
        self.admit()
        try:
            out = fn()
        except jobqueue.Defer:
            raise
        except Exception as exc:
            if not _is_guard_block(exc):
                group = tool_errors.classify_exception(exc)[0]
                self.guard.after_outcome(self.caller, group)
            raise
        self.guard.after_outcome(self.caller, None)
        return out


def _core(obj) -> _Core:
    return object.__getattribute__(obj, "_core")


class GuardedGarmin:
    """The client handlers use: the read methods (get_*, download_*, connectapi with GET) pass the guard first;
    any other name, and every name that starts with "_", raises AttributeError. for_job(lost=, stop=) gives a
    copy for one job: its short guard waits end early when the job lost its lease or the worker stops."""

    __slots__ = ("_core",)

    def __init__(self, inner, guard, caller: str = "worker", *, clock: Callable[[], float] = time.time,
                 sleep: Optional[Callable[[float], None]] = None, lost=None, stop=None):
        object.__setattr__(self, "_core", _Core(inner, guard, caller, clock, sleep, lost, stop))

    def __getattribute__(self, name):
        if name.startswith("_") and not (name.startswith("__") and name.endswith("__")):
            raise AttributeError(name)
        return object.__getattribute__(self, name)

    def __setattr__(self, name, value):
        raise AttributeError(name)

    def __repr__(self):
        return "GuardedGarmin(<hidden>)"

    def for_job(self, *, lost=None, stop=None) -> "GuardedGarmin":
        c = _core(self)
        return GuardedGarmin(c.inner, c.guard, c.caller, clock=c.clock, sleep=c.sleep, lost=lost, stop=stop)

    def __getattr__(self, name):
        if not is_read_method(name):
            raise AttributeError(name)
        core = _core(self)
        inner = core.inner

        def call(*args, **kwargs):
            _get_only(name, args, kwargs)
            ready = getattr(inner, "ready", None)
            if callable(ready):
                ready()          # TokenStale before a token is taken
            if isinstance(inner, StoredTokenGarmin):
                if inner.profile_pending():
                    core.guarded(inner.load_profile)     # its own call, its own token
                return core.guarded(lambda: inner.call(name, *args, **kwargs))
            return core.guarded(lambda: getattr(inner, name)(*args, **kwargs))

        call.__name__ = name
        return call


def inner_client(client):
    """The client a GuardedGarmin wraps (tests and diagnostics; handlers never get it)."""
    return _core(client).inner


# --- building it from the environment ---------------------------------------------------------------------------------

def _key_cache(environ) -> str:
    import key_mode

    path = key_mode.cache_path(environ)
    if path is not None:
        folder = os.path.dirname(path)
        try:
            os.makedirs(folder, mode=0o700, exist_ok=True)
            if os.access(folder, os.W_OK):
                return path
        except OSError:
            pass
    return os.path.join(tempfile.gettempdir(), key_mode.CACHE_DIR_NAME, "token.json")


def build(environ, guard, *, dsn: Optional[str] = None, token_store=None):
    """The worker's guarded Garmin client for GARMIN_AUTH in `environ` (os.environ in production: take_key removes
    the key from it). Raises ValueError with a fixed line on a bad setting."""
    import key_mode

    mode = key_mode.auth_mode(environ)
    if mode == key_mode.MODE_KEY:
        cache = _key_cache(environ)
        errors = key_mode.check({**environ, key_mode.CACHE_ENV: cache})
        if errors:
            raise ValueError(errors[0])
        key = key_mode.take_key(environ)
        tool_errors.register_secret(key)
        key_mode.prepare_cache(cache)
        tokens = key_mode.TokenManager(key, cache, authenticator=key_mode.library_authenticator(cache))
        del key
        inner = key_mode.KeyModeGarmin(tokens, key_mode._LibraryHttp())
        log.info("worker garmin: key mode")
    elif mode == key_mode.MODE_FULL:
        if token_store is None:
            token_key = parse_token_key(environ.get(TOKEN_KEY_ENV))
            token_store = PgTokenStore(dsn, token_key)
        inner = StoredTokenGarmin(token_store.get)
        log.info("worker garmin: full mode, token from the app (sealed in Postgres)")
    else:
        raise ValueError(key_mode.ERR_AUTH_UNSET if not environ.get(key_mode.AUTH_ENV) else key_mode.ERR_AUTH_INVALID)
    return GuardedGarmin(inner, guard)
