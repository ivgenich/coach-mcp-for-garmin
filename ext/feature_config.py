"""The optional features' settings: tracing (#3) and history search (#4).

Standard library only and pure: load() and problems() read a mapping (the
environment), never the file system or the network, and never raise for a
bad value. launch.check_config() adds problems() to the --check-config lines,
so entrypoint.sh stops before anything starts when a value is wrong. Both
features are off by default; with both off nothing here changes what the
server does.

    TRACING                      off (default) | on
    OTEL_EXPORTER_OTLP_ENDPOINT  the tracing service's OTLP/HTTP base URL; required with TRACING=on
    OTEL_EXPORTER_OTLP_HEADERS   optional, secret: name=value pairs, comma-separated (the auth header)
    OTEL_SDK_DISABLED            true turns tracing off whatever TRACING says (checked only with TRACING=on)
    HISTORY_SEARCH               off (default) | on
    DATABASE_URL                 secret: the Postgres URL; required with HISTORY_SEARCH=on
    ROLE                         app (default) | worker | enqueue (worker and enqueue need HISTORY_SEARCH=on;
                                 any other value is checked only with HISTORY_SEARCH=on)
    HISTORY_FORGET               yes: delete the stored history at startup (unset to keep it; a value other
                                 than yes is checked only with HISTORY_SEARCH=on)
    HISTORY_TOKEN_KEY            secret: 32 random bytes in base64; required with HISTORY_SEARCH=on in full mode
    WORKER_CONCURRENCY           1 (default) to 4: job threads in one worker
    PLAN_TOOLS                   off (default) | on: the training-plan read tools (plan_assess, plan_preview,
                                 plan_get); needs HISTORY_SEARCH=on and GARMIN_AUTH=full, ROLE=app only
    PLAN_WRITES                  off (default) | on: needs PLAN_TOOLS=on; plan_commit and plan_cancel write the
                                 plan's own workouts to Garmin (docs/training-plan.md)
    PLAN_TRACKING                off (default) | on: needs PLAN_TOOLS=on; links finished activities to the plan's
                                 sessions, adds the plan to get_daily_status / get_weekly_status and serves
                                 log_feedback and plan_activate (write only the app's own database, never
                                 Garmin)

LANGFUSE_* variables are read by the eval runner only, never by the server.

Every error line is a fixed text (the ERR_T*, ERR_H* and ERR_P* constants): no line
ever holds a value, so a secret pasted into the wrong variable is never
printed. secret_values() lists the secret values (and the forms a log line
could carry them in) for tool_errors.register_secret().
"""

from __future__ import annotations

import base64
import binascii
import ipaddress
import re
import urllib.parse
from dataclasses import dataclass, field
from typing import List, Mapping, Optional, Tuple

TRACING_ENV = "TRACING"
HISTORY_ENV = "HISTORY_SEARCH"
ROLE_ENV = "ROLE"
DATABASE_URL_ENV = "DATABASE_URL"
FORGET_ENV = "HISTORY_FORGET"
TOKEN_KEY_ENV = "HISTORY_TOKEN_KEY"
CONCURRENCY_ENV = "WORKER_CONCURRENCY"
OTLP_ENDPOINT_ENV = "OTEL_EXPORTER_OTLP_ENDPOINT"
OTLP_HEADERS_ENV = "OTEL_EXPORTER_OTLP_HEADERS"
SDK_DISABLED_ENV = "OTEL_SDK_DISABLED"
PLAN_TOOLS_ENV = "PLAN_TOOLS"
PLAN_WRITES_ENV = "PLAN_WRITES"
PLAN_TRACKING_ENV = "PLAN_TRACKING"

ROLES = ("app", "worker", "enqueue")
# Secrets: entrypoint.sh starts Caddy without them (env -u), and launch.py
# takes them out of os.environ once read.
SECRET_ENVS = (OTLP_HEADERS_ENV, DATABASE_URL_ENV, TOKEN_KEY_ENV)
# OpenTelemetry variables the server sets itself: one of them set by hand
# could add a resource attribute, an exporter or a sampler the export filter
# was not written for.
REFUSED_OTEL = ("OTEL_RESOURCE_ATTRIBUTES", "OTEL_TRACES_EXPORTER", "OTEL_METRICS_EXPORTER", "OTEL_LOGS_EXPORTER",
                "OTEL_SERVICE_NAME", "OTEL_METRICS_EXEMPLAR_FILTER", "OTEL_TRACES_SAMPLER",
                "OTEL_TRACES_SAMPLER_ARG", "OTEL_PROPAGATORS")
