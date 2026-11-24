"""Traces of live eval runs: Langfuse (and, optionally, the server's Grafana stack), off unless configured.

A live run (python -m evals run) exports one trace per trial when either destination is set:

    Langfuse   LANGFUSE_PUBLIC_KEY + LANGFUSE_SECRET_KEY, optional LANGFUSE_BASE_URL (or LANGFUSE_HOST;
               default https://cloud.langfuse.com, the EU region). Spans go over OTLP/HTTP to
               <base>/api/public/otel/v1/traces with Basic auth (public:secret), no Langfuse SDK.
    Grafana    TRACING=on + OTEL_EXPORTER_OTLP_ENDPOINT + OTEL_EXPORTER_OTLP_HEADERS, the same variables the
               server reads (docs/tracing.md): spans and the token/cost metrics go there too, tagged
               deployment.environment.name=eval.

Neither set: nothing is imported, nothing is sent, and the run is exactly as before. A half-set Langfuse
config (one key only, or a base URL that is not https) refuses the run (problems()).

The trace of one trial (docs/tracing.md, "Eval runs"):

    eval.trial                    langfuse.observation.type=agent; case id, variant, trial, model, stop reason,
    |                             turns, cost (coach.eval.*); Langfuse trace name "eval <case> <variant>",
    |                             session = the run id, tags = variant and model
    +- chat <model>               one per Messages call (wrap_client): generation; tokens in, out, cache read,
    |                             cache write; cost in USD (evals/cost.py prices)
    +- execute_tool <tool>        one per server tool call (tool_call); type tool; the server's own spans
       +- tools/call <tool>       (mcp SDK SERVER span, ext/telemetry.py) nest under it, in process
          +- tool <tool> ...

Judge calls get a root "chat <model>" span of their own. Prompt text, answers, tool arguments and tool
results are never exported (the fixtures are synthetic, but one rule holds everywhere): every attribute
passes ext/telemetry.py's allowlist and export filter. LangSmith is not involved.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import sys

from evals import cost as cost_mod

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXT = os.path.join(ROOT, "ext")
LANGFUSE_DEFAULT = "https://cloud.langfuse.com"
LANGFUSE_PATH = "/api/public/otel/v1/traces"
FLUSH_CAP_S = 10.0

_ON = False


def _telemetry():
    if EXT not in sys.path:
        sys.path.insert(0, EXT)
    import telemetry

    return telemetry


def _langfuse_env(env):
    pk, sk = env.get("LANGFUSE_PUBLIC_KEY", "").strip(), env.get("LANGFUSE_SECRET_KEY", "").strip()
    base = (env.get("LANGFUSE_BASE_URL") or env.get("LANGFUSE_HOST") or "").strip()
    return pk, sk, base


def wanted(env=None) -> bool:
    """True when the environment asks for eval traces (any LANGFUSE_* key, or TRACING=on)."""
    env = os.environ if env is None else env
    pk, sk, _ = _langfuse_env(env)
    return bool(pk or sk) or env.get("TRACING", "").strip().lower() == "on"


def problems(env=None) -> list[str]:
    """One line per configuration problem (never showing a key); [] when fine or when nothing is set."""
    env = os.environ if env is None else env
    out = []
    pk, sk, base = _langfuse_env(env)
    if pk or sk:
        if not (pk and sk):
            out.append("eval traces: set both LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY, or neither")
        if base and not _endpoint_ok(base):
            out.append("eval traces: LANGFUSE_BASE_URL must be an https URL (no query or user info)")
    if env.get("TRACING", "").strip().lower() == "on":
        if not _endpoint_ok(env.get("OTEL_EXPORTER_OTLP_ENDPOINT", "")):
            out.append("eval traces: TRACING=on needs OTEL_EXPORTER_OTLP_ENDPOINT (an https URL)")
        if not env.get("OTEL_EXPORTER_OTLP_HEADERS"):
            out.append("eval traces: TRACING=on needs OTEL_EXPORTER_OTLP_HEADERS")
    return out


def _endpoint_ok(url) -> bool:
    # pure Python: importing ext/telemetry.py does not import OpenTelemetry
    return _telemetry().endpoint_ok(url)


def langfuse_target(env) -> tuple[str, dict, list[str]]:
    """(traces URL, headers, secret strings) for the Langfuse OTLP endpoint."""
    pk, sk, base = _langfuse_env(env)
    base = (base or LANGFUSE_DEFAULT).rstrip("/")
    token = base64.b64encode(f"{pk}:{sk}".encode()).decode()
    return base + LANGFUSE_PATH, {"Authorization": "Basic " + token, "x-langfuse-ingestion-version": "4"}, \
        [sk, token, f"{pk}:{sk}"]


def setup(env=None, exporters=None) -> bool:
    """Start eval tracing when the environment asks for it; True when on. Takes the secrets out of env
    (os.environ by default). exporters: [(span_exporter, metric_exporter_or_None)] for tests."""
    global _ON
    env = os.environ if env is None else env
    if _ON:
        return True
    if exporters is None and (not wanted(env) or problems(env)):
        return False
    t = _telemetry()
    spans, metrics = [], []
    if exporters is not None:
        for s, m in exporters:
            spans.append(s)
            if m is not None:
                metrics.append(m)
    else:
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.metrics import NoOpMeterProvider

        noop = NoOpMeterProvider()
        # Take every OTLP header variable out of the environment before building any exporter: the OTLP
        # exporter merges OTEL_EXPORTER_OTLP_HEADERS (and the per-signal ones) from os.environ into its own
        # headers, so Grafana's headers would otherwise also go to Langfuse.
        grafana_headers = t.take_headers(env)
        if env is not os.environ:
            t.take_headers(os.environ)
        pk, sk, _ = _langfuse_env(env)
        if pk and sk:
            url, headers, secrets = langfuse_target(env)
            env.pop("LANGFUSE_SECRET_KEY", None)
            _register(t, secrets)
            spans.append(OTLPSpanExporter(endpoint=url, headers=headers, timeout=t.EXPORT_TIMEOUT_S,
                                          meter_provider=noop))
        if env.get("TRACING", "").strip().lower() == "on":
            endpoint = env.get("OTEL_EXPORTER_OTLP_ENDPOINT")
            spans.append(OTLPSpanExporter(endpoint=t.signal_url(endpoint, "traces"), headers=grafana_headers,
                                          timeout=t.EXPORT_TIMEOUT_S, meter_provider=noop))
            metrics.append(OTLPMetricExporter(endpoint=t.signal_url(endpoint, "metrics"), headers=grafana_headers,
                                              timeout=t.EXPORT_TIMEOUT_S, meter_provider=noop))
    _ON = t.start(spans, metrics, role="eval", environment="eval")
    return _ON


def _register(t, secrets):
    try:
        import tool_errors
    except ImportError:
        return
    for s in secrets:
        tool_errors.register_secret(s)


def shutdown() -> None:
    """Flush (at most FLUSH_CAP_S) and stop; no-op when off."""
    global _ON
    if not _ON:
        return
    _ON = False
    _telemetry().shutdown(cap_s=FLUSH_CAP_S)


def enabled() -> bool:
    return _ON


# --- hooks the runner calls -------------------------------------------------------------------------------

class _Trial:
    def __init__(self, cm, handle, run_id):
        self.cm, self.handle, self.run_id = cm, handle, run_id


_RUN_ID = {"id": None}


def set_run(run_id) -> None:
    """The run id becomes the Langfuse session of every trial trace."""
    _RUN_ID["id"] = run_id


def known_tools(names) -> None:
    """The server's tool names (the only ones that may appear in span names)."""
    if _ON:
        _telemetry().register_tools(names)


