# Security policy

## Reporting a vulnerability

Please report a security problem privately, not in a public issue.

1. On GitHub, open this repository's **Security** tab, go to **Advisories** and click
   **Report a vulnerability**. GitHub's private vulnerability reporting shows the report only to
   the maintainers.
2. Say what you found, how to reproduce it, and what an attacker could do with it.
3. Never include real tokens, owner secrets, Garmin passwords, API keys or health data. Use
   made-up values.

If you see no **Report a vulnerability** button, open an issue that says only that you have a
security report to send, with no details, and the maintainer will reply with a private way to
send it.

This is a personal project, maintained on a best-effort basis. Expect a first answer within 14
days. A fix goes into the newest release, and the CHANGELOG describes it once the fix is out.

## Supported versions

| Version | Security fixes |
|---|---|
| The newest 1.x release | Yes |
| Older 1.x releases | No: update to the newest 1.x release |
| Copies from before 1.0.0 | No |

## Scope

In scope, this repository's code and configuration:

- The owner's sign-in (OAuth 2.1): the owner-secret page, client registration, codes, access and
  refresh tokens, revocation, and the optional static bearer token.
- The read-only tool policy: any way to make the server expose or run a tool that changes Garmin
  data.
- Leaks of secrets, tokens, Garmin credentials or keys, or health data through tool answers,
  errors, logs, files or telemetry. With tracing on, Grafana and Langfuse receive metadata only
  (tool names, timings, error groups, token counts and cost), never health values or text
  ([Tracing](docs/tracing.md)).
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
  filter and the read-only policy hold. [How it works](docs/how-it-works.md) explains the limits.
- Attacks that start with the owner secret, the static token, the Garmin key, the tunnel token,
  or access to the host account or to the server's `/data` volume already in hand.
- Denial of service by traffic volume, and reports from automated scanners with no demonstrated
  impact.

## Known advisories

- **PYSEC-2026-3467** (CVE-2026-54447): garminconnect 0.3.4 and older save the Garmin login
  tokens in a file that other users of the same machine can read; 0.3.5 fixes it. This server
  pins garminconnect 0.3.2, the exact version upstream garmin_mcp requires. Here the risk is
  contained: the server runs alone in its container, `garmin-mcp-auth` makes the token folder
  and its files owner-only after a login, and the server makes the folder owner-only (0700) on
  every start. The pin is reviewed again by 15 February 2027, or sooner when upstream moves.
