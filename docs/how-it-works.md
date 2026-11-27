# How it works

## Architecture

```
claude.ai chat / phone app ------+
Claude Code, cloud routine ------+  HTTPS + OAuth access token
  (optional recipe: report page) |  (you approve each sign-in with the owner secret)
                                 v
     Railway's edge -> Caddy :$PORT
 or: Cloudflare edge -> cloudflared (container "tunnel") -> Caddy :8080
                                 |
                                 v
+------------- container on Railway or Docker, /data volume --------------+
| Caddy :$PORT (PID 1): proxies every request; holds no secret;           |
|   |   keeps tokens and query strings out of its access log              |
|   v                                                                     |
| garmin-mcp 127.0.0.1:8000: the sign-in (OWNER_SECRET) and the token     |
|    check, upstream's tools + ext/ tools (get_daily_status, ...)         |
|    -- the one Garmin login, or the key --> Garmin                       |
| /data/tokens: Garmin login    /data/auth.sqlite: sign-ins               |
| /data/garmin-key: key mode's 24-hour access token                       |
+-------------------------------------------------------------------------+
```

## What runs where

| Piece | Runs | Secrets it sees | Does |
|---|---|---|---|
| Caddy | container, PID 1, public `$PORT`, user `caddy` | none | Proxies every request to garmin-mcp, passing tokens through; answers `410` for the removed token-in-URL form; caps sign-in request bodies; access log without query strings or credentials |
| garmin-mcp (`ext/launch.py`) | container, `127.0.0.1:8000`, user `app` | `OWNER_SECRET`, the optional `MCP_BEARER_TOKEN`; sign-ins in `/data/auth.sqlite`; Garmin tokens on `/data/tokens`; in `key` mode `GARMIN_PERSONAL_API_KEY` and its access token on `/data/garmin-key` | The sign-in (OAuth 2.1 for one owner) and the token check on `/mcp`; upstream's tools plus this repo's |
| Cloud routine, optional (`recipes/claude-ai-morning-report/`) | claude.ai's cloud, on a schedule | your claude.ai connectors | Stores the report in the report page's database |

The split keeps each secret out of the programs that don't use it, and so out of their
logs and tool output. Each runs as its own unprivileged user, so Caddy cannot read the
server's environment or files. Only the start script (`entrypoint.sh`) runs as root: on
every start of the server it gives the user `app` whatever on `/data` is not its own yet (a
volume from an older version, or Garmin tokens that `garmin-mcp-auth` wrote over
`railway ssh` or `docker compose exec`, which run as root), then starts each program as its
user. So after a login, the restart the login steps describe is enough; nothing needs `chown`
by hand. `AUTH_DB` and `GARMINTOKENS` set to a folder outside `/data` must be writable by the
user `app` (uid 10001). It is still not a sandbox against someone who can run commands in
the container. With Docker, the separate cloudflared container runs as a non-root user. In
`key` mode, the key goes to the server's Python only; Caddy gets no secret.

If the server process exits, the start script starts it again, waiting 2 s, then twice as long
after each quick exit, up to 60 s; a run of 5 minutes resets the wait. After 8 quick exits in a
row it stops the container (exit status 2), so Railway or Docker restarts it, and the server log
shows why.

## Security

