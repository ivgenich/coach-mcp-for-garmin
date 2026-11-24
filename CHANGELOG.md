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
