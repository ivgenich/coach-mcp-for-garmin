# coach-mcp-for-garmin

[![CI](https://github.com/ivgenich/coach-mcp-for-garmin/actions/workflows/ci.yml/badge.svg)](https://github.com/ivgenich/coach-mcp-for-garmin/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

An MCP server you deploy for yourself that lets Claude read your Garmin Connect data: activities,
sleep, HRV, training load, planned workouts and more. MCP (Model Context Protocol) is the open
standard Claude uses to call tools on servers like this one. Ask "How am I today?" in the Claude
app on your phone, and Claude answers from your own numbers. Every tool is read-only unless you
turn on a training-plan switch (each off by default): plan tracking (`PLAN_TRACKING=on`) adds
`log_feedback` and `plan_activate`, which write only the server's own database, and plan writes
(`PLAN_WRITES=on`) add tools that put the plan's own workouts on your Garmin calendar or delete
them. The server has a sign-in of its own, so only you can connect it to Claude. On top of the tools of
[Taxuspt/garmin_mcp](https://github.com/Taxuspt/garmin_mcp) it adds a daily status (a green,
yellow or red level and a recommendation for today's workout, by fixed rules), a weekly status
and a course climb profile.

> **Read this first.** This project is not affiliated with or endorsed by Garmin. In `full` mode
> it reaches Garmin through an unofficial interface that may stop working, may breach Garmin's
> terms and can get your Garmin account restricted; `key` mode uses Garmin's official Personal
> API under Garmin's own terms, with less data. Not medical advice. Your Garmin data goes to the
> AI provider you use. Details: [Risks and privacy](#risks-and-privacy).

**Contents:** [Is this for you?](#is-this-for-you) ·
[Quickstart](#quickstart) · [What it costs](#what-it-costs) ·
[Claude plans and apps](#claude-plans-and-apps) · [Other MCP clients](#other-mcp-clients) ·
[One server per person](#one-server-per-person) · [Risks and privacy](#risks-and-privacy) ·
[What you get](#what-you-get) · [Updates and versions](#updates-and-versions) ·
[Uninstall and revoke](#uninstall-and-revoke) · [Lost the owner secret?](#lost-the-owner-secret) ·
[Evals](#evals) · [Help](#help) · [Docs](#docs) · [License and credit](#license-and-credit)

## Is this for you?

It is if you use a Garmin watch with Garmin Connect, want Claude to answer from your own data,
and are fine running a few commands in a terminal and paying for your own small server (about
$5 a month on Railway). You need a computer with bash, git and python3: macOS, Linux, or WSL on
Windows. The Railway CLI installs with Homebrew (optional) or npm; on Windows, inside WSL.

It is not if you want an app to install from a store, a service someone else runs for you, one
server for several people, or a tool that changes your Garmin data on its own: by default every tool
only reads, and the optional training-plan writes (`PLAN_WRITES=on`) only add or delete the plan's own
workouts after you approve them.

## Quickstart

You need a Claude account ([any plan](#claude-plans-and-apps)), a Garmin Connect account, your
time zone as an IANA name such as `Europe/London`, about an hour, a terminal and the Railway CLI
([Before you start](docs/deploy-railway.md#before-you-start)).

1. **Choose a Garmin mode.** `GARMIN_AUTH` is required and has no default:

   | `GARMIN_AUTH` | What you get | How the server reaches Garmin |
   |---|---|---|
   | `full` | Every tool: activities with laps and time in zones, sleep and recovery, training readiness, status and load, planned workouts, courses, records, the daily and weekly status | You log in to Garmin once with your email and password, on your server; an unofficial interface |
   | `key` | Less: activities, sleep, HRV, daily stats, Body Battery, heart-rate zones and VO2max, and the daily and weekly status. No laps or time in zones, weekly stress, training readiness, training status or load, endurance score, lactate threshold, cycling FTP, progress summaries, planned workouts, courses, race predictions or records; the daily status works without readiness and planned workouts | A Garmin Personal API key you create on the Garmin Connect website; Garmin's official API, which the server only reads with |

   Upkeep: `full`: log in again about every 6 months. `key`: a new key before it expires; create
   it now ([how](docs/garmin-login.md#key-mode-create-a-key)). [More](docs/garmin-login.md).
2. **Deploy your own server**, one per person, on Railway (about $5 a month). In a terminal:

   ```bash
   git clone https://github.com/ivgenich/coach-mcp-for-garmin.git
   cd coach-mcp-for-garmin
   git checkout v1.1.0    # or the newest release tag
   ./scripts/deploy.sh    # asks for your mode and your time zone
   ```

   It creates your **owner secret**: save it in your password manager ([details](docs/deploy-railway.md)).
   Advanced, partly untested: [Docker](docs/deploy-docker.md) on your own computer, behind a Cloudflare Tunnel.
3. **Connect Garmin** (`full` only): the one-time login runs on your server, not on your computer
   (the `garmin-mcp-auth` command exists only there). deploy.sh offers to start it for you over
   `railway ssh` ([how](docs/garmin-login.md#full-mode-the-one-time-login)). `key`: nothing to do.
4. **Add it to Claude.** On claude.ai: **Customize → Connectors → Add → Add custom connector**.
   URL: `https://<your-host>/mcp`, exactly, with no trailing slash.
   Authentication: **Sign in now**. OAuth client: **Register automatically** (keep them if
   Claude marks them **Detected**). Click **Add**, then **Connect** if Claude shows it.
   A page on your server opens. Check that the address bar shows your server before you type
   the owner secret, then click **Approve**. [Details, and Claude Code](docs/connect-claude.md).
5. **Ask** in the Claude app on your phone: "How am I today?" It works when Claude answers with
   a short status: a green, yellow or red level, a few of last night's numbers and a
   recommendation for today. If it doesn't: [Troubleshooting](docs/troubleshooting.md).

## What it costs

Prices as of November 2026. Check the providers' pages before you start.

| Part | Cost |
|---|---|
| Railway | $5 a month: the Hobby plan costs $5 and includes $5 of usage, and this server uses about $2-4. A new Railway account gets a one-time $5 trial credit for up to 30 days. Upgrade to Hobby before the trial ends: afterwards the account falls back to the Free plan's $1 of credit a month, which does not cover this server, and Railway deletes volumes (the server's disk that survives redeploys) made during the trial 30 days after the credit expires, with your Garmin login and sign-ins on them. [Railway plans](https://docs.railway.com/reference/pricing/plans) |
| Docker on your own computer | No hosting fee. The computer must stay on, and the Cloudflare Tunnel needs a domain on Cloudflare, which you pay your domain registrar for. Cloudflare says Cloudflare Tunnel is "Available on all plans" ([Cloudflare Tunnel](https://developers.cloudflare.com/tunnel/), checked November 2026). |
| Claude | Any plan works, Free included: see [Claude plans and apps](#claude-plans-and-apps). |
| Optional features | Tracing: a free tracing service is enough ([Tracing](docs/tracing.md)). History search (Railway only for now): a Postgres, a small worker and a daily scheduler, an estimated $4-8 a month more ([History worker](docs/history-worker.md#cost)). Both are off by default. |
| Garmin | `full` mode: no fee. `key` mode: Garmin's Personal API pages state no price ([Personal API FAQ](https://developer.garmin.com/gc-developer-program/personal/personal-faq/), checked November 2026). |

Each person runs their own server, so each person pays for their own hosting.

## Claude plans and apps

Any Claude plan can add this server as a custom connector: Free, Pro, Max, Ultra, Team and
Enterprise.

- **Free:** On the Free plan you can add one custom connector, so this server uses your one slot.
- **Team and Enterprise:** only an Owner can add a custom connector, in **Organization settings →
  Connectors** (on Enterprise, also a member whose custom role includes managing the
  organization's libraries). It is added for the whole organization, and each member then
  connects with their own account. Other members see the connector, but only someone who knows
  your owner secret can finish a sign-in. Chats in a work organization follow that
  organization's settings, so a personal plan is the better place for your health data.
- **Apps:** once added, the connector works in Claude on the web, in the desktop and mobile apps,
  and in Claude Code signed in with the same Claude account.

Source: Anthropic's
[Add a connector that isn't in the directory](https://claude.com/docs/connectors/custom/add-unlisted.md),
checked November 2026.

## Other MCP clients

The server is a remote MCP server over streamable HTTP, with an OAuth sign-in (and an optional
static bearer token). It is tested with Claude only. Other clients that support remote MCP
servers with OAuth should work, but are untested.

## One server per person

One server serves one Garmin account and one owner. Whoever knows the owner secret can approve a
sign-in and read all the Garmin data the server can reach, so never share it, and don't run the
server for other people. Everyone who wants this, a family member included, deploys their own
server, with their own Garmin login or key and their own owner secret.

## Risks and privacy

- **Not affiliated with or endorsed by Garmin.** Garmin and Garmin Connect are trademarks of
  Garmin Ltd. or its subsidiaries; this project names them only to say what it works with.
- **`full` mode uses an unofficial interface.** The server reaches Garmin Connect through an
  unofficial, reverse-engineered interface (upstream garmin_mcp and the garminconnect library).
  It may stop working without notice, using it may breach Garmin's terms of use, and too many
  logins or calls can get your Garmin account rate-limited or restricted.
- **`full` mode's tokens give full account access.** The login tokens kept on your server can do
  anything you can do in Garmin Connect, including changing and deleting data. By default the
  server only exposes read-only tools (with `PLAN_WRITES=on` it can also add and delete its own
  training-plan workouts), but anyone who gets the tokens is not limited by that. Protect the
  server and your host account like your Garmin password.
- **`key` mode uses Garmin's official Personal API, under Garmin's own terms.** You accept them
  when you create the key: read them there first. The key expires after the period you choose,
  and you can delete it in Garmin Connect at any time. It gives less data than `full` mode.
  Treat the key like a password: whoever has it can read your Garmin data until you delete it.
- **Your own account only.** Connect only your own Garmin account, and don't run the server for
  other people.
- **Not medical advice.** The levels, flags and recommendations are fixed rules over Garmin's
  numbers, for general fitness only. They are not a diagnosis. Talk to a professional about
  symptoms or health concerns.
- **What leaves your server.** When you ask about your data, your AI client receives it (sleep,
  heart rate, HRV, activities with their GPS tracks and locations, your Garmin display name, and
  more) and sends it to its provider, for example Anthropic for Claude. There it falls under that
  provider's terms and your account's settings: on Claude's Free, Pro and Max plans you choose in
  your Privacy Settings whether your chats may be used to improve Claude
  ([Anthropic](https://www.anthropic.com/news/updates-to-our-consumer-terms)). Your host, Railway
  or Cloudflare in front of your own computer, carries the traffic and can read it. The server
  itself calls only Garmin. It sends no telemetry by default. Optional tracing, which you turn on,
  sends a fixed list of fields to the tracing service you name: tool names, timings, outcomes,
  error groups, size buckets, date-range lengths, the auth mode, the caller, the Garmin endpoint
  family, job and queue counts, history result counts and lag, source names, and the server's
  version and role; never health values, names, ids, tokens or text
  ([what is sent](docs/tracing.md#what-is-sent-and-what-never-is)). Optional history search keeps
  a copy of your numbers in your own Postgres and sends nothing out; its embedding model's public
  files are downloaded from huggingface.co (the app keeps them on its volume; the worker has no
  volume, so it downloads them again after each worker deploy) and the model runs on your server
  ([History search](docs/history-search.md)). More:
  [What leaves your server](docs/how-it-works.md#what-leaves-your-server).
- **Text from Garmin is untrusted.** Names and notes can come from apps and coaches linked to your
  Garmin account. The server cleans them and tells Claude they are data, but an instruction in
  such text can still reach Claude: take care when the same chat also has connectors that can act,
  such as sending mail.

## What you get

- **Daily status** (`get_daily_status`): last night's recovery and your planned workouts become a
  green, yellow or red level and a recommendation for today, by fixed rules computed on the
  server; Claude only phrases them. [Fields and rules](docs/daily-status.md).
- **Weekly status** (`get_weekly_status`): the last 7 days against the 7 before.
  [Fields and rules](docs/weekly-status.md).
- **Course profile** (`get_course_profile`): the climbs and elevation profile of a saved course.
- **Your Garmin data** through the read-only tools of upstream garmin_mcp: activities, sleep,
  HRV, Body Battery, VO2max and more, and in `full` mode also laps, time in zones, weekly stress,
  training status, race predictions and records. No tool can change your Garmin data unless you
  turn on training-plan writes, which add and delete only the plan's own workouts, after you
  approve them. [Every tool, per mode](docs/tools.md).
- **Optional add-ons, off by default.** The basic deploy is one service, as in the
  [Quickstart](#quickstart); neither add-on is needed.
  [Tracing](docs/tracing.md) (`TRACING=on`): tool latency, errors and Garmin rate limits on a
  dashboard, with no health values. [History search](docs/history-search.md)
  (`HISTORY_SEARCH=on`, Railway only for now): "find weeks like this one" over your stored
  history; it adds three services next to the server (a Postgres, a
  [worker](docs/history-worker.md) and a daily scheduler), and its embedding model runs on your
  server.
- **Optional recipes for claude.ai:** a [coach Project](recipes/coach-project/README.md), and an
  experimental [morning report page](recipes/claude-ai-morning-report/README.md).

In `key` mode these are off: laps and time in zones, weekly stress, training readiness, training
status and load, endurance score, lactate threshold, cycling FTP, progress summaries, planned
workouts, courses (the course profile included), race predictions and records. The daily and
weekly status work with fewer values: [Every tool, per mode](docs/tools.md).

## Updates and versions

Releases follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html) and are listed in
[CHANGELOG.md](CHANGELOG.md). A MAJOR release (2.0.0) may break something you rely on; MINOR and
PATCH releases don't. The public API (tool names, the fields of `get_daily_status` and
`get_weekly_status`, `RULES_VERSION`, the settings), how to update and how to roll back:
[Updates and versions](docs/updates.md). To hear about new releases, watch this repository on
GitHub: **Watch → Custom → Releases**.

## Uninstall and revoke

1. **Remove the connector from Claude.** On claude.ai: **Customize → Connectors**, select the
   connector, and click **Remove** (or find it in the three-dot menu). On Team and Enterprise, an Owner
   removes it in **Organization settings → Connectors**. If you added the server to Claude Code
   with `claude mcp add`, run `claude mcp logout garmin`, then `claude mcp remove garmin`.
2. **Delete the server and its volume.** That deletes the Garmin login or key, the owner secret
   and every sign-in.
   - Railway: open the volume's **Settings** tab and click **Wipe Volume**: Railway deletes its
     data and its backups and restarts the service. Then delete the volume
     (`railway volume delete --volume <name>` in your clone, or from the project canvas), and
     then the project: its **Settings**, **Danger** tab, **Delete Project**, or
     `railway delete --project <name>`, which asks you to confirm (with 2FA on, have your code
     ready). Railway keeps a deleted volume for up to 48 hours and emails you a link to restore
     it; the wipe has already emptied it.
   - Docker: in the project folder, run `docker compose down --volumes --rmi all`. It removes
     the containers, the `coach-data` volume (the Garmin login or key cache, and every sign-in)
     and the images. Then run `rm .env tunnel.env`. In the Cloudflare dashboard, delete the
     tunnel (**Networking → Tunnels**) and the DNS record of its hostname if one is left.
3. **Revoke Garmin access.** `key` mode: delete the key on the Garmin Connect website (the
   Personal API Key page); Garmin says a deleted key and its access tokens stop working. `full`
   mode: deleting the volume deleted the server's Garmin tokens. If you think they leaked, also
   change your Garmin password; Garmin does not document whether that ends sessions that already
   exist.
4. **Optional features.** History search: delete the Postgres, worker and cron services with the
   project; Postgres backups can still hold copies, so check your Railway backup settings
   ([History search](docs/history-search.md#forgetting)). Tracing: delete the tracing project or stack at your
   tracing service and revoke its token.
5. **Optional:** delete the Claude chats that hold your health data, the routines and the page of
   the morning report recipe, and the owner secret in your password manager.

## Lost the owner secret?

It is stored in your server's settings, so you can read it there again: on Railway, in the
service's variables (`OWNER_SECRET`; with the CLI,
`railway variable list --kv | grep ^OWNER_SECRET=`); with Docker, in the `.env` file in your
project folder (`grep '^OWNER_SECRET=' .env`). If you can't read it, or it may have leaked, set a
new one:
[Rotate the owner secret](docs/maintenance.md#rotate-the-owner-secret). A new secret signs every
client out, so sign in again: **Reconnect** in claude.ai, `/mcp` in Claude Code. Your Garmin
login or key is not affected.

## Evals

Beyond the unit tests, `evals/` measures how a Claude model behaves with these tools: the right
tool, no invented numbers, following the server's error texts, key-mode limits, planted
instructions in Garmin text ignored. It runs the real server in process on synthetic data, never
yours. Live runs are started by hand and cost a few dollars; every pull request grades the
committed transcripts offline. [Evals](docs/evals.md).

## Help

- Something doesn't work: [Troubleshooting](docs/troubleshooting.md).
- A security problem: report it privately, as [SECURITY.md](SECURITY.md) explains.
- Questions and bug reports: GitHub issues (the **Question** and **Bug report** forms). Never paste tokens, secrets, keys, emails or health
  data into an issue. To change something: [CONTRIBUTING.md](CONTRIBUTING.md).

This is a personal project, maintained on a best-effort basis, with no SLA.

## Docs

| Page | What it covers |
|---|---|
| [Deploy on Railway](docs/deploy-railway.md) | `scripts/deploy.sh`, the settings, the health check |
| [Deploy with Docker](docs/deploy-docker.md) | Docker Compose on your own computer, behind a Cloudflare Tunnel |
| [Garmin modes and login](docs/garmin-login.md) | `full` or `key`, the one-time login, the key |
| [Connect Claude](docs/connect-claude.md) | claude.ai, Team and Enterprise, the apps, Claude Code |
| [Tools](docs/tools.md) | Every tool in each mode, and how to turn tools off |
| [Daily status](docs/daily-status.md) | What `get_daily_status` returns, its rules, calibration |
| [Weekly status](docs/weekly-status.md) | What `get_weekly_status` returns, and its rules |
| [Maintenance](docs/maintenance.md) | Garmin re-login, key renewal, new secrets and tokens |
| [Updates and versions](docs/updates.md) | Versions, the public API, updating, rolling back |
| [MCP protocol](docs/protocol.md) | Protocol versions, server instructions, checking a deploy |
| [Tracing](docs/tracing.md) | Optional: traces and metrics with no health values, the dashboard and alerts |
| [History search](docs/history-search.md) | Optional: the history tools, what is stored, retrieval quality, forgetting |
| [Training plans](docs/training-plan.md) | Optional: plan previews, Garmin writes, tracking and the weekly review, the plan rules, what is stored |
| [History worker](docs/history-worker.md) | Optional: the Postgres, worker and cron services, the job queue |
| [Evals](docs/evals.md) | How a Claude model behaves with the tools: cases, running, cost, results |
| [How it works](docs/how-it-works.md) | Architecture, the sign-in, security, what leaves your server |
| [Troubleshooting](docs/troubleshooting.md) | Symptoms, causes and fixes |
| [ext/README.md](ext/README.md) | For developers: the modules and the tests |

## License and credit

MIT, see [LICENSE](LICENSE) and [NOTICE](NOTICE). The MCP server itself is
[Taxuspt/garmin_mcp](https://github.com/Taxuspt/garmin_mcp) by Alexandre Domingues (MIT), which
the Docker build downloads at a pinned commit; this repository adds the proxy and the sign-in,
the deployment, the `ext/` tools and the recipes around it. In `key` mode the server uses Garmin's
official client, [garmin/connect-personal-api-python](https://github.com/garmin/connect-personal-api-python)
(Apache-2.0), which the Docker build installs.
