"""Tracing and metrics (TRACING=on): OpenTelemetry spans and metrics over OTLP/HTTP, privacy by construction.

Off by default. With TRACING off (or OTEL_SDK_DISABLED=true) setup() imports nothing from opentelemetry,
starts no thread, and every helper below is a no-op: the server behaves exactly as without this module.
The stdlib test leg (no OpenTelemetry SDK installed) runs the same no-op path. docs/tracing.md is the
owner's guide (what is recorded, what never is, Grafana Cloud setup, alerts).

    setup(cfg)                    start exporting when cfg.tracing (feature_config.load()); no-op otherwise
    span(name, **attrs)           context manager; yields a handle with .set(**attrs) and .error(group)
    metric_count / metric_observe / metric_gauge(name, value, **attrs)
    inject() -> str | None        W3C traceparent of the current span, for job payloads
    extract_link(traceparent)     a span link to the enqueuer's span (or None), for span(..., links=[...])
    bind_context(fn)              fn that runs in the current trace context on another thread
    register_tools(names)         tool names that may appear in spans (others read "other")
    shutdown()                    flush with a short cap (the enqueue role calls it before it exits)
    selfcheck()                   the SDK hooks this module relies on are present ([] = fine)

Attribute keys: only ALLOWED_ATTRS. Callers pass full keys in a dict (span("job index_day",
{"coach.job.kind": "index_day"})) or the short names of ALIASES as keywords (span("tool get_x",
tool="get_x", caller="tool")). An unknown key or a value that fails its check is dropped (raises
ValueError when STRICT is true, which the tests set). Values are fixed names, small integers and
booleans: never health values, names, ids, dates, query text, URLs, tokens or error texts.

Privacy, three layers:
1. creation: the helpers keep only allowlisted keys with checked values;
2. export filter (_FilteringSpanExporter, _FilteringMetricExporter): every span, ours and the mcp SDK's,
   is rebuilt from the allowlist before OTLP encoding: unknown span names become "span", unknown tool
   names "other", events (exception text, tracebacks) and status descriptions are dropped, link attributes
   dropped, jsonrpc.request.id kept only when it matches ^[A-Za-z0-9-]{1,64}$; a metric data point with
   any key outside its allowlist is dropped; exemplars are off (AlwaysOff) and also stripped;
3. secrets: OTEL_EXPORTER_OTLP_HEADERS is taken out of os.environ and every part of it (the value, the
   Basic token, its decoded form) registered with tool_errors.register_secret().
The exporters' own self-metrics go to a no-op meter. Inbound trace context and baggage are ignored (a
propagator that extracts nothing); a client's traceparent only sets coach.remote_parent=true on the MCP
SERVER span. Resource attributes are built here, never from OTEL_RESOURCE_ATTRIBUTES/OTEL_SERVICE_NAME.

Failure behaviour: spans go to a bounded queue (2,048) exported on a background thread every 2 s with a
5 s timeout; a full queue drops spans. A dead, slow or refusing backend never slows a tool call. Export
errors are collapsed to one WARNING per kind per 10 minutes ("telemetry: export failed: <kind>"). Any
bug inside a helper is swallowed, logged once, and turns telemetry off for the process.
"""

from __future__ import annotations

import base64
import contextlib
import contextvars
import json
import logging
import math
import os
import re
import threading
import time
import urllib.parse

log = logging.getLogger("garmin_ext.telemetry")

SERVICE_NAME = "coach-mcp-for-garmin"
SCOPE_NAME = "coach-mcp"
STRICT = os.environ.get("COACH_TELEMETRY_STRICT") == "1"  # tests: unknown keys raise

# Export settings (constants on purpose: no tuning variables in v1).
QUEUE_SIZE = 2048
BATCH_SIZE = 256
SCHEDULE_DELAY_MS = 2000
EXPORT_TIMEOUT_S = 5.0
METRIC_INTERVAL_MS = 60000
SHUTDOWN_CAP_S = 3.0
ERROR_LOG_EVERY_S = 600.0

# Variables that could add exporters or attributes past the allowlist, or print spans to the logs.
# feature_config reports them (ERR_T*); setup() also refuses to start while any is set.
REFUSED_ENV = ("OTEL_RESOURCE_ATTRIBUTES", "OTEL_SERVICE_NAME", "OTEL_TRACES_EXPORTER", "OTEL_METRICS_EXPORTER",
               "OTEL_LOGS_EXPORTER", "OTEL_METRICS_EXEMPLAR_FILTER", "OTEL_TRACES_SAMPLER", "OTEL_TRACES_SAMPLER_ARG",
               "OTEL_PROPAGATORS")
REFUSED_ENV_PREFIXES = ("OTEL_PYTHON_",)
HEADERS_ENV = "OTEL_EXPORTER_OTLP_HEADERS"
# Every other OTEL_EXPORTER_OTLP_* variable is refused (feature_config.REFUSED_OTEL does the same): the OTLP
# exporter reads them itself, e.g. the per-signal _TRACES_HEADERS / _METRICS_HEADERS are merged into its
# headers past take_headers, and _TRACES_ENDPOINT would send spans somewhere else.
OTLP_ENV_PREFIX = "OTEL_EXPORTER_OTLP_"
OTLP_ENV_ALLOWED = ("OTEL_EXPORTER_OTLP_ENDPOINT", HEADERS_ENV)
# The per-signal header variables: take_headers removes them from the environment with the main one.
SIGNAL_HEADERS_ENV = ("OTEL_EXPORTER_OTLP_TRACES_HEADERS", "OTEL_EXPORTER_OTLP_METRICS_HEADERS",
                      "OTEL_EXPORTER_OTLP_LOGS_HEADERS")


# The Garmin API path families a "garmin GET <family>" span and garmin.endpoint_family may name
# (launch.GARMIN_ENDPOINT_FAMILIES is the same list); anything else is "other".
GARMIN_ENDPOINT_FAMILIES = frozenset((
    "activity-service", "activitylist-service", "adhocchallenge-service", "badge-service",
    "badgechallenge-service", "biometric-service", "bloodpressure-service", "calendar-service", "course-service",
    "device-service", "download-service", "fitnessage-service", "fitnessstats-service", "gcs-golfcommunity",
    "gear-service", "goal-service", "graphql-gateway", "hrv-service", "lifestylelogging-service",
    "metrics-service", "mobile-gateway", "nutrition-service", "periodichealth-service", "personalrecord-service",
    "sleep-service", "trainingplan-service", "upload-service", "userprofile-service", "userstats-service",
    "usersummary-service", "web-gateway", "weight-service", "wellness-service", "workout-service",
))