def _session():
    rid = _RUN_ID["id"]
    return {"langfuse.session.id": rid} if rid else {}


def begin_trial(case_id, variant, trial, model):
    """Open the trial's root span and make it current; pass the result to end_trial(). None when off."""
    if not _ON:
        return None
    t = _telemetry()
    attrs = {"coach.caller": "eval", "coach.eval.case_id": case_id, "coach.eval.variant": variant,
             "coach.eval.trial": trial, "gen_ai.request.model": model, "langfuse.observation.type": "agent",
             "langfuse.trace.name": f"eval {case_id} {variant}", "langfuse.trace.tags": [variant, model],
             **_session()}
    if _RUN_ID["id"]:
        attrs["coach.eval.run_id"] = _RUN_ID["id"]
    cm = t.span("eval.trial", attrs)
    handle = cm.__enter__()
    return _Trial(cm, handle, _RUN_ID["id"])


def end_trial(token, tr) -> None:
    """Close the trial span with the transcript's numbers (stop reason, turns, cost); never raises."""
    if token is None:
        return
    with contextlib.suppress(Exception):
        attrs = {"coach.eval.stop_reason": tr.get("stop_reason"), "coach.eval.turn": tr.get("turns") or None,
                 "coach.eval.cost_usd": tr.get("cost_usd")}
        token.handle.set({k: v for k, v in attrs.items() if v is not None})
        if tr.get("error"):
            token.handle.error()
    with contextlib.suppress(Exception):
        token.cm.__exit__(None, None, None)


