# Deploy on Railway

Below, `<your-app>.up.railway.app` stands for your service's domain: the deploy prints it, and
with a custom domain it is the host in your `PUBLIC_URL`.

## Before you start

- **Railway plan.** The trial is a one-time $5 credit for 30 days. This service uses about
  $2–4 a month, so the trial ends on **day 30**. **Upgrade to Hobby ($5/mo, includes $5 of
  usage) before then.** Otherwise the service stops, and Railway deletes trial volumes 30
  days later, which deletes your Garmin login and means redoing the [Garmin login](garmin-login.md#full-mode-the-one-time-login).
- **Verified Railway account** (the Full trial, not the Limited one; Railway asks you to
  connect GitHub). The Limited trial restricts outbound network access, which can block
  the server's calls to Garmin.
- **Turn on 2FA for Railway.** Anyone with your Railway account can read your Garmin
  session and your owner secret.
- **Railway CLI:** `brew install railway`, or `npm i -g @railway/cli` (any system with
  Node.js), or `scoop install railway` on Windows. `railway ssh` also needs an SSH key
  registered with Railway. The CLI offers to register `~/.ssh/id_ed25519.pub`; if you have no
  key, run `ssh-keygen -t ed25519` first.
- **bash, git and python3.** deploy.sh is a bash script and reads the Railway CLI's JSON with
  python3. macOS and most Linux systems have all three (on macOS, `xcode-select --install`
  adds git and python3). On Windows, run it inside WSL, with the Railway CLI installed there.
- **Your time zone** as an IANA name, such as `Europe/London` or `America/New_York`
  ([list](https://en.wikipedia.org/wiki/List_of_tz_database_time_zones)). The server
  requires it (see [Settings](#settings)).
- **Your Garmin mode**, `full` or `key`: see [Garmin modes and login](garmin-login.md). The server
  requires it too. In `key` mode, create the key first
  ([Key mode: create a key](garmin-login.md#key-mode-create-a-key)): the deploy asks for it.
- Docker is **not** needed locally; Railway builds the image.

## Deploy with deploy.sh

Get the files, in a terminal:

```
git clone <repository URL>
cd coach-mcp-for-garmin
git checkout v1.0.3
```

`<repository URL>` is the HTTPS address under the **Code** button on this repository's GitHub
page. Use the newest release tag from the repository's **Releases** page in place of `v1.0.3`.
If no release is listed yet, skip the `git checkout` line and stay on `main`.

```bash
GARMIN_AUTH=full REPORT_TZ=Area/City ./scripts/deploy.sh    # your IANA zone, e.g. REPORT_TZ=Europe/London
```

In a terminal you can also run just `./scripts/deploy.sh`: it asks for the mode and the time
zone when neither your shell nor Railway has them.

For `key` mode use `GARMIN_AUTH=key`: the script then asks for your Garmin Personal API key at a
prompt that does not show it. Later, `./scripts/deploy.sh --set-garmin-key` replaces the key. If
you have more than one Railway workspace, add `DEPLOY_WORKSPACE=<workspace ID or name>` to the
first deploy line.

`REPORT_TZ` is required: it decides what "today" and "yesterday" mean for the daily and
weekly status. The script refuses a name that is not a time zone on this machine (match
the case exactly). Later runs keep the zone already on Railway; to change it, run the
script again with a new `REPORT_TZ=...`. Without a valid `REPORT_TZ` the server does not
start, and `railway logs` shows a one-line error saying why.

On a first deploy, deploy.sh checks `GARMIN_AUTH` and `REPORT_TZ` before it creates anything:
if one is missing and it cannot ask (no terminal), it stops and nothing is created. In `key`
mode the first deploy also needs a terminal, for the key prompt. On later runs it stops before
it sets a variable or deploys anything when `GARMIN_AUTH` on Railway is not `full` or `key`, and
in `full` mode while `GARMIN_PERSONAL_API_KEY` is still set on Railway. Later runs keep the
values on Railway unless you export new ones.

The script creates the project and service, attaches a volume at `/data`, creates the
service's public domain, generates the **owner secret** (`OWNER_SECRET`), builds and
deploys (a few minutes; it waits until Railway reports the deployment live), and prints the
**service URL** and the **connector URL**. In `full` mode, on the first deploy and in a
terminal, it then offers to run the [Garmin login](garmin-login.md#full-mode-the-one-time-login)
on the server for you.

The project and service are named `coach-mcp-for-garmin`; for another service name, put
`DEPLOY_SERVICE=<name>` in front of every run, not only the first.

Before it changes anything, the script prints the Railway project and service this folder is
linked to, and it stops (nothing changed) when:

- the folder is linked to a service with another name than `DEPLOY_SERVICE` (or
  `coach-mcp-for-garmin`). If that service is your server, run again with
  `DEPLOY_SERVICE=<its name>`; otherwise link the right one with `railway service link <name>`.
- the folder has no link of its own and the Railway CLI would use a parent folder's link. Then
  `railway up` would upload that whole parent folder. Give this folder its own link with
  `railway link` (an existing project) and run again.
- `RAILWAY_TOKEN`, `RAILWAY_PROJECT_ID`, `RAILWAY_ENVIRONMENT_ID` or `RAILWAY_SERVICE_ID` is set
  in your shell: the CLI then ignores the folder's link. Unset it and run again.

If you already deployed from another folder (an older clone, say), run `railway link` in the
new folder and pick the same project and service before you run the script: in an unlinked
folder it creates a new project.

The owner secret is what you type on your server's sign-in page when Claude connects ([Connect Claude](connect-claude.md)): six groups of five letters and digits, joined by dashes. The script never prints
it. Read it from Railway and save it in your password manager:

```bash
railway variable list --kv | grep ^OWNER_SECRET=
```

Later runs keep it; `./scripts/deploy.sh --rotate-secret` replaces it and signs everyone out
(see [Rotate the owner secret](maintenance.md#rotate-the-owner-secret)). If the script stops at "Public domain", create a domain in the dashboard
(the service's Settings > Networking > Generate Domain), or set `PUBLIC_URL` ([Settings](#settings)), and
run it again.

## Settings

Sign-in settings are service variables. Set one with `railway variable set NAME=value`,
except the two secrets: for `OWNER_SECRET` and `MCP_BEARER_TOKEN` use
`./scripts/deploy.sh --rotate-secret` or `--rotate-token`, or type the value at a prompt that
does not echo and pass it on stdin, never as `NAME=value` (that puts the secret in your
shell history and the process list):

    read -rs VALUE && printf '%s' "$VALUE" | railway variable set NAME --stdin; unset VALUE

Never add a variable named `GARMIN_MCP_REF`: Railway passes it to the build and it would change
the upstream code built. Don't set `EDGE_CLIENT_IP_HEADER` on Railway either: only Docker Compose
uses it, and sets it itself.

| Variable | Needed | What it does |
|---|---|---|
| `GARMIN_AUTH` | yes, no default | `full` or `key`: how the server reaches Garmin ([Garmin modes and login](garmin-login.md)). Without a valid value the server does not start. |
| `REPORT_TZ` | yes, no default | Your IANA time zone, such as `Europe/London`: what "today" and "yesterday" mean for the daily and weekly status. Without a valid value the server does not start. |
| `GARMIN_PERSONAL_API_KEY` | only in `key` mode | Your Garmin Personal API key, a secret. `./scripts/deploy.sh` asks for it, and `--set-garmin-key` replaces it; without deploy.sh, use the `read -rs` line above with `NAME` = `GARMIN_PERSONAL_API_KEY`. In `full` mode leave it unset or empty, or the server does not start. |
| `OWNER_SECRET` | yes; `deploy.sh` generates it | The secret for the sign-in page: 32 to 256 characters, at least 12 different ones. A new value revokes every sign-in (not the static token). |
| `PUBLIC_URL` | only with a custom domain | This server's public https origin, e.g. `https://coach.example.com`, with no path. Unset, the server uses its Railway domain. The connector URL is this plus `/mcp`. Set it before you add the connector. |
| `MCP_BEARER_TOKEN` | no | A static token for an `Authorization: Bearer` header, for Claude Code or CI (`./scripts/deploy.sh --rotate-token` creates it). claude.ai does not use it. |
| `AUTH_DB` | no | Where the sign-ins are stored. Default `/data/auth.sqlite`, on the volume. |
| `ALLOWED_REDIRECT_URIS` | no | Extra exact https redirect URIs a client may register, comma-separated, at most 10. claude.ai's and Claude Code's (`http://localhost:<port>/...`) are always allowed. |
| `ALLOWED_ORIGINS` | no | Extra browser origins allowed to call `/mcp`, comma-separated, at most 10, each `https://` (for example `https://app.example.com`; `http://` only for a loopback host when running locally). This server's own origin, `https://claude.ai` and `https://claude.com` are always allowed; requests without an `Origin` header are not checked. |
| `ORIGIN_CHECK` | no, default `on` | `off` turns the `/mcp` Origin check off. |

The optional features are off until you set their switch; see
[Optional: tracing and history search](#optional-tracing-and-history-search). A wrong value
stops the server at startup with a `launch: config error:` line, like the settings above.

| Variable | Needed | What it does |
|---|---|---|
| `TRACING` | no, default `off` | `on` sends traces and metrics, with no health values, to your tracing service ([Tracing](tracing.md)). |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | with `TRACING=on` | The tracing service's OTLP/HTTP https URL. |
| `OTEL_EXPORTER_OTLP_HEADERS` | usually, with `TRACING=on` | The service's auth header as `name=value`, a secret: set it with the `read -rs` line above. Caddy never sees it, and the server takes it out of its environment once read. |
| `HISTORY_SEARCH` | no, default `off` | `on` adds the history tools `find_similar_weeks` and `search_history` ([History search](history-search.md)). Needs an image built with `HISTORY=1`, a Postgres service and a worker service. |
| `HISTORY` | with `HISTORY_SEARCH=on` | `1` makes Railway build the image with the history packages (about 140 MB more). Railway passes service variables to the build. Set it on the app, the worker and the cron service, before their first build. |
| `DATABASE_URL` | with `HISTORY_SEARCH=on` | The Postgres URL, a secret. On Railway set it to the reference `${{Postgres.DATABASE_URL}}` in the dashboard, so the value is never typed. |
| `HISTORY_TOKEN_KEY` | with `HISTORY_SEARCH=on` in `full` mode | 32 random bytes in base64, a secret, the same on the app and the worker: the worker reads the Garmin login from Postgres with it ([History worker](history-worker.md)). |
| `ROLE` | no, default `app` | `worker` on the worker service, `enqueue` on the cron service; both need `HISTORY_SEARCH=on`. |
| `WORKER_CONCURRENCY` | no, default `1` | Job threads in one worker, 1 to 4. More threads never mean more Garmin calls: every worker shares one rate limit. |
| `HISTORY_FORGET` | no | `yes` deletes the stored history at the next start (set it on the app, the worker and the cron service), then delete the variable again ([History search](history-search.md)). |

## Health check

deploy.sh sets the service's health check path to `/healthz` on its first run. If it says it
could not, set it in the dashboard: the service's **Settings → Deploy → Healthcheck Path**,
`/healthz`. Never `/readyz`: in `full` mode it answers 503 until the Garmin login, so the first
deploy would never finish.

## Optional: tracing and history search

Both are off by default, and nothing changes until you set their variables (table above).

- **Tracing** needs only variables on this service: `TRACING=on`, the endpoint and the auth
  header of your tracing service. [Tracing](tracing.md) walks through Grafana Cloud's free tier.
- **History search** needs three more services in the same Railway project: a Postgres with the
  `vector` extension (private network only), a worker (this repository again, with `ROLE=worker`)
  and a cron service (`ROLE=enqueue`, once a day). Set `HISTORY=1` and `HISTORY_SEARCH=on` on all
  three, and deploy them from your clone, the same commit as the app:
  `railway up --ci --service <worker>` and `railway up --detach --service <scheduler>`. [History worker](history-worker.md) has the steps; [History search](history-search.md)
  says what is stored and how to delete it.

The worker and the cron service run no web server: give them no domain, no volume and no health
check path. The worker needs the same `GARMIN_AUTH` and `REPORT_TZ` as the app, and in `key` mode
the same `GARMIN_PERSONAL_API_KEY` (in `full` mode the same `HISTORY_TOKEN_KEY` instead). The cron
service never calls Garmin: it needs only `REPORT_TZ`, `HISTORY=1`, `HISTORY_SEARCH=on`,
`DATABASE_URL` and `ROLE=enqueue`, and no Garmin key.

## Next

1. `full` mode: log in to Garmin once: [Full mode: the one-time login](garmin-login.md#full-mode-the-one-time-login).
2. Add the server to Claude: [Connect Claude](connect-claude.md).