def _refused(name) -> bool:
    if name in REFUSED_ENV or name.startswith(REFUSED_ENV_PREFIXES):
        return True
    return name.startswith(OTLP_ENV_PREFIX) and name not in OTLP_ENV_ALLOWED


def refused_env(env=None) -> list[str]:
    """The refused OTEL_* variable names that are set in env (os.environ by default), sorted."""
    env = os.environ if env is None else env
    return sorted(k for k in env if _refused(k))


# --- value checks (pure Python, so the stdlib leg tests them) --------------------------------------------

_DROP = object()

ERROR_GROUPS = ("login", "rate_limited", "unavailable", "bad_input", "no_data", "internal")
TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_KNOWN_TOOLS: frozenset = frozenset()
_KNOWN_LOCK = threading.Lock()

MCP_METHODS = frozenset({
    "initialize", "notifications/initialized", "notifications/cancelled", "notifications/progress",
    "notifications/roots/list_changed", "ping", "server/discover", "tools/list", "tools/call",
    "resources/list", "resources/templates/list", "resources/read", "resources/subscribe",
    "resources/unsubscribe", "prompts/list", "prompts/get", "completion/complete", "logging/setLevel",
    "sampling/createMessage", "elicitation/create", "roots/list",
})
JOB_KINDS = ("index_day", "build_week", "embed_doc", "sweep", "rebuild")
CHANNELS = ("vector", "keyword", "numbers", "rerank")
BYTES_BUCKETS = ((1024, "lt_1k"), (4096, "1k_4k"), (16384, "4k_16k"), (65536, "16k_64k"), (262144, "64k_256k"))
BYTES_BUCKET_TOP = "ge_256k"
TRACEPARENT_RE = re.compile(r"^00-(?!0{32})[0-9a-f]{32}-(?!0{16})[0-9a-f]{16}-[0-9a-f]{2}$")
MODEL_RE = re.compile(r"^claude-[a-z0-9.-]{1,40}$")


def register_tools(names) -> None:
    """Add tool names (identifiers) that may appear in spans and metrics; any other name reads "other"
    (a tool name sent by a client is caller text). launch.py calls this with the served tool list."""
    global _KNOWN_TOOLS
    good = {n for n in names if isinstance(n, str) and TOOL_NAME_RE.fullmatch(n)}
    with _KNOWN_LOCK:
        _KNOWN_TOOLS = _KNOWN_TOOLS | frozenset(good)


def tool_label(name) -> str:
    """name when it is a registered tool, else "other"."""
    return name if isinstance(name, str) and name in _KNOWN_TOOLS else "other"


def bytes_bucket(n) -> str:
    """A fixed bucket name for an answer size in bytes (the exact size is not exported)."""
    if not isinstance(n, int) or isinstance(n, bool) or n < 0:
        return "lt_1k"
    for limit, name in BYTES_BUCKETS:
        if n < limit:
            return name
    return BYTES_BUCKET_TOP


def _enum(*values):
    allowed = frozenset(values)
    return lambda v: v if isinstance(v, str) and v in allowed else _DROP


def _int(lo, hi):
    def check(v):
        if isinstance(v, bool) or not isinstance(v, int):
            return _DROP
        return v if lo <= v <= hi else _DROP
    return check


def _float(lo, hi):
    def check(v):
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            return _DROP
        return float(v) if lo <= v <= hi else _DROP
    return check


def _bool(v):
    return v if isinstance(v, bool) else _DROP


def _pattern(regex):
    rx = re.compile(regex)
    return lambda v: v if isinstance(v, str) and rx.fullmatch(v) else _DROP


def _tool(v):
    return tool_label(v) if isinstance(v, str) else _DROP


def _error_type(v):
    """error.type: a fixed group, a JSON-RPC code, or "tool_error"; an exception class name becomes
    "exception" (the SDK sets type(e).__qualname__ on unexpected errors)."""
    if not isinstance(v, str):
        return _DROP
    if v in ERROR_GROUPS or v in ("tool_error", "timeout", "other"):
        return v
    if re.fullmatch(r"-?[0-9]{1,6}", v):
        return v
    return "exception"


def _method(v):
    return v if isinstance(v, str) and v in MCP_METHODS else ("other" if isinstance(v, str) else _DROP)


def _channels(v):
    if not isinstance(v, str):
        return _DROP
    parts = v.split(",")
    return v if parts and all(p in CHANNELS for p in parts) and len(set(parts)) == len(parts) else _DROP


def _json_numbers(keys):
    allowed = frozenset(keys)

    def check(v):
        try:
            data = json.loads(v) if isinstance(v, str) else None
        except ValueError:
            return _DROP
        if not isinstance(data, dict) or not data or not set(data) <= allowed:
            return _DROP
        for x in data.values():
            if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) or x < 0:
                return _DROP
        return json.dumps({k: data[k] for k in sorted(data)}, separators=(",", ":"))
    return check


def _tags(v):
    if not isinstance(v, (list, tuple)) or len(v) > 8:
        return _DROP
    rx = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,40}$")
    out = tuple(t for t in v if isinstance(t, str) and rx.fullmatch(t))
    return out if len(out) == len(v) else _DROP


_COUNT = _int(0, 10_000_000)
_TOKENS = ("input", "output", "cache_read_input_tokens", "cache_creation_input_tokens", "total")

