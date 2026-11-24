"""Key mode (GARMIN_AUTH=key): read Garmin through an official Garmin Personal API key.

GARMIN_AUTH is required, full or key, with no default. In key mode the server never logs in to
Garmin with an email and password: a Personal API key (GARMIN_PERSONAL_API_KEY) becomes a 24-hour access token,
cached at GARMIN_KEY_TOKEN_CACHE, and KeyModeGarmin stands in for the garminconnect client the tools
already call, backed by Garmin's own client (garmin-connect-personal-api-python). Upstream's tools and
this repository's status tools run unchanged on it; every name it does not implement raises NotInKeyMode (an
AttributeError), so key mode fails closed.

What launch.py calls, in this order:
- auth_mode(environ): "full", "key", or None when GARMIN_AUTH is unset, empty or neither;
- check(environ): the startup error lines of the mode and key settings, the GARMIN_ENABLED_TOOLS line aside
  (that one is the tool policy's, with KEY_MODE_TOOLS and ERR_TOOL_NOT_IN_KEY_MODE);
- cache_path(environ): the token cache file, or None when GARMIN_KEY_TOKEN_CACHE breaks the path rule;
- take_key(environ): the key, removed from the environment with GARMIN_PERSONAL_API_BASE_URL, and registered
  with tool_errors.register_secret() so no log line can carry it;
- install(key, cache_file): replaces garmin_mcp.init_api (upstream's login hook) with key mode's login;
- selfcheck(): the Docker build's check that Garmin's client and upstream's hook are still what this file needs.

The key and its tokens never reach a log line, an exception text or a client text: every key-mode
exception carries a fixed text and is raised `from None`, outside any `except` block; an HttpError is read only
for its status code. Garmin's client and requests are imported only inside key-mode code paths, so this module
imports on Python 3.10 with the standard library and tool_errors only, and full mode never loads the client.
"""

import datetime
import hashlib
import json
import logging
import math
import os
import re
import stat
import tempfile
import threading
import time

import tool_errors

log = logging.getLogger("garmin_ext.key_mode")

# --- names and constants -------------------------------------------------------------------------------------

AUTH_ENV = "GARMIN_AUTH"                       # values: "full" | "key"
MODE_FULL = "full"
MODE_KEY = "key"
KEY_ENV = "GARMIN_PERSONAL_API_KEY"
CACHE_ENV = "GARMIN_KEY_TOKEN_CACHE"           # internal; the Dockerfile sets /data/garmin-key/token.json
CACHE_DIR_NAME = "garmin-key"                  # the cache file's folder must have this name
DEFAULT_CACHE = "~/.config/garmin-key/token.json"   # only when CACHE_ENV is unset (outside the image)
BASE_URL_ENV = "GARMIN_PERSONAL_API_BASE_URL"  # refused, and removed from os.environ in key mode
IS_CN_ENV = "GARMIN_IS_CN"
OFFICIAL_BASE_URL = "https://personal.connectapi.garmin.com"
KEY_MIN, KEY_MAX = 20, 512                     # shorter is a paste that did not land, not a key
KEY_RE = re.compile(r"[!-~]{20,512}")          # fullmatch, after strip(); KEY_MIN to KEY_MAX characters
_KEY_CHARS_RE = re.compile(r"[!-~]+")          # a key's characters, any length
TOKEN_RE = re.compile(r"[A-Za-z0-9_-]+={0,2}(?:\.[A-Za-z0-9_-]+={0,2}){2}")   # fullmatch, and 20 <= len <= 8192
TOKEN_MIN, TOKEN_MAX = 20, 8192
REFUSED_LATCH_S = 900
REFRESH_WINDOW_S = 3600                        # a token with less left is replaced (the library's own window)
SHORT_TOKEN_S = 23 * 3600                      # a new token that lasts less: the key itself expires then
HELD_MARGIN_S = 300                            # a failed refresh keeps the held token while more than this is left
HELD_RETRY_S = 60                              # ... and asks Garmin again only after this long
KEY_MODE_TOOLS = frozenset({
    "get_activities", "get_activities_by_date", "get_activity", "get_activity_fit_data",
    "get_stats", "get_sleep_summary", "get_sleep_summary_range", "get_hrv_trend", "get_body_battery",
    "get_heart_rate_zones", "get_vo2max_trend", "get_daily_status", "get_weekly_status"})

# --- config error lines (4.2; printed by launch as "launch: config error: <line>"; never a secret value) -------

ERR_AUTH_UNSET = ("GARMIN_AUTH is not set: set it to full (log in to Garmin once with your email and password; "
                  "every tool) or key (a Garmin Personal API key; fewer tools and less data), then restart the server")
ERR_AUTH_INVALID = "GARMIN_AUTH must be full or key; the value set is neither (it is not shown)"
ERR_KEY_MISSING = ("GARMIN_AUTH=key needs GARMIN_PERSONAL_API_KEY: make a Personal API key in Garmin Connect on the "
                   "web and set it as this variable")
ERR_KEY_INVALID = ("GARMIN_PERSONAL_API_KEY has spaces, line breaks or other characters a key cannot have, or is "
                   "longer than 512 characters: copy the key again from Garmin Connect")
ERR_KEY_SHORT = ("GARMIN_PERSONAL_API_KEY is shorter than 20 characters, so it is not a whole key: copy the key again "
                 "from Garmin Connect")
ERR_KEY_UNUSED = ("GARMIN_PERSONAL_API_KEY is set but GARMIN_AUTH=full does not use it: delete the variable or leave "
                  "it empty, or set GARMIN_AUTH=key")