REFUSED_OTEL_PREFIX = "OTEL_PYTHON_"
# Every OTEL_EXPORTER_OTLP_* variable is refused too (per-signal endpoints, headers, certificates, protocol,
# timeout, compression: the exporters are configured in code), except these two.
REFUSED_OTEL_EXPORTER_PREFIX = "OTEL_EXPORTER_OTLP_"
ALLOWED_OTEL_EXPORTER = ("OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_HEADERS")
HEADERS_MAX = 2048
TOKEN_KEY_BYTES = 32
TOKEN_KEY_MIN_DISTINCT = 16   # a random 32-byte key has about 30 distinct byte values
TOKEN_KEY_MAX_PERIOD = 16     # a key made of one block repeated, the block this long or shorter, is refused
# An OTLP header name: an HTTP token (RFC 9110 tchar), at most 64 characters, after URL-decoding.
HEADER_NAME_RE = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,64}")
CONCURRENCY_MAX = 4
RAILWAY_DEPLOYMENT_VARS = ("RAILWAY_PROJECT_ID", "RAILWAY_ENVIRONMENT_ID", "RAILWAY_SERVICE_ID", "RAILWAY_DEPLOYMENT_ID")
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")

# --- the --check-config lines (launch.py prefixes "launch: config error: ") -------

ERR_T1 = "TRACING must be off or on (the default is off)"
ERR_T2 = ("TRACING=on needs OTEL_EXPORTER_OTLP_ENDPOINT: set it to your tracing service's OTLP https URL, "
          "or set TRACING=off")
ERR_T3 = ("OTEL_EXPORTER_OTLP_ENDPOINT must be an https:// URL with a host and no user info, query or fragment "
          "(http:// only for 127.0.0.1, localhost or [::1], and never on Railway)")
ERR_T4 = ("OTEL_EXPORTER_OTLP_HEADERS must be name=value pairs separated by commas, printable ASCII, "
          "each name an HTTP header name (letters, digits and !#$%&'*+-.^_`|~), "
          "at most 2048 characters (the value is not shown)")
ERR_T5 = ("{name} is set by this server itself and cannot be changed: delete the variable "
          "(see docs/tracing.md)")
ERR_T6 = ("OTEL_SDK_DISABLED must be true or false; with TRACING=on, true keeps tracing off")
ERR_H1 = "HISTORY_SEARCH must be off or on (the default is off)"
ERR_H2 = ("HISTORY_SEARCH=on needs DATABASE_URL: on Railway add a Postgres service and set DATABASE_URL to "
          "its reference variable ${{Postgres.DATABASE_URL}}; or set HISTORY_SEARCH=off")
ERR_H3 = ("DATABASE_URL must be a postgresql:// or postgres:// URL with a host and a database name "
          "(the value is not shown)")
ERR_H4 = "ROLE must be app, worker or enqueue (the default is app)"
ERR_H5 = "ROLE={role} runs the history jobs and needs HISTORY_SEARCH=on"
ERR_H6 = "HISTORY_FORGET must be yes, or unset to keep the stored history"
ERR_H7 = ("HISTORY_TOKEN_KEY must be 32 random bytes in base64 (44 characters; the value is not shown); "
          "make one with: python3 -c \"import base64, secrets; "
          "print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())\"")
ERR_H8 = ("HISTORY_SEARCH=on with GARMIN_AUTH=full needs HISTORY_TOKEN_KEY, the same value on the app and the "
          "worker: the worker reads the Garmin login from the database with it (see docs/history-worker.md)")
ERR_H9 = "WORKER_CONCURRENCY must be a whole number from 1 to 4 (the default is 1)"
ERR_H10 = "HISTORY_FORGET=yes needs DATABASE_URL: there is no database to delete the history from"
ERR_H12 = ("HISTORY_TOKEN_KEY is not random (typed text, a repeated pattern or a counting sequence; the value is "
           "not shown): make a new one "
           "with: python3 -c \"import base64, secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())\"")