# The only attribute keys that may leave the process, each with its value check.
ALLOWED_ATTRS = {
    # mcp SDK SERVER span (mcp/server/_otel.py) and outbound "MCP send" span
    "mcp.method.name": _method,
    "mcp.protocol.version": _pattern(r"[0-9]{4}-[0-9]{2}-[0-9]{2}"),
    "jsonrpc.request.id": _pattern(r"[A-Za-z0-9-]{1,64}"),
    "gen_ai.operation.name": _enum("execute_tool", "chat"),
    "gen_ai.tool.name": _tool,
    "error.type": _error_type,
    "rpc.response.status_code": _pattern(r"-?[0-9]{1,6}"),
    # ours (server, worker, enqueuer)
    "coach.caller": _enum("tool", "worker", "enqueue", "eval"),
    "coach.tool": _tool,
    "coach.error_group": _enum(*ERROR_GROUPS),
    "coach.mode": _enum("full", "key"),
    "coach.outcome": _enum("ok", "error", "skipped", "missing", "timeout", "blocked", "retry", "done",
                           "failed", "empty", "hit", "miss"),
    "coach.answer_bytes_bucket": _enum(*(name for _, name in BYTES_BUCKETS), BYTES_BUCKET_TOP),
    "coach.range_days": _int(0, 400),
    "coach.job.kind": _enum(*JOB_KINDS),
    "coach.job.status": _enum("queued", "running", "done", "failed", "retry"),
    "coach.job.attempt": _int(0, 100),
    "coach.queue.wait_s": _float(0, 1e8),
    "coach.history.result_count": _int(0, 1000),
    "coach.history.channels": _channels,
    "coach.history.lag_days": _int(0, 100_000),
    "coach.remote_parent": _bool,
    "coach.error.ref": _pattern(r"[0-9a-f]{8}"),
    "coach.source.name": _pattern(r"[a-z][a-z0-9_]{0,40}"),
    "coach.garmin.token_source": _enum("held", "file", "garmin", "refused_latch", "kept_held"),
    "coach.garmin.retry_after_401": _bool,
    "garmin.endpoint_family": _enum(*GARMIN_ENDPOINT_FAMILIES, "other"),
    "http.request.method": _enum("GET"),
    "http.response.status_code": _int(100, 599),
    # eval runner (evals/tracing.py): numbers and ids from the case files, never prompt or answer text
    "gen_ai.provider.name": _enum("anthropic"),
    "gen_ai.request.model": _pattern(MODEL_RE.pattern),
    "gen_ai.response.model": _pattern(MODEL_RE.pattern),
    "gen_ai.usage.input_tokens": _COUNT,
    "gen_ai.usage.output_tokens": _COUNT,
    "gen_ai.usage.cache_read_input_tokens": _COUNT,
    "gen_ai.usage.cache_creation_input_tokens": _COUNT,
    "gen_ai.usage.cost": _float(0, 1000),
    "gen_ai.response.finish_reasons": _tags,
    "coach.eval.case_id": _pattern(r"[A-Z]{1,4}[0-9]{1,4}"),
    "coach.eval.variant": _enum("bare", "instr", "coach"),
    "coach.eval.trial": _int(1, 100),
    "coach.eval.turn": _int(1, 100),
    "coach.eval.passed": _bool,
    "coach.eval.cost_usd": _float(0, 1000),
    "coach.eval.run_id": _pattern(r"[A-Za-z0-9._-]{1,80}"),
    "coach.eval.stop_reason": _enum("end_turn", "tool_use", "max_tokens", "refusal", "pause_turn", "stop_sequence",
                                    "max_turns", "budget", "harness_error", "error"),
    # Langfuse's OTLP mapping (eval traces only)
    "langfuse.observation.type": _enum("span", "generation", "tool", "agent", "evaluator"),
    "langfuse.observation.model.name": _pattern(MODEL_RE.pattern),
    "langfuse.observation.usage_details": _json_numbers(_TOKENS),
    "langfuse.observation.cost_details": _json_numbers(_TOKENS),
    "langfuse.session.id": _pattern(r"[A-Za-z0-9._-]{1,80}"),
    "langfuse.trace.name": _pattern(r"eval [A-Z]{1,4}[0-9]{1,4}( [a-z]{1,8})?"),
    "langfuse.trace.tags": _tags,
}

# Short keyword names for the common keys: span("x", caller="tool") == span("x", {"coach.caller": "tool"}).
ALIASES = {
    "caller": "coach.caller", "tool": "coach.tool", "error_group": "coach.error_group", "mode": "coach.mode",
    "outcome": "coach.outcome", "answer_bytes_bucket": "coach.answer_bytes_bucket",
    "range_days": "coach.range_days", "job_kind": "coach.job.kind", "job_status": "coach.job.status",
    "job_attempt": "coach.job.attempt", "queue_wait_s": "coach.queue.wait_s",
    "result_count": "coach.history.result_count", "channels": "coach.history.channels",
    "lag_days": "coach.history.lag_days", "remote_parent": "coach.remote_parent", "error_ref": "coach.error.ref",
    "source": "coach.source.name", "token_source": "coach.garmin.token_source",
    "retry_after_401": "coach.garmin.retry_after_401", "endpoint_family": "garmin.endpoint_family",
    "status_code": "http.response.status_code",
}

RESOURCE_ATTRS = {
    "service.name": _enum(SERVICE_NAME),
    "service.version": _pattern(r"[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}([a-z0-9.+-]{0,20})?"),
    "deployment.environment.name": _enum("production", "eval", "dev"),
    "coach.role": _enum("app", "worker", "enqueue", "eval"),
    "coach.mode": _enum("full", "key"),
}

SCOPES = ("mcp-python-sdk", SCOPE_NAME)

# Span names: fixed names, "<prefix> <checked suffix>", or MCP methods; anything else becomes "span".
_FIXED_NAME_RE = re.compile(r"^(history|worker|enqueue|queue|eval)\.[a-z_]{1,32}$")
_SUFFIX_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,40}$")


def _name_suffix(prefix, rest):
    if prefix in ("tool", "execute_tool"):
        return tool_label(rest)
    if prefix == "chat":
        return rest if MODEL_RE.fullmatch(rest) else "other"
    if prefix == "job":
        return rest if rest in JOB_KINDS else "other"
    if prefix == "garmin":
        return rest if rest.startswith("GET ") and rest[4:] in GARMIN_ENDPOINT_FAMILIES else "GET other"
    if prefix in ("source", "retriever"):
        return rest if _SUFFIX_RE.fullmatch(rest) else "other"
    return None