ERR_BASE_URL = ("GARMIN_PERSONAL_API_BASE_URL is not supported: this server only talks to Garmin's own host; delete "
                "the variable")
ERR_IS_CN = "GARMIN_IS_CN is not supported with GARMIN_AUTH=key; delete it, or set GARMIN_AUTH=full"
# The tool policy's line (launch.resolve_tools with available=KEY_MODE_TOOLS); <name> comes from launch._shown().
ERR_TOOL_NOT_IN_KEY_MODE = ("GARMIN_ENABLED_TOOLS: {name} is not available with GARMIN_AUTH=key; remove it, or set "
                            "GARMIN_AUTH=full")
ERR_CACHE_PATH = ("GARMIN_KEY_TOKEN_CACHE must be an absolute path to a .json file in a folder named garmin-key (the "
                  "value is not shown); the image sets /data/garmin-key/token.json, so delete the variable")
ERR_CACHE_WRITE = "GARMIN_KEY_TOKEN_CACHE: cannot write {folder} ({strerror}); mount a writable volume at /data"

# --- exception texts; the tool_errors table reads them --------------------------------------------------------

TEXT_KEY_REFUSED = "Garmin authentication failed: Personal API key refused (HTTP {code})"
TEXT_TOKEN_REFUSED = "Garmin authentication failed: Personal API token refused (HTTP 401)"
TEXT_RATE_LIMITED = "Garmin rate limit hit (HTTP 429)"
TEXT_NOT_FOUND = "no data from Garmin (HTTP 404)"
TEXT_ACTIVITY_FORBIDDEN = "no data from Garmin (HTTP 403 for this activity)"
TEXT_EMPTY_FILE = "no data from Garmin (empty activity file)"
TEXT_EMPTY_ANSWER = "no data from Garmin (empty answer)"   # get_stats on an empty day, as garminconnect raises
TEXT_BAD_REQUEST = "Garmin rejected the request (api error 400)"
TEXT_REQUEST_FAILED = "Garmin Connect request failed (api error {code})"
TEXT_NO_ANSWER = "Garmin Connect did not answer (HTTP {code})"
TEXT_BAD_TOKEN = "Garmin Connect did not answer: unusable token answer"
TEXT_FAILED_TYPE = "Garmin Connect did not answer: request failed ({type})"
TEXT_NOT_IN_KEY_MODE = "not available with a Garmin Personal API key: {name}"

# --- log lines (4.11; logger garmin_ext.key_mode; never the key or a token) ------------------------------------

LOG_ON = "garmin key mode: on, token cache %s"
LOG_NEW_TOKEN = "garmin key mode: new access token, valid until %s"
LOG_CACHED_TOKEN = "garmin key mode: access token from the cache, valid until %s"
LOG_TOKEN_REFUSED = "garmin key mode: Garmin refused the access token (HTTP 401); asking for a new one"
LOG_KEY_EXPIRES = ("garmin key mode: the Personal API key expires at %s; make a new key in Garmin Connect before "
                   "then")
LOG_KEY_REFUSED = ("garmin key mode: Garmin refused the Personal API key (HTTP %d); make a new key in Garmin Connect, "
                   "set GARMIN_PERSONAL_API_KEY and restart")
LOG_FIRST_FAILED = "garmin key mode: the first token request failed (%s); tool calls will try again"
LOG_FAILED = "garmin key mode: token request failed (%s)"
LOG_HELD = "garmin key mode: using the held access token, valid until %s, until a token request works"
LOG_MODE_FIXED = "garmin key mode: token cache file mode fixed to 0600"

# Upstream's login hook: main() must still log in through this call for install() to take over.
HOOK_TEXT = "init_api(email, password)"


# --- exceptions ----------------------------------------------------------------------------------------------

class KeyModeError(tool_errors.KnownFailure):
    """Base of key mode's failures: one fixed message argument, so upstream's copy.copy() keeps type and text."""


class KeyModeLoginError(KeyModeError):
    """Garmin refused the key (token endpoint 400/401/403, or the 15-minute latch) or a fresh token: group login."""


class KeyModeRateLimited(KeyModeError):
    """Any 429: group rate_limited."""


class KeyModeNoData(KeyModeError):
    """404, 403 on an activity or its file, an empty activity file or an empty summary: group no_data."""


class KeyModeBadRequest(KeyModeError):
    """A data 400: group bad_input (GARMIN_400_DETAIL)."""


class KeyModeGarminError(KeyModeError):
    """403 elsewhere, 5xx, other 4xx, an unusable token, any other requests error: group unavailable."""


class NotInKeyMode(AttributeError):
    """A garminconnect name key mode does not implement. An AttributeError, so getattr(client, name, None) is None;
    no tool_errors row matches its text, so a tool that still calls it gets an internal error (fails closed)."""


_SHOWN_JUNK_RE = re.compile(r"[^A-Za-z0-9_./-]")


def _not_in_key_mode(name):
    text = _SHOWN_JUNK_RE.sub("", str(name).split("?")[0])[:60] or "?"
    return NotInKeyMode(TEXT_NOT_IN_KEY_MODE.format(name=text))


# --- configuration -------------------------------------------------------------------------------------------

def _value(environ, name):
    """The variable's value stripped of surrounding spaces; "" when unset ("set" = non-empty after stripping)."""
    raw = environ.get(name)
    return raw.strip() if isinstance(raw, str) else ""


def auth_mode(environ):
    """MODE_FULL or MODE_KEY from GARMIN_AUTH (spaces and case ignored); None when it is unset, empty or neither."""
    mode = _value(environ, AUTH_ENV).lower()
    return mode if mode in (MODE_FULL, MODE_KEY) else None