@contextlib.contextmanager
def tool_call(name):
    """A span around one server tool call; yields a handle with .error() for an is_error result."""
    if not _ON:
        yield _NoopHandle()
        return
    t = _telemetry()
    with t.span(f"execute_tool {name}", {"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": name,
                                         "langfuse.observation.type": "tool", **_session()}) as h:
        yield h


class _NoopHandle:
    def set(self, *a, **k):
        return None

    def error(self, *a, **k):
        return None


def _get(obj, key):
    return obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)


def _record_call(handle, model, response) -> None:
    t = _telemetry()
    u = cost_mod.usage_dict(_get(response, "usage"))
    attrs = {"gen_ai.response.model": _get(response, "model"),
             "gen_ai.usage.input_tokens": u["input_tokens"], "gen_ai.usage.output_tokens": u["output_tokens"],
             "gen_ai.usage.cache_read_input_tokens": u["cache_read_input_tokens"],
             "gen_ai.usage.cache_creation_input_tokens": u["cache_creation_input_tokens"],
             "langfuse.observation.usage_details": json.dumps({"input": u["input_tokens"], "output": u["output_tokens"],
                                                               "cache_read_input_tokens": u["cache_read_input_tokens"],
                                                               "cache_creation_input_tokens":
                                                                   u["cache_creation_input_tokens"]})}
    stop = _get(response, "stop_reason")
    if isinstance(stop, str):
        attrs["gen_ai.response.finish_reasons"] = [stop]
    try:
        usd = cost_mod.usage_cost(model, u)
    except cost_mod.UnknownModel:
        usd = None
    if usd is not None:
        attrs["gen_ai.usage.cost"] = round(usd, 8)
        attrs["coach.eval.cost_usd"] = round(usd, 8)
        attrs["langfuse.observation.cost_details"] = json.dumps({"total": round(usd, 8)})
    handle.set(attrs)
    for kind, n in (("input", u["input_tokens"]), ("output", u["output_tokens"]),
                    ("cache_read", u["cache_read_input_tokens"]),
                    ("cache_creation", u["cache_creation_input_tokens"])):
        if n:
            t.metric_observe("gen_ai.client.token.usage", n,
                             attrs={"gen_ai.token.type": kind, "gen_ai.request.model": model})
    if usd:
        t.metric_count("coach.eval.cost", usd, attrs={"gen_ai.request.model": model})


class _TracedMessages:
    def __init__(self, inner):
        self._inner = inner

    def create(self, **kwargs):
        if not _ON:
            return self._inner.create(**kwargs)
        model = kwargs.get("model", "")
        t = _telemetry()
        with t.span(f"chat {model}", {"gen_ai.operation.name": "chat", "gen_ai.provider.name": "anthropic",
                                      "gen_ai.request.model": model, "langfuse.observation.type": "generation",
                                      "langfuse.observation.model.name": model, **_session()},
                    kind="client") as h:
            response = self._inner.create(**kwargs)
            with contextlib.suppress(Exception):
                _record_call(h, model, response)
            return response


class TracedClient:
    """Wraps an Anthropic client so each messages.create is a "chat <model>" span with tokens and cost.
    Transparent when tracing is off."""

    def __init__(self, client):
        self._client = client
        self.messages = _TracedMessages(client.messages)

    def __getattr__(self, name):
        return getattr(self._client, name)


def wrap_client(client):
    return TracedClient(client)