# launch.check_config() adds these when the image lacks a turned-on feature's packages.
ERR_P1 = "PLAN_TOOLS must be off or on (the default is off)"
ERR_P2 = "PLAN_WRITES must be off or on (the default is off)"
ERR_P3 = "PLAN_TOOLS=on needs HISTORY_SEARCH=on: the plans are kept in the history database"
ERR_P4 = "PLAN_TOOLS=on needs GARMIN_AUTH=full: Garmin's Personal API key does not serve the plan's sources"
ERR_P5 = "PLAN_WRITES=on needs PLAN_TOOLS=on"
ERR_P_TRACKING_VALUE = "PLAN_TRACKING must be off or on (the default is off)"
ERR_P_TRACKING_NEEDS = "PLAN_TRACKING=on needs PLAN_TOOLS=on"
ERR_P6 = "PLAN_TOOLS and PLAN_WRITES are read by ROLE=app only: leave them unset on the worker and enqueue services"
ERR_T7 = ("TRACING=on needs the OpenTelemetry SDK, which this image does not have: build the image from this "
          "repository's Dockerfile, which always installs config/requirements-telemetry.txt")
ERR_H11 = ("HISTORY_SEARCH=on (or HISTORY_FORGET=yes) needs the history packages, which this image does not have: "
           "build it with the build argument HISTORY=1 (Railway: add the service variable HISTORY=1 and redeploy)")
# The modules ERR_H11 looks for (find_spec, never imported by the check).
HISTORY_PACKAGES = ("psycopg", "langchain_postgres")

# Information lines (launch.py logs them at startup; never errors).
NOTE_SDK_DISABLED = "tracing: off, because OTEL_SDK_DISABLED=true"
NOTE_TRACING_OFF_SETTINGS = "tracing: off; OTEL_EXPORTER_OTLP_* settings are set but not used"
NOTE_PLAN_WRITES = ("training plans: PLAN_WRITES=on: plan_commit and plan_cancel may write the plan's own workouts "
                    "to the Garmin calendar, only for callers whose sign-in carries the plan:write consent")
NOTE_HISTORY_OFF_DATABASE = "history search: off; DATABASE_URL is set but only used to forget (HISTORY_FORGET=yes)"


@dataclass(frozen=True)
class FeatureConfig:
    """The features' settings. Secret fields hold their values but never show in repr()."""

    tracing: bool = False
    history: bool = False
    role: str = "app"
    database_url_set: bool = False
    history_forget: bool = False
    otlp_endpoint: Optional[str] = None
    otlp_headers_set: bool = False
    sdk_disabled: bool = False
    history_token_key_set: bool = False
    worker_concurrency: int = 1
    garmin_auth: str = "full"
    plan_tools: bool = False
    plan_writes: bool = False
    plan_tracking: bool = False
    database_url: Optional[str] = field(default=None, repr=False, compare=False)
    otlp_headers: Optional[str] = field(default=None, repr=False, compare=False)
    history_token_key: Optional[bytes] = field(default=None, repr=False, compare=False)
    notes: Tuple[str, ...] = ()

    @property
    def any_on(self) -> bool:
        return self.tracing or self.history

    def summary(self) -> str:
        """A short text for the startup and --check-config lines: '' when both features are off and ROLE=app."""
        parts = []
        if self.role != "app":
            parts.append(f"ROLE={self.role}")
        if self.tracing:
            parts.append("tracing on")
        if self.history:
            parts.append("history search on")
        if self.history_forget:
            parts.append("HISTORY_FORGET=yes")
        if self.plan_tools:
            parts.append("plan tools on")
        if self.plan_writes:
            parts.append("plan writes on")
        if self.plan_tracking:
            parts.append("plan tracking on")
        return ", ".join(parts)


def _value(env: Mapping[str, str], name: str) -> str:
    return (env.get(name) or "").strip()


def _switch(env: Mapping[str, str], name: str) -> Optional[bool]:
    """True for on, False for off or unset/blank, None for anything else."""
    value = _value(env, name).lower()
    if value in ("", "off"):
        return False
    if value == "on":
        return True
    return None


def _on_railway(env: Mapping[str, str]) -> bool:
    return any(_value(env, name) for name in RAILWAY_DEPLOYMENT_VARS)


def _endpoint_ok(raw: str, env: Mapping[str, str]) -> bool:
    if any(ch <= " " or ch == "\x7f" for ch in raw) or "#" in raw or "?" in raw or len(raw) > 512:
        return False
    try:
        parts = urllib.parse.urlsplit(raw)
        host = parts.hostname or ""
        port = parts.port
    except ValueError:
        return False
    if not host or "@" in parts.netloc or port == 0:
        return False
    if parts.scheme == "https":
        return True
    return parts.scheme == "http" and host in LOOPBACK_HOSTS and not _on_railway(env)