The owner secret gives full access to this server: whoever has it can approve a sign-in
and read all your Garmin data through the read-only tools it exposes (see [Tools](tools.md)), and
with training-plan writes on (`PLAN_WRITES=on`) also approve the writes, which add and delete the
plan's own workouts. So can the optional static token, without any sign-in page (it never gets
the write tools). Keep both in a password manager,
and approve only a sign-in you started yourself just now. The Garmin tokens on
`/data/tokens` are more powerful: they give full access to your Garmin account, so anyone
who can read the volume (for example through your Railway account, or on your own
computer) can change or delete your Garmin data. If you think your Garmin session leaked,
change your Garmin password and redo the
[one-time login](garmin-login.md#full-mode-the-one-time-login).

In `key` mode the volume holds no Garmin login, only a 24-hour access token made from the
key. A leaked key, or its 24-hour token, can read all the data Garmin's Personal API offers,
including activity files with GPS tracks, women's health data and food logs, until you
delete the key in Garmin Connect or it expires. Garmin says that deleting the key also stops
the access tokens made from it
([Renew the Garmin key](maintenance.md#renew-the-garmin-key-key-mode)).

The sign-ins are kept in `/data/auth.sqlite` (file mode 0600): codes and tokens only as
SHA-256 hashes, the secrets of registered clients as issued (a client secret alone gets no
token). A new owner secret revokes every sign-in (see
[Rotate the owner secret](maintenance.md#rotate-the-owner-secret)). The sign-in endpoints
(`/consent`, `/authorize`, `/register`) are rate-limited per caller address, and three
wrong secrets cancel a sign-in. On Railway, Caddy takes the caller address from
`X-Real-IP`, which Railway's edge sets. Behind a Cloudflare Tunnel, it takes
`CF-Connecting-IP`, which Cloudflare sets on requests from its edge; compose.yaml fixes
this, and no port is published, so only the tunnel reaches the server. Cloudflare does not
document whether it replaces a `CF-Connecting-IP` that the caller sends. Caddy hands the
address to the server as `X-Client-IP`; it feeds only the rate limits, never the sign-in
decision. Caddy drops `X-Forwarded-For`, and the server
tells uvicorn to trust no proxy header (an empty `FORWARDED_ALLOW_IPS`), so callers who
reach it through the same edge never share one limit.

With Docker, the tunnel token in `tunnel.env` is a secret like the owner secret: anyone who
has it can run your tunnel ([2. Create the tunnel](deploy-docker.md#2-create-the-tunnel)
says what to do if it leaked).

Caddy still passes a caller's own `X-Forwarded-Proto` and `X-Forwarded-Host` through, so
nothing in the server may build a URL or make a security decision from the request's
scheme, `Host` or `X-Forwarded-*` headers. The server's own URL comes only from `PUBLIC_URL`
or the Railway domain.

Browsers name the page that sends a request in its `Origin` header. On `/mcp`, the server
answers `403` to a request whose `Origin` is not its own (`PUBLIC_URL`), `https://claude.ai`,
`https://claude.com`, or one listed in `ALLOWED_ORIGINS` (comma-separated, such as
`http://localhost:6274` for a local MCP inspector), as the MCP specification asks. Requests with
no `Origin` (Claude's servers, Claude Code, scripts) pass. `ORIGIN_CHECK=off` turns the check off;
the sign-in and the token check still apply. The OAuth endpoints are meant to be called from
other sites and are not checked; the owner-secret page refuses a form posted from another site.

Refresh tokens rotate on every use, and a used one that comes back revokes its sign-in, except
when the same client repeats it within 30 seconds (a retry): then it gets the same new tokens
again. That window lives in memory, so after a restart a repeated refresh token revokes the
sign-in and the client signs in again ([Security policy](../SECURITY.md#how-the-sign-in-behaves)).

## The Garmin call guard

Every Garmin call from a tool passes a guard in the server (`ext/garmin_guard.py`). After Garmin
answers with a rate limit, the guard answers further tool calls with the same rate-limit text for
15 minutes, without calling Garmin: models tend to retry, and each retry can extend Garmin's block.
A Garmin login error is never held back this way, since a new login fixes it at once. With history
search on, the guard's state is shared through Postgres with the worker, which pauses 6 hours after
a rate limit and 24 hours after a login error, never calls Garmin while a tool call ran in the last
minute, keeps to one call every 5 seconds and 300 a day, and calls Garmin only from 12:00 to 22:00
in `REPORT_TZ`. In `full` mode the guard also lets only one thread at a time refresh the Garmin
login.

## Optional services

History search adds services to the same Railway project: a Postgres with pgvector on the private
network (the stored history, the job queue and the guard's shared state), a worker
(`ROLE=worker`, the same image) that indexes days through the guard, and a cron service
(`ROLE=enqueue`) that queues the day's jobs once a day. The worker and the cron service start
straight from `entrypoint.sh` into Python: no Caddy, no web server, no volume. In `full` mode the
app stores its Garmin login in Postgres sealed with `HISTORY_TOKEN_KEY`, and the worker reads it
from there and never refreshes it ([History worker](history-worker.md)). The secrets of the
optional features (`OTEL_EXPORTER_OTLP_HEADERS`, `DATABASE_URL`, `HISTORY_TOKEN_KEY`) go to
Python only, never to Caddy, and the server takes them out of its environment once read.

## Text from Garmin is untrusted

Names, descriptions and notes come from you or from apps
and coaches linked to your Garmin account. Before they reach Claude, the server removes
invisible and control characters, caps their length and replaces most links and HTML tags
with `[link]` and `[html]` (`ext/text_hygiene.py`), and the server instructions and tool
descriptions tell Claude that this text is data. That lowers the risk of prompt injection
but cannot remove it: a visible instruction in a workout description still reaches Claude
as text. Take care when the same chat also has connectors that can act, such as sending
mail or writing files.

## The report page

If you use the morning report recipe, its page's database holds your daily and weekly
statuses, which are health data: keep the page private, and keep its one write rule for
the owner only.

## What leaves your server

- **To your AI provider.** A tool's answer goes to your AI client, and from it to the client's
  provider: for Claude, Anthropic. It can hold health data (sleep, heart rate, HRV, stress, Body
  Battery), activities with their GPS tracks and locations, your Garmin display name and gear.
  It then falls under that provider's terms and your account's settings. On Claude's Free, Pro
  and Max plans you choose in your Privacy Settings whether your chats may be used to improve
  Claude; Team and Enterprise plans fall under Anthropic's commercial terms and your
  organization's settings ([Anthropic](https://www.anthropic.com/news/updates-to-our-consumer-terms),
  checked November 2026).
- **To Garmin.** The server calls Garmin Connect (`full` mode) or Garmin's Personal API (`key`
  mode), for your own data only.
- **Through your host.** Railway's edge, or Cloudflare in front of your own computer, ends HTTPS
  before your server, so it can read the traffic. Railway keeps your service's logs and shows them
  for 7 days on Hobby and 30 on Pro; upgrading a plan brings older logs back, so Railway stores
  them for longer ([Railway](https://docs.railway.com/observability/logs), checked November
  2026). The server's logs keep no tokens, secrets or query strings; a failed tool call logs its
  original error with tokens, passwords, keys and emails removed. An internal error also logs a
  traceback, which is not filtered.
- **Nothing else, unless you turn it on.** With the default settings the server calls only Garmin.
  It sends no analytics or telemetry: the MCP SDK's OpenTelemetry hooks get no exporter until you
  set `TRACING=on`, so they do nothing. With `TRACING=on` it also sends traces and metrics to the
  tracing service you name: tool names, durations, outcomes, error groups and size buckets, never
  health values, names, ids, dates, tokens, tool arguments or answers ([Tracing](tracing.md)).
  With `HISTORY_SEARCH=on` it keeps a copy of your daily and weekly numbers in your own Postgres
  service and sends nothing out: the public embedding model's files are downloaded from
  huggingface.co (the app keeps them on its volume; the worker has no volume, so it downloads them
  again after each worker deploy), and the model runs on your server, so no health text goes to an
  embedding service ([History search](history-search.md)).

On the server, `/data` holds the Garmin login tokens (`full` mode) and the sign-in database. In
`key` mode the key itself is in your server's settings (`GARMIN_PERSONAL_API_KEY`), and a
24-hour access token made from it is cached in `/data/garmin-key/token.json`, with owner-only
file modes (folder 0700, file 0600). Anyone who can
read the volume can use that token until it expires. Delete the volume and all of these are gone,
except the key, which you delete in Garmin Connect:
[Uninstall and revoke](../README.md#uninstall-and-revoke).
