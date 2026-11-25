# Deploy with Docker

Run the server with Docker Compose on a computer of your own that stays on, and reach it from
Claude through a Cloudflare Tunnel. Claude needs a public HTTPS address that never changes; the
tunnel gives your server one without opening a port on your router.

This is the advanced path, and partly untested: the Cloudflare Tunnel steps follow Cloudflare's
documentation and were not yet tested end to end. History search is not available here yet: it
needs Railway ([History search](history-search.md)).

## What you need

- A computer that stays on, with Docker and Docker Compose. The image runs on 64-bit x86 (amd64)
  and 64-bit ARM (arm64), such as Apple silicon or a Raspberry Pi 4 or 5 with a 64-bit system.
  32-bit ARM is not supported. Plan for 1 GB of free memory, and turn off sleep: while the
  computer sleeps, Claude cannot reach the server.
- A Cloudflare account and a domain on Cloudflare: Cloudflare requires one to publish an
  application through a tunnel ([Cloudflare](https://developers.cloudflare.com/tunnel/get-started/),
  checked November 2026). If your domain is not on Cloudflare yet, add it first: you change its
  nameservers at your registrar, which Cloudflare says can take up to 24 hours
  ([Cloudflare](https://developers.cloudflare.com/dns/zone-setups/full-setup/setup/)).
  Cloudflare's quick tunnels don't fit: their `trycloudflare.com` address is temporary, and
  Claude's sign-in needs one that stays the same.
- Your Garmin mode (`full` or `key`: [Garmin modes and login](garmin-login.md)) and your time
  zone as an IANA name, such as `Europe/London`. In `key` mode, your Garmin Personal API key:
  [create it first](garmin-login.md#key-mode-create-a-key).
- git and a bash or zsh shell. The commands below are for macOS and Linux; on Windows, use WSL
  (this guide was not tested there).

## What Cloudflare sees

Read this before you create the tunnel. Cloudflare ends HTTPS at its edge and passes the
traffic through the tunnel, so it can read the requests and answers, your Garmin data and the
sign-in pages included.

Claude calls your server from Anthropic's servers, not from a browser, and some Cloudflare
features block such calls ([Claude connectors: troubleshooting](https://claude.com/docs/connectors/building/troubleshooting.md)).
For this hostname:
- Don't put a Cloudflare Access application on it. Claude cannot sign in through Access; your
  server has its own sign-in.
- Turn off **Bot Fight Mode** in the domain's security settings (bot traffic). It can challenge
  API calls, and no rule can skip it ([Cloudflare](https://developers.cloudflare.com/bots/get-started/bot-fight-mode/)).
  It applies to the whole domain.
- Don't set the AI agent policy to **Block** ([Cloudflare](https://developers.cloudflare.com/bots/additional-configurations/block-ai-bots/)).
- Never turn on **Under Attack** mode. Add no WAF, rate-limiting or cache rule for this hostname.
  If other rules on the domain could catch it, allow Claude's addresses, `160.79.104.0/21` and
  `153.61.208.0/20` ([Anthropic](https://platform.claude.com/docs/en/api/ip-addresses)).
- Use a first-level subdomain, such as `coach.example.com`. Deeper ones need an Advanced
  Certificate.
- Never run cloudflared with `--loglevel debug`: it logs every request header, sign-in tokens
  included.

## 1. Get the files

```
git clone https://github.com/ivgenich/coach-mcp-for-garmin.git
cd coach-mcp-for-garmin
git checkout v1.0.5
```

Use the newest release tag from the repository's **Releases** page in place of `v1.0.5`.

Clone it into a folder that no cloud drive syncs (not the iCloud Desktop or Documents folders,
Dropbox or OneDrive): the two settings files you create next hold your secrets in plain text.

## 2. Settings

Create the settings file. It holds secrets, so only you may read it:

```
cp .env.example .env
chmod 600 .env
```

Open `.env` in a text editor and set:
- `GARMIN_AUTH=full` or `GARMIN_AUTH=key`.
- `REPORT_TZ=` your time zone, such as `Europe/London`.
- `PUBLIC_URL=https://<your-host>`: remove the `#` in front of it. It is required here, since
  there is no Railway domain. Choose `<your-host>` now: a first-level subdomain of your domain
  on Cloudflare, such as `coach.example.com`. You give the tunnel this same hostname in
  [3. Create the tunnel](#3-create-the-tunnel).

Generate your owner secret. This command writes it into `.env` and prints nothing:

```
(umask 077; S="$(LC_ALL=C tr -dc 23456789abcdefghjkmnpqrstuvwxyz </dev/urandom | head -c 30 | sed 's/...../&-/g; s/-$//')"; grep -v '^OWNER_SECRET=' .env > .env.tmp; printf 'OWNER_SECRET=%s\n' "$S" >> .env.tmp && mv .env.tmp .env)
```

Read it with `grep '^OWNER_SECRET=' .env` and save it in your password manager.

`key` mode only: add your Garmin Personal API key without typing it on a command line. Run this
in bash or zsh, paste the key at the silent prompt, and press Enter:

```
(umask 077; IFS= read -rs K; grep -v '^GARMIN_PERSONAL_API_KEY=' .env > .env.tmp; printf "GARMIN_PERSONAL_API_KEY='%s'\n" "$K" >> .env.tmp && mv .env.tmp .env)
```

If you edit `.env` by hand, put any value that contains a `$` in single quotes
(`NAME='a$b'`): Docker Compose replaces `$NAME` in values with another variable.

## 3. Create the tunnel

In the Cloudflare dashboard: **Networking → Tunnels → Create Tunnel**. Then open the tunnel and,
under **Routes**, choose **Add route → Published application**: the hostname you chose for
`<your-host>` in [2. Settings](#2-settings) and the service URL `http://app:8080` (`app` is the
server's name inside Docker Compose).

Then copy the tunnel's token: the install command Cloudflare shows for the tunnel ends with
`--token` and the token. Copy only the token. In the project folder, run this, paste the token at
the silent prompt, and press Enter:

```
(umask 077; IFS= read -rs T; printf 'TUNNEL_TOKEN=%s\n' "$T" > tunnel.env)
```

Anyone who has this token can run your tunnel. If it may have leaked, follow Cloudflare's steps
for a compromised token ([Tunnel permissions](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/configure-tunnels/remote-tunnel-permissions/)):
open the tunnel, click **Refresh token** on its **Overview** tab, then drop all of its connections
with Cloudflare's API, because a refresh leaves connectors that already run connected. Put the new
token in `tunnel.env` with the command above, then run `docker compose up -d --force-recreate tunnel`.

## 4. Start

```
docker compose up -d --build
docker compose ps
```

The first build takes a few minutes. After about 30 seconds, `app` shows as healthy, and then the
tunnel starts. `docker compose logs app` should show `launch: config OK`. If the start stops
because `app` is unhealthy, `docker compose logs app` names the setting to fix. Fix it in `.env`
and run `docker compose up -d` again.

## 5. Connect Garmin and Claude

`full` mode: log in to Garmin once, in the project folder:

```
docker compose exec app garmin-mcp-auth --token-path /data/tokens
```

([More](garmin-login.md#with-docker).) In `key` mode there is nothing to do: `.env` has your key.
Then add the server to Claude with the connector URL `https://<your-host>/mcp`, exactly, with no
trailing slash: [Connect Claude](connect-claude.md).

## 6. Check

In the project folder:

```
./scripts/test-connector.sh https://<your-host>
```

It should end with `RESULT: PASS`. Before the `full`-mode Garmin login, `/readyz` answers 503 and
that check fails; `./scripts/test-connector.sh --expect-unready https://<your-host>` expects
exactly that.

## Logs, updates and removal

```
docker compose logs -f app       # the server; Ctrl+C stops following
docker compose logs tunnel       # the tunnel
```

Update to a new release (your settings and the volume stay):

```
git fetch --tags
git checkout <new tag>
docker compose up -d --build
```

To go back, do the same with the previous tag. A release of this project also updates the pinned
cloudflared image.

Remove everything: `docker compose down --volumes --rmi all` deletes the containers, the images
and the volume, with the Garmin login or key cache and every sign-in. Then run `rm .env tunnel.env`,
and delete the tunnel and its DNS record in Cloudflare ([Uninstall and revoke](../README.md#uninstall-and-revoke)).
