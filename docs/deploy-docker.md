# Deploy with Docker

Run the server with Docker Compose on a computer of your own that stays on, and reach it from
Claude through a Cloudflare Tunnel. Claude needs a public HTTPS address that never changes; the
tunnel gives your server one without opening a port on your router.

This is the advanced path. CI builds `compose.yaml` from source on amd64 and arm64 and tests the
server behind a stand-in for the tunnel, sign-in included ([How this path is tested](#how-this-path-is-tested)).
The real Cloudflare Tunnel is still untested: those steps follow Cloudflare's documentation and were
not yet tested end to end. History search is not available here yet: it needs Railway
([History search](history-search.md)).

`scripts/setup.sh` does the setup for you: it asks a few questions, writes the two settings files,
builds and starts the server, and prints the connector URL. If you'd rather type each step
yourself, follow [Manual setup](#manual-setup) instead.

## Requirements

Each server builds the image itself from a release tag of this repository: there is no ready-made
image to download.

- A computer that stays on, with Docker and Docker Compose 2.20 or newer. The image runs on 64-bit
  x86 (amd64) and 64-bit ARM (arm64), such as Apple silicon or a Raspberry Pi 4 or 5 with a 64-bit
  system. 32-bit ARM is not supported. Plan for 1 GB of free memory, and turn off sleep: while
  the computer sleeps, Claude cannot reach the server.
- A Cloudflare account and a domain on Cloudflare: Cloudflare requires one to publish an
  application through a tunnel ([Cloudflare](https://developers.cloudflare.com/tunnel/get-started/),
  checked November 2026). If your domain is not on Cloudflare yet, add it first: you change its
  nameservers at your registrar, which Cloudflare says can take up to 24 hours
  ([Cloudflare](https://developers.cloudflare.com/dns/zone-setups/full-setup/setup/)).
  Cloudflare's quick tunnels don't fit: their `trycloudflare.com` address is temporary, and
  Claude's sign-in needs one that stays the same.
- Your Garmin mode (`key` or `full`: [Garmin modes and login](garmin-login.md)) and your time
  zone as an IANA name, such as `Europe/London` (setup.sh suggests your computer's own). In
  `key` mode, your Garmin Personal API key: [create it first](garmin-login.md#key-mode-create-a-key).
- git and a bash or zsh shell. The commands below are for macOS and Linux; on Windows, use WSL
  (this guide was not tested there).

## Who can see your traffic

Cloudflare ends HTTPS at its edge and passes the traffic through the tunnel to your computer, so
Cloudflare can read the requests and answers, your Garmin data and the sign-in pages included.
Your computer keeps the settings files and the `coach-data` volume (the Garmin login or key cache,
and the sign-ins). If you don't want Cloudflare in the path, use Railway instead
([Deploy on Railway](deploy-railway.md)).

## Cloudflare settings that block Claude

Read this before you create the tunnel. Claude calls your server from Anthropic's servers, not
from a browser, and some Cloudflare features block such calls
([Claude connectors: troubleshooting](https://claude.com/docs/connectors/building/troubleshooting.md)).
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
git checkout v1.1.1
```

Use the newest release tag from the repository's **Releases** page in place of `v1.1.1`.

Clone it into a folder that no cloud drive syncs (not the iCloud Desktop or Documents folders,
Dropbox or OneDrive): the two settings files hold your secrets in plain text.

## 2. Create the tunnel

Choose the server's host name now: a first-level subdomain of your domain on Cloudflare, such as
`coach.example.com`.

In the Cloudflare dashboard: **Networking → Tunnels → Create Tunnel**. Then open the tunnel and,
under **Routes**, choose **Add route → Published application**: your host name and the service
URL `http://app:8080` (`app` is the server's name inside Docker Compose).

Then find the tunnel's token: the install command Cloudflare shows for the tunnel ends with
`--token` and the token. You paste only the token in the next step.

Anyone who has this token can run your tunnel. If it may have leaked, follow Cloudflare's steps
for a compromised token ([Tunnel permissions](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/configure-tunnels/remote-tunnel-permissions/)):
open the tunnel, click **Refresh token** on its **Overview** tab, then drop all of its connections
with Cloudflare's API, because a refresh leaves connectors that already run connected. Then put
the new token in place: `rm tunnel.env`, run `./scripts/setup.sh` again (it keeps `.env` and asks
only for the token), or use the command in [Tunnel token by hand](#tunnel-token-by-hand).

## 3. Run setup.sh

In the project folder:

```
./scripts/setup.sh
```

It checks Docker and Compose, then asks:
- the host name (it sets `PUBLIC_URL=https://<your-host>`; it refuses `http://`, ports, paths and
  IP addresses),
- the Garmin mode (`key` is the default and the simpler one),
- your time zone (Enter takes your computer's),
- in `key` mode, the Garmin Personal API key, and the tunnel token. These two are read at a
  prompt that does not show what you paste.

It writes `.env` and `tunnel.env` (only you may read them: mode 600), generates your owner secret,
runs `docker compose up -d --build` (the first build takes a few minutes), waits until the server
is healthy and checks the tunnel. In `full` mode it offers to run the Garmin login
([More](garmin-login.md#with-docker)). At the end it prints:
- the connector URL, `https://<your-host>/mcp`, and a link that opens claude.ai's **Add custom
  connector** form with it filled in;
- your **owner secret**, once. Save it in your password manager right away. Later you can read it
  with `grep '^OWNER_SECRET=' .env`.

Run it again at any time: it keeps `.env`, `tunnel.env` and the owner secret, and only asks
before it changes them. Without a terminal (a provisioning script, CI), set
`SETUP_NONINTERACTIVE=1` and the answers as environment variables: the list is at the top of
`scripts/setup.sh`.

## 4. Connect Claude

Add the server to Claude with the connector URL `https://<your-host>/mcp`, exactly, with no
trailing slash, or open the link setup.sh printed: [Connect Claude](connect-claude.md).

## Check

```
./scripts/setup.sh --check
```

It changes nothing. It lists the settings by name (never their values), the state of the two
containers, what `/readyz` says, and runs `./scripts/test-connector.sh https://<your-host>` when
this computer can reach the server's public URL. Each problem comes with a next step. Before the
`full`-mode Garmin login, `/readyz` answers 503: that is expected until you log in.

## Backup and restore

The `coach-data` volume holds all the server keeps: the Garmin login (`full` mode) or the key's
token cache (`key` mode), and the sign-ins.

```
./scripts/setup.sh --backup            # into backups/ in the project folder
./scripts/setup.sh --backup /some/dir  # or a folder of your choice
```

It stops the server for a few seconds, so the sign-in database is copied whole, writes
`coach-data-<date>-<time>.tar.gz` (mode 600) with the server's own image (it pulls no other
image), and starts the server again. The file holds your Garmin login: keep it as private as
`.env`. It does not hold `.env` or `tunnel.env`: keep copies of them in your password manager.
git and the image build both ignore `backups/`.

Restore a backup, on this computer or on a new one where `.env` and `tunnel.env` are in place:

```
./scripts/setup.sh --restore backups/coach-data-<date>-<time>.tar.gz
```

It asks first, stops the server, replaces everything on the volume with the backup, and starts
the server again.

## Upgrade and roll back

Upgrade to a release by its tag (the **Releases** page and [CHANGELOG.md](../CHANGELOG.md) list
them):

```
./scripts/setup.sh --upgrade v1.2.0
```

It refuses to run if the checkout has local changes. It makes a backup first, then runs
`git fetch --tags`, checks out the tag, runs `docker compose up -d --build` and waits until the
server is healthy. Your settings and the volume stay. At the end it prints the rollback command:
the same command with the tag you came from, such as `./scripts/setup.sh --upgrade v1.1.1`. If
the CHANGELOG says a release changed the sign-in database, roll back with the backup too:
`./scripts/setup.sh --restore <the backup it made>`. A release of this project also updates the
pinned cloudflared image. More: [Updates and versions](updates.md).

Releases from before setup.sh (1.1.1 and older) do not have it: after a roll back to one of them,
use the commands in [Upgrade and roll back by hand](#upgrade-and-roll-back-by-hand).

## Logs and removal

```
docker compose logs -f app       # the server; Ctrl+C stops following
docker compose logs tunnel       # the tunnel
```

Remove everything: `docker compose down --volumes --rmi all` deletes the containers, the images
and the volume, with the Garmin login or key cache and every sign-in. Then run
`rm .env tunnel.env`, delete `backups/` if you made backups, and delete the tunnel and its DNS
record in Cloudflare ([Uninstall and revoke](../README.md#uninstall-and-revoke)).

## Manual setup

The same setup without the script: after [1. Get the files](#1-get-the-files) and
[2. Create the tunnel](#2-create-the-tunnel), in the project folder.

### Settings

Create the settings file. It holds secrets, so only you may read it:

```
cp .env.example .env
chmod 600 .env
```

Open `.env` in a text editor and set:
- `GARMIN_AUTH=full` or `GARMIN_AUTH=key`.
- `REPORT_TZ=` your time zone, such as `Europe/London`.
- `PUBLIC_URL=https://<your-host>`: remove the `#` in front of it. It is required here, since
  there is no Railway domain. `<your-host>` is the host name of the tunnel's route.

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

### Tunnel token by hand

In the project folder, run this, paste the tunnel's token at the silent prompt, and press Enter:

```
(umask 077; IFS= read -rs T; printf 'TUNNEL_TOKEN=%s\n' "$T" > tunnel.env)
```

After a new token, run `docker compose up -d --force-recreate tunnel`.

### Start

```
docker compose up -d --build
docker compose ps
```

The first build takes a few minutes. After about 30 seconds, `app` shows as healthy, and then the
tunnel starts. `docker compose logs app` should show `launch: config OK`. If the start stops
because `app` is unhealthy, `docker compose logs app` names the setting to fix. Fix it in `.env`
and run `docker compose up -d` again.

### Garmin login and Claude

`full` mode: log in to Garmin once, in the project folder:

```
docker compose exec app garmin-mcp-auth --token-path /data/tokens
```

([More](garmin-login.md#with-docker).) In `key` mode there is nothing to do: `.env` has your key.
Then add the server to Claude with the connector URL `https://<your-host>/mcp`, exactly, with no
trailing slash: [Connect Claude](connect-claude.md).

### Check by hand

```
./scripts/test-connector.sh https://<your-host>
```

It should end with `RESULT: PASS`. Before the `full`-mode Garmin login, `/readyz` answers 503 and
that check fails; `./scripts/test-connector.sh --expect-unready https://<your-host>` expects
exactly that.

### Upgrade and roll back by hand

Make a backup first: `./scripts/setup.sh --backup`. By hand: `docker compose stop app`, then
`(umask 077; mkdir -p backups && docker compose run --rm --no-deps -T --entrypoint tar app -czf - -C /data . > backups/coach-data.tar.gz)`,
then `docker compose up -d`. The archive holds your Garmin login and sign-ins: `backups/` is
git-ignored, and `umask 077` keeps the file readable by you only. Then:

```
git fetch --tags
git checkout <new tag>
docker compose up -d --build
```

To go back, do the same with the previous tag.

## How this path is tested

On every change, CI runs `scripts/ci-compose.sh` on amd64 and arm64 (you can run it too; it needs
Docker). It builds this `compose.yaml` from source, with fake secrets, and puts a small test edge in
the tunnel's place: it forwards to `http://app:8080` and sets `CF-Connecting-IP` as Cloudflare does.
Through that edge it checks the health checks, the sign-in end to end in `full` mode (no Garmin
login) and in `key` mode (a fake key, no network), that the server takes the caller's address from
the edge's `CF-Connecting-IP` and not from one the caller sent, and that the sign-ins on the volume
survive a stop, a start and a crash. It never runs the real tunnel or calls Garmin.