def key_ok(key):
    """True when `key` (already stripped) passes the key check: 20-512 printable ASCII characters, no space."""
    return isinstance(key, str) and KEY_RE.fullmatch(key) is not None


def key_too_short(key):
    """True when `key` (already stripped) has only a key's characters but fewer than KEY_MIN of them."""
    return isinstance(key, str) and len(key) < KEY_MIN and _KEY_CHARS_RE.fullmatch(key) is not None


def token_ok(token):
    """True when `token` can go into an Authorization header: TOKEN_RE (three base64url parts), 20-8,192 chars."""
    return (isinstance(token, str) and TOKEN_MIN <= len(token) <= TOKEN_MAX
            and TOKEN_RE.fullmatch(token) is not None)


def _path_ok(path):
    if not isinstance(path, str) or not path or not os.path.isabs(path):
        return False
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in path):
        return False
    return (os.path.normpath(path) == path and path.endswith(".json") and len(os.path.basename(path)) > 5
            and os.path.basename(os.path.dirname(path)) == CACHE_DIR_NAME)


def cache_path(environ):
    """The token cache file: GARMIN_KEY_TOKEN_CACHE, or ~/.config/garmin-key/token.json when it is unset or empty.
    None when the path breaks the path rule: absolute, normalised (no .., ., // or trailing /), a .json file, in
    a folder named garmin-key. So key mode can only ever create or chmod a folder named garmin-key."""
    raw = environ.get(CACHE_ENV)
    if isinstance(raw, str) and raw.strip():
        path = raw
    else:
        path = os.path.expanduser(DEFAULT_CACHE)
    return path if _path_ok(path) else None


def _make_private_folder(folder):
    os.makedirs(folder, mode=0o700, exist_ok=True)
    os.chmod(folder, 0o700)


def check_cache_writable(path):
    """[] when the cache file's folder can be created (0700) and written, else [the ERR_CACHE_WRITE line]."""
    folder = os.path.dirname(path)
    try:
        _make_private_folder(folder)
        fd, probe = tempfile.mkstemp(dir=folder, prefix=".write-check-", suffix=".tmp")
        os.close(fd)
        os.unlink(probe)
    except OSError as exc:
        why = exc.strerror if isinstance(exc.strerror, str) and exc.strerror else type(exc).__name__
        return [ERR_CACHE_WRITE.format(folder=folder, strerror=why)]
    return []


def check(environ):
    """The startup error lines of GARMIN_AUTH and the key-mode settings, in the order of 4.2 (empty: all fine).
    No line ever holds a value: a pasted key must not reach the log. The GARMIN_ENABLED_TOOLS line is the tool
    policy's (ERR_TOOL_NOT_IN_KEY_MODE). In key mode this creates the cache folder (0700), as the server would."""
    errors = []
    raw_mode = _value(environ, AUTH_ENV)
    mode = auth_mode(environ)
    if not raw_mode:
        errors.append(ERR_AUTH_UNSET)
    elif mode is None:
        errors.append(ERR_AUTH_INVALID)
    key = _value(environ, KEY_ENV)
    if mode == MODE_KEY:
        if not key:
            errors.append(ERR_KEY_MISSING)
        elif key_too_short(key):
            errors.append(ERR_KEY_SHORT)
        elif not key_ok(key):
            errors.append(ERR_KEY_INVALID)
    elif mode == MODE_FULL and key:
        errors.append(ERR_KEY_UNUSED)
    if _value(environ, BASE_URL_ENV):
        errors.append(ERR_BASE_URL)
    if mode == MODE_KEY and _value(environ, IS_CN_ENV).lower() in ("true", "1", "yes"):
        errors.append(ERR_IS_CN)
    if mode == MODE_KEY:
        path = cache_path(environ)
        if path is None:
            errors.append(ERR_CACHE_PATH)
        else:
            errors += check_cache_writable(path)
    return errors


def take_key(environ=None):
    """The key (stripped; "" when unset), after removing GARMIN_PERSONAL_API_KEY and GARMIN_PERSONAL_API_BASE_URL
    from `environ` (os.environ by default) and registering the key with tool_errors.register_secret(), so no
    child process inherits it and no log line can carry it."""
    environ = os.environ if environ is None else environ
    raw = environ.pop(KEY_ENV, None)
    environ.pop(BASE_URL_ENV, None)
    key = raw.strip() if isinstance(raw, str) else ""
    if key:
        tool_errors.register_secret(key)
    return key


def prepare_cache(path):
    """The cache folder 0700 (created or fixed); an existing cache file wider than 0600 chmodded to 0600."""
    _make_private_folder(os.path.dirname(path))
    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)
    except FileNotFoundError:
        return
    if mode & 0o077:
        os.chmod(path, 0o600)
        log.info(LOG_MODE_FIXED)