def clean_span_name(name) -> str:
    """The span name that may leave the process (see the module docstring)."""
    if not isinstance(name, str):
        return "span"
    if name in MCP_METHODS or _FIXED_NAME_RE.fullmatch(name):
        return name
    if name.startswith("MCP send "):
        method, _, target = name[len("MCP send "):].partition(" ")
        method = method if method in MCP_METHODS else "other"
        return f"MCP send {method}" + (f" {tool_label(target)}" if target and method == "tools/call" else "")
    head, _, rest = name.partition(" ")
    if head in MCP_METHODS and rest:
        return f"{head} {tool_label(rest)}" if head == "tools/call" else head
    suffix = _name_suffix(head, rest) if rest else None
    return f"{head} {suffix}" if suffix is not None else "span"


def clean_attrs(attrs, allowed=None, strict=None) -> dict:
    """attrs with every key outside `allowed` (ALLOWED_ATTRS) and every value failing its check removed.
    In strict mode (default: STRICT) an unknown key or a failing value raises ValueError instead (the value
    is not shown)."""
    allowed = ALLOWED_ATTRS if allowed is None else allowed
    strict = STRICT if strict is None else strict
    out = {}
    for key, value in (attrs or {}).items():
        check = allowed.get(key)
        cleaned = check(value) if check is not None else _DROP
        if cleaned is _DROP:
            if strict:
                raise ValueError(f"telemetry: attribute {key!r} is not allowed or its value fails its check")
            continue
        out[key] = cleaned
    return out


def _expand(attrs, kw) -> dict:
    merged = dict(attrs or {})
    for k, v in kw.items():
        full = ALIASES.get(k)
        if full is None:
            if STRICT:
                raise ValueError(f"telemetry: unknown keyword {k!r} (see ALIASES, or pass full keys in a dict)")
            continue
        merged[full] = v
    return merged


def valid_traceparent(value) -> bool:
    return isinstance(value, str) and bool(TRACEPARENT_RE.fullmatch(value))


# --- headers (secret) ----------------------------------------------------------------------------------

def parse_headers(raw) -> dict:
    """OTEL_EXPORTER_OTLP_HEADERS ("k=v,k2=v2", values URL-encoded) as a dict; {} when empty or malformed.
    feature_config.parse_header_pairs() is the rule, the one --check-config applies (ERR_T4), so every header
    the check passes is sent. Never logs or raises with the value."""
    import feature_config

    return feature_config.parse_header_pairs(raw) or {}


def header_secrets(headers) -> list[str]:
    """Every string that must never appear in a log line: each value, the token after "Basic "/"Bearer ",
    and for Basic auth the decoded "user:token" and its token part."""
    out = []
    for value in headers.values():
        out.append(value)
        scheme, _, token = value.partition(" ")
        if token and scheme.lower() in ("basic", "bearer"):
            out.append(token)
            if scheme.lower() == "basic":
                try:
                    decoded = base64.b64decode(token, validate=True).decode("utf-8")
                except (ValueError, UnicodeDecodeError):
                    decoded = ""
                if decoded:
                    out.append(decoded)
                    out.append(decoded.partition(":")[2])
    return [s for s in out if s]


def take_headers(environ=None, raw=None) -> dict:
    """The OTLP headers (from `raw` when given, e.g. feature_config's otlp_headers, else from the variable),
    after removing OTEL_EXPORTER_OTLP_HEADERS and the per-signal header variables (SIGNAL_HEADERS_ENV, never
    used) from environ (os.environ by default) and registering every secret part of all of them with
    tool_errors.register_secret()."""
    environ = os.environ if environ is None else environ
    from_env = environ.pop(HEADERS_ENV, None)
    others = [environ.pop(name, None) for name in SIGNAL_HEADERS_ENV]
    headers = parse_headers(raw if raw else from_env)
    try:
        import tool_errors
    except ImportError:  # evals import this module with ext/ on sys.path; tool_errors is always there
        tool_errors = None
    if tool_errors is not None:
        secrets = header_secrets(headers)
        for value in [from_env] + others:
            if isinstance(value, str) and value:
                secrets += [value] + header_secrets(parse_headers(value))
        for s in secrets:
            tool_errors.register_secret(s)
    return headers


def endpoint_ok(url) -> bool:
    """https://host[:port][/path] (http only for 127.0.0.1/localhost/::1); no query, fragment or userinfo."""
    if not isinstance(url, str) or not url:
        return False
    try:
        p = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    if p.query or p.fragment or p.username or p.password or not p.hostname:
        return False
    if p.scheme == "https":
        return True
    return p.scheme == "http" and p.hostname in ("127.0.0.1", "localhost", "::1")


def signal_url(endpoint, signal) -> str:
    """OTEL_EXPORTER_OTLP_ENDPOINT semantics: <base>/v1/traces, <base>/v1/metrics."""
    return endpoint.rstrip("/") + f"/v1/{signal}"


# --- export-error log collapse (pure logging) ----------------------------------------------------------

class CollapseExportErrors(logging.Filter):
    """On the "opentelemetry" loggers: swallow every record, and instead write one fixed WARNING per kind
    ("telemetry: export failed: http_401") on garmin_ext.telemetry at most once per `every` seconds. The
    OTel texts (endpoint, response body, exception) never reach the logs."""

    def __init__(self, every=ERROR_LOG_EVERY_S, clock=time.monotonic, out=None):
        super().__init__()
        self.every, self.clock, self.out = every, clock, out or log
        self.last: dict = {}
        self.counts: dict = {}
        self._lock = threading.Lock()

    @staticmethod
    def kind(record) -> str | None:
        msg = str(record.msg)
        args = record.args if isinstance(record.args, tuple) else ()
        if msg.startswith("Transient error"):
            return None  # the exporter retries; only the final failure counts
        if msg.startswith("Failed to export") and "code:" in msg and len(args) >= 2:
            code = args[1]
            if isinstance(code, int) and 100 <= code <= 599:
                return f"http_{code}"
            return "connection"
        if "timeout" in msg:
            return "timeout"
        if msg.startswith("Queue full"):
            return "queue_full"
        if "encode" in msg:
            return "encode"
        if record.levelno >= logging.WARNING:
            return "other"
        return None

    def filter(self, record) -> bool:
        try:
            k = self.kind(record)
            if k is None:
                return False
            with self._lock:
                self.counts[k] = self.counts.get(k, 0) + 1
                now = self.clock()
                if now - self.last.get(k, -math.inf) < self.every:
                    return False
                self.last[k] = now
            self.out.warning("telemetry: export failed: %s", k)
        except Exception:  # noqa: BLE001 - logging must never fail the export thread
            pass
        return False


