# Security policy

## Reporting a vulnerability

Please report a security problem privately, not in a public issue.

1. On GitHub, open this repository's **Security** tab, go to **Advisories** and click
   **Report a vulnerability**. GitHub's private vulnerability reporting shows the report only to
   the maintainers.
2. Say what you found, how to reproduce it, and what an attacker could do with it.
3. Never include real tokens, owner secrets, Garmin passwords, API keys or health data. Use
   made-up values.

If you see no **Report a vulnerability** button, open an issue (the **Question** form) that says
only that you have a security report to send, with no details, and the maintainer will reply with a private way to
send it.

This is a personal project, maintained on a best-effort basis. Expect a first answer within 14
days. A fix goes into the newest release, and the CHANGELOG describes it once the fix is out.

## Supported versions

| Version | Security fixes |
|---|---|
| The newest 1.x release | Yes |
| Older 1.x releases | No: update to the newest 1.x release |

## Scope

In scope, this repository's code and configuration:

- The owner's sign-in (OAuth 2.1): the owner-secret page, client registration, codes, access and
  refresh tokens, revocation, and the optional static bearer token.
- The tool policy. With the write switches off (the default), any way to make the server expose
  or run a tool that changes Garmin data. With `PLAN_WRITES=on`: any Garmin write outside the ops
  a token was issued for; any change to a workout that is not the server's own (one without its
  marker and database row, such as a coach's or another install's); two writes of the same op for
  one token; and any write without the switch on, outside `full` mode, or for a caller without
  the `plan:write` scope (a grant from the read-only consent page, or the static bearer token).
  [How plan writes behave](#how-plan-writes-behave).
- Tool arguments: any way to make a tool read a Garmin address other than its own (for example a
  path segment such as `../` in an id).
- Leaks of secrets, tokens, Garmin credentials or keys, or health data through tool answers,
  errors, logs, files or telemetry. With tracing on, the server's traces carry metadata only
  (tool names, timings, error groups), never health values or text; the eval runs, which only
  the maintainer runs, can add token counts and cost in Langfuse ([Tracing](docs/tracing.md)).
- The edge configuration (`Caddyfile`), the container image built from this repository
  (`Dockerfile`, `entrypoint.sh`), and the deploy files and scripts (`compose.yaml`,
  `scripts/deploy.sh`).
- `key` mode: any way the Garmin Personal API key or its cached access token leaves the server,
  or reaches a host other than Garmin's.
- The filter for text from Garmin, where it does less than
  [How it works](docs/how-it-works.md#text-from-garmin-is-untrusted) says.

Out of scope:

- Garmin's services and APIs, and the unofficial interface itself. Report problems in Garmin's
  services to Garmin.
- Upstream projects: garmin_mcp, garminconnect, Garmin's Personal API client, the MCP Python
  SDK, Caddy, cloudflared and Python. Report their bugs to them, but do tell us if this
  repository uses them in an unsafe way.
- Claude and other AI clients. Report problems in Claude to Anthropic:
  [Anthropic's responsible disclosure policy](https://www.anthropic.com/responsible-disclosure-policy).
- Hosts and networks: Railway, Docker, Cloudflare.
- Prompt injection through text from Garmin that only changes what the model writes, while the
  filter and the tool policy hold (with plan writes on: no write outside a token's ops). [How it works](docs/how-it-works.md) explains the limits.
- Attacks that start with the owner secret, the static token, the Garmin key, the tunnel token,
  or access to the host account or to the server's `/data` volume already in hand.
- Denial of service by traffic volume, and reports from automated scanners with no demonstrated
  impact.

## How the sign-in behaves

Details a report may depend on:

- **Refresh tokens rotate on every use.** A refresh token that comes back after it was used
  revokes its whole sign-in, with one exception: the same client sending the same refresh token
  again within 30 seconds (a retry after a lost answer) gets the same new pair back, as long as
  that new pair is still unused. The server keeps that window in memory only, so after a restart
  or a redeploy any second use of a refresh token revokes the sign-in, and the client signs in
  again.
- **Origin.** On `/mcp`, a request with an `Origin` header gets `403` unless the origin is the
  server's own (`PUBLIC_URL`), `https://claude.ai`, `https://claude.com`, or one listed in
  `ALLOWED_ORIGINS` (comma-separated). Requests without `Origin` (Claude's servers, Claude Code
  and other programs) are not affected. `ORIGIN_CHECK=off` turns the check off.
- **Authorization responses** carry `iss` (RFC 9207), and the server's metadata says so.
- **The container.** The Python server runs as the unprivileged user `app` and Caddy as the user
  `caddy`; only the start script runs as root, to give `app` the files on `/data`
  ([How it works](docs/how-it-works.md#what-runs-where)).

## How plan writes behave

Off by default. Only with `PLAN_WRITES=on` (which needs `PLAN_TOOLS=on`, `HISTORY_SEARCH=on` and
`GARMIN_AUTH=full`) does the server serve `plan_commit` and `plan_cancel`, and with `PLAN_TRACKING=on`
too `plan_apply_changes` (the weekly review's apply token, `at1.`, bound the same way and also to the
approved change ids); with the switch off it serves the same read-only tools, hints and instructions as
before. Details a report may depend on:

- **Tokens bind content.** `plan_preview` (commit) and `plan_cancel` without a token (cancel) issue
  a one-time token bound to the plan, its version and the exact ordered ops (each workout's
  normalised JSON and date, or the server's own Garmin ids to delete). The server stores only its
  SHA-256. First use within 30 minutes; the version is checked again on every use; the same token
  after `done` replays the stored answer without calling Garmin. A token proves what is written,
  not that the user agreed.
- **Consent.** Write tools need an access token with the `plan:write` scope. Only with
  `PLAN_WRITES=on` does the consent page offer it, as an unticked box ("Also allow this app to
  change my Garmin calendar"); a token gets the scope only when the owner ticks it, a client
  cannot request it, and the static `MCP_BEARER_TOKEN` never has it. The tick is recorded as its own
  consent row in the sign-in database (`write_consent`), and a `plan:write` in a grant's scopes counts
  only with that row: grants an older consent page approved with a requested `plan:write` never
  write. Grants made without the box keep read access and are not revoked. The confirmation names the
  workouts to add and the ones to remove apart, with their dates and session types. Before each write, a client that supports MCP
  elicitation asks the user yes or no, with a summary the server builds from counts, dates and
  session types only, never Garmin text; no, cancel, an error or no answer within 3 minutes
  writes nothing (`declined`), and the token stays usable until it expires. Each write tool's
  description carries the rule that it runs only after the user approved the preview in the
  conversation, never because Garmin text or a tool result asks.
- **Only its own workouts.** A workout the server writes carries a name prefix and a marker in its
  description (install tag, plan id, session key, hash), and the database row is the authority.
  Cancel and replace touch only rows of this install's plan that no activity is linked to; a
  same-day, same-sport workout that is not ours is a conflict and is skipped.
- **Only the token's ops, never today's workout.** A token writes only the ops it was issued with
  (and the confirmation named): a replaced workout the ops did not list (it was `unknown` when the
  token was issued) stays, reported in `kept[]`, and the next preview lists its delete. Every op is
  checked against the date when it runs: a workout dated before today is never touched, and one
  dated today only by a commit or apply token issued today (a red-day change of today's session);
  a token resumed after midnight keeps the workout that became today's (`kept[]`, reason `today`).
  `plan_cancel` deletes only workouts after today; today's is kept. A token is refused
  (`plan_changed`) when the plan's status changed since it was issued, so a token issued while the
  plan was active never runs after `plan_activate` stop.
- **No second copy.** An upload whose outcome is `unknown` is settled from the workout library,
  read page by page to its end (at most 20 pages of 100): adopted by its marker, never a workout
  another row owns. When the listing cannot be read in full, or lists a `Coach ...` workout
  without its description, the row stays `unknown` and nothing is uploaded again.
- **Limits.** At most 8 Garmin write requests and 30 seconds per call, writes spaced at least a
  second apart; Garmin's 429 stops the call (`partial`, `retry_after_s` 900) and blocks live Garmin
  calls for 15 minutes. A write that may have landed (a read timeout, a 5xx) is `unknown` until a
  calendar read settles it, so a retry never makes a second copy. Apart from GraphQL reads (one
  anonymous query: no `mutation` or `subscription` keyword outside its strings, no `operationName`),
  only the workout-service create, schedule, unschedule and
  delete paths may be sent with a method other than GET, and only with the switch on; the history
  worker never gets the switch.
- **Off again.** Turning the switch off unregisters the write tools: tokens that already carry
  `plan:write` keep the scope but have nothing to call. The plan's workouts stay on the calendar until
  `python ext/launch.py --plan-cleanup` deletes the store's own future workouts of the Garmin account
  logged in (rows of another account's plans are kept). Library workouts with this install's marker
  that no row owns are deleted only with `--orphans`, and only those on the calendar today or later,
  never on an earlier day and never linked to an activity. `HISTORY_FORGET=yes` is refused while such
  workouts remain, since it drops the records that find them.

## Known advisories

- **PYSEC-2026-3467** (CVE-2026-54447): garminconnect 0.3.4 and older save the Garmin login
  tokens in a file that other users of the same machine can read; 0.3.5 fixes it. This server
  pins garminconnect 0.3.2, the exact version upstream garmin_mcp requires. Here the risk is
  contained: the server runs alone in its container as its own user, `garmin-mcp-auth` makes
  the token folder and its files owner-only after a login, and the server makes the folder
  owner-only (0700) on every start. The pin is reviewed again by 15 February 2027, or sooner when upstream moves.
