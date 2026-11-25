# History worker: the job queue, the worker and the daily scheduler

History search (`HISTORY_SEARCH=on`, see [history-search.md](history-search.md)) fills its
index in the background. The work runs as jobs in a Postgres queue, done by a separate
worker service, and a small scheduler adds the day's jobs once a day. This page explains how
that works and how to set up the two extra services on Railway: you create them and set their
variables in the dashboard, then deploy them from your clone with the Railway CLI, the same way
`scripts/deploy.sh` deploys the app.

With history search off (the default) none of this exists: no worker, no scheduler, no
database.

## The services

| Service | Same image? | Variables that differ | Volume | Public domain |
|---|---|---|---|---|
| app (your existing MCP server) | yes | `HISTORY_SEARCH=on`, `DATABASE_URL` | `/data` (as today) | yes |
| worker | yes | `ROLE=worker` | none | none |
| scheduler (Railway cron) | yes | `ROLE=enqueue`, a cron schedule | none | none |
| Postgres (with pgvector) | Railway template | | its own | none (private network) |

All three of ours are built from the same checkout of this repository with the build argument
`HISTORY=1` (the service variable `HISTORY=1`; Railway passes service variables to the build, so
set it before a service's first build). Deploy all three from the same commit: each of them
updates the database tables when it starts.

- The **app** answers the history tools from Postgres. It never runs jobs. At startup it
  adds the catch-up jobs in the background, so a first deploy starts indexing without
  waiting for the scheduler.
- The **worker** takes jobs from the queue, calls Garmin under the shared call guard
  (below), computes the embeddings and writes Postgres. It serves nothing and needs no
  port, domain or volume.
- The **scheduler** starts once a day, adds the day's jobs (the daily sweep, and a rebuild
  when the numbers history stores changed), and exits.

## Set it up on Railway

Do these in order, in the folder of your clone that `scripts/deploy.sh` deploys from.