_COLLAPSE: CollapseExportErrors | None = None


def _install_log_collapse() -> None:
    global _COLLAPSE
    if _COLLAPSE is not None:
        return
    _COLLAPSE = CollapseExportErrors()
    for name in ("opentelemetry", "opentelemetry.exporter", "opentelemetry.sdk",
                 "opentelemetry.exporter.otlp.common.http", "opentelemetry.sdk._shared_internal",
                 "opentelemetry.exporter.otlp.proto.http.trace_exporter",
                 "opentelemetry.exporter.otlp.proto.http.metric_exporter"):
        lg = logging.getLogger(name)
        lg.addFilter(_COLLAPSE)
    # Handlers on the root logger see records of child loggers whose own filters did not run for them
    # (filters run only on the logger a record was created on), so the base logger also stops propagation
    # through a handler-level filter.
    base = logging.getLogger("opentelemetry")
    base.propagate = False
    handler = logging.StreamHandler()
    handler.addFilter(_COLLAPSE)
    base.addHandler(handler)


# --- state ---------------------------------------------------------------------------------------------

class _State:
    def __init__(self):
        self.on = False
        self.tracer = None
        self.tracer_provider = None
        self.meter_provider = None
        self.instruments: dict = {}
        self.lock = threading.Lock()
        self.failed_once = False


_S = _State()
_REMOTE_PARENT = contextvars.ContextVar("coach_remote_parent", default=False)


def enabled() -> bool:
    return _S.on


def _disable(where: str) -> None:
    """A bug in a helper: log once (no detail) and turn telemetry off for the process."""
    if not _S.failed_once:
        _S.failed_once = True
        log.warning("telemetry: internal error in %s; telemetry is off for this process", where)
    _S.on = False


# --- metrics ---------------------------------------------------------------------------------------------

DURATION_BUCKETS = (0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10, 20, 30, 60, 120, 300)
SIZE_BUCKETS = (256, 1024, 4096, 16384, 65536, 262144)
WAIT_BUCKETS = (1, 10, 60, 300, 1800, 3600, 21600, 86400)
TOKEN_BUCKETS = (100, 1000, 5000, 10000, 20000, 50000, 100000, 200000)

# name: (kind, unit, description, allowed keys, buckets). In Grafana (OTLP to Prometheus names) dots become
# underscores, counters end in _total and the units s, By and d add _seconds, _bytes and _days; a unit in
# braces adds nothing: coach.tool.calls -> coach_tool_calls_total, coach.tool.duration ->
# coach_tool_duration_seconds_bucket. docs/tracing.md lists them.
METRICS = {
    "coach.tool.calls": ("counter", "{call}", "Tool calls by outcome",
                         ("coach.tool", "coach.outcome", "coach.error_group", "coach.mode"), None),
    "coach.tool.duration": ("histogram", "s", "Tool call duration", ("coach.tool", "coach.mode"), DURATION_BUCKETS),
    "coach.tool.answer.size": ("histogram", "By", "Tool answer size", ("coach.tool",), SIZE_BUCKETS),
    "coach.garmin.requests": ("counter", "{request}", "Garmin calls by caller and outcome",
                              ("coach.caller", "coach.outcome", "coach.error_group", "coach.mode"), None),
    "coach.garmin.request.duration": ("histogram", "s", "Garmin call duration",
                                      ("coach.caller", "coach.mode"), DURATION_BUCKETS),
    "coach.garmin.cooldown.active": ("gauge", "{state}", "1 while the shared Garmin cooldown is on",
                                     ("coach.error_group",), None),
    "coach.garmin.token": ("counter", "{lookup}", "Key-mode token source", ("coach.garmin.token_source",), None),
    "coach.status.sources": ("counter", "{source}", "Status sources by outcome",
                             ("coach.source.name", "coach.outcome"), None),
    "coach.history.lag_days": ("gauge", "d", "Days between yesterday and the newest indexed day", (), None),
    "coach.history.searches": ("counter", "{search}", "History tool calls",
                               ("coach.tool", "coach.outcome", "coach.history.channels"), None),
    "coach.jobs": ("counter", "{job}", "Job state changes", ("coach.job.kind", "coach.job.status"), None),
    "coach.queue.depth": ("gauge", "{job}", "Jobs by status", ("coach.job.status",), None),
    "coach.queue.oldest_age": ("gauge", "s", "Age of the oldest queued job", (), None),
    "coach.queue.wait": ("histogram", "s", "Queue wait before a job starts", ("coach.job.kind",), WAIT_BUCKETS),
    "gen_ai.client.token.usage": ("histogram", "{token}", "Tokens per model call (evals)",
                                  ("gen_ai.token.type", "gen_ai.request.model"), TOKEN_BUCKETS),
    "coach.eval.cost": ("counter", "{USD}", "Eval spend", ("gen_ai.request.model", "coach.eval.variant"), None),
}
# metric-only key
_METRIC_ONLY = {"gen_ai.token.type": _enum("input", "output", "cache_read", "cache_creation")}


def _metric_attrs(name, attrs):
    keys = METRICS[name][3]
    allowed = {k: (ALLOWED_ATTRS.get(k) or _METRIC_ONLY[k]) for k in keys}
    return clean_attrs(attrs, allowed)


def _record(name, value, attrs, kw):
    if not _S.on:
        return
    try:
        spec = METRICS.get(name)
        if spec is None:
            if STRICT:
                raise ValueError(f"telemetry: unknown metric {name!r}")
            return
        clean = _metric_attrs(name, _expand(attrs, kw))
        inst = _S.instruments.get(name)
        if inst is None:
            return
        if spec[0] == "counter":
            inst.add(value, clean)
        elif spec[0] == "histogram":
            inst.record(value, clean)
        else:
            inst.set(value, clean)
    except ValueError:
        if STRICT:
            raise
    except Exception:  # noqa: BLE001
        _disable("metric")


def metric_count(name, value=1, *, attrs=None, **kw) -> None:
    """Add value to counter `name` (METRICS)."""
    _record(name, value, attrs, kw)


def metric_observe(name, value, *, attrs=None, **kw) -> None:
    """Record value in histogram `name` (METRICS)."""
    _record(name, value, attrs, kw)


