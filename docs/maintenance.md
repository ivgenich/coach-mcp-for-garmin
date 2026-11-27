# Maintenance

## Garmin login expired (full mode)

The Garmin login tokens last about 6 months. When they expire, tool calls answer `Error: Garmin login missing or
expired. ...`, while `/readyz` can still say 200. Run `railway ssh`, then
`garmin-mcp-auth --token-path /data/tokens --force-reauth`, then
`kill "$(cat /tmp/garmin-mcp.pid)"` (the server restarts with the new tokens within a few
seconds, and first makes the files the login wrote as root its own), then `exit`.

With Docker: in the project folder, run
`docker compose exec app garmin-mcp-auth --token-path /data/tokens --force-reauth`, then
`docker compose restart app`. The server loads the new tokens in a few seconds. It does not
notice them by itself, because old tokens were already there when it started.

## Renew the Garmin key (key mode)

Garmin emails you about a week before your key expires. In its last day, the server log also
warns: `garmin key mode: the Personal API key expires at <time>; make a new key in Garmin
Connect before then`. Once the key has expired, or you deleted it in Garmin Connect, every tool
answers `Error: Garmin login missing or expired. This server cannot read Garmin data until its
owner makes a new Personal API key in Garmin Connect and sets it on the server.`

Create a new key ([Key mode: create a key](garmin-login.md#key-mode-create-a-key)) and put it in
place of the old one, without typing it on a command line:

- Railway, with deploy.sh: `./scripts/deploy.sh --set-garmin-key` in your clone. It asks for the
  key at a prompt that does not show it, and deploys.
- Railway, with the CLI only: the `read -rs` line in [Settings](deploy-railway.md#settings), with
  `NAME` = `GARMIN_PERSONAL_API_KEY`.
- Railway, in the dashboard: the service's **Variables**, `GARMIN_PERSONAL_API_KEY`, then deploy
  the change.
- Docker: the key command in [Settings](deploy-docker.md#settings), then
  `docker compose up -d --force-recreate app`.

The server makes a new access token from the new key by itself. Then delete the old key in
Garmin Connect.

## Rotate the owner secret

Do it right away if the secret may have leaked. It also signs everyone out:
`./scripts/deploy.sh --rotate-secret`. Every sign-in is revoked when the
new deployment starts, so every client signs in again with the new secret: in claude.ai,
**Reconnect** the Garmin connector (Customize → Connectors); in Claude Code, `/mcp`. The
static `MCP_BEARER_TOKEN` is not affected: rotate it too (`--rotate-token`) or delete it
(`railway variable delete MCP_BEARER_TOKEN`).

Without deploy.sh:

- Railway, with the CLI (this deploys the change):

  ```
  S="$(LC_ALL=C tr -dc 23456789abcdefghjkmnpqrstuvwxyz </dev/urandom | head -c 30 | sed 's/...../&-/g; s/-$//')"; printf '%s' "$S" | railway variable set OWNER_SECRET --stdin; unset S
  ```

  Then read it with `railway variable list --kv | grep ^OWNER_SECRET=`.
- Railway, in the dashboard: the service's **Variables**, `OWNER_SECRET`, edit, paste 32 or more
  random characters from your password manager's generator, then deploy the change.
- Docker: run the owner-secret command from [Settings](deploy-docker.md#settings) again, then
  `docker compose up -d --force-recreate app`.

A secret you choose yourself must be 32 to 256 characters with at least 12 different ones; a
password manager's generator makes one.

## Rotate the static token

For the optional `MCP_BEARER_TOKEN`. Do it right away if the token may have leaked:
`./scripts/deploy.sh --rotate-token` prints the new one once. Then update the
header wherever you use it (Claude Code: `claude mcp remove garmin`, then add it again).
To turn it off: `railway variable delete MCP_BEARER_TOKEN`.

Without deploy.sh:

- Railway, with the CLI:
  `T="$(openssl rand -hex 32)"; printf '%s' "$T" | railway variable set MCP_BEARER_TOKEN --stdin; unset T`.
  Read it with `railway variable list --kv | grep ^MCP_BEARER_TOKEN=`.
- Docker:

  ```
  (umask 077; T="$(openssl rand -hex 32)"; grep -v '^MCP_BEARER_TOKEN=' .env > .env.tmp; printf 'MCP_BEARER_TOKEN=%s\n' "$T" >> .env.tmp && mv .env.tmp .env)
  ```

  Then run `docker compose up -d --force-recreate app`, and read it with
  `grep '^MCP_BEARER_TOKEN=' .env`.

## Moving from the token-in-URL version

Copies of this server from before 1.0.0 took a token in the URL. The `https://<your-app>.up.railway.app/<token>/mcp`
form now answers `410 Gone`, and claude.ai no longer uses the token. Run
`./scripts/deploy.sh` once (it generates the owner secret), then remove the old Garmin
connector in claude.ai and add it again as in [Connect Claude](connect-claude.md). If you ever used the URL form, the
token may be in Railway's HTTP logs: run `./scripts/deploy.sh --rotate-token`, or delete
it: `railway variable delete MCP_BEARER_TOKEN`.

`GARMIN_AUTH` is required (`full` or `key`, no default). Copies from before 1.0.0: set
`GARMIN_AUTH=full` before you update, or the server does not start.

## Upstream update (for maintainers)

The Dockerfile pins `GARMIN_MCP_REF` to a commit, so a plain
redeploy rebuilds the same version. To take a fix, set it to a newer commit from
[the repo](https://github.com/Taxuspt/garmin_mcp/commits/main), regenerate
`constraints.txt` from that commit's `uv.lock`, regenerate `config/requirements-core.txt` and `config/requirements-build.txt`
(the same versions with their hashes; its header has the commands), and redeploy. The build fails,
instead of the server, if the update breaks a hook or a helper `ext/` relies on.
Keep the `mcp==2.3.0` pin and its 2.x dependencies when you regenerate: upstream's
`uv.lock` still pins mcp 1.x (see [MCP protocol](protocol.md)). Once upstream itself
moves to mcp 2.x (its issue #344), `ext/mcp2_compat.py` can go.

The two base images (`caddy` and `python` in the Dockerfile) are pinned by tag and by digest.
Dependabot proposes new ones; to bump by hand, change the tag and put in the digest that
`docker buildx imagetools inspect <image>:<tag>` prints. Keep the Caddy version equal to
`scripts/ci-tools.sh` and the Python version equal to `FULL_PYTHON` in
`.github/workflows/ci.yml`.

## Release (for maintainers)

Set `VERSION` in `ext/version.py` to the new version (the release check fails if it
differs), then run `scripts/release-check.py` before tagging (its header lists the checks). Commit and tag
with `TZ=UTC` and your GitHub noreply address: the check refuses local time zone offsets on
commits made after the previous release tag (before the first tag, after the baseline commit
named in the script; `--since COMMIT` overrides it), and on the tag itself. Add a
`## [X.Y.Z] - <UTC date>` section under `## [Unreleased]` in `CHANGELOG.md`, keep the
`## [Unreleased]` line, then create an annotated tag `vX.Y.Z` on that commit. Push the
tag and publish a GitHub Release from it with that version's `CHANGELOG.md` section as its
notes.
