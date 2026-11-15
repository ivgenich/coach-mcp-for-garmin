# coach-mcp-for-garmin

A small Railway service that runs [Taxuspt/garmin_mcp](https://github.com/Taxuspt/garmin_mcp)
(an MCP server for Garmin Connect) behind a Caddy proxy that requires a secret token.
You add it to claude.ai once as a custom connector, and then you can ask Claude about
your Garmin activities, training effect, VO2max and sleep from the mobile app.

It also adds tools of its own (`ext/`): a course climb profile, a **daily status** that
turns last night's recovery and your planned workouts into a green / yellow / red level
and a recommendation by fixed rules, a **weekly status**, and an optional, experimental **morning
report** recipe built from them and stored on a private Claude artifact page.

## Read this first

- **Not affiliated with or endorsed by Garmin.** Garmin and Garmin Connect are
  trademarks of Garmin Ltd. or its subsidiaries.
- **Unofficial interface.** The server reaches Garmin Connect through an unofficial,
  reverse-engineered interface (upstream garmin_mcp and the garminconnect library).
  It may stop working without notice, using it may breach Garmin's terms of use, and
  too many logins or calls can get your Garmin account rate-limited or restricted. A
  read-only mode that uses Garmin's official personal API key is planned; it is not
  available yet.
- **The Garmin tokens give full account access.** The login tokens stored on the
  server (`/data/tokens`) can do anything you can do in Garmin Connect, including
  changing and deleting data. The server itself only exposes read-only tools, but
  anyone who gets the tokens is not limited by that. Protect the server and your
  Railway account like your Garmin password.
- **Your own account only.** Connect only your own Garmin account, and don't run the
  server for other people.
- **Not medical advice.** The levels, flags and recommendations are fixed rules over
  Garmin's numbers, for general fitness only. They are not a diagnosis. Talk to a
  professional about symptoms or health concerns.
- **What leaves your server.** When you ask about your data, your AI client receives
  it (sleep, heart rate, HRV, activities with their GPS tracks and locations, your Garmin
  display name, and more) and sends it to its provider, for example Anthropic for Claude.
  There it falls under that provider's retention and training settings for your account.
  Check those settings before you connect.

Below, `<your-app>.up.railway.app` stands for your service's domain (`deploy.sh` prints it).

## Architecture

```
claude.ai chat / phone app ------+
Claude Code cloud routine -------+  HTTPS + bearer token
  (optional recipe: report page) |
                                 v
+------------------- Railway container, /data volume -------------------+
| Caddy :$PORT (PID 1): checks the token, then drops it                 |
|   |                                                                   |
|   v                                                                   |
| garmin-mcp 127.0.0.1:8000 -- the one Garmin login --> Garmin Connect  |
|    upstream's tools + ext/ tools (get_daily_status, ...)              |
| /data/tokens: Garmin login                                            |
+-----------------------------------------------------------------------+
```

### What runs where

| Piece | Runs | Secrets it sees | Does |
|---|---|---|---|
| Caddy | container, PID 1, public `$PORT` | `MCP_BEARER_TOKEN` | Checks the token on everything but `/healthz` and `/readyz`; proxies to garmin-mcp |
| garmin-mcp (`ext/launch.py`) | container, `127.0.0.1:8000` | none (Garmin tokens on `/data/tokens`) | Upstream's tools plus this repo's |
| Cloud routine, optional (`recipes/claude-ai-morning-report/`) | claude.ai's cloud, on a schedule | your claude.ai connectors | Stores the report in the report page's database |

The split keeps each secret out of the programs that don't use it, and so out of their
logs and tool output. It is not a sandbox: everything in the container runs as root.

## Before you start

- **Railway plan.** The trial is a one-time $5 credit for 30 days. This service uses about
  $2–4 a month, so the trial ends on **day 30**. **Upgrade to Hobby ($5/mo, includes $5 of
  usage) before then.** Otherwise the service stops, and Railway deletes trial volumes 30
  days later, which deletes your Garmin login and means redoing step 2.
- **Verified Railway account** (the Full trial, not the Limited one; Railway asks you to
  connect GitHub). The Limited trial restricts outbound network access, which can block
  the server's calls to Garmin.
- **Turn on 2FA for Railway.** Anyone with your Railway account can read your Garmin
  session.
- **Railway CLI:** `brew install railway`. `railway ssh` also needs an SSH key registered
  with Railway. The CLI offers to register `~/.ssh/id_ed25519.pub`; if you have no key,
  run `ssh-keygen -t ed25519` first.
- **Your time zone** as an IANA name, such as `Europe/London` or `America/New_York`
  ([list](https://en.wikipedia.org/wiki/List_of_tz_database_time_zones)). The server
  requires it (step 1).
- Docker is **not** needed locally; Railway builds the image.

## 1. Deploy

```bash
cd coach-mcp-for-garmin    # your clone of this repo
REPORT_TZ=Area/City ./scripts/deploy.sh    # your IANA zone, e.g. REPORT_TZ=Europe/London
```

`REPORT_TZ` is required: it decides what "today" and "yesterday" mean for the daily and
weekly status. The script refuses a name that is not a time zone on this machine (match
the case exactly) and stops before building if no zone is set. Later runs keep the zone
already on Railway; to change it, run the script again with a new `REPORT_TZ=...`.
Without a valid `REPORT_TZ` the server does not start, and `railway logs` shows a
one-line error saying why.

The script creates the project and service, attaches a volume at `/data`, generates the
token, builds and deploys (a few minutes), and prints the **service URL**, the
**connector URL** and the **token** once (only when its output is a terminal; otherwise it
tells you how to read the token from Railway). Save the token in your password manager. If
a run stops after the token was set, it says so and shows the same command. The project
and service are named `coach-mcp-for-garmin`; for another name, add
`DEPLOY_SERVICE=<name>` to the first run.

Optional: in the Railway dashboard, under the service's Settings > Deploy, set the
Healthcheck Path to `/healthz`. Never use `/readyz` there; it stays 503 until step 2.

## 2. One-time Garmin login

Your Garmin email and password exist only inside this SSH session; they are never stored.

```bash
railway ssh
garmin-mcp-auth --token-path /data/tokens   # Garmin email, password, MFA code
exit
```

The first run prints "Existing tokens are invalid … Proceeding with re-authentication".
That is expected, because the token folder starts out empty. The tokens last about
**6 months** and survive redeploys.

Wait about 30 seconds while the server notices the tokens and restarts itself, then check:

```bash
./scripts/test-connector.sh https://<your-app>.up.railway.app   # asks for the token; expect RESULT: PASS
```

If `/readyz` still fails, run `railway logs` and look for `Garmin tokens found` followed by
`Garmin Connect client initialized successfully`. Do **not** re-run `garmin-mcp-auth`
unless it printed an error, because repeated logins can get your Garmin account rate-limited.

## 3. Add to claude.ai (in a browser; mobile picks it up by itself)

Sign-in is being reworked before the public release.

1. **Customize → Connectors → + Add → Add custom connector**
2. Name: `Garmin`. URL: `https://<your-app>.up.railway.app/mcp`. Click **Continue**.
3. Authentication: choose **No sign-in**. claude.ai pre-selects "Sign in now (Detected)",
   an OAuth mode this server doesn't have; it fails with "Couldn't register with … sign-in service".
4. Leave the OAuth client settings alone; they don't apply with No sign-in.
5. Under **Request headers**, add the name `Authorization` with the value `Bearer <token>`.
6. Click **Add**.

**No Request headers section?** It is a beta that not every account has. Remove the
connector and add it again with the URL `https://<your-app>.up.railway.app/<token>/mcp` and
**No sign in**. The token then travels in the URL, so treat that URL like a password.
Caddy never logs it, but Railway's own HTTP logs record request paths, so with this mode
anyone who can read the service's logs in the Railway dashboard can see the token.

## 4. Try it on your phone

- "How am I today?"
- "Pull my latest activity from Garmin and summarize it"
- "What was the training effect of my last run?"
- "How did I sleep last night?"
- "Compare my last two runs"

## 5. Optional: coach Project

1. On claude.ai in a browser, create a Project from the chat sidebar (**Projects → New
   project**), not one under claude.ai/code: those projects' main conversation has no connectors.
2. **Set project instructions:** paste `recipes/coach-project/instructions.md`, fill in
   the "My details" lines, then **Save instructions**. Keep your filled-in copy as
   `recipes/coach-project/instructions.local.md`: git ignores `*.local.md`, so your
   profile stays off GitHub.
3. In each new chat in the project, check Garmin is on under **+ → Connectors**. The project
   works on your phone too.

## Daily status in chat

Ask "How am I today?" and Claude calls `get_daily_status` (the coach Project's instructions
make it the first call). The tool returns JSON with:

- `level`: `green`, `yellow`, `red`, or `incomplete` when neither sleep nor Training
  Readiness has synced yet; `recommendation` for today's planned workout.
- `recovery`: sleep (duration, score, stages), HRV beside its 7-day average and Garmin's
  status, resting HR beside its 7-day average, Training Readiness, recovery time, Body
  Battery, stress, SpO2, respiration.
- `load`: acute and chronic load, ACWR with Garmin's status, yesterday's sessions.
- `today` and `tomorrow`: planned workouts, each classed `hard_or_long` or easy and
  marked `completed` once done.
- `flags` (which rules fired) and `missing` (which values Garmin had none for).

Every number, flag, level and recommendation is computed on the server by the rules
below; Claude only phrases them. A value Garmin doesn't have is `null` and listed in
`missing`, never guessed, and a rule that needs it is skipped. Arguments: `date`
(YYYY-MM-DD, default today in the time zone of the required service variable
`REPORT_TZ`; see step 1 and `.env.example`), `text_format` (`markdown` adds the
report text, in English), `probe` (only "have last night's sleep and Training Readiness
synced?", 2 Garmin calls) and `include_sources` (adds trimmed raw Garmin payloads and
their key paths, for debugging). A full status takes about a dozen Garmin calls, made in
parallel with per-call timeouts; a call that fails marks its values missing instead of
failing the status.

## Daily status rules v1

Baselines are your own: Garmin's 7-day averages. The thresholds are starting values (see
"Calibration"); each status carries them under `thresholds`, and the lactate threshold
and FTP values the hard-workout rule used under `thresholds_athlete`, with `rules_version`.

| Flag | Fires when | Level |
|---|---|---|
| `low_hrv` | last night's HRV ≤ its 7-day average − 10 ms | yellow |
| `hrv_low_trend` | Garmin's 7-day HRV average below the low end of your balanced range on each of the last 3 days (needs all three) | red |
| `high_rhr` | resting HR ≥ its 7-day average + 5 bpm | yellow |
| `short_sleep` | under 6 h of sleep (not when `sleep_debt` fires) | yellow |
| `sleep_debt` | under 6 h last night and the night before | red |
| `low_readiness` | Training Readiness under 40 | yellow |
| `load_spike` | Garmin's ACWR status is HIGH or VERY_HIGH; with no status, ACWR > 1.5 | red |
| `load_rising` | ACWR > 1.3 while Garmin's status is not HIGH or VERY_HIGH (never together with `load_spike`) | yellow |
| `recovery_incomplete` | a hard or long workout still to do today and Garmin's recovery time is still ≥ 6 h | yellow |

Garmin's schedule has a date but no time of day, so "recovery time longer than the time
until the workout" is approximated by the 6 h rule.

**Level:** any red flag → red; otherwise sleep and readiness both missing → incomplete;
otherwise 3 or more yellow flags → red, 1–2 → yellow, none → green.

**Recommendation:** nothing planned today → `no_planned_workout`; everything planned today
already done → `done_today`; level incomplete → `no_recovery_data` (decide by feel);
otherwise, by the workouts still to do:

| Level | Yellow flags | A hard or long one left today | Only easy ones left |
|---|---|---|---|
| green | 0 | `as_planned` | `as_planned` |
| yellow | 1 | `as_planned_may_stop`: as planned, stop if it feels wrong | `as_planned` |
| yellow | 2 | `zone2_or_shorten`: make it Zone 2 or shorten it | `as_planned` |
| red | any | `short_zone2_or_rest`: short Zone 2, or rest | `shorten` |

**Hard or long:** a workout is hard when its steps at intensity add up to at least 300 s
or 1000 m of work (`hard_min_work_s`, `hard_min_work_m`), so a few strides never make a
run hard. A step other than warm-up, cool-down, recovery or rest is at intensity when it
has:

- a heart rate, power or pace zone target of 4 or higher (primary or secondary target);
- a pace or speed range whose midpoint is at least 0.90 × your running lactate-threshold
  speed (`hard_pace_of_lt_speed`);
- a power range whose midpoint is at least 0.91 × your cycling FTP (`hard_power_of_ftp`);
- a heart rate range (bpm) whose midpoint is at least 0.95 × your lactate-threshold heart
  rate (`hard_hr_of_lthr`).

Without the threshold a range needs (and always for swim pace), a range counts only on an
interval step inside a repeat of 2 or more. A range alone proves little: workouts synced
from TrainingPeaks put pace ranges even on easy single-step runs. Your lactate threshold
and FTP are read from Garmin only when a workout needs them. A workout without steps
counts as hard when its name says so, in English (interval, tempo, threshold, VO2, race,
hard, brick). A name in another language does not count.

A workout is long when its estimated duration or distance reaches run 75 min / 15 km,
bike 150 min / 70 km, swim 75 min / 3.5 km, other sports 90 min. The duration is Garmin's
estimate when there is one; otherwise it is added up from the steps only when every step
is timed. A workout set by distance, like most swims, then has no duration, and only its
distance can make it long.

**Done:** a planned workout counts as done when Garmin linked an activity to it or, since
Garmin rarely does, when an activity of the same sport (run, bike, swim, other) was
recorded on the same local date; each activity completes one workout at most. Done
workouts drop out of the hard-day rules: the table's hard column and
`recovery_incomplete` look only at workouts still to do.

## Morning report page (optional recipe, experimental, claude.ai only)

The same status can land on a private Claude artifact page you open in the Claude app:
four tabs (today, a 28-day history, weekly, calibration), and on today's report three
buttons to tap how you felt (good / ok / tired). A **Claude Code cloud routine**, a
scheduled Claude session on claude.ai's servers, calls `get_daily_status` through your
Garmin connector a few times each morning and stores the result in the page's own
database. It needs cloud routines and the artifact database, so it works on claude.ai
only. Files and setup: `recipes/claude-ai-morning-report/README.md`.

## Weekly report

`get_weekly_status` covers the 7 days ending `end_date` (default yesterday) against the 7
days before: HRV 7-day against 28-day average, average sleep and nights under 6 h, volume
per sport, load (acute, chronic, ACWR), plan completion, the week's long session (aerobic
decoupling), VO2max and training status, weight, and gear mileage.

Plan completion is counted over the whole week, because planned sessions often move a day
or two: a planned workout is done when Garmin linked an activity to it or an activity of
the same sport was recorded on any day of that week (same-day matches first, one activity
per workout). The plan line gives how many were done and how many of those on the planned
day; in the JSON, `plan` holds `scheduled`, `completed`, `on_planned_day`, `linked`
(Garmin's own links) and `completion_pct`. The daily status keeps the same-day rule.

With the morning report recipe, the cloud routine adds the weekly report to the page's
weekly tab on Sundays.

| Flag | Fires when | Level |
|---|---|---|
| `hrv_down` | 7-day HRV average ≤ 28-day average − 5 ms, or below your balanced range | yellow |
| `short_nights` | 3 or more nights under 6 h | yellow |
| `volume_jump` | total hours ≥ 1.3 × the week before, which had at least 3 h | yellow |
| `load_high` | Garmin's ACWR status is HIGH or VERY_HIGH | red |
| `low_completion` | under 70% of planned workouts done (on any day of the week), with at least 3 planned | yellow |
| `gear_near_limit` | a gear item at 90% or more of its maximum distance | yellow |

The level counts flags as the daily one does; it is `incomplete` only when HRV, sleep and
volume are all missing.

## Calibration

The thresholds are general starting values. After about four weeks of reports, compare
the flags with how you actually felt: on the report page (morning report recipe), tap
good / ok / tired each morning. The one-time routine in
`recipes/claude-ai-morning-report/calibration.md` then counts how
often each flag fired on days you felt fine or tired, and proposes at most five one-step
threshold changes in the page's calibration tab (it needs at least 14 days with a feel
pick). It changes nothing itself. To apply a proposal, edit `THRESHOLDS` in
`ext/daily_status.py` (or `ext/weekly_status.py`), bump `RULES_VERSION`, run the tests and
deploy.

## Maintenance

- **Garmin tokens expired (~6 months).** Tool calls fail with "Garmin authentication
  failed", while `/readyz` can still say 200. Run `railway ssh`, then
  `garmin-mcp-auth --token-path /data/tokens --force-reauth`, then
  `kill "$(cat /tmp/garmin-mcp.pid)"` (the server restarts with the new tokens in about 3 s),
  then `exit`.
- **Rotate the token** (do it right away if it may have leaked):
  `./scripts/deploy.sh --rotate-token`. Then remove the Garmin connector in claude.ai and
  add it again with the new token. Connector headers can't be edited in place.
- **Upstream update.** The Dockerfile pins `GARMIN_MCP_REF` to a commit, so a plain
  redeploy rebuilds the same version. To take a fix, set it to a newer commit from
  [the repo](https://github.com/Taxuspt/garmin_mcp/commits/main), regenerate
  `constraints.txt` from that commit's `uv.lock`, and run `railway up`. The build fails,
  instead of the server, if the update breaks a hook or a helper `ext/` relies on.
  Keep the `mcp==2.3.0` pin and its 2.x dependencies when you regenerate: upstream's
  `uv.lock` still pins mcp 1.x (see "MCP protocol versions" below). Once upstream itself
  moves to mcp 2.x (its issue #344), `ext/mcp2_compat.py` can go.

## MCP protocol versions

The server runs the official MCP Python SDK **2.3.0**, so it speaks both protocol eras:

- **2026-07-28** (current claude.ai and Claude Code): the client opens with
  `server/discover`, and every request is a self-contained POST carrying its own
  `MCP-Protocol-Version`, `Mcp-Method` and (for `tools/call`) `Mcp-Name` headers. Errors
  come back with a real HTTP status (400 bad or mismatched headers, 404 unknown method,
  405 for GET/DELETE). A tool call that runs longer than 15 s switches to an SSE stream with
  keep-alive pings, which Caddy passes through unbuffered. The pings only fire while the
  event loop is free: most upstream tools make blocking Garmin calls inside `async def`, so
  during such a call no ping is sent (same as on mcp 1.x).
- Never run `pip install` inside the container (for example over `railway ssh`):
  upstream still declares `mcp<2`, and pip could swap the SDK back to 1.x.
- **2024-11-05 to 2025-11-25** (older clients): the `initialize` handshake, as before.

Upstream garmin_mcp is still written for mcp 1.x (`from mcp.server.fastmcp import
FastMCP`, which mcp 2.x removed). `ext/mcp2_compat.py` supplies that name as a thin
adapter over mcp 2.x's `MCPServer`, installed by `ext/launch.py` before upstream is
imported. The Dockerfile installs upstream with `--no-deps` to get past its `mcp<2` cap,
then the pinned dependencies from `constraints.txt`. The build fails if the adapter no
longer fits the installed SDK.

**Check both eras** after a deploy (needs a Python with `mcp==2.3.0`, e.g.
`python3.12 -m venv /tmp/mcpv && /tmp/mcpv/bin/pip install mcp==2.3.0`):

```bash
read -rs MCP_BEARER_TOKEN && export MCP_BEARER_TOKEN   # paste the token; not echoed, not in history
/tmp/mcpv/bin/python scripts/test-protocol.py --url https://<your-app>.up.railway.app/mcp
# optional: also call one tool in both eras
/tmp/mcpv/bin/python scripts/test-protocol.py --url https://<your-app>.up.railway.app/mcp --tool-call get_daily_status
```

Every check prints PASS or FAIL; the exit status is 1 on any FAIL. The tools checks expect
every tool in `config/enabled-tools.txt`: if `GARMIN_ENABLED_TOOLS` narrows the list, pass
`--expect-tools N` (the number of names in it). `scripts/test-connector.sh` still checks
the auth gate and the old handshake.

**Roll back** if a new version misbehaves: redeploy the previous deployment or image. On
Railway: dashboard → the service → **Deployments** → the last deployment from before the
change → **Redeploy**. The `/data` volume (Garmin login) is untouched.

## Tools

The server exposes only read-only tools, and the list is baked into the image from
`config/enabled-tools.txt`. At startup it checks every name against the read-only tools
(upstream's plus this repo's), and it never registers a tool that changes data, whatever
the settings say. No setting can turn on a write tool.

- **Add or remove a tool for good:** edit `config/enabled-tools.txt` (only read-only
  tools are accepted), then redeploy (`railway up`).
- **Turn tools off without a rebuild:** set the service variable `GARMIN_ENABLED_TOOLS`
  to a comma-separated list of tool names. It can only narrow the baked list, never add
  to it. Every name must be in `config/enabled-tools.txt`: a write tool, an unknown name
  or a read-only tool the file leaves out stops the server at startup, with a one-line
  error in `railway logs`. Delete the variable to go back to the whole baked list. If it
  is set when you add a tool to the file, add the tool to the variable too, and when you
  remove a tool from the file, remove it from the variable too.

`GARMIN_DISABLED_TOOLS` is no longer supported: if it is set, the server does not start
(and `deploy.sh` stops before deploying). Delete it:
`railway variable delete GARMIN_DISABLED_TOOLS`.

`get_course_profile`, `get_daily_status` and `get_weekly_status` are not upstream's: this
repo adds them from `ext/` (see `ext/README.md`). After adding a tool, open the Garmin
connector on claude.ai (Customize → Connectors), choose **Refresh tools list** from the
menu next to Disconnect, and start a new chat; claude.ai keeps the tool list it saw when
the connector was added.

## Security

The token gives full access to this server: all your Garmin data, through the read-only
tools it exposes (see "Tools"). The Garmin tokens on `/data/tokens` are more powerful:
they give full access to your Garmin account, so anyone who can read the volume (for
example through your Railway account) can change or delete your Garmin data. If you
think your Garmin session leaked, change your Garmin password and redo step 2.

If you use the morning report recipe, its page's database holds your daily and weekly
statuses, which are health data: keep the page private, and keep its one write rule for
the owner only.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `401` | Missing or wrong token | Header value must be `Bearer <token>`, or use the URL-with-token form |
| `/readyz` 503 | Garmin login not done or still loading | Do step 2; wait 30 s after it; check `railway logs` |
| The deployment keeps failing at startup; `railway logs` shows a one-line config error | `REPORT_TZ` is missing or not a valid IANA zone, `GARMIN_ENABLED_TOOLS` names a tool that is not in `config/enabled-tools.txt`, or `GARMIN_DISABLED_TOOLS` is set | Fix the variable the error names (step 1, "Tools"); Railway redeploys by itself |
| Tool call fails right after a deploy | The only container is being replaced | Wait about a minute and retry |
| Tool calls fail, `/readyz` 200 | Garmin tokens expired | See Maintenance |
| Garmin login keeps failing | Garmin rate limit or lockout | Wait 1–24 h; check you can log in at connect.garmin.com |
| `server/discover` answers `400 Unsupported protocol version` | The running image is still on mcp 1.x (an old deployment, or a rollback) | Deploy the current repo; `scripts/test-protocol.py` should then pass its `modern` check |
| `400` with `"header does not match"` (code -32020) | A 2026-07-28 client sent `Mcp-Method` / `Mcp-Name` / `MCP-Protocol-Version` headers that disagree with the request body. Caddy forwards these headers unchanged | Client-side bug; retry with another client, or check a proxy in between is not rewriting headers |
| `413 Request body too large` | mcp 2.x rejects request bodies over 4 MB | No tool here needs that; check the client |
| A tool error says `Error executing tool <name>: <message>` | The tool raised, e.g. `Garmin login failed` before login. mcp 2.x alone would send only `Error executing tool <name>`; `ext/mcp2_compat.py` keeps the message, as mcp 1.x did. `railway logs` shows it as `Tool '<name>' failed: ...` (no traceback) | Act on the message (for login: `garmin-mcp-auth`, see above). A bare `Error executing tool <name>` with nothing after it means the adapter is not in use: check the deploy |
| New tools missing in claude.ai | claude.ai keeps the tool list it saw when the connector was added | Customize → Connectors → Garmin → **Refresh tools list**, then start a new chat. Check the tool is in `config/enabled-tools.txt` and deployed |
| The report says incomplete (`Incomplete data`) | Neither sleep nor Training Readiness had reached Garmin Connect when it was built | Open the Garmin Connect app to sync the watch; the routine's later runs fill the report in, or ask "How am I today?" |
| `deploy.sh` stops with "Could not read the service variables" | Railway was unreachable or the login expired; it stops before changing anything, so the token is never replaced by accident | `railway whoami`, `railway login` if needed, then run it again |
| Rides are classed hard too often | The cycling FTP stored in Garmin Connect is out of date, so ordinary power targets sit above 0.91 × FTP | Update your FTP in Garmin Connect; `thresholds_athlete` in the status shows the value used |

## License and credit

MIT, see `LICENSE`. The MCP server itself is [Taxuspt/garmin_mcp](https://github.com/Taxuspt/garmin_mcp)
by Alexandre Domingues (MIT), which the Docker build downloads at a pinned commit; this
repository adds the proxy, the deployment, the `ext/` tools and the report around it.