def metric_gauge(name, value, *, attrs=None, **kw) -> None:
    """Set gauge `name` (METRICS) to value."""
    _record(name, value, attrs, kw)


# --- spans ---------------------------------------------------------------------------------------------

class _NoopSpan:
    def set(self, attrs=None, **kw):
        return None

    def error(self, group=None):
        return None


_NOOP = _NoopSpan()


class _Span:
    def __init__(self, span):
        self._span = span

    def set(self, attrs=None, **kw):
        try:
            self._span.set_attributes(clean_attrs(_expand(attrs, kw)))
        except ValueError:
            if STRICT:
                raise
        except Exception:  # noqa: BLE001
            _disable("span.set")

    def error(self, group=None):
        """Mark the span failed with a fixed group (never an error text)."""
        try:
            from opentelemetry.trace import Status, StatusCode

            if group in ERROR_GROUPS:
                self._span.set_attribute("coach.error_group", group)
                self._span.set_attribute("error.type", group)
            self._span.set_status(Status(StatusCode.ERROR))
        except Exception:  # noqa: BLE001
            _disable("span.error")


_KINDS = ("internal", "client", "server", "producer", "consumer")


def _parent_context(traceparent):
    from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

    return TraceContextTextMapPropagator().extract({"traceparent": traceparent})


@contextlib.contextmanager
def span(name, attrs=None, *, parent=None, links=None, kind="internal", **kw):
    """A span named `name` (see clean_span_name) with allowlisted attributes. parent: a traceparent string
    (a job's enqueuer) the span becomes a child of; links: from extract_link(). Exceptions raised inside
    pass through unchanged; the span records only error.type=exception, never the text."""
    if not _S.on:
        yield _NOOP
        return
    try:
        from opentelemetry import trace

        clean = clean_attrs(_expand(attrs, kw))
        ctx = _parent_context(parent) if valid_traceparent(parent) else None
        cm = _S.tracer.start_as_current_span(
            name, context=ctx, kind=getattr(trace.SpanKind, (kind if kind in _KINDS else "internal").upper()),
            attributes=clean, links=[lk for lk in (links or ()) if lk is not None],
            record_exception=False, set_status_on_exception=False)
        otel_span = cm.__enter__()
    except ValueError:
        if STRICT:
            raise
        yield _NOOP
        return
    except Exception:  # noqa: BLE001
        _disable("span")
        yield _NOOP
        return
    handle = _Span(otel_span)
    try:
        yield handle
    except BaseException as e:
        with contextlib.suppress(Exception):
            if not isinstance(e, GeneratorExit):
                from opentelemetry.trace import Status, StatusCode

                otel_span.set_attribute("error.type", "exception")
                otel_span.set_status(Status(StatusCode.ERROR))
        with contextlib.suppress(Exception):
            cm.__exit__(type(e), e, e.__traceback__)
        raise
    else:
        with contextlib.suppress(Exception):
            cm.__exit__(None, None, None)


def inject() -> str | None:
    """The W3C traceparent of the current span (for a job's payload), or None when off or no span."""
    if not _S.on:
        return None
    try:
        from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

        carrier: dict = {}
        TraceContextTextMapPropagator().inject(carrier)
        tp = carrier.get("traceparent")
        return tp if valid_traceparent(tp) else None
    except Exception:  # noqa: BLE001
        _disable("inject")
        return None


def extract_link(traceparent):
    """A span Link to the span named by `traceparent` (a stored job's), or None when off or malformed."""
    if not _S.on or not valid_traceparent(traceparent):
        return None
    try:
        from opentelemetry import trace

        sc = trace.get_current_span(_parent_context(traceparent)).get_span_context()
        return trace.Link(sc) if sc.is_valid else None
    except Exception:  # noqa: BLE001
        _disable("extract_link")
        return None


def bind_context(fn):
    """fn wrapped to run in the caller's current trace context (for threading.Thread targets, which do not
    inherit contextvars). Returns fn unchanged when off."""
    if not _S.on:
        return fn
    ctx = contextvars.copy_context()

    def run(*a, **k):
        return ctx.run(fn, *a, **k)
    return run


# --- SDK parts (imported only when on) -------------------------------------------------------------------