1. **Postgres.** In the project: New, Database, PostgreSQL, using a template with the
   pgvector extension. Keep it on the private network (no public TCP proxy). Turn backups
   on or off as you prefer (see "Forget" under
   [Stopping, redeploying, forgetting](#stopping-redeploying-forgetting)).
2. **App.** On the existing service, add the variables `HISTORY=1`, `HISTORY_SEARCH=on` and
   `DATABASE_URL` = `${{Postgres.DATABASE_URL}}` (a reference variable: pick it from the
   list). With `GARMIN_AUTH=full` also add `HISTORY_TOKEN_KEY` (below). Then run
   `./scripts/deploy.sh`, which builds the app again with `HISTORY=1`.
3. **Worker.** In the project: New, Empty Service, and name it `worker`. Create it in the
   dashboard, not with `railway add`, which would link your folder to the new service (then
   `railway service link coach-mcp-for-garmin` links it back). Before the first deploy, in
   Variables, use "Shared variables" or copy from the app: `GARMIN_AUTH`, `REPORT_TZ`,
   `HISTORY=1`, `HISTORY_SEARCH=on`, `DATABASE_URL` = `${{Postgres.DATABASE_URL}}`, then add
   `ROLE=worker`.
   - Key mode (`GARMIN_AUTH=key`): add `GARMIN_PERSONAL_API_KEY` too. The worker gets its
     own 24-hour access token and caches it in a temporary folder (no volume needed).
   - Full mode (`GARMIN_AUTH=full`): add the same `HISTORY_TOKEN_KEY` as the app. Do not
     add your Garmin email or password.
   - Optional: `WORKER_CONCURRENCY` (1 to 4, default 1). More threads only help the
     embedding work: Garmin calls share one bucket whatever the number of workers.
   - In Settings, Networking: no domain. No volume. Replicas: 1 is enough.
   - Then deploy it from your clone: `railway up --ci --service worker`.
4. **Scheduler.** New, Empty Service again, named `scheduler`. Its variables: `REPORT_TZ`,
   `HISTORY=1`, `HISTORY_SEARCH=on`, `DATABASE_URL` = `${{Postgres.DATABASE_URL}}` and
   `ROLE=enqueue`. No `GARMIN_AUTH`, Garmin key or token key: it never calls Garmin. In
   Settings, Cron Schedule: for example `15 9 * * *` (09:15 UTC daily). Deploy it from your
   clone: `railway up --detach --service scheduler`, then watch its build in the dashboard.
   The time hardly matters: the worker calls Garmin only from 12:00 to 22:00 in `REPORT_TZ`,
   and the jobs say "index what is missing", not "index this hour".
5. Check the logs: the worker says `worker: on, 1 thread(s), handlers from ...`; the
   scheduler says `enqueue: N job(s) planned, M new` and exits with status 0.

### HISTORY_TOKEN_KEY (full mode only)

Railway volumes attach to one service, so the worker cannot read the app's Garmin login on
`/data/tokens`. The app therefore copies its token file into Postgres whenever it changes,
sealed with AES-256-GCM under `HISTORY_TOKEN_KEY`, and the worker opens it. The app stays
the only one that logs in and refreshes; the worker never refreshes. Make the key once:

```sh
python3 -c "import base64, secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"
```

(or `python3 -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"`, the same
thing in standard base64) and paste the same value into the app's and the worker's variables.
Generate it with one of these commands; do not type one. A key that is not random is refused at
startup without being shown: one made of printable characters (a phrase or password, base64
encoded), bytes that count up or down (such as 0, 1, 2 ... 31), one short block repeated, or
fewer than 16 different byte values. If the stored token is
missing or about to expire, the worker's Garmin jobs wait 15 minutes (`token_stale`, no
attempt used) until the app stores a fresh one. Each time the worker opens a new token it
loads the Garmin profile again (one call of its own through the call guard), because
garminconnect builds most addresses from the profile's display name; if Garmin's answer names
no display name, the job waits 15 minutes (`profile_missing`) and stores nothing.

With `GARMIN_AUTH=key` no token is stored: the app deletes a sealed token row an earlier
full-mode setup left in `history.garmin_token` at each start (the log then says "history search
runs in key mode: the stored Garmin token was deleted from the database").

## How the queue works

Jobs live in the table `history.jobs`. Each has a `kind` (`index_day`, `build_week`,
`embed_doc`, `sweep`, `rebuild`), a `key` (for example `index_day:2026-11-23`), a small JSON
payload with dates only, a status (`queued`, `running`, `done`, `failed`), its attempts, the
time it may run next, and a lease.

- **Claiming.** A worker takes the next ready job with
  `UPDATE ... WHERE id = (SELECT ... FOR UPDATE SKIP LOCKED LIMIT 1)`: two workers never get
  the same job, and neither waits for the other.
- **Leases.** A claim holds the job for 5 minutes and the worker renews it every minute. If
  a worker dies (a crash, a redeploy), its lease runs out and any worker's reaper (every
  minute) puts the job back in the queue. Each claim has its own token, so a late write
  from the dead worker changes nothing.
- **No double work.** A key can have only one unfinished job, so adding a job twice adds
  no second job: the waiting job's payload takes in the new one (new keys win, and a flag
  such as `recheck` or `backfill` that was true stays true). Adding a job whose key is running
  merges its payload the same way and runs that job once more after it finishes, at most 3
  times in a row (so a job that adds its own key cannot run for ever); after that the add
  changes nothing and the job ends. Every write is an upsert keyed by account, date and kind,
  so running a job again writes the same rows.
- **All or nothing.** A job's own rows, the follow-up jobs it adds (for example
  `index_day` adds `embed_doc` and, when the week is complete, `build_week`) and its "done"
  are one Postgres transaction (the outbox pattern). A crash in between leaves nothing
  half-done.
- **Retries.** A failed job waits 1 min, 5 min, 30 min, 2 h, then 6 h (each +-20% so retries
  spread out), and after 6 attempts it is `failed`: the dead-letter state, kept 30 days.
  Bad input and "no data" are not retried. Done jobs are deleted after 7 days.
- **Garmin limits are not failures.** A Garmin rate limit or login error pauses every worker
  (6 h or 24 h, below), and the job waits for that without using an attempt. So does a job
  the call guard holds back (quiet hours, a live call running, the bucket empty).

## The shared Garmin call guard

All Garmin calls, from the app's tools and from every worker, go through one guard whose
state is one row in Postgres (`history.guard_state`):

- a rate limit seen anywhere pauses the workers 6 hours (a login error: 24 hours) and live
  tool calls 15 minutes;
