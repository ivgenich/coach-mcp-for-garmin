# Changelog

All notable changes to this project are listed here, newest first. The format follows
[Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/), and the versions follow
[Semantic Versioning 2.0.0](https://semver.org/spec/v2.0.0.html). What counts as the public API,
and so what a MAJOR, MINOR or PATCH release may change:
[docs/updates.md](docs/updates.md#the-public-api).

## [Unreleased]

### Added

- Training plans, off by default (`PLAN_TOOLS=on`, which needs `HISTORY_SEARCH=on` and
  `GARMIN_AUTH=full`; [docs/training-plan.md](docs/training-plan.md)): the read tools
  `plan_assess`, `plan_preview` and `plan_get`. A marathon or half-marathon plan from your own
  Garmin numbers by fixed, versioned rules (plan rules v2: v1's weekly-km, long-run, quality and
  taper rules plus a planned all-sport acute:chronic cap of 1.3 from the recorded training load),
  checked by invariants, with B races, what-if variants, overrides that only move or reduce load,
  and optional per-segment race pacing from a saved course (`course_id`). Previews are stored as
  drafts in Postgres (migration 003); `plan_get` shows only the plans of the account the history
  index belongs to. An active plan becomes `finished` the day after its A race; retention drops
  untouched drafts after 30 days and ended plans 392 days after they ended. With this switch alone
  nothing is written to Garmin.
- Garmin writes for plans, off by default (`PLAN_WRITES=on`, which needs `PLAN_TOOLS=on`):
  `plan_commit` writes an approved preview's next days (`commit_days` 1 to 21, default 7) to the
  Garmin calendar and replaces the plan's own days whose workout changed; `plan_cancel` deletes the
  plan's own future workouts not linked to an activity and cancels the plan. Each runs on a
  single-use, resumable token from the preview (stored as SHA-256 only), needs a sign-in with the
  `plan:write` consent (never the static bearer), and asks the user to confirm through MCP form
  elicitation when the client declares it: on protocol 2026-07-28 with the input-required round
  (the first call writes nothing and calls no Garmin; the client calls the tool again with the
  answer, which counts only with the server's sealed state for that same tool, plan and token),
  before that with an elicitation request during the call. The form is one required choice
  between two fixed labels ("Add them" / "Don't add" and the like): only the approving label is a
  yes, as a ticked checkbox can come back false. Writes run one at a time (at most 8 per call);
  answers are `done`, `partial` (with `retry_after_s`, 900 after a 429; also when Garmin refused an
  upload or schedule, listed in `failed[]`), `unknown` (settled from Garmin before anything is sent
  again, so a retry never makes a second copy), `refused` or `declined`. Only the plan's own
  workouts are touched (a per-install marker; the database is the authority), and a session Garmin
  linked to an activity is never deleted or written again. `python ext/launch.py --plan-cleanup
  [--dry-run] [--orphans]` deletes this install's own future workouts, also with writes off;
  `HISTORY_FORGET=yes` is refused while such workouts remain.
- Every Garmin request passes a method allowlist (`garmin_paths.check_garmin_request`, in the path
  guard and the tracing hook): GET, one anonymous GraphQL read query (comments end at a carriage
  return or a line feed; no `data` or `files` body beside the checked `json`), and only with
  `PLAN_WRITES=on` the four workout-service paths the plan writes use.
- Plan tracking, off by default (`PLAN_TRACKING=on`, which needs `PLAN_TOOLS=on`): finished
  activities are linked to the active plan's sessions without any Garmin write (a draft is never
  tracked: no plan section in the status reports, and `log_feedback` and `plan_review_week` refuse
  it with a fixed text; `plan_get` still shows it); `plan_activate` starts following a stored draft
  or stops following the active plan (it becomes a draft again; refused while the plan has workouts
  on the Garmin calendar), in the app's own database only and without any Garmin call, so tracking
  works with `PLAN_WRITES` off (`plan_commit` also starts a plan); refreshed by the status
  tools and shown by `plan_get`; `log_feedback` stores RPE, pain and a fixed pain location in the
  app's own database only; `plan_review_week` reviews a week by plan review rules v1 and proposes
  changes that only hold or reduce load, which `plan_preview` (`accept_changes`) applies to the
  stored plan and, with `PLAN_WRITES=on` too, `plan_apply_changes` writes to Garmin with an apply
  token. The server instructions name the tools that change data when these switches are on; with
  every switch off the tools, annotations, instructions and reports are unchanged.
- Plan rules v2, B races: a long run 5 to 13 days before a B race and the first one after it are
  held at 0.70 x the peak long run (0.60 after a full-distance triathlon, whose first long run waits
  10 days), race-week runs at 8 km, the km not made up (warning `b_race_capped`).

### Changed

- History search keeps its own rules version, bumped only when the numbers it stores change
  (a test pins them), so weekly rules v3 does not rebuild or re-embed the history index.
- `launch.py` keeps this repo's read tools (`OWN_READ_TOOLS`) apart from the later write tools
  (`OWN_WRITE_TOOLS`): only the read tools are labelled read-only. The default tool list is
  unchanged.
- The history worker's daily `sweep` also applies the plans' retention.

### Fixed

- Weekly status: plan completion counts only the workouts already due. `completion_pct` is
  completed of due (it was completed of scheduled, so a week in progress read low in the text),
  and a workout still ahead that Garmin already linked counts in the new `plan.done_early`
  instead of `completed`, so the share never passes 100%. New `plan.due`. Workouts and
  activities use the same week edges: an activity counts only from the week's first day at
  00:00 up to the end of the week or today. Weekly rules version 3 (old: `completion_pct` =
  completed / scheduled, a linked future workout in `completed`; new: completed / due, that
  workout in `done_early`). The tool description and the docs no longer say "any day of the
  week".
- Activity times: one parser for Garmin's start times in both status tools. Without
  `startTimeLocal`, the daily status now dates an activity by the day it asked Garmin for
  (it dropped such an activity from plan completion), and the weekly status reads
  `startTimeGMT` in `REPORT_TZ` as before.
- Daily status: a swim workout's distance steps are read in meters in every pool, a yard pool
  too, for its distance and the hard-work total. Verified (spike S0.3): Garmin stores a yard
  pool's step distance in meters, and the steps add up to the workout's
  `estimatedDistanceInMeters`.
- Plan completion matching (`status_common.match_completion_pairs`, weekly status and plan
  tracking): among the matchings that complete the most workouts it now picks the one with the
  most same-day matches (then 1 day away, ...); the augmenting step could move a workout off its
  own day's activity. Part of weekly rules version 3.
- Plan tracking: exactly 80% of the planned distance or time is `done`, and other sports adding
  up to exactly 80% replace a session (the floating-point bound made them `partial` / `missed`);
  an activity listed twice completes one session only; with two sessions on one day, a run too
  short for one swaps with the other's run when each fits the other.
- Plan tracking in the weekly status: a failed activity search settles nothing as missed, and a
  full page of 100 activities settles nothing before the day after its oldest activity.
- Plan tracking in the daily status: an activity with only a GMT start time is dated by the day
  Garmin listed it under, as the rest of the daily report dates it.

## [1.0.5] - 2026-11-25

### Security

- The history worker's Garmin client now uses the same path guard as the app, and the image
  build fails if the garminconnect method the guard wraps is missing.

### Changed

- `ext/launch.py` is split into `garmin_paths.py`, `tool_answers.py`, `feature_startup.py` and
  `launch_logging.py`; `launch.py` keeps every name it had. Its tests are split the same way.
- The call guard's Postgres state has a `close()` that stops its background thread.
- CI measures coverage (the full leg fails under 86%) and runs mypy strict on the status modules.
  Test-only packages (coverage, hypothesis) are pinned in `config/requirements-test.txt` and
  never go into the image.
- The test runner fails a test that leaves a thread running or a handler on the root logger.
  Several leaking tests and one flaky telemetry test are fixed.
- Property-based tests (Hypothesis) for the path guard, the plan-completion matcher, `missing`
  and the id checks, and tests that pin the units the code assumes for Garmin's answers.

## [1.0.4] - 2026-11-25

### Security

- Full mode: the upstream tool `get_workout_by_id` put any id containing `-` into a Garmin
  address unchecked, so an id with `../` read other parts of the Garmin account (the profile
  with email and birth date, for example) with the owner's Garmin login. It only read, and the
  login never left Garmin's servers; a client the owner approved, or a model steered by
  planted text, could use it. Now every Garmin request of the app is checked before it is sent:
  an address with `..`, a `.` segment, `%`, `#`, `\`, `//`, a control character, a tab or line
  break, or a non-ASCII character other than a letter is refused, and a `?` may only start
  garminconnect's own query names (a plain space and letters such as `Í` stay allowed, because
  the owner's Garmin display name is part of some addresses). `get_workout_by_id` accepts only a number or a UUID, and `get_vo2max_trend` only
  `YYYY-MM-DD` dates (it accepted `20261101/x`). The workout and gear ids the status reports
  read from Garmin's answers are checked too.

### Fixed

- Weekly status: plan completion no longer credits a workout with any same-sport activity of
  the week. A workout counts done by an activity from 1 day before to 2 days after its date,
  never by one after today, and a workout dated after today is never done; the most workouts
  possible are paired. New `plan.moved` (done on another day) and `plan.upcoming` (still
  ahead); `low_completion` counts only workouts already due. Weekly rules version 2 (the
  history index is rebuilt once after the update; its numbers do not change).
- Weekly status: the long session's decoupling is judged only for an even effort. Indoors is
  always even; outdoors a ride needs a variability index (normalized / average power) of at
  most 1.10 and under 300 m of climbing per hour, a run under 100 m per hour. Otherwise
  `interpretation_code` is `not_applicable` (a hilly ride read as "Large drift"). New
  `long_session.steady`.
- Daily status: `missing` lists every value Garmin had none for, as the docs said, not only
  the ones in the text (fields null by design are named in the docs and left out).
- Daily status: a workout step with an intensity target but no values gives the reason
  "intensity unknown: targets without values" instead of "no step at zone 4+".
- History search: `index.stale` is also true when the newest finished week is not indexed in
  time, and `index.weeks_to` names the newest indexed week.

### Added

- Daily status: `today_done`, today's finished activities (and a "Done today" text line);
  `readiness_at`, `readiness_context` (`wake_up`, or the fallback `post_exercise` /
  `realtime` when there was no wake-up reading) and `recovery_hours_at`, so the readiness
  score (the morning reading) and the recovery clock (the newest reading) say where they come
  from; `readiness_latest` when a later reading has another score; `body_battery_wake`. The
  text line names the reading ("Readiness at wake-up: 94").
- Upstream tools: `get_progress_summary_between_dates` adds `unit` and the values in meters,
  seconds and whole kcal next to Garmin's raw cm, ms and unrounded calories;
  `get_training_status` and `get_training_load_trend` add `optimal_acute_load_min/max`. Notes
  in the tool descriptions say which times are GMT, that the VO2max trend gives one sport, and
  that the "optimal chronic load" band is the target for acute load.

### Changed

- `deploy.sh` waits for `/readyz` (up to 90 s, in a terminal) before it says to add the
  connector, in key mode and after the full-mode Garmin login. A failed Railway call also
  names a Railway CLI too old for the script as a cause.
- The web server's own INFO lines (uvicorn) go to stdout too, so Railway no longer marks them
  as errors.
- Docs: the Quickstart runs `./scripts/deploy.sh` alone, which asks for the mode and time zone
  (the example `export` line chose both for whoever pasted it). The clone address is written
  out, the "if no release is listed" notes are gone, and on Windows the Railway CLI is
  installed inside WSL (Scoop installs it where WSL does not see it). `.env.example` no longer
  lists `GARMINTOKENS`, `GARMIN_MCP_STATELESS_HTTP` and `PORT`, which the image sets.

## [1.0.3] - 2026-11-25

### Changed

- The weekly report keeps a found power:HR drift in memory (the 16 newest activities), so a
  second report of the same week does not download and decode the long session's FIT file
  again. Only a found drift is kept; a failure or a file without drift is read again next time.
  The memory is per server process and starts empty at each deploy.
- Log lines: INFO and below go to stdout, warnings and errors to stderr, in the app, the history
  worker and the scheduler, so Railway (which marks every stderr line as an error) shows only
  real warnings and errors in red. With the stdio transport every line stays on stderr, since
  stdout carries the protocol there. The line format is unchanged.
- The image compiles ext/ at build time, so a start no longer compiles it again.
- ruff also checks bugbear (B, without B905) and ASYNC rules; the few hits are fixed.
- CI: the container smoke test installs its mcp client by hash, like every other job.
- Updated pins, each release at least 14 days old: jsonpatch 1.46, jsonpointer 3.11 and
  langchain-core 1.7.7 (history search), ruff 0.17.4 (CI only).
- Dependabot leaves charset-normalizer alone: like requests, it keeps upstream's pin.

## [1.0.2] - 2026-11-25

### Fixed

- History search: a probe before the backfill floor whose fetch hit a Garmin failure was stored
  open and never probed again (the sweep's recheck only covers days after the floor), so older
  data behind a long break could stay unindexed. Such a probe is now probed again once, as a
  recheck, and after that on the late sweep.

### Changed

- Dependabot leaves curl-cffi and ua-generator (the Garmin login's libraries, which follow
  upstream) and pgvector 0.4 and later (langchain-postgres caps it below 0.4) alone.
- docs/history-worker.md: Railway kills a stopping service at once unless it has a draining
  time; the worker should get one (`RAILWAY_DEPLOYMENT_DRAINING_SECONDS=30`) so it finishes its
  running job. The app needs none.

## [1.0.1] - 2026-11-24

### Changed

- Updated pins, each release at least 14 days old: attrs 26.1.0, cffi 2.1.1, pycparser 3.12,
  rpds-py 2026.9.1, the Caddy image 2.11.9 (with the CI copy in `scripts/ci-tools.sh`) and
  actions/upload-artifact 7.0.2.
- Dependabot waits 14 days before proposing a release, proposes only patch releases of the
  python base image (the image stays on 3.12), and leaves garminconnect, requests and
  python-dotenv alone: upstream garmin_mcp pins them exactly, so they move with
  `GARMIN_MCP_REF`. garminconnect stays at upstream's 0.3.2; its advisory
  (GHSA-wjhr-76vg-2hvc, token file mode) is reviewed in `scripts/ci-checks.py` AUDIT_IGNORES:
  the image keeps the tokens in a 0700 directory of a one-user container.

### Fixed

- `scripts/release-check.py` R6 checked the commit emails of remote-tracking branches too, so
  a fetched Dependabot branch failed the release check. It now checks local branches and tags.

## [1.0.0] - 2026-11-24

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
- The status reports share one limit of 6 concurrent Garmin calls per server, and a Garmin rate
  limit or login failure on any report source starts the call guard's cooldown; a partial answer
  no longer ends a login cooldown. Both reports format numbers the same way and escape `~`.
- History backfill stops only after 14 days on which Garmin answered with no data; that stop is
  re-checked later, and failed days never count. Garmin 5xx and network failures are retried
  (a 403 is not, and does not keep a day open), Garmin's "no data" is stored as an answer,
  days left open are fetched again later, and `index.complete` counts them as missing. A recheck
  stores a source's empty answer instead of keeping old values. The history worker calls Garmin
  outside its database transaction, and every history connection has statement, lock and idle
  timeouts. Deploy the app, worker and scheduler from the same commit.
- `deploy.sh` prints the linked Railway project and service and stops when the folder's link
  belongs to another service or comes from a parent folder; every change names the service. It
  asks for `GARMIN_AUTH` and `REPORT_TZ` in a terminal, creates nothing until they are valid,
  offers to run the full-mode Garmin login over `railway ssh`, and now needs `python3`.
- The refresh-token retry window is 30 s (was 120 s).
- A manual reload (`kill "$(cat /tmp/garmin-mcp.pid)"`) or any exit with status 0 no longer counts
  toward the quick exits that stop the container.
- The container runs Python as the user `app` and Caddy as `caddy`; `/data` is given to `app`
  at each start. The server's restart loop backs off up to 60 s and stops the container after 8
  quick exits in a row.
- The release check looks at time zone offsets only on commits after the previous release tag.
- Docs: the history worker and scheduler deploy with `railway up` from the same checkout, the
  scheduler needs no Garmin settings, and history search is Railway-only for now. The README
  has a quickstart block, complete key-mode lists and a note on other MCP clients. `LICENSE` is
  plain MIT; the third-party note is in `NOTICE`. Added `CONTRIBUTING.md` and an issue form.
- Dependabot keeps GitHub Actions, Python pins and the Docker base images up to date. `railway up`
  and the Docker build leave out the eval data, dashboards and recipes.

### Removed

- The token-in-URL form `https://<host>/<token>/mcp` of earlier, unreleased copies: it now
  answers `410 Gone`. Use `https://<host>/mcp` and sign in.

### Fixed

- Workout names such as "Post-race recovery jog", "Pre-race shakeout" or "Easy run, not hard" no
  longer count as hard.
- The daily report reads further activity pages when a day fills the first page.
- Course profiles no longer lose distance at a point without coordinates.
- Every text block of a tool answer is cleaned, not only the first.
- History tools answer "the database did not answer" for every database failure.

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
- `/mcp` refuses a request whose `Origin` is not this server's, Claude's or listed in
  `ALLOWED_ORIGINS` (403); `ORIGIN_CHECK=off` turns this off.
- Sign-in redirects carry `iss` (RFC 9207), and the server metadata advertises it.
- The core Python packages are installed by hash, and the base images are pinned by digest.
- The Garmin token file is written atomically with mode 0600.
- `OWNER_SECRET` and `MCP_BEARER_TOKEN` values are hidden in every log line.
- An image build check fails if the Garmin library drops the internal names full mode uses; the
  worker logs a warning while its jobs wait for a fresh Garmin token. The unused token digest
  is no longer written; its column stays, now nullable, so a rolling deploy or a rollback keeps
  working (a later release drops it).
- The container stops leftover `app` processes before giving `/data` to `app`, never follows a
  link while doing it, and creates the Garmin token folder as `app`.
- The image installs its build tools by hash, so no unhashed package is fetched during the build;
  CI installs in the same order.
- `ALLOWED_ORIGINS` accepts `http://` only for loopback hosts, and never on Railway.
