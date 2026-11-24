# Garmin modes and login

The server reaches Garmin in one of two ways. You choose with `GARMIN_AUTH`, which is required
and has no default.

## Choose a mode

| | `full` | `key` |
|---|---|---|
| How the server reaches Garmin | An unofficial, reverse-engineered interface to Garmin Connect (upstream garmin_mcp and the garminconnect library) | Garmin's official [Personal API](https://developer.garmin.com/gc-developer-program/personal/personal-api) |
| How you log in | Once, in a terminal: your Garmin email, password and MFA code. The password is never stored | You create a key on the Garmin Connect website before the deploy, and the deploy puts it in your server's settings |
| Tools | Every tool ([list](tools.md)) | Less: activities, sleep, HRV, daily stats, Body Battery, heart-rate zones and VO2max. No training readiness, training status or load, planned workouts, courses, race predictions or records; the daily status works without readiness and planned workouts ([list](tools.md)) |
| What the server keeps | Login tokens that can do anything you can do in Garmin Connect, including changing and deleting data. They last about 6 months | The key, in your server's settings, and a 24-hour access token made from it, on `/data`. The key lasts the period you choose: 30, 90 or 180 days, or one year |
| Upkeep | Log in again about every 6 months | Make a new key before the old one expires; Garmin emails you about a week before |
| Risks | It may stop working without notice, it may breach Garmin's terms of use, and repeated logins can get your account rate-limited or restricted | You accept Garmin's terms for the Personal API when you create the key: read them there first. Garmin meant it for "individual Garmin users who want programmatic access to their own data" |
| To revoke | Delete the server's volume; if you think the tokens leaked, also change your Garmin password | Delete the key in Garmin Connect: Garmin says the key and its access tokens then stop working |

Sources: Garmin's [Personal API FAQ](https://developer.garmin.com/gc-developer-program/personal/personal-faq/)
and [program overview](https://developer.garmin.com/gc-developer-program/overview/), checked
November 2026.

In `key` mode these tools are off, because Garmin's Personal API has no data for them: planned
workouts (`get_scheduled_workouts`, `get_workout_by_id`); courses (`get_courses`,
`get_course_details`, `get_course_profile`); laps and time in zones (`get_activity_splits`,
`get_activity_hr_in_timezones`, `get_activity_power_in_timezones`); weekly stress
(`get_weekly_stress`); and training readiness, status and load, endurance score, lactate
threshold, cycling FTP, race predictions, personal records and progress summaries
(`get_training_readiness`, `get_training_status`, `get_training_load_trend`,
`get_endurance_score`, `get_lactate_threshold`, `get_cycling_ftp`, `get_race_predictions`,
`get_personal_record`, `get_progress_summary_between_dates`). Sleep comes without the SpO2
summary, restless moments, sleep resting heart rate and overnight HRV. The daily and weekly
status still work, with fewer values, and each says so in a note:
[Daily status in key mode](daily-status.md#in-key-mode),
[Weekly status in key mode](weekly-status.md#in-key-mode).

## Full mode: the one-time login

### On Railway

You need the Railway CLI and an SSH key registered with Railway
([Before you start](deploy-railway.md#before-you-start)). The first `railway ssh` asks to register
a local key; `railway ssh keys add` does the same, and `railway ssh keys github` uses the keys of
your GitHub account.

Your Garmin email and password exist only inside this SSH session; they are never stored.

On a first deploy in `full` mode, `./scripts/deploy.sh` offers to run this login for you once the
server is up (answer Enter or `y`). To run it yourself, in your clone:

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
./scripts/test-connector.sh https://<your-app>.up.railway.app   # expect RESULT: PASS
```

Use the service URL your deploy printed. The script checks the probes, the first steps of
Claude's sign-in (a `401` that points to the server's metadata, and that metadata) and the
`410` for the old token-in-URL form. It checks the optional static token only when
`MCP_BEARER_TOKEN` is set in your shell; otherwise it prints `SKIP`. To run a whole
sign-in against the server (any python3 3.10 or newer; it asks for the owner secret, and
revokes its own sign-in at the end):

```bash
python3 scripts/test-signin.py --live --base https://<your-app>.up.railway.app
```

If `/readyz` still fails, run `railway logs` and look for `Garmin tokens found` followed by
`Garmin Connect client initialized successfully`. Do **not** re-run `garmin-mcp-auth`
unless it printed an error, because repeated logins can get your Garmin account rate-limited.

### With Docker

In the project folder, with the server running:

```
docker compose exec app garmin-mcp-auth --token-path /data/tokens
```

Type your Garmin email, password and MFA code at its prompts. The tokens go to the `coach-data`
volume; the password is not stored. The server picks them up by itself. Wait about 30 seconds, then
check that `https://<your-host>/readyz` answers ready.

## Key mode: create a key

1. Sign in to the Garmin Connect website and open the
   [Personal API Key page](https://connect.garmin.com/app/settings/personalApiToken).
2. Read and accept Garmin's terms and conditions for the Personal API.
3. Choose an expiration period: 30, 90 or 180 days, or one year.
4. Click **Create Key**. Garmin shows the key only once: copy it into your password manager. It is
   a secret like a password: never paste it into a chat, an issue or a command line.
5. Keep it for the deploy, which asks for it: [Railway](deploy-railway.md#deploy-with-deploysh) or
   [Docker](deploy-docker.md#2-settings). To change it later:
   [Renew the Garmin key](maintenance.md#renew-the-garmin-key-key-mode).

Garmin emails you about a week before the key expires. Then create a new key and replace the old
one: [Renew the Garmin key](maintenance.md#renew-the-garmin-key-key-mode).

## Switch modes

Change `GARMIN_AUTH` and deploy again:

- From `full` to `key`: create a key first. Railway: `GARMIN_AUTH=key ./scripts/deploy.sh` asks
  for it. Docker: set `GARMIN_AUTH=key` in `.env`, add the key with the command in
  [Settings](deploy-docker.md#2-settings), then run `docker compose up -d --force-recreate app`.
  The Garmin login tokens stay on `/data` until you delete them: `railway ssh -- rm -rf /data/tokens`
  on Railway, `docker compose exec app rm -rf /data/tokens` with Docker.
- From `key` to `full`: remove the key setting first, since the server does not start in `full`
  mode while `GARMIN_PERSONAL_API_KEY` is set. Railway: `railway variable delete GARMIN_PERSONAL_API_KEY`,
  then `GARMIN_AUTH=full ./scripts/deploy.sh`. Docker: delete the `GARMIN_PERSONAL_API_KEY` line
  in `.env`, set `GARMIN_AUTH=full`, then `docker compose up -d --force-recreate app`. Then do the
  [one-time login](#full-mode-the-one-time-login), and delete the key in Garmin Connect.
  If old login tokens from an earlier `full` mode are still on `/data`, the server does not notice
  the new login by itself. Delete them before you switch (the commands above), or log in with
  `--force-reauth` and then restart the server: on Railway, `railway ssh`, then
  `kill "$(cat /tmp/garmin-mcp.pid)"`, then `exit`; with Docker, `docker compose restart app`.

After a switch the server lists other tools: in claude.ai, open the connector and choose
**Refresh tools list** ([Tools](tools.md)).
