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
| Caddy | container, PID 1, public `$PORT` | none | Proxies every request to garmin-mcp, passing tokens through; answers `410` for the removed token-in-URL form; caps sign-in request bodies; access log without query strings or credentials |
| garmin-mcp (`ext/launch.py`) | container, `127.0.0.1:8000` | `OWNER_SECRET`, the optional `MCP_BEARER_TOKEN`; sign-ins in `/data/auth.sqlite`; Garmin tokens on `/data/tokens`; in `key` mode `GARMIN_PERSONAL_API_KEY` and its access token on `/data/garmin-key` | The sign-in (OAuth 2.1 for one owner) and the token check on `/mcp`; upstream's tools plus this repo's |
| Cloud routine, optional (`recipes/claude-ai-morning-report/`) | claude.ai's cloud, on a schedule | your claude.ai connectors | Stores the report in the report page's database |

The split keeps each secret out of the programs that don't use it, and so out of their
logs and tool output. It is not a sandbox: everything in the container runs as root.
With Docker, the separate cloudflared container runs as a non-root user. In `key` mode,
the key goes to the server's Python only; Caddy gets no secret.

## Security

The owner secret gives full access to this server: whoever has it can approve a sign-in
and read all your Garmin data through the read-only tools it exposes (see [Tools](tools.md)). So
can the optional static token, without any sign-in page. Keep both in a password manager,
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
has it can run your tunnel ([3. Create the tunnel](deploy-docker.md#3-create-the-tunnel)
says what to do if it leaked).

Caddy still passes a caller's own `X-Forwarded-Proto` and `X-Forwarded-Host` through, so
nothing in the server may build a URL or make a security decision from the request's
scheme, `Host` or `X-Forwarded-*` headers (a future Host or Origin check included). The
server's own URL comes only from `PUBLIC_URL` or the Railway domain.

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
- **Nothing else.** The server calls only Garmin. It sends no analytics or telemetry: the MCP
  SDK's OpenTelemetry hooks have no exporter in the image, so they do nothing.

On the server, `/data` holds the Garmin login tokens (`full` mode) and the sign-in database. In
`key` mode the key itself is in your server's settings (`GARMIN_PERSONAL_API_KEY`), and a
24-hour access token made from it is cached in `/data/garmin-key/token.json`, with owner-only
file modes (folder 0700, file 0600). Anyone who can
read the volume can use that token until it expires. Delete the volume and all of these are gone,
except the key, which you delete in Garmin Connect:
[Uninstall and revoke](../README.md#uninstall-and-revoke).