- live calls first: a worker makes no call while a tool call ran in the last minute;
- one bucket for all workers: at most one call every 5 seconds and 300 a day;
- workers call Garmin only from 12:00 to 22:00 local time (`REPORT_TZ`).

The app never waits on Postgres for this: it keeps its own copy and writes in the
background. A worker that cannot reach Postgres makes no Garmin call at all.

## Watching it

With tracing on ([tracing.md](tracing.md)) the worker sends:

- one span per job (`history.job`, with the kind, the attempt and how long it waited in the
  queue), joined to the scheduler's trace;
- `coach.jobs` (finished jobs by kind and status), `coach.queue.wait`,
  `coach.queue.depth` (jobs by status) and `coach.queue.oldest_age`. The dashboard alerts on
  any failed job and on a ready job older than a day.

Without tracing, the worker's log says what it does (job ids, kinds and fixed error groups;
never Garmin data, names or tokens). The Garmin client library's own log lines (a failed call
quotes the address, which holds your display name, and Garmin's answer) are replaced by one
fixed line each, `garmin library: one ERROR line of garminconnect was hidden ...`, in the
worker, the scheduler and the app alike; any value registered as a secret reads `<redacted>`. To look at the queue directly, open the Postgres
service's Data tab (or `psql` from a Railway shell) and run:

```sql
SELECT kind, status, count(*), min(next_run_at) FROM history.jobs GROUP BY 1, 2 ORDER BY 1, 2;
SELECT key, attempts, last_error_group, finished_at FROM history.jobs WHERE status = 'failed';
```

`last_error_group` is a fixed name: `rate_limited`, `login`, `unavailable`, `no_data`,
`bad_input`, `internal`, `lease_expired`, `token_stale`, `profile_missing`, `quiet_window`, `live_busy`,
`bucket`, `cooldown`, `daily_cap`, `guard_unavailable` or a handler's own (for example
`account_mismatch`, or `prepare_stale` when a day changed while its Garmin data was fetched).

To run a failed job again after fixing the cause, delete its row; the next sweep adds it
again:

```sql
DELETE FROM history.jobs WHERE status = 'failed' AND key = 'index_day:2026-11-23';
```

## Stopping, redeploying, forgetting

- **Redeploy or restart.** The worker finishes its running job on SIGTERM when it can; if
  the platform kills it first, the lease runs out and another run picks the job up.
  Nothing is lost. Railway sends SIGKILL right after SIGTERM unless the service sets a
  draining time: give the worker one, for example the service variable
  `RAILWAY_DEPLOYMENT_DRAINING_SECONDS=30` (a day's Garmin calls take a few seconds). The app
  needs none: it holds no state, and Caddy (PID 1) stops at once, which ends the server with
  it.
- **Update.** Deploy the same commit to all three: in your clone, `./scripts/deploy.sh` (the
  app), then `railway up --ci --service worker` and `railway up --detach --service scheduler`.
- **Pause indexing.** Remove the worker service, or set its replicas to 0. Jobs wait in the
  queue.
- **Forget.** Set `HISTORY_FORGET=yes` on the app, the worker and the scheduler (see
  [history-search.md](history-search.md#forgetting)). The app deletes the schema `history`, which holds
  the stored history and also the job queue, the guard's row and the stored Garmin token.
  While the flag is set the worker stays idle and the scheduler adds nothing, so neither
  recreates the tables. Remove the flag from all three afterwards.
- **Turn it off.** Delete the worker and scheduler services, set `HISTORY_SEARCH=off` on the
  app (or remove it), and delete the Postgres service if you no longer want the data.
  In full mode the app deletes the sealed Garmin token row (`history.garmin_token`) at its next
  start, as long as `DATABASE_URL` is still set on it; the log then says "history search is off:
  the stored Garmin token was deleted from the database". An image built without `HISTORY=1`
  has no database driver and cannot check: it logs one line saying the stored token could not be
  checked or deleted (build once with `HISTORY=1`, or drop the table `history.garmin_token` by
  hand). Postgres backups taken before keep
  their copy of that row for as long as your backup settings keep them (check your Railway
  backup settings). Remove `HISTORY_TOKEN_KEY` from the worker service too (and from the app).

## Cost

Estimates on Railway's usage pricing: the worker is small and mostly idle, about $1-3 a
month; the scheduler runs for a few seconds a day, near $0; the Postgres about $3-5. In all,
about $4-8 a month on top of the app.
