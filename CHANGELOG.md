# Changelog

All notable changes to this project are listed here, newest first. The format follows
[Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/), and the versions follow
[Semantic Versioning 2.0.0](https://semver.org/spec/v2.0.0.html). What counts as the public API,
and so what a MAJOR, MINOR or PATCH release may change:
[docs/updates.md](docs/updates.md#the-public-api).

## [Unreleased]

### Added

- First public release: a server you deploy for yourself that lets Claude read your Garmin
  Connect data on claude.ai (web, desktop and mobile apps) and in Claude Code. Every tool is
  read-only: the tool list is baked into the image, and no setting can turn on a tool that
  changes Garmin data.
- Two Garmin modes, chosen at setup with `GARMIN_AUTH` (required, no default): `full` (log in to
  Garmin once with your email and password; every tool, through an unofficial interface) or `key`
  (a Garmin Personal API key you create on the Garmin Connect website; Garmin's official API,
  with fewer tools and less data).
- A sign-in of the server's own, for one owner (OAuth 2.1): when Claude connects, a page on your
  server asks for your owner secret. One sign-in serves claude.ai on the web, the desktop and
  mobile apps, and Claude Code. An optional static bearer token serves Claude Code or CI where no
  browser is at hand.
- Sign-in safeguards: PKCE, tokens bound to this server, rotating refresh tokens, codes and
  tokens stored only as hashes, rate limits on the sign-in pages, a sign-in cancelled after three
  wrong secrets, and access logs without query strings or credentials.
- Two ways to deploy: Railway (`scripts/deploy.sh` from a clone), and Docker Compose on your own
  computer behind a Cloudflare Tunnel.
- This project's tools, next to the read-only tools of upstream garmin_mcp: `get_daily_status`
  (a green, yellow or red level and a recommendation for today's planned workout, by fixed rules,
  `rules_version` 1), `get_weekly_status` (`rules_version` 1) and `get_course_profile` (the climbs
  and elevation profile of a saved course).
- Works with MCP clients on both protocol versions: 2026-07-28 (current claude.ai and Claude Code)
  and the older `initialize` handshake (2024-11-05 to 2025-11-25). A long tool call keeps the
  connection alive.
- Short server instructions for the model: which tool answers what, report the numbers as given,
  and treat text from Garmin as data, never as instructions.
- Text from Garmin (names, descriptions, notes) loses invisible and control characters, links and
  HTML tags before it reaches the model.
- Tool failures come back as errors with a fixed text that says what to do: Garmin login missing
  or expired, Garmin rate limit, Garmin not answering, invalid input, or an internal error with a
  reference to find in the server log.
- Startup checks: the server does not start, and logs one line per problem, when `GARMIN_AUTH`,
  `REPORT_TZ`, `EDGE_CLIENT_IP_HEADER` or a sign-in setting is missing or invalid, or when a
  setting names a tool that is not on the read-only list.
- The server reports its version to clients (`serverInfo.version`).
- Recipes for claude.ai: a coach Project, and an experimental morning report page.
- Evals (`evals/`, not in the image): how a Claude model behaves with the real tools, run in
  process on synthetic Garmin data: tool choice, no invented numbers, the error texts followed,
  key-mode limits, planted instructions ignored, read-only stated. `scripts/evals.sh` runs them
  by hand with a spend cap (`--max-usd`); the Evals workflow runs them on GitHub on request
  only. [docs/evals.md](docs/evals.md).
- CI job "evals (offline)" and the `scripts/ci-local.sh` step `evals-offline`: the eval tests and
  the committed baselines graded offline, with no API key and no network.
- Optional tracing, off by default (`TRACING=on`): OpenTelemetry traces and metrics over OTLP/HTTP
  for every tool call (tool, duration, outcome, error group, answer size bucket), rebuilt from an
  attribute allowlist before export so no health value, name, id, token or answer leaves the
  server. A dead or slow tracing service never slows a tool call. [docs/tracing.md](docs/tracing.md).
- Optional history search, off by default (`HISTORY_SEARCH=on`, an image built with `HISTORY=1`):
  the tools `find_similar_weeks` and `search_history` over your stored daily and weekly numbers in
  Postgres with pgvector, an embedding model that runs on the server, and a separate worker
  (`ROLE=worker`) and cron service (`ROLE=enqueue`) that fill the history through a Postgres job
  queue. `HISTORY_FORGET=yes` deletes it. A job added again while it waits or runs merges its
  payload (true flags stay true), and runs again at most 3 times in a row. A worker that opens a
  fresh Garmin token loads the profile again before any data call.
  [docs/history-search.md](docs/history-search.md), [docs/history-worker.md](docs/history-worker.md).
- A Garmin call guard in every install: after a Garmin rate limit, tool calls get the rate-limit
  text for 15 minutes without calling Garmin; full mode's login refresh runs one thread at a time.
- Startup checks for the optional features' settings (`TRACING`, `OTEL_EXPORTER_OTLP_*`,
  `HISTORY_SEARCH`, `DATABASE_URL`, `HISTORY_TOKEN_KEY`, `ROLE`, `WORKER_CONCURRENCY`,
  `HISTORY_FORGET`): a wrong value stops the server with one line that never shows a secret.

### Changed

- `GARMIN_AUTH` is required (`full` or `key`, no default). Copies from before 1.0.0: set
  `GARMIN_AUTH=full` before you update, or the server does not start.

### Removed

- The token-in-URL form `https://<host>/<token>/mcp` of earlier, unreleased copies: it now
  answers `410 Gone`. Use `https://<host>/mcp` and sign in.

### Security

- certifi 2026.10.23: versions before 2026.10.18 bundle emSign root certificates whose
  intermediate keys were compromised (PYSEC-2026-4320).
- Garmin's Personal API client, which receives your key, is installed by hash.
- The optional features' secrets (`OTEL_EXPORTER_OTLP_HEADERS`, `DATABASE_URL`,
  `HISTORY_TOKEN_KEY`) never reach Caddy, are taken out of the server's environment once read, and
  are hidden in every log line. The tracing and history packages are installed by hash.
- The Garmin client library's own log lines (a failed call quotes the address with your display
  name and Garmin's answer) are replaced by one fixed line in the app, the history worker and the
  scheduler; registered secrets read `<redacted>` in every log line on the root handlers.
- `HISTORY_TOKEN_KEY` must be random: a key of printable characters (a typed phrase), of bytes that
  count up or down, or of one short block repeated is refused at startup, without being shown.
- The history worker reads only: `connectapi` takes no keyword but `params`, `get_*` and
  `download_*` take no `data`, `json`, `files`, `headers` or `method`, and the wrapped client is not
  reachable through its private names. With `GARMIN_AUTH=key` the app deletes a stored full-mode
  Garmin token at start; an image without the database driver says it could not check.