def parse_header_pairs(raw) -> Optional[dict]:
    """OTEL_EXPORTER_OTLP_HEADERS ("name=value,name2=value2", URL-encoded) as a dict of the decoded, stripped
    names and values; None when any pair is malformed: no "=", an empty value, a name that is not an HTTP
    token (HEADER_NAME_RE), or a decoded value that is not printable ASCII. Empty pairs are skipped. One rule
    for --check-config (ERR_T4) and telemetry.parse_headers(), so a header the check passes is never dropped."""
    if not isinstance(raw, str):
        return None
    out = {}
    for part in raw.split(","):
        if not part.strip():
            continue
        name, sep, value = part.partition("=")
        name, value = urllib.parse.unquote(name).strip(), urllib.parse.unquote(value).strip()
        if (not sep or not value or not HEADER_NAME_RE.fullmatch(name)
                or not all(" " <= ch <= "~" for ch in value)):
            return None
        out[name] = value
    return out


def _headers_ok(raw: str) -> bool:
    if not raw or len(raw) > HEADERS_MAX or not all(" " <= ch <= "~" for ch in raw):
        return False
    return bool(parse_header_pairs(raw))


def _database_url_ok(raw: str) -> bool:
    if any(ch <= " " or ch == "\x7f" for ch in raw) or len(raw) > 2048:
        return False
    try:
        parts = urllib.parse.urlsplit(raw)
        host = parts.hostname or ""
        parts.port  # noqa: B018 - raises ValueError for a bad port
    except ValueError:
        return False
    if parts.scheme not in ("postgresql", "postgres") or not host:
        return False
    if host.startswith("["):
        try:
            ipaddress.IPv6Address(host.strip("[]"))
        except ValueError:
            return False
    return parts.path.strip("/") != ""


def _decode_key_bytes(raw: str) -> Optional[bytes]:
    """32 bytes from standard or URL-safe base64 (padding optional), else None."""
    if not raw or len(raw) > 64:
        return None
    text = raw.replace("-", "+").replace("_", "/")
    text += "=" * (-len(text) % 4)
    try:
        key = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError):
        return None
    return key if len(key) == TOKEN_KEY_BYTES else None


def weak_key(key: bytes) -> bool:
    """True when `key` (the decoded HISTORY_TOKEN_KEY) is not random: fewer than TOKEN_KEY_MIN_DISTINCT
    different byte values (all zeros, a typed word); every byte printable ASCII (a typed phrase, which
    os.urandom gives with odds of about 1 in 10^16); bytes that count up or down by one step (bytes 0..31); or
    one block of at most TOKEN_KEY_MAX_PERIOD bytes repeated. The one rule for feature_config and
    worker_garmin.parse_token_key. Never shows the key."""
    key = bytes(key)
    if len(set(key)) < TOKEN_KEY_MIN_DISTINCT:
        return True
    if all(0x20 <= b <= 0x7E for b in key):
        return True
    if len(key) > 2 and len({(b - a) % 256 for a, b in zip(key, key[1:])}) == 1:
        return True
    return any(all(key[i] == key[i - p] for i in range(p, len(key)))
               for p in range(1, min(TOKEN_KEY_MAX_PERIOD, len(key) - 1) + 1))


def _decode_key(raw: str) -> Optional[bytes]:
    """The key when it is 32 random-looking bytes in base64, else None."""
    key = _decode_key_bytes(raw)
    return None if key is None or weak_key(key) else key


def _refused_otel(env: Mapping[str, str]) -> List[str]:
    """The refused OTEL_* names set in `env`, sorted: REFUSED_OTEL, OTEL_PYTHON_* and OTEL_EXPORTER_OTLP_* except
    ALLOWED_OTEL_EXPORTER."""
    return sorted(n for n in env if isinstance(n, str) and (
        n in REFUSED_OTEL or n.startswith(REFUSED_OTEL_PREFIX)
        or (n.startswith(REFUSED_OTEL_EXPORTER_PREFIX) and n not in ALLOWED_OTEL_EXPORTER)))


