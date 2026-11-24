# Updates and versions

## Versions

Releases follow [Semantic Versioning 2.0.0](https://semver.org/spec/v2.0.0.html) and are listed,
newest first, in [CHANGELOG.md](../CHANGELOG.md). For a version MAJOR.MINOR.PATCH:

- **PATCH** (1.0.1): fixes only. Nothing in the public API changes.
- **MINOR** (1.1.0): new things that keep the public API working, such as a new tool, a new
  optional argument, a new field in a status, or a change in the daily or weekly rules.
- **MAJOR** (2.0.0): a change that can break what you built on the public API. The CHANGELOG then
  says what to change.

The server reports its version to clients as `serverInfo.version`. Each release has a
`vX.Y.Z` tag and an entry on the repository's **Releases** page.

## The public API

You may rely on these; only a MAJOR release breaks them:

1. **Tool names and arguments**: the names, types and defaults of each tool's arguments.
2. **The default tool list of each mode**: removing a tool from it is MAJOR; adding one is MINOR.
3. **The JSON of `get_daily_status` and `get_weekly_status`**: every field name, the values of
   `level` and `recommendation`, the flag names, and what each means.
4. **`RULES_VERSION`**: each status carries it as `rules_version`. Any change to a rule or a
   threshold raises it, and the CHANGELOG lists the old and the new values. Removing or renaming
   a flag, a level or a recommendation is MAJOR.
5. **Settings**: the names, accepted values and meaning of the variables in `.env.example`,
   `GARMIN_AUTH` included.
6. **The connector URL** (`https://<your-host>/mcp`) and the sign-in Claude uses.
7. **What `/data` keeps**: the `full`-mode Garmin login, `key` mode's cached 24-hour access token
   and your Claude sign-ins; an update keeps them. The key itself is in your server's settings,
   which an update keeps too.
8. **The first words of the tool error texts**, the same in both modes:
   `Error: Garmin login missing or expired.`, `Error: Garmin rate limit.`,
   `Error: Garmin Connect did not answer`, `Error: Invalid input:`,
   `Error: Internal server error (ref` and `No data:`.

Not part of the public API, so any release may change them: the wording of the markdown report
text (`text_format="markdown"`), tool descriptions and server instructions; log lines; the
`sources` and debug parts of a status (`include_sources`); the answers of upstream's tools, which
are Garmin's own data as JSON and change when Garmin changes it; Python names inside `ext/`; the
output of the scripts.

If a new upstream version removes or renames one of its tools that this server exposes, that is a
MAJOR release here, or the upstream pin stays where it is.

## How to update

Your Garmin login or key and your Claude sign-ins stay, so you don't reconnect Claude.

- Railway with deploy.sh: in your clone, `git fetch --tags`, `git checkout vX.Y.Z` (the release
  you want), then `./scripts/deploy.sh`.
- Docker: in the project folder, `git fetch --tags`, `git checkout vX.Y.Z` (the release you
  want), then `docker compose up -d --build`. Your settings and the volume stay.

Updating a copy from before 1.0.0: set `GARMIN_AUTH=full` first
([Moving from the token-in-URL version](maintenance.md#moving-from-the-token-in-url-version)).

If the CHANGELOG says a release adds tools or changes tool descriptions, open the connector in
claude.ai and choose **Refresh tools list** ([Tools](tools.md)).

## Roll back

Roll back if a new version misbehaves: redeploy the previous deployment or image. On
Railway: dashboard → the service → **Deployments** → the last deployment from before the
change → **Redeploy**. The `/data` volume (Garmin login) is untouched.

With Docker: `git checkout` the previous release tag, then `docker compose up -d --build`. If a
release changes the sign-in database, its CHANGELOG entry says whether rolling back is safe.

## Support

A personal project, maintained on a best-effort basis, with no SLA. Security fixes go into the
newest release only: see [SECURITY.md](../SECURITY.md).