def _sdk_classes():
    """Classes built on SDK base classes; defined lazily so the module imports without the SDK."""
    from opentelemetry.context import Context
    from opentelemetry.propagators.textmap import TextMapPropagator
    from opentelemetry.sdk.metrics.export import (
        Gauge, Histogram, Metric, MetricExporter, MetricExportResult, MetricsData, ResourceMetrics,
        ScopeMetrics, Sum,
    )
    from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor
    from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
    from opentelemetry.sdk.util.instrumentation import InstrumentationScope
    from opentelemetry.trace import Link, Status

    def scope_of(scope):
        name = getattr(scope, "name", "") if scope is not None else ""
        return InstrumentationScope(name if name in SCOPES else "other", None)

    class FilteringSpanExporter(SpanExporter):
        """Rebuilds every span from the allowlist before the inner exporter encodes it."""

        def __init__(self, inner, resource):
            self.inner, self.resource = inner, resource

        @staticmethod
        def rebuild(s, resource):
            attrs = dict(s.attributes or {})
            if "gen_ai.tool.name" in attrs:
                attrs["gen_ai.tool.name"] = tool_label(attrs["gen_ai.tool.name"])
            return ReadableSpan(
                name=clean_span_name(s.name), context=s.context, parent=s.parent, resource=resource,
                attributes=_clean_quiet(attrs), events=(),
                links=tuple(Link(lk.context) for lk in (s.links or ())), kind=s.kind,
                status=Status(s.status.status_code), start_time=s.start_time, end_time=s.end_time,
                instrumentation_scope=scope_of(s.instrumentation_scope))

        def export(self, spans):
            try:
                rebuilt = [self.rebuild(s, self.resource) for s in spans]
            except Exception:  # noqa: BLE001 - never send an unfiltered span
                log.warning("telemetry: export filter failed; batch dropped")
                return SpanExportResult.FAILURE
            return self.inner.export(rebuilt)

        def shutdown(self):
            return self.inner.shutdown()

        def force_flush(self, timeout_millis=30000):
            return self.inner.force_flush(timeout_millis)

    class FilteringMetricExporter(MetricExporter):
        """Keeps only METRICS names; drops a data point whose attributes fail its allowlist; no exemplars."""

        def __init__(self, inner, resource):
            super().__init__(preferred_temporality=getattr(inner, "_preferred_temporality", None),
                             preferred_aggregation=getattr(inner, "_preferred_aggregation", None))
            self.inner, self.resource = inner, resource

        def rebuild(self, data):
            out_metrics = []
            for rm in data.resource_metrics:
                for sm in rm.scope_metrics:
                    for m in sm.metrics:
                        if m.name not in METRICS:
                            continue
                        keys = METRICS[m.name][3]
                        allowed = {k: (ALLOWED_ATTRS.get(k) or _METRIC_ONLY[k]) for k in keys}
                        points = []
                        for dp in m.data.data_points:
                            raw = dict(dp.attributes or {})
                            clean = _clean_quiet(raw, allowed)
                            if clean != raw:
                                continue
                            points.append(_replace(dp, attributes=clean, exemplars=[]))
                        if not points:
                            continue
                        if isinstance(m.data, Sum):
                            d = Sum(points, m.data.aggregation_temporality, m.data.is_monotonic)
                        elif isinstance(m.data, Histogram):
                            d = Histogram(points, m.data.aggregation_temporality)
                        elif isinstance(m.data, Gauge):
                            d = Gauge(points)
                        else:
                            continue
                        spec = METRICS[m.name]
                        out_metrics.append(Metric(m.name, spec[2], spec[1], d))
            scope = InstrumentationScope(SCOPE_NAME, None)
            return MetricsData([ResourceMetrics(self.resource, [ScopeMetrics(scope, out_metrics, "")], "")])

        def export(self, metrics_data, timeout_millis=10000, **kwargs):
            try:
                data = self.rebuild(metrics_data)
            except Exception:  # noqa: BLE001
                log.warning("telemetry: metric filter failed; batch dropped")
                return MetricExportResult.FAILURE
            return self.inner.export(data, timeout_millis=timeout_millis, **kwargs)

        def force_flush(self, timeout_millis=10000):
            return self.inner.force_flush(timeout_millis)

        def shutdown(self, timeout_millis=30000, **kwargs):
            return self.inner.shutdown(timeout_millis=timeout_millis, **kwargs)

    class IgnoreInbound(TextMapPropagator):
        """Extracts nothing (no trace context, no baggage) and injects nothing. Notes that a traceparent
        was sent, so the next span started in this context gets coach.remote_parent=true."""

        def extract(self, carrier, context=None, getter=None):
            try:
                if isinstance(carrier, dict) and "traceparent" in carrier:
                    _REMOTE_PARENT.set(True)
            except Exception:  # noqa: BLE001
                pass
            return context if context is not None else Context()

        def inject(self, carrier, context=None, setter=None):
            return None

        @property
        def fields(self):
            return set()

    class RemoteParentMarker(SpanProcessor):
        def on_start(self, span, parent_context=None):
            try:
                if _REMOTE_PARENT.get():
                    _REMOTE_PARENT.set(False)
                    span.set_attribute("coach.remote_parent", True)
            except Exception:  # noqa: BLE001
                pass

        def on_end(self, span):
            return None

        def shutdown(self):
            return None

        def force_flush(self, timeout_millis=30000):
            return True

    return FilteringSpanExporter, FilteringMetricExporter, IgnoreInbound, RemoteParentMarker


_GLOBAL_SET = False


def _install_global(trace) -> None:
    """Set the global tracer provider once, to one that forwards to whichever provider start() built last
    (the mcp SDK caches its tracer on first use, and the global provider can be set only once)."""
    global _GLOBAL_SET
    if _GLOBAL_SET:
        return

    class Forwarding(trace.Tracer):
        def __init__(self, args):
            self.args = args

        def _real(self):
            tp = _S.tracer_provider if _S.on else None
            return tp.get_tracer(*self.args) if tp is not None else trace.NoOpTracer()

        def start_span(self, *a, **k):
            return self._real().start_span(*a, **k)

        def start_as_current_span(self, *a, **k):
            return self._real().start_as_current_span(*a, **k)

    class Provider(trace.TracerProvider):
        def get_tracer(self, instrumenting_module_name, instrumenting_library_version=None, schema_url=None,
                       attributes=None):
            return Forwarding((instrumenting_module_name, instrumenting_library_version))

    trace.set_tracer_provider(Provider())
    _GLOBAL_SET = True


def _replace(dp, **changes):
    import dataclasses

    return dataclasses.replace(dp, **changes)


def _clean_quiet(attrs, allowed=None):
    """clean_attrs that never raises (the export filter must not fail on an SDK span in strict tests)."""
    return clean_attrs(attrs, allowed, strict=False)


def _resource(role, environment):
    from opentelemetry.sdk.resources import Resource

    try:
        import version

        ver = version.VERSION
    except ImportError:
        ver = "0.0.0"
    mode = os.environ.get("GARMIN_AUTH")
    raw = {"service.name": SERVICE_NAME, "service.version": ver, "deployment.environment.name": environment,
           "coach.role": role}
    if mode in ("full", "key"):
        raw["coach.mode"] = mode
    return Resource(_clean_quiet(raw, RESOURCE_ATTRS))  # the constructor reads no OTEL_* variable