def _concurrency(raw: str) -> Optional[int]:
    if raw == "":
        return 1
    if not raw.isdigit() or len(raw) > 2:
        return None
    n = int(raw)
    return n if 1 <= n <= CONCURRENCY_MAX else None


def _garmin_auth(env: Mapping[str, str]) -> str:
    """full or key as key_mode spells it (spaces and case ignored); anything else counts as full here
    (key_mode.check() reports a bad GARMIN_AUTH)."""
    value = "".join((env.get("GARMIN_AUTH") or "").split()).lower()
    return "key" if value == "key" else "full"


def _parse(env: Mapping[str, str]) -> Tuple[FeatureConfig, List[str]]:
    errors: List[str] = []
    notes: List[str] = []

    tracing = _switch(env, TRACING_ENV)
    if tracing is None:
        errors.append(ERR_T1)
        tracing = False
    disabled_raw = _value(env, SDK_DISABLED_ENV).lower()
    sdk_disabled = disabled_raw == "true"
    if tracing and disabled_raw not in ("", "true", "false"):
        errors.append(ERR_T6)   # with tracing off the value changes nothing (and the old server ignored it)
    endpoint = _value(env, OTLP_ENDPOINT_ENV)
    headers = env.get(OTLP_HEADERS_ENV) or ""
    if tracing:
        if not endpoint:
            errors.append(ERR_T2)
        elif not _endpoint_ok(endpoint, env):
            errors.append(ERR_T3)
        if headers and not _headers_ok(headers.strip()):
            errors.append(ERR_T4)
        for name in [n for n in REFUSED_OTEL if n in env] + [n for n in _refused_otel(env) if n not in REFUSED_OTEL]:
            # The name is the variable's, never its value; it is shown only when it is printable ASCII.
            prefix = REFUSED_OTEL_PREFIX if name.startswith(REFUSED_OTEL_PREFIX) else REFUSED_OTEL_EXPORTER_PREFIX
            shown = name if all("!" <= ch <= "~" for ch in name) and len(name) <= 80 else prefix + "*"
            errors.append(ERR_T5.format(name=shown))
        if sdk_disabled:
            notes.append(NOTE_SDK_DISABLED)
    elif endpoint or headers:
        notes.append(NOTE_TRACING_OFF_SETTINGS)

    history = _switch(env, HISTORY_ENV)
    if history is None:
        errors.append(ERR_H1)
        history = False
    role = _value(env, ROLE_ENV).lower() or "app"
    if role not in ROLES:
        if history:
            errors.append(ERR_H4)
        role = "app"   # with history off the old server ignored ROLE; an unknown value runs the app
    elif role != "app" and not history:
        errors.append(ERR_H5.format(role=role))

    db_url = _value(env, DATABASE_URL_ENV)
    db_ok = bool(db_url) and _database_url_ok(db_url)
    forget_raw = _value(env, FORGET_ENV).lower()
    forget = forget_raw == "yes"
    if forget_raw not in ("", "yes") and history:
        errors.append(ERR_H6)   # with history off only yes does something; other values are ignored
    if history and not db_url:
        errors.append(ERR_H2)
    elif db_url and not db_ok and (history or forget):
        errors.append(ERR_H3)
    if forget and not db_url:
        errors.append(ERR_H10)
    if db_url and not history and not forget:
        pass  # a database kept for later: unused, not an error
    elif db_url and not history and forget:
        notes.append(NOTE_HISTORY_OFF_DATABASE)

    garmin_auth = _garmin_auth(env)
    key_raw = _value(env, TOKEN_KEY_ENV)
    key_bytes = _decode_key_bytes(key_raw) if key_raw else None
    token_key = key_bytes if key_bytes is not None and not weak_key(key_bytes) else None
    if key_raw and token_key is None and history:
        errors.append(ERR_H7 if key_bytes is None else ERR_H12)
    if history and garmin_auth == "full" and role in ("app", "worker") and not key_raw:
        errors.append(ERR_H8)

    concurrency = _concurrency(_value(env, CONCURRENCY_ENV))
    if concurrency is None:
        if history:
            errors.append(ERR_H9)
        concurrency = 1

    plan_tools = _switch(env, PLAN_TOOLS_ENV)
    if plan_tools is None:
        errors.append(ERR_P1)
        plan_tools = False
    plan_writes = _switch(env, PLAN_WRITES_ENV)
    if plan_writes is None:
        errors.append(ERR_P2)
        plan_writes = False
    if (plan_tools or plan_writes) and role != "app":
        errors.append(ERR_P6)
    elif plan_tools:
        if not history:
            errors.append(ERR_P3)
        if garmin_auth != "full":
            errors.append(ERR_P4)
    if plan_writes and not plan_tools:
        errors.append(ERR_P5)
    plan_tracking = _switch(env, PLAN_TRACKING_ENV)
    if plan_tracking is None:
        errors.append(ERR_P_TRACKING_VALUE)
        plan_tracking = False
    if plan_tracking and not plan_tools:
        errors.append(ERR_P_TRACKING_NEEDS)
    plan_ok = bool(plan_tools) and bool(history) and garmin_auth == "full" and role == "app"
    if plan_ok and plan_writes:
        notes.append(NOTE_PLAN_WRITES)

    effective_tracing = bool(tracing) and not sdk_disabled
    cfg = FeatureConfig(
        tracing=effective_tracing,
        history=bool(history),
        role=role,
        database_url_set=db_ok,
        history_forget=forget,
        otlp_endpoint=endpoint if effective_tracing and endpoint else None,
        otlp_headers_set=bool(headers.strip()),
        sdk_disabled=sdk_disabled,
        history_token_key_set=token_key is not None,
        worker_concurrency=concurrency,
        garmin_auth=garmin_auth,
        plan_tools=plan_ok,
        plan_writes=plan_ok and bool(plan_writes),
        plan_tracking=plan_ok and bool(plan_tracking),
        database_url=db_url if db_ok else None,
        otlp_headers=headers.strip() or None,
        history_token_key=token_key,
        notes=tuple(notes),
    )
    return cfg, errors