def _delete_cache(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def _cached_access_token(path):
    """The access token in the cache file, or None (used only to tell a cached token from a new one)."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    token = data.get("access_token") if isinstance(data, dict) else None
    return token if isinstance(token, str) else None


# --- failures from Garmin's client, read without keeping their text ------------------------------------------

_TYPE_JUNK_RE = re.compile(r"[^A-Za-z]")


class _Failure:
    """What a caught exception was, read inside `except` from its type and status code only. `exc` is kept only
    for the two kinds raised again: "network" (requests' ConnectionError and Timeout: their text holds the URL
    only) and "other" (a bug, raised with its chain cut)."""

    __slots__ = ("kind", "status", "type_name", "exc")

    def __init__(self, kind, status=None, type_name="", exc=None):
        self.kind, self.status, self.type_name, self.exc = kind, status, type_name, exc


def _describe(exc):
    try:
        mro = type(exc).__mro__
        names = {getattr(cls, "__name__", "") for cls in mro}
        requests_names = {getattr(cls, "__name__", "") for cls in mro
                          if str(getattr(cls, "__module__", "") or "").split(".")[0] == "requests"}
        type_name = (_TYPE_JUNK_RE.sub("", type(exc).__name__) or "Error")[:40]
    except Exception:  # noqa: BLE001 - an odd class: treat it as a bug
        return _Failure("other", exc=exc)
    if "HttpError" in names:
        status = getattr(exc, "status_code", None)
        if type(status) is int:
            return _Failure("http", status=status)
    if "GarminPersonalApiError" in names:
        try:
            message = str(exc)
        except Exception:  # noqa: BLE001
            message = ""
        if "was empty" in message:
            return _Failure("empty_file")
        if "exp claim" in message:
            return _Failure("no_exp")
        return _Failure("library", type_name="GarminPersonalApiError")
    if requests_names & {"ConnectionError", "Timeout"}:
        return _Failure("network", exc=exc)
    if requests_names:
        return _Failure("requests", type_name=type_name)
    return _Failure("other", exc=exc)


def _code(status):
    return status if 100 <= status <= 599 else 0


def _other(failure):
    """The bug exception of an "other" failure, its chain cut: no linked HttpError or requests error (whose texts
    hold Garmin's answer or a header) can reach a rendered traceback."""
    exc = failure.exc
    try:
        exc.__cause__ = None
        exc.__context__ = None
        exc.__suppress_context__ = True
    except Exception:  # noqa: BLE001
        return RuntimeError("key mode: unexpected error in Garmin's client")
    return exc


def _data_failure(failure, activity):
    """The exception for a failed data request (by HTTP status or failure kind), never raised here."""
    kind, status = failure.kind, failure.status
    if kind == "http":
        if status == 429:
            return KeyModeRateLimited(TEXT_RATE_LIMITED)
        if status == 404:
            return KeyModeNoData(TEXT_NOT_FOUND)
        if status == 403:
            return KeyModeNoData(TEXT_ACTIVITY_FORBIDDEN) if activity else KeyModeGarminError(
                TEXT_REQUEST_FAILED.format(code=403))
        if status == 400:
            return KeyModeBadRequest(TEXT_BAD_REQUEST)
        if 500 <= status <= 599:
            return KeyModeGarminError(TEXT_REQUEST_FAILED.format(code=status))
        return KeyModeGarminError(TEXT_NO_ANSWER.format(code=_code(status)))
    if kind == "empty_file":
        return KeyModeNoData(TEXT_EMPTY_FILE)
    if kind == "no_exp":
        return KeyModeGarminError(TEXT_BAD_TOKEN)
    if kind in ("library", "requests"):
        return KeyModeGarminError(TEXT_FAILED_TYPE.format(type=failure.type_name))
    if kind == "network":
        return failure.exc
    return _other(failure)


def _token_failure(failure):
    """The exception for a failed token request (refused keys are handled by the caller)."""
    kind, status = failure.kind, failure.status
    if kind == "http":
        if status == 429:
            return KeyModeRateLimited(TEXT_RATE_LIMITED)
        if 500 <= status <= 599:
            return KeyModeGarminError(TEXT_REQUEST_FAILED.format(code=status))
        return KeyModeGarminError(TEXT_NO_ANSWER.format(code=_code(status)))
    if kind in ("no_exp", "empty_file"):
        return KeyModeGarminError(TEXT_BAD_TOKEN)
    return _data_failure(failure, False)


def _group(exc):
    try:
        return tool_errors.classify_exception(exc)[0]
    except Exception:  # noqa: BLE001 - classify_exception never raises; a log word must not fail a call
        return tool_errors.INTERNAL


def _when(epoch):
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%MZ")


def _expiry_ok(expires_at):
    if isinstance(expires_at, bool) or not isinstance(expires_at, (int, float)):
        return False
    return math.isfinite(expires_at) and 0 < expires_at < 32503680000  # before the year 3000


# --- token manager -------------------------------------------------------------------------------------------

class TokenManager:
    """Holds the access token for `key`; one lock around every token request.

    `authenticator` has obtain_token(api_key) -> an object with access_token and expires_at (Garmin's
    PersonalApiAuthenticator, which reads `cache_file` first and writes it 0600). `clock` gives epoch seconds.
    """

    def __init__(self, key, cache_file, *, authenticator, clock=time.time):
        self._key = key
        self._cache = cache_file
        self._auth = authenticator
        self._clock = clock
        self._lock = threading.Lock()
        self._token = None
        self._expires = 0.0
        self._refused_until = None
        self._refused_code = 0
        self._retry_at = 0.0
        self._holding = False

    def token(self, *, first=False):
        """The access token: the one held while more than 1 h is left, else one from the cache file or Garmin.
        When that request fails for any reason but a refused key, the held token is returned while more than
        HELD_MARGIN_S is left, and Garmin is asked again only HELD_RETRY_S later (LOG_HELD once).
        `first` marks the startup request (its failure logs "the first token request failed"). Raises
        KeyModeLoginError (refused key, also for 15 minutes after a refusal, without a request),
        KeyModeRateLimited, KeyModeGarminError, or requests' ConnectionError or Timeout unchanged."""
        with self._lock:
            now = self._clock()
            if self._token is not None and self._expires - now > REFRESH_WINDOW_S:
                return self._token
            if now < self._retry_at and self._held(now):
                return self._token
            if self._refused_until is not None and now < self._refused_until:
                raise KeyModeLoginError(TEXT_KEY_REFUSED.format(code=self._refused_code)) from None
            return self._obtain(now, first)

    def _held(self, now):
        if self._token is None:  # after a restart: the token Garmin's client cached for this key, if still good
            self._adopt_cached()
        return self._token is not None and self._expires - now > HELD_MARGIN_S

    def _adopt_cached(self):
        """Take the cache file's token as the held one, when the file is the one Garmin's client wrote for this
        key and base URL (its fingerprint) and the token and expiry are well formed. A token dropped after a data
        401 is never taken: refused() deletes the file."""
        base = getattr(self._auth, "base_url", None)
        if not isinstance(base, str):
            return
        try:
            with open(self._cache, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        fingerprint = hashlib.sha256(f"{base}\0{self._key}".encode()).hexdigest()
        token, expires = data.get("access_token"), data.get("expires_at")
        if data.get("fingerprint") == fingerprint and token_ok(token) and _expiry_ok(expires):
            self._token, self._expires = token, float(expires)

    def _keep_held(self, now):
        """A refresh failed (not a refused key) while the held token is still good: keep it, retry later."""
        self._retry_at = now + HELD_RETRY_S
        if not self._holding:
            self._holding = True
            log.warning(LOG_HELD, _when(self._expires))
        return self._token

    def _obtain(self, now, first):
        before = _cached_access_token(self._cache)
        failure = None
        result = None
        try:
            result = self._auth.obtain_token(self._key)
        except Exception as exc:  # noqa: BLE001 - mapped below, outside this block
            failure = _describe(exc)
        if failure is not None:
            if failure.kind == "http" and failure.status in (400, 401, 403):
                self._refused_until = now + REFUSED_LATCH_S
                self._refused_code = failure.status
                self._holding = False
                log.warning(LOG_KEY_REFUSED, failure.status)
                raise KeyModeLoginError(TEXT_KEY_REFUSED.format(code=failure.status)) from None
            if failure.kind == "no_exp":
                _delete_cache(self._cache)
            error = _token_failure(failure)
            log.warning(LOG_FIRST_FAILED if first else LOG_FAILED, _group(error))
            if self._held(now):
                return self._keep_held(now)
            if error is failure.exc:
                raise error
            raise error from None
        token = getattr(result, "access_token", None)
        expires = getattr(result, "expires_at", None)
        if not token_ok(token) or not _expiry_ok(expires):
            _delete_cache(self._cache)
            log.warning(LOG_FIRST_FAILED if first else LOG_FAILED, tool_errors.UNAVAILABLE)
            if self._held(now):
                return self._keep_held(now)
            raise KeyModeGarminError(TEXT_BAD_TOKEN) from None
        expires = float(expires)
        self._refused_until = None
        self._retry_at, self._holding = 0.0, False
        if before is not None and before == token:
            log.info(LOG_CACHED_TOKEN, _when(expires))
        else:
            log.info(LOG_NEW_TOKEN, _when(expires))
            if expires - now < SHORT_TOKEN_S:
                log.warning(LOG_KEY_EXPIRES, _when(expires))
        self._token, self._expires = token, expires
        return token

    def refused(self, token):
        """A data call got 401 with `token`: drop it and delete the cache file (else Garmin's client would hand
        it back), so the next token() asks Garmin. Only the first caller with that token does this."""
        with self._lock:
            if self._token is None or self._token != token:
                return
            self._token, self._expires = None, 0.0
            self._retry_at, self._holding = 0.0, False
            _delete_cache(self._cache)
            log.info(LOG_TOKEN_REFUSED)


# --- garminconnect's validators (the same bad-input texts in both modes) -------------------------------------

def _date_format(date_str, param_name="date"):
    """garminconnect 0.3.2's _validate_date_format (used when garminconnect is not installed)."""
    if not isinstance(date_str, str):
        raise ValueError(f"{param_name} must be a string")
    date_str = date_str.strip()
    if not re.fullmatch(r"^\d{4}-\d{2}-\d{2}$", date_str):
        raise ValueError(f"{param_name} must be in format 'YYYY-MM-DD', got: {date_str}")
    try:
        datetime.datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError as e:
        raise ValueError(f"invalid {param_name}: {e}") from e
    return date_str


def _non_negative_integer(value, param_name="value"):
    """garminconnect 0.3.2's _validate_non_negative_integer."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{param_name} must be an integer")
    if value < 0:
        raise ValueError(f"{param_name} must be non-negative, got: {value}")
    return value


def _positive_integer(value, param_name="value"):
    """garminconnect 0.3.2's _validate_positive_integer."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{param_name} must be an integer")
    if value <= 0:
        raise ValueError(f"{param_name} must be a positive integer, got: {value}")
    return value


_LOCAL_VALIDATORS = (_date_format, _positive_integer, _non_negative_integer, 1000)
_VALIDATORS = None


def _validators():
    """(date, positive int, non-negative int, MAX_ACTIVITY_LIMIT): garminconnect's own when it is installed (the
    image), else the copies above (the stdlib test leg)."""
    global _VALIDATORS
    if _VALIDATORS is None:
        try:
            import garminconnect as gc

            _VALIDATORS = (gc._validate_date_format, gc._validate_positive_integer,
                           gc._validate_non_negative_integer, gc.MAX_ACTIVITY_LIMIT)
        except (ImportError, AttributeError):
            _VALIDATORS = _LOCAL_VALIDATORS
    return _VALIDATORS


def _date(value, name):
    return _validators()[0](value, name)


def _activity_id(value):
    """str(_validate_positive_integer(int(x), "activity_id")), as garminconnect's get_activity builds it."""
    return str(_validators()[1](int(value), "activity_id"))


_DIGITS_RE = re.compile(r"[0-9]{1,7}")
_ACTIVITY_TYPE_RE = re.compile(r"[A-Za-z0-9_]{1,64}")


def _count(value, name):
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value < 10_000_000:
        return str(value)
    if isinstance(value, str) and _DIGITS_RE.fullmatch(value):
        return str(int(value))
    raise ValueError(f"{name} must be an integer")


def _activity_type(value):
    if isinstance(value, str) and _ACTIVITY_TYPE_RE.fullmatch(value):
        return value
    raise ValueError("activity_type must be one of Garmin's activity type keys (letters, digits and _)")


def _query(params):
    if not params:
        return None
    return {key: ("true" if value is True else "false" if value is False else value) for key, value in params.items()}


# --- the adapter ---------------------------------------------------------------------------------------------

_ACTIVITIES_PATH = "/activitylist-service/activities/search/activities"
_METRICS_PATH = "/metrics-service/metrics/maxmet/daily"
_ZONES_PATH = "/biometric-service/heartRateZones"
_ZONES_V2_PATH = "/biometric-service/heartRateZones/v2"
_ACTIVITIES_RE = re.compile(r"/?activitylist-service/activities/search/activities")
_METRICS_RE = re.compile(r"/?metrics-service/metrics/maxmet/daily/([^/]+)/([^/]+)")
_ZONES_RE = re.compile(r"/?biometric-service/heartRateZones")
_PATH_BAD = ("..", "%", "?", "#", "//", "\\")
_SEARCH_PARAMS = frozenset({"start", "limit", "startDate", "endDate", "activityType"})


class KeyModeGarmin:
    """Stands in for garminconnect's Garmin client in key mode.

    Exactly the names of _IMPLEMENTED (each mirrors garminconnect 0.3.2's signature and input checks) and the two
    path attributes; any other name raises NotInKeyMode. A path segment or query value is built only from a
    validator's return value, never from the raw argument. `tokens` is a TokenManager; `http` has
    get(token, path, params) -> decoded JSON or None, and download(token, activity_id) -> bytes (_LibraryHttp
    over Garmin's client in the image; a fake in tests).
    """

    garmin_connect_activities = _ACTIVITIES_PATH
    garmin_connect_metrics_url = _METRICS_PATH
    _IMPLEMENTED = ("connectapi", "get_activities", "get_activity", "download_activity", "get_stats",
                    "get_user_summary", "get_sleep_data", "get_hrv_data", "get_body_battery", "get_weigh_ins",
                    "get_body_composition")

    def __init__(self, tokens, http):
        self._tokens = tokens
        self._http = http

    def __getattr__(self, name):
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        raise _not_in_key_mode(name)

    def __repr__(self):
        return "KeyModeGarmin()"

    # -- requests, one retry after a refused token, mapping outside `except`

    def _call(self, op, activity=False):
        for attempt in (1, 2):
            token = self._tokens.token()
            failure = None
            try:
                return op(token)
            except Exception as exc:  # noqa: BLE001 - mapped below, outside this block
                failure = _describe(exc)
            if failure.kind == "http" and failure.status == 401:
                if attempt == 1:
                    self._tokens.refused(token)
                    continue
                raise KeyModeLoginError(TEXT_TOKEN_REFUSED) from None
            error = _data_failure(failure, activity)
            if error is failure.exc:
                raise error
            raise error from None
        raise KeyModeLoginError(TEXT_TOKEN_REFUSED) from None  # not reached

    def _get(self, path, params=None, activity=False):
        query = _query(params)
        return self._call(lambda token: self._http.get(token, path, query), activity)

    # -- garminconnect's names

    def connectapi(self, path, **kwargs):
        """GET one of three garminconnect paths with optional params; anything else raises NotInKeyMode."""
        if set(kwargs) - {"params"} or not isinstance(path, str) or not path.isascii():
            raise _not_in_key_mode(f"connectapi {path}" if isinstance(path, str) else "connectapi")
        if any(bad in path for bad in _PATH_BAD) or path.endswith("/") or any(ch <= " " for ch in path):
            raise _not_in_key_mode(path)
        params = kwargs.get("params")
        if params is not None and not isinstance(params, dict):
            raise _not_in_key_mode(path)
        if _ACTIVITIES_RE.fullmatch(path):
            # Garmin's client gives None for an empty answer (HTTP 204): an empty list, as get_activities.
            activities = self._get(_ACTIVITIES_PATH, self._search_params(params or {}))
            return [] if activities is None else activities
        m = _METRICS_RE.fullmatch(path)
        if m:
            if params:
                raise _not_in_key_mode(path)
            start, end = _date(m.group(1), "startdate"), _date(m.group(2), "enddate")
            return self._get(f"{_METRICS_PATH}/{start}/{end}")
        if _ZONES_RE.fullmatch(path):
            if params:
                raise _not_in_key_mode(path)
            return self._get(_ZONES_V2_PATH)
        raise _not_in_key_mode(path)

    @staticmethod
    def _search_params(params):
        unknown = set(params) - _SEARCH_PARAMS
        if unknown:
            raise _not_in_key_mode("connectapi params " + ",".join(sorted(str(k) for k in unknown)))
        out = {}
        for name in ("startDate", "endDate"):
            if params.get(name) is not None:
                out[name] = _date(params[name], name)
        for name in ("start", "limit"):
            if params.get(name) is not None:
                out[name] = _count(params[name], name)
        if params.get("activityType"):
            out["activityType"] = _activity_type(params["activityType"])
        return out

    def get_activities(self, start=0, limit=20, activitytype=None):
        _date_v, positive, non_negative, max_limit = _validators()
        start = non_negative(start, "start")
        limit = positive(limit, "limit")
        if limit > max_limit:
            raise ValueError(f"limit cannot exceed {max_limit}")
        params = {"start": str(start), "limit": str(limit)}
        if activitytype:
            params["activityType"] = _activity_type(str(activitytype))
        activities = self._get(_ACTIVITIES_PATH, params)
        return [] if activities is None else activities

    def get_activity(self, activity_id):
        return self._get(f"/activity-service/activity/{_activity_id(activity_id)}", activity=True)

    def download_activity(self, activity_id, dl_fmt=None):
        """The original activity file (ZIP bytes). Only the ORIGINAL format; the id gets get_activity's check
        (garminconnect checks nothing here: key mode is stricter)."""
        if dl_fmt is not None and getattr(dl_fmt, "name", None) != "ORIGINAL":
            raise _not_in_key_mode("download_activity format")
        number = int(_activity_id(activity_id))
        data = self._call(lambda token: self._http.download(token, number), activity=True)
        if not data:
            raise KeyModeNoData(TEXT_EMPTY_FILE) from None
        return data

    def get_user_summary(self, cdate):
        cdate = _date(cdate, "cdate")
        response = self._get("/usersummary-service/usersummary/daily", {"calendarDate": cdate})
        if not response:
            raise KeyModeNoData(TEXT_EMPTY_ANSWER) from None
        return response

    def get_stats(self, cdate):
        return self.get_user_summary(_date(cdate, "cdate"))

    def get_sleep_data(self, cdate):
        cdate = _date(cdate, "cdate")
        answer = self._get("/sleep-service/sleep/dailySleep", {"date": cdate})
        return {"dailySleepDTO": answer} if isinstance(answer, dict) else {}

    def get_hrv_data(self, cdate):
        cdate = _date(cdate, "cdate")
        return self._get(f"/hrv-service/hrv/{cdate}")

    def get_body_battery(self, startdate, enddate=None):
        startdate = _date(startdate, "startdate")
        enddate = startdate if enddate is None else _date(enddate, "enddate")
        return self._get("/wellness-service/wellness/bodyBattery/reports/daily",
                         {"startDate": startdate, "endDate": enddate})

    def get_weigh_ins(self, startdate, enddate):
        startdate = _date(startdate, "startdate")
        enddate = _date(enddate, "enddate")
        return self._get(f"/weight-service/weight/range/{startdate}/{enddate}", {"includeAll": True})

    def get_body_composition(self, startdate, enddate=None):
        """The weight range, as garminconnect's {"dateWeightList": [{"calendarDate", "weight"}, ...]} (weight in
        grams), which upstream's get_activity_fit_data reads for W/kg."""
        startdate = _date(startdate, "startdate")
        enddate = startdate if enddate is None else _date(enddate, "enddate")
        if startdate > enddate:
            raise ValueError("startdate cannot be after enddate")
        raw = self.get_weigh_ins(startdate, enddate)
        entries = []
        summaries = raw.get("dailyWeightSummaries") if isinstance(raw, dict) else None
        for day in summaries if isinstance(summaries, list) else []:
            metrics = day.get("allWeightMetrics") if isinstance(day, dict) else None
            for metric in metrics if isinstance(metrics, list) else []:
                if not isinstance(metric, dict):
                    continue
                date = metric.get("calendarDate") or day.get("summaryDate")
                entries.append({"calendarDate": date, "weight": metric.get("weight")})
        return {"dateWeightList": entries}


# --- Garmin's client (imported only here, in key mode) -------------------------------------------------------

def _library():
    import garmin_personal_api
    import garmin_personal_api.constants

    return garmin_personal_api


def _session():
    """A requests.Session that keeps no cookies (bearer auth only)."""
    import http.cookiejar

    import requests

    session = requests.Session()
    session.cookies.set_policy(http.cookiejar.DefaultCookiePolicy(allowed_domains=[]))
    return session


class _LibraryHttp:
    """KeyModeGarmin's HTTP layer over GarminPersonalApiClient: one shared session for data calls, one client per
    token (a client's headers are fixed when it is built, so a new token never changes a request in flight), and
    the base URL forced to Garmin's own host whatever the environment says."""

    def __init__(self, *, session=None, timeout=30.0):
        self._lib = _library()
        self._session = session if session is not None else _session()
        self._timeout = timeout
        self._lock = threading.Lock()
        self._client = None
        self._token = None

    def _client_for(self, token):
        with self._lock:
            if self._client is None or self._token != token:
                client = self._lib.GarminPersonalApiClient(token, timeout=self._timeout, session=self._session)
                client.base_url = OFFICIAL_BASE_URL
                self._client, self._token = client, token
            return self._client

    def get(self, token, path, params):
        return self._client_for(token).get(path, params=params)

    def download(self, token, activity_id):
        return self._client_for(token).download_activity(activity_id)


def library_authenticator(cache_file, *, session=None, clock=time.time):
    """Garmin's PersonalApiAuthenticator on its own session, caching at `cache_file`, base URL forced."""
    lib = _library()
    auth = lib.PersonalApiAuthenticator(session=session if session is not None else _session(),
                                        bearer_token_cache_file=cache_file, clock=clock)
    auth.base_url = OFFICIAL_BASE_URL
    return auth


def _check_hook(garmin_mcp):
    import inspect

    try:
        source = inspect.getsource(garmin_mcp.main)
    except (AttributeError, OSError, TypeError) as exc:
        raise RuntimeError("key_mode: cannot read garmin_mcp.main's source; the key-mode login hook needs a new "
                           "place") from exc
    if HOOK_TEXT not in source or not callable(getattr(garmin_mcp, "init_api", None)):
        raise RuntimeError("key_mode: upstream no longer logs in through init_api(email, password); the key-mode "
                           "login hook needs a new place")


def login(tokens, http):
    """Key mode's init_api(): the adapter, after one token request. Garmin refusing the key raises
    KeyModeLoginError, so upstream's /readyz says failed; a rate limit, a network failure, a 5xx or an unusable
    token answer is logged (by TokenManager) and the adapter is returned anyway: each call asks again."""
    garmin = KeyModeGarmin(tokens, http)
    try:
        tokens.token(first=True)
    except (KeyModeRateLimited, KeyModeGarminError, KeyModeNoData, OSError):
        pass  # requests' ConnectionError and Timeout are OSErrors
    return garmin


def install(key, cache_file=None, *, authenticator=None, http=None, clock=time.time):
    """Make upstream log in with the key: replace garmin_mcp.init_api before upstream's main() runs.

    `key` is take_key()'s value and must pass the key check; `cache_file` is cache_path()'s (None: cache_path of
    os.environ, read now). Prepares the cache
    (folder 0700, file 0600), quiets Garmin's client logger to WARNING, logs "garmin key mode: on", and returns the
    TokenManager. `authenticator` and `http` default to Garmin's client (imported here); tests pass fakes.
    Raises RuntimeError if upstream's hook moved, ValueError on a programming error (a key or path that failed
    the checks)."""
    if not key_ok(key):
        raise ValueError("key_mode.install: the key failed the key check (it is not shown)")
    if cache_file is None:
        cache_file = cache_path(os.environ)
    if not _path_ok(cache_file):
        raise ValueError("key_mode.install: the token cache path failed the path rule")
    import garmin_mcp

    _check_hook(garmin_mcp)
    logging.getLogger("garmin_personal_api").setLevel(logging.WARNING)
    prepare_cache(cache_file)
    if authenticator is None:
        authenticator = library_authenticator(cache_file, clock=clock)
    if http is None:
        http = _LibraryHttp()
    tokens = TokenManager(key, cache_file, authenticator=authenticator, clock=clock)

    def init_api(email=None, password=None):
        return login(tokens, http)

    garmin_mcp.init_api = init_api
    log.info(LOG_ON, cache_file)
    return tokens


_SELFCHECK = "key_mode: Garmin's Personal API client or upstream changed: {what}"


def selfcheck():
    """Raise RuntimeError unless Garmin's client still has what key mode uses (Docker build check, 4.5): it
    imports; DEFAULT_BASE_URL and BASE_URL_ENV_VAR are this module's; HttpError takes keyword arguments only;
    PersonalApiAuthenticator takes bearer_token_cache_file and obtain_token(api_key); GarminPersonalApiClient has
    get and download_activity; upstream still logs in through init_api(email, password); garminconnect still has
    every name of KeyModeGarmin._IMPLEMENTED and the validators key mode calls. No network."""
    import inspect

    def fail(what):
        raise RuntimeError(_SELFCHECK.format(what=what))

    try:
        lib = _library()
        from garmin_personal_api import constants
    except ImportError:
        fail("garmin_personal_api does not import")
    if getattr(lib, "DEFAULT_BASE_URL", None) != OFFICIAL_BASE_URL:
        fail("DEFAULT_BASE_URL is not " + OFFICIAL_BASE_URL)
    if getattr(constants, "BASE_URL_ENV_VAR", None) != BASE_URL_ENV:
        fail("BASE_URL_ENV_VAR is not " + BASE_URL_ENV)
    try:
        err = lib.HttpError(status_code=401, method="GET", url="u", response_text="b")
    except Exception:  # noqa: BLE001
        err = None
    if getattr(err, "status_code", None) != 401 or not isinstance(err, lib.GarminPersonalApiError):
        fail("HttpError(status_code=, method=, url=, response_text=) no longer works")
    try:
        auth = lib.PersonalApiAuthenticator(bearer_token_cache_file="/nonexistent/garmin-key/token.json")
        auth.close()
        params = inspect.signature(lib.PersonalApiAuthenticator.obtain_token).parameters
    except Exception:  # noqa: BLE001
        params = {}
    if "api_key" not in params:
        fail("PersonalApiAuthenticator(bearer_token_cache_file=...).obtain_token(api_key) no longer works")
    for name in ("get", "download_activity"):
        if not callable(getattr(lib.GarminPersonalApiClient, name, None)):
            fail(f"GarminPersonalApiClient has no {name}()")
    import garmin_mcp

    try:
        _check_hook(garmin_mcp)
    except RuntimeError:
        fail("garmin_mcp.main no longer calls " + HOOK_TEXT)
    import garminconnect

    for name in KeyModeGarmin._IMPLEMENTED:
        if not callable(getattr(garminconnect.Garmin, name, None)) or not callable(getattr(KeyModeGarmin, name)):
            fail(f"garminconnect.Garmin has no {name}()")
    for name in ("_validate_date_format", "_validate_positive_integer", "_validate_non_negative_integer",
                 "MAX_ACTIVITY_LIMIT"):
        if not hasattr(garminconnect, name):
            fail(f"garminconnect has no {name}")
