# Tracing and cost

Tracing is optional and **off by default**. When you turn it on, the server sends traces and
metrics about its own work to a hosted OpenTelemetry backend: which tool was called, how long it
took, whether Garmin answered, which fixed error group a failure fell into, and how big the
answer was. A dashboard and four email alerts then answer questions like "are sleep calls slow
today?" and "how many rate-limit errors did Garmin give this week, and who caused them?".

The recommended backend is **Grafana Cloud Free** (traces, metrics, dashboards and alerts in
one place, 14 days of data, no card needed). Everything is set in the Railway dashboard: no
code change, no deploy script flag, nothing running on your computer.

Live eval runs (`python -m evals run`, normally the manual GitHub Actions job) can also send
their traces, with tokens and dollars per model call, to **Langfuse** and to the same Grafana
stack. See [Eval runs](#eval-runs).

## What is sent and what never is

The code is `ext/telemetry.py`. Privacy is built in three layers:

1. Our code only sets attributes from a fixed allowlist (`ALLOWED_ATTRS`), each with a value
   check: fixed names (tool name, error group, mode, outcome), small integers (range length in
   days, attempt number) and yes/no flags.
2. An export filter rebuilds every span and metric point before it is encoded, including the
   spans the MCP SDK makes on its own. Anything outside the allowlist is dropped. Span events
   (where exception messages and tracebacks live) and status messages are dropped. A tool name
   the server does not serve becomes `other` (a client could put any text there). The JSON-RPC
   request id is kept only when it is short and plain (`^[A-Za-z0-9-]{1,64}$`).
3. The `OTEL_EXPORTER_OTLP_HEADERS` value (your Grafana token) is taken out of the process
   environment at start and every part of it is registered with the log redaction, so no log
   line can show it.

The Garmin endpoint family on a `garmin GET <family>` span is the first segment of the Garmin API
path only when it is one of the fixed Garmin service names the client library calls
(`wellness-service`, `usersummary-service`, ...; `telemetry.GARMIN_ENDPOINT_FAMILIES`); any other
segment, such as a display name, is sent as `other`.

Sent: service name and version, Garmin mode (`full` or `key`), the role (`app`, `worker`,
`enqueue`), tool names, durations, fixed error groups (`login`, `rate_limited`, `unavailable`,
`bad_input`, `no_data`, `internal`), answer size, Garmin call counts by caller (`tool` for a
live call, `worker` for the history indexer), job kinds and states, history index lag in days.

Never sent: health values, dates, names, notes, activity or account ids, tool arguments or
answers, error texts, query text, URLs or paths, IP addresses, user agents, tokens, keys, or
email addresses. Metric exemplars are off, so no trace ids ride on metrics. A test plants fake
secrets, health numbers, names, a date and a hostile request id in every place a value could
enter and checks the exact bytes sent to a local receiver (`ext/test_telemetry.py`,
`WireCanary`).

Inbound trace context is ignored: if a client sends a `traceparent`, the server still starts
its own trace and only notes `coach.remote_parent=true` on the request span. Baggage is
ignored too.

Metadata is still metadata: tool names and call times show when you check your sleep or HRV.
That goes to Grafana Labs (a third party) and is kept for 14 days on the free plan. If that is
not acceptable, leave tracing off.

## Turn it on with Grafana Cloud Free

You need a Grafana Cloud account (free, sign-up at grafana.com) and the Railway dashboard of
your deployment ([deploy-railway.md](deploy-railway.md)).

### Create the stack and the token

1. Sign up for Grafana Cloud and create a stack. Pick the region closest to your Railway
   region (EU or US).
2. In the Grafana Cloud portal, open your stack and find the **OpenTelemetry** tile
   (Configure, or Connections then "OpenTelemetry (OTLP)").
3. Generate a token there. The page then shows two environment variables:
   `OTEL_EXPORTER_OTLP_ENDPOINT` (an `https://otlp-gateway-...` address ending in `/otlp`)
   and `OTEL_EXPORTER_OTLP_HEADERS` (it starts with `Authorization=Basic%20`). Copy both.
   Treat the header like a password.

### Set the variables in Railway

1. Open the Railway dashboard (web or phone), your project, then the app service.
2. Under **Variables** add:

   | Variable | Value |
   |---|---|
   | `TRACING` | `on` |
   | `OTEL_EXPORTER_OTLP_ENDPOINT` | the endpoint from Grafana |
   | `OTEL_EXPORTER_OTLP_HEADERS` | the header from Grafana (mark it sealed if offered) |

3. Railway redeploys the service. If you run the history worker and the daily enqueue
   service ([history-worker.md](history-worker.md)), add the same three variables there, so
   one trace follows a job from the enqueuer through the queue to the worker.
4. Check the deploy logs for `telemetry: on (OTLP/HTTP)`. Make one tool call from Claude, then
   in Grafana open **Explore**, pick the traces data source and search for service name
   `coach-mcp-for-garmin`. A trace appears within about a minute.

A wrong value stops the server before it starts, with one `ERR_T...` line in the deploy logs
(`launch.py --check-config` runs these checks; the header value is never printed). Each header
name must be an HTTP header name (letters, digits and ``!#$%&'*+-.^_`|~``, such as
`X-Scope-OrgID` or `X_Scope_OrgID`), and each value printable ASCII after URL decoding (`%20` for a
space); the check and the exporter read the headers by the same rule, so a header the check
passes is always sent. A backend
that is down or refuses the token does not stop anything: tools answer as before, and the logs
get at most one line per kind of failure every 10 minutes, such as
`telemetry: export failed: http_401`.

These variables are refused, because they could add exporters or attributes past the allowlist,
or print spans to the logs: `OTEL_RESOURCE_ATTRIBUTES`, `OTEL_SERVICE_NAME`,
`OTEL_TRACES_EXPORTER`, `OTEL_METRICS_EXPORTER`, `OTEL_LOGS_EXPORTER`,
`OTEL_METRICS_EXEMPLAR_FILTER`, `OTEL_TRACES_SAMPLER`, `OTEL_TRACES_SAMPLER_ARG`,
`OTEL_PROPAGATORS`, any `OTEL_PYTHON_*`, and every `OTEL_EXPORTER_OTLP_*` other than
`OTEL_EXPORTER_OTLP_ENDPOINT` and `OTEL_EXPORTER_OTLP_HEADERS` (the OTLP exporter reads those
itself: the per-signal `OTEL_EXPORTER_OTLP_TRACES_HEADERS` and `_METRICS_HEADERS` would be merged
into its headers, and `_TRACES_ENDPOINT` would send spans elsewhere). The per-signal header
variables are also taken out of the environment at start, and Caddy starts without any of them.
`OTEL_SDK_DISABLED=true` turns tracing off even with `TRACING=on`.

### Import the dashboard

1. In Grafana open **Dashboards**, then **New**, then **Import**.
2. Upload `dashboards/grafana/coach-mcp.json` from this repository.
3. Pick your stack's Prometheus data source (named like `grafanacloud-<stack>-prom`) and its
   Tempo data source (`grafanacloud-<stack>-traces`), then **Import**.

The file holds no stack address and no ids; Grafana fills them in on import.

Panels: calls per tool, error rate by group, p50 and p95 latency per tool, answer size, Garmin
calls by caller and outcome, rate limits and logins in the last 7 days, Garmin latency, the
cooldown state, missing status sources, key-mode token source, history lag, queue depth, jobs
done and failed, queue wait, eval cost and tokens, and a table of recent traces.

### Create the four alerts

In Grafana open **Alerting**, set up an **email contact point** with your address, then create
one alert rule per row (**Alert rules**, **New alert rule**, Prometheus data source, the query
below, evaluated every 5 minutes, sent to the email contact point):

| Alert | Query | Fires when |
|---|---|---|
| Garmin rate limited | `sum(increase(coach_garmin_requests_total{coach_error_group="rate_limited"}[1h])) or sum(increase(coach_tool_calls_total{coach_error_group="rate_limited"}[1h]))` | above 0 |
| Garmin login expired | `sum(increase(coach_garmin_requests_total{coach_error_group="login"}[1h])) or sum(increase(coach_tool_calls_total{coach_error_group="login"}[1h]))` | above 0 |
| Tool error rate | `sum(increase(coach_tool_calls_total{coach_outcome="error"}[1h])) / sum(increase(coach_tool_calls_total[1h]))` | above 0.2 for 30 minutes |
| History lag | `max(coach_history_lag_days)` | above 3 (only with history search on) |

To test an alert, temporarily lower its threshold to below the current value, wait for the
email, then set it back.

If you use **Honeycomb** instead (free plan, set its OTLP endpoint and `x-honeycomb-team`
header the same way), note that its free plan has only 2 triggers: keep "rate limited" and
"login expired".

## Metric names

`ext/telemetry.py` names metrics the OpenTelemetry way; Grafana stores them under Prometheus
names (dots become underscores, counters end in `_total`, seconds, bytes and days add a unit
suffix):

| Metric | In Grafana | Labels |
|---|---|---|
| `coach.tool.calls` | `coach_tool_calls_total` | `coach_tool`, `coach_outcome`, `coach_error_group`, `coach_mode` |
| `coach.tool.duration` | `coach_tool_duration_seconds_bucket` | `coach_tool`, `coach_mode` |
| `coach.tool.answer.size` | `coach_tool_answer_size_bytes_bucket` | `coach_tool` |
| `coach.garmin.requests` | `coach_garmin_requests_total` | `coach_caller`, `coach_outcome`, `coach_error_group`, `coach_mode` |
| `coach.garmin.request.duration` | `coach_garmin_request_duration_seconds_bucket` | `coach_caller`, `coach_mode` |
| `coach.garmin.cooldown.active` | `coach_garmin_cooldown_active` | `coach_error_group` |
| `coach.garmin.token` | `coach_garmin_token_total` | `coach_garmin_token_source` |
| `coach.status.sources` | `coach_status_sources_total` | `coach_source_name`, `coach_outcome` |
| `coach.history.lag_days` | `coach_history_lag_days` | none |
| `coach.history.searches` | `coach_history_searches_total` | `coach_tool`, `coach_outcome`, `coach_history_channels` |
| `coach.jobs` | `coach_jobs_total` | `coach_job_kind`, `coach_job_status` |
| `coach.queue.depth` | `coach_queue_depth` | `coach_job_status` |
| `coach.queue.oldest_age` | `coach_queue_oldest_age_seconds` | none |
| `coach.queue.wait` | `coach_queue_wait_seconds_bucket` | `coach_job_kind` |
| `gen_ai.client.token.usage` | `gen_ai_client_token_usage_bucket` | `gen_ai_token_type`, `gen_ai_request_model` |
| `coach.eval.cost` | `coach_eval_cost_total` | `gen_ai_request_model` |

The last two rows come only from eval runs: the server and its workers never send token counts
or cost, since they make no model calls.

The whole set stays well under 2,000 series, far below the free plan's 10,000.

## Spans

```
tools/call get_daily_status     SERVER, made by the MCP SDK
+- tool get_daily_status        our tool wrapper: outcome, error group, answer size bucket
   +- source sleep              one per status data source: ok, missing, skipped, timeout
      +- garmin GET <family>    one per Garmin call: caller, status code
enqueue.daily                   the daily enqueue run (a cron service)
job index_day                   a worker job, child of the enqueuer's span (W3C traceparent
                                stored with the job), with its queue wait
history.index, retriever ...    the indexer and each retrieval channel
```

No automatic HTTP instrumentation is used: Garmin URL paths can hold the account's display
name, so only the first path segment (the endpoint family) is recorded, after a pattern check.

## Failure behaviour

- Spans go to an in-memory queue of at most 2,048; a background thread sends them every 2
  seconds with a 5 second timeout. A full queue drops spans, never blocks a tool call. A test
  points the exporter at a closed port, a port that never answers and a backend that answers
  401, and checks tool-call overhead stays within 50 ms.
- Metrics are sent every 60 seconds.
- A redeploy can lose the last few seconds of telemetry (the server gets no shutdown signal
  behind Caddy). Dashboards use `rate()` and `increase()`, so a gap does not distort them.
- A bug in the telemetry code itself is caught, logged once, and turns telemetry off for the
  process; tool answers are never affected.

## Eval runs

`evals/tracing.py` traces live eval runs. It is off unless you set Langfuse keys or
`TRACING=on`; a half-set Langfuse config refuses the run.

| Variable | Meaning |
|---|---|
| `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` | a Langfuse project's API keys (Langfuse Cloud Hobby is free) |
| `LANGFUSE_BASE_URL` | optional; default `https://cloud.langfuse.com` (EU); `https://us.cloud.langfuse.com` for US |
| `TRACING`, `OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_EXPORTER_OTLP_HEADERS` | optional; also send eval spans and token/cost metrics to Grafana, marked `deployment.environment.name=eval` |

Spans go to Langfuse over its OpenTelemetry endpoint (`/api/public/otel`, OTLP/HTTP, Basic
auth); no Langfuse SDK is installed. Each trial is one trace, named `eval <case> <variant>`,
with the run id as its Langfuse session and the variant and model as tags:

```
eval.trial                      agent: case, variant, trial, stop reason, turns, cost
+- chat claude-sonnet-5-5       generation: input, output, cache read and cache write tokens; cost in USD
+- execute_tool get_daily_status  tool
   +- tools/call get_daily_status  the server's own spans, in the same process
      +- tool get_daily_status ...
```

Judge calls are traces of their own (`chat <judge model>`). Costs come from `evals/cost.py`.
Prompts, answers, tool arguments and tool results are never sent, even though the eval data is
synthetic: Langfuse shows the structure, timings, tokens and cost, not the text. LangSmith is
not used.

For the manual GitHub Actions eval job, store the two Langfuse keys as secrets of the GitHub
environment that holds `ANTHROPIC_API_KEY`, and pass them to the "Live run" step.

## Turn it off and delete the data

Set `TRACING=off` (or delete the three variables) in the Railway dashboard. The server then
loads no OpenTelemetry code at all. Data already sent stays at Grafana until it ages out (14
days on the free plan) or until you delete the stack in the Grafana Cloud portal. In Langfuse,
delete the project. Revoke the Grafana token in the portal under Access Policies.

## Dependencies

The OpenTelemetry SDK and OTLP/HTTP exporter 1.45.2 and their closure (9 packages) are pinned
by hash in `config/requirements-telemetry.txt`, generated by a real `pip install --dry-run
--report` resolve (the file's header has the exact command), and installed in every image but
imported only when tracing is on. All are Apache-2.0 except protobuf (BSD-3-Clause).