def problems(env: Mapping[str, str]) -> List[str]:
    """The ERR_T*, ERR_H* and ERR_P* lines for this environment, in a fixed order; [] when the settings are valid."""
    try:
        return _parse(env)[1]
    except Exception as exc:  # noqa: BLE001 - one fixed line, never a value
        return [f"the feature settings could not be checked: {type(exc).__name__}"]


def load(env: Optional[Mapping[str, str]] = None) -> FeatureConfig:
    """The FeatureConfig for this environment (os.environ by default). A bad value counts as off; call
    problems() first to refuse it. Never raises."""
    if env is None:
        import os

        env = os.environ
    try:
        return _parse(env)[0]
    except Exception:  # noqa: BLE001 - both features off is always safe
        return FeatureConfig()


def secret_values(cfg: FeatureConfig) -> List[str]:
    """Every secret value of `cfg`, in the forms a log line could carry it, for tool_errors.register_secret():
    DATABASE_URL whole, its password and its password URL-decoded; each OTLP header value as written,
    URL-decoded, and for an "Authorization=Basic <b64>" header the decoded user:token and the token;
    HISTORY_TOKEN_KEY as base64. Short values are left out by register_secret() itself."""
    out: List[str] = []

    def add(value):
        if isinstance(value, str) and value and value not in out:
            out.append(value)

    if cfg.database_url:
        add(cfg.database_url)
        try:
            password = urllib.parse.urlsplit(cfg.database_url).password
        except ValueError:
            password = None
        if password:
            add(password)
            add(urllib.parse.unquote(password))
    if cfg.otlp_headers:
        add(cfg.otlp_headers)
        for pair in cfg.otlp_headers.split(","):
            _, _, value = pair.partition("=")
            value = value.strip()
            for form in (value, urllib.parse.unquote(value)):
                add(form)
                scheme, _, credential = form.partition(" ")
                credential = credential.strip()
                if not credential:
                    continue
                add(credential)
                if scheme.lower() == "basic":
                    try:
                        decoded = base64.b64decode(credential + "=" * (-len(credential) % 4)).decode("utf-8")
                    except (binascii.Error, ValueError, UnicodeDecodeError):
                        continue
                    add(decoded)
                    add(decoded.partition(":")[2])
    if cfg.history_token_key:
        add(base64.urlsafe_b64encode(cfg.history_token_key).decode())
        add(base64.b64encode(cfg.history_token_key).decode())
    return out


def take_secrets(environ) -> None:
    """Remove the secret variables from `environ` (os.environ at startup, after load() and the telemetry
    setup read them), so nothing started later inherits them."""
    for name in SECRET_ENVS:
        environ.pop(name, None)