def start(span_exporters, metric_exporters=(), *, role="app", environment="production",
          schedule_delay_ms=SCHEDULE_DELAY_MS, metric_interval_ms=METRIC_INTERVAL_MS,
          set_global=True) -> bool:
    """Turn telemetry on with these SDK exporters (each wrapped in the export filter). setup() calls it with
    the OTLP exporters; evals/tracing.py and the tests call it with theirs. True when on."""
    with _S.lock:
        if _S.on:
            return True
        from opentelemetry import trace
        from opentelemetry.metrics import NoOpMeterProvider
        from opentelemetry.propagate import set_global_textmap
        from opentelemetry.sdk.metrics import AlwaysOffExemplarFilter, MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.metrics.view import ExplicitBucketHistogramAggregation, View
        from opentelemetry.sdk.trace import SpanLimits, TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.sdk.trace.sampling import ALWAYS_ON

        span_filter, metric_filter, ignore_inbound, marker = _sdk_classes()
        resource = _resource(role, environment)
        noop_meter = NoOpMeterProvider()
        tp = TracerProvider(sampler=ALWAYS_ON, resource=resource, shutdown_on_exit=False,
                            span_limits=SpanLimits(max_attributes=32, max_events=0, max_links=8,
                                                   max_attribute_length=256),
                            meter_provider=noop_meter)
        tp.add_span_processor(marker())
        for exp in span_exporters:
            tp.add_span_processor(BatchSpanProcessor(
                span_filter(exp, resource), max_queue_size=QUEUE_SIZE, schedule_delay_millis=schedule_delay_ms,
                max_export_batch_size=BATCH_SIZE, export_timeout_millis=EXPORT_TIMEOUT_S * 1000,
                meter_provider=noop_meter))
        readers = [PeriodicExportingMetricReader(metric_filter(exp, resource), export_interval_millis=metric_interval_ms,
                                                 export_timeout_millis=10000) for exp in metric_exporters]
        views = [View(instrument_name=name, aggregation=ExplicitBucketHistogramAggregation(spec[4]))
                 for name, spec in METRICS.items() if spec[0] == "histogram"]
        mp = MeterProvider(metric_readers=readers, resource=resource, exemplar_filter=AlwaysOffExemplarFilter(),
                           shutdown_on_exit=False, views=views)
        meter = mp.get_meter(SCOPE_NAME)
        instruments = {}
        for name, (kind, unit, desc, _, _) in METRICS.items():
            make = {"counter": meter.create_counter, "histogram": meter.create_histogram,
                    "gauge": meter.create_gauge}[kind]
            instruments[name] = make(name, unit=unit, description=desc)
        if set_global:
            _install_global(trace)  # the mcp SDK's spans use the global provider
            set_global_textmap(ignore_inbound())
        _install_log_collapse()
        _S.tracer_provider, _S.meter_provider, _S.instruments = tp, mp, instruments
        _S.tracer = tp.get_tracer(SCOPE_NAME)
        _S.on = True
        return True


def setup(cfg, environ=None) -> None:
    """Start exporting to OTEL_EXPORTER_OTLP_ENDPOINT when cfg.tracing; otherwise do nothing at all.
    cfg: feature_config.load()'s result (reads .tracing, .otlp_endpoint, .role). Never raises: a problem
    logs one fixed line and leaves telemetry off (feature_config's --check-config stops a bad config
    before this runs)."""
    environ = os.environ if environ is None else environ
    if not getattr(cfg, "tracing", False):
        return
    if str(environ.get("OTEL_SDK_DISABLED", "")).strip().lower() == "true":
        log.info("telemetry: OTEL_SDK_DISABLED=true; tracing stays off")
        return
    try:
        refused = refused_env(environ)
        # The header variables leave the environment either way, also when tracing then stays off.
        headers = take_headers(environ, getattr(cfg, "otlp_headers", None))
        if refused:
            log.warning("telemetry: a refused OTEL_* variable is set (see docs/tracing.md); tracing stays off")
            return
        endpoint = getattr(cfg, "otlp_endpoint", None) or environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
        if not endpoint_ok(endpoint):
            log.warning("telemetry: OTEL_EXPORTER_OTLP_ENDPOINT is missing or not an https URL; tracing stays off")
            return
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.metrics import NoOpMeterProvider

        noop = NoOpMeterProvider()
        spans = OTLPSpanExporter(endpoint=signal_url(endpoint, "traces"), headers=headers, timeout=EXPORT_TIMEOUT_S,
                                 meter_provider=noop)
        metrics = OTLPMetricExporter(endpoint=signal_url(endpoint, "metrics"), headers=headers,
                                     timeout=EXPORT_TIMEOUT_S, meter_provider=noop)
        role = getattr(cfg, "role", "app")
        start([spans], [metrics], role=role if role in ("app", "worker", "enqueue") else "app")
        log.info("telemetry: on (OTLP/HTTP)")
    except ImportError:
        log.warning("telemetry: TRACING=on but the OpenTelemetry SDK is not installed; tracing stays off")
    except Exception:  # noqa: BLE001
        _disable("setup")


def shutdown(cap_s=SHUTDOWN_CAP_S) -> None:
    """Flush and stop the exporters, waiting at most cap_s seconds in all (a dead backend never holds the
    process). Safe to call when off or twice."""
    tp, mp = _S.tracer_provider, _S.meter_provider
    _S.on = False
    _S.tracer_provider = _S.meter_provider = None
    if tp is None or mp is None:
        return

    def stop():
        for fn in (lambda: tp.force_flush(int(cap_s * 1000)), tp.shutdown,
                   lambda: mp.shutdown(timeout_millis=cap_s * 1000)):
            with contextlib.suppress(Exception):
                fn()
    t = threading.Thread(target=stop, name="telemetry-shutdown", daemon=True)
    t.start()
    t.join(cap_s)


def selfcheck() -> list[str]:
    """[] when the SDK and every hook this module relies on import and work (the Docker build runs it);
    otherwise one line per problem."""
    problems = []
    try:
        import inspect

        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.metrics import AlwaysOffExemplarFilter  # noqa: F401
        from opentelemetry.sdk.trace import ReadableSpan
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        for cls, params in ((ReadableSpan, ("name", "context", "parent", "resource", "attributes", "events", "links",
                                            "kind", "status", "start_time", "end_time", "instrumentation_scope")),
                            (OTLPSpanExporter, ("endpoint", "headers", "timeout", "meter_provider")),
                            (BatchSpanProcessor, ("max_queue_size", "export_timeout_millis", "meter_provider"))):
            have = inspect.signature(cls.__init__).parameters
            missing = [p for p in params if p not in have]
            if missing:
                problems.append(f"telemetry: {cls.__name__} lacks {', '.join(missing)}")
        _sdk_classes()
    except Exception as e:  # noqa: BLE001
        problems.append(f"telemetry: OpenTelemetry SDK unusable ({type(e).__name__})")
    return problems


def _reset_for_tests() -> None:
    """Turn off and forget the providers (tests only; the global tracer provider cannot be unset)."""
    shutdown(cap_s=1.0)
    _S.__init__()
    _REMOTE_PARENT.set(False)
