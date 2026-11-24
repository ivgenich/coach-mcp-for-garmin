#!/bin/sh
# Container entrypoint (POSIX sh; /bin/sh is dash in the image).
#   1. Refuses to start with a PORT that is no port number or is 8000 (the
#      internal garmin-mcp port), with Garmin credentials in the environment, or when
#      launch.py --check-config rejects the tool settings, GARMIN_AUTH (full
#      or key) and, for key, GARMIN_PERSONAL_API_KEY, REPORT_TZ,
#      EDGE_CLIENT_IP_HEADER or the sign-in settings (OWNER_SECRET,
#      PUBLIC_URL, the optional MCP_BEARER_TOKEN, AUTH_DB,
#      ALLOWED_REDIRECT_URIS).
#      The check also covers the optional features (TRACING, HISTORY_SEARCH,
#      ROLE and their settings; ext/feature_config.py).
#   2. ROLE=worker or ROLE=enqueue (history search's job worker and its daily
#      cron run, docs/history-worker.md): replaces itself with worker.py or
#      enqueue.py; no Caddy, no MCP server, no volume needed.
#   3. Otherwise (ROLE=app, the default) runs garmin-mcp on 127.0.0.1:8000 in
#      the background and restarts it if it exits (waiting longer after each
#      quick exit, and stopping the container after 8 in a row so the platform
#      restarts it; an exit with status 0 or 143, as after a SIGTERM, is a
#      requested restart and never counts), and (full mode) once more when
#      first-time Garmin tokens appear.
#   4. Replaces itself with Caddy (PID 1), which owns the public $PORT.
# The script runs as root only to give the user app what it keeps on /data
# (own_data); Python runs as app and Caddy as caddy (run_as, setpriv).
# Python does the sign-in, so it gets OWNER_SECRET, the optional
# MCP_BEARER_TOKEN and, in key mode, GARMIN_PERSONAL_API_KEY, and the optional
# features' secrets (OTEL_EXPORTER_OTLP_HEADERS, DATABASE_URL,
# HISTORY_TOKEN_KEY); Caddy is only the edge and gets no secret. Secret values
# are never printed.
set -eu

log() { echo "entrypoint: $*" >&2; }
die() {
	log "FATAL: $*"
	exit 1
}

# run_as USER COMMAND...: replaces this shell (or the subshell it runs in)
# with COMMAND, run as USER with no supplementary root groups, no capabilities
# and no way to gain privileges again. HOME is the user's own.
run_as() {
	run_as_user=$1
	shift
	exec env HOME="/home/$run_as_user" setpriv --reuid="$run_as_user" --regid="$run_as_user" --init-groups \
		--no-new-privs -- "$@"
}

# own_data: give the user app everything on /data that is not app's yet: the
# volume's root, the sign-in database, the Garmin tokens, key mode's token
# cache and the embedding model. A volume made before the server ran as app
# holds root's files, and `garmin-mcp-auth` over `railway ssh` or `docker
# compose exec` runs as root and writes root's files; every start of the
# server runs this first. ext/own_data.py (as root, standard library only)
# first kills every process still running as app and waits until none is
# left, so nothing can swap a file for a link during the walk; the walk stays
# on the volume's file system, never follows a symbolic link, changes files
# through descriptors and leaves a file with a second hard link alone.
# Idempotent. Modes do not change.
own_data() {
	python -I /opt/garmin-ext/own_data.py </dev/null ||
		log "WARNING: could not give /data to the user app; the server may fail to read or write there."
}

# Railway injects PORT; 8080 for local runs.
PORT="${PORT:-8080}"
# The Caddyfile proxies to 127.0.0.1:8000, so these three are fixed, not defaults.
GARMIN_MCP_TRANSPORT=streamable-http
GARMIN_MCP_HOST=127.0.0.1
GARMIN_MCP_PORT=8000
# Defaults. The Dockerfile bakes the same values in as ENV so that
# `railway ssh` shells, which never run this script, see them too.
GARMIN_MCP_STATELESS_HTTP="${GARMIN_MCP_STATELESS_HTTP:-true}"
GARMINTOKENS="${GARMINTOKENS:-/data/tokens}"
export PORT GARMIN_MCP_TRANSPORT GARMIN_MCP_HOST GARMIN_MCP_PORT \
	GARMIN_MCP_STATELESS_HTTP GARMINTOKENS

# --- PORT: the public port Caddy listens on ----------------------------------
# A whole number from 1 to 65535 (no sign, no leading zero), and never
# GARMIN_MCP_PORT: Caddy proxies to 127.0.0.1:$GARMIN_MCP_PORT, so the two
# would want the same port and the server would never answer.
case "$PORT" in
[1-9] | [1-9][0-9] | [1-9][0-9][0-9] | [1-9][0-9][0-9][0-9] | [1-9][0-9][0-9][0-9][0-9]) port_ok=yes ;;
*) port_ok=no ;;
esac
{ [ "$port_ok" = yes ] && [ "$PORT" -le 65535 ]; } ||
	die "PORT must be a whole number from 1 to 65535. Unset it (the default is 8080) or fix it (Railway: service variables; Docker: compose.yaml)."
[ "$PORT" -ne "$GARMIN_MCP_PORT" ] ||
	die "PORT must not be $GARMIN_MCP_PORT: that is garmin-mcp's internal port, which Caddy proxies to. Unset PORT (the default is 8080) or pick another port."

# --- Garmin credentials never live in the environment ------------------------
# With them set, upstream would attempt a password login on every start.
[ -z "${GARMIN_EMAIL:-}${GARMIN_PASSWORD:-}${GARMIN_EMAIL_FILE:-}${GARMIN_PASSWORD_FILE:-}" ] ||
	die "GARMIN_EMAIL/GARMIN_PASSWORD (or a *_FILE variant) is set. Delete it; in full mode, log in once with garmin-mcp-auth inside the container instead (docs/garmin-login.md)."

# --- Config checks: fail closed, before anything starts ----------------------
# The read-only tool policy (config/enabled-tools.txt, narrowed only by
# GARMIN_ENABLED_TOOLS; no write tool, no unknown name, no
# GARMIN_DISABLED_TOOLS; in key mode only the tools key mode has),
# GARMIN_AUTH (required: full or key) and, for key, GARMIN_PERSONAL_API_KEY,
# REPORT_TZ (required, a valid IANA zone), EDGE_CLIENT_IP_HEADER (unset,
# X-Real-IP or CF-Connecting-IP) and the sign-in: OWNER_SECRET (required),
# PUBLIC_URL (or Railway's public domain), the optional MCP_BEARER_TOKEN and
# ALLOWED_REDIRECT_URIS, and AUTH_DB, which the check creates and opens as the
# server would. launch.py prints one line per problem, never a secret value.
# This runs here, not only inside the supervisor loop below: there a bad
# setting would make launch.py exit and be restarted forever while Caddy keeps
# serving.
# It runs as app, after own_data, so the sign-in database it may create is app's.
own_data
(run_as app python /opt/garmin-ext/launch.py --check-config </dev/null) ||
	die "configuration check failed (see the launch: lines above). Fix the settings (Railway: service variables; Docker: .env) and redeploy or restart."

# --- ROLE=worker or ROLE=enqueue: history search's jobs, not the server -------
# The check above refused any other ROLE, and these two without
# HISTORY_SEARCH=on. exec makes the worker PID 1, so the platform's SIGTERM
# reaches it (its leases are released, docs/history-worker.md).
role=$(printf '%s' "${ROLE:-}" | tr -d '[:space:]' | tr '[:upper:]' '[:lower:]')
case "$role" in
worker)
	log "ROLE=worker: starting the history job worker (no MCP server here)."
	run_as app python /opt/garmin-ext/worker.py </dev/null
	;;
enqueue)
	run_as app python /opt/garmin-ext/enqueue.py </dev/null
	;;
esac

# GARMIN_AUTH is full or key here: the check above refuses anything else.
case "$(printf '%s' "${GARMIN_AUTH:-}" | tr -d '[:space:]' | tr '[:upper:]' '[:lower:]')" in
key) key_mode=yes ;;
*) key_mode=no ;;
esac

# --- The /data volume: Garmin tokens and the sign-in database ----------------
# Both live on the volume by default. Without it they are lost on the next
# deploy: Garmin needs a new login (full mode) and every Claude app must sign
# in again. Key mode keeps only a 24-hour access token there, which the server
# simply fetches again.
lost=""
if [ "$key_mode" = no ]; then
	case "$GARMINTOKENS" in
	/data | /data/*) lost="Garmin tokens" ;;
	esac
fi
case "${AUTH_DB:-/data/auth.sqlite}" in
/data | /data/*) lost="${lost:+$lost and }sign-ins" ;;
esac
if [ -n "$lost" ] && ! grep -qs ' /data ' /proc/mounts; then
	log "WARNING: no volume is mounted at /data; $lost will be lost on the next deploy."
fi

has_tokens() { [ -n "$(ls -A "$GARMINTOKENS" 2>/dev/null)" ]; }
if [ "$key_mode" = yes ]; then
	if has_tokens; then
		log "GARMIN_AUTH=key: the Garmin account tokens in $GARMINTOKENS are not used. Delete them if you will not switch back to GARMIN_AUTH=full."
	fi
else
	# As app, never as root: root would follow a link someone put in the path.
	[ ! -L "$GARMINTOKENS" ] ||
		die "GARMINTOKENS ($GARMINTOKENS) is a symbolic link. Point GARMINTOKENS at a real folder, or remove the link."
	{ (run_as app mkdir -p -- "$GARMINTOKENS" </dev/null) && (run_as app chmod -- 700 "$GARMINTOKENS" </dev/null); } ||
		die "the user app could not create $GARMINTOKENS with mode 700. Use a folder under /data (the default is /data/tokens) or give it to app (uid 10001)."
	has_tokens ||
		log "No Garmin tokens in $GARMINTOKENS yet. One-time login inside this container: garmin-mcp-auth --token-path $GARMINTOKENS (Railway: railway ssh first; Docker: docker compose exec app garmin-mcp-auth --token-path $GARMINTOKENS). garmin-mcp restarts by itself once the tokens are written."
fi

# --- garmin-mcp, supervised in the background --------------------------------
# To reload tokens later (e.g. after garmin-mcp-auth --force-reauth), run
# inside the container:  kill "$(cat /tmp/garmin-mcp.pid)"
pidfile="${TMPDIR:-/tmp}/garmin-mcp.pid"

# Restart backoff. after_exit STATUS SECONDS_RUN sets `delay`, the seconds to
# wait before the next start, and counts quick exits in a row (`quick`): a run
# of HEALTHY_RUN seconds or more resets the count; status 143 or 0 is a
# requested restart (SIGTERM: the token restart below, or the kill above, after
# which uvicorn usually exits 0): it restarts after 1 s and counts nothing; any
# other quick exit doubles the wait from 2 s up to MAX_DELAY. The MAX_QUICK-th
# quick exit in a row returns 1: stop the container.
HEALTHY_RUN=300
MAX_DELAY=60
MAX_QUICK=8
delay=2
quick=0
after_exit() {
	if [ "$2" -ge "$HEALTHY_RUN" ]; then quick=0; fi
	if [ "$1" -eq 143 ] || [ "$1" -eq 0 ]; then
		delay=1
		return 0
	fi
	if [ "$2" -ge "$HEALTHY_RUN" ]; then
		delay=2
		return 0
	fi
	quick=$((quick + 1))
	[ "$quick" -lt "$MAX_QUICK" ] || return 1
	delay=2
	n=1
	while [ "$n" -lt "$quick" ] && [ "$delay" -lt "$MAX_DELAY" ]; do
		delay=$((delay * 2))
		n=$((n + 1))
	done
	[ "$delay" -le "$MAX_DELAY" ] || delay=$MAX_DELAY
	return 0
}

# $$ is this script's PID, which Caddy takes over below.
caddy_pid=$$
(
	while :; do
		# Key mode needs no Garmin tokens: never wait for them.
		had_tokens=no
		stopped_by_us=no
		if [ "$key_mode" = yes ] || has_tokens; then had_tokens=yes; fi
		# Tokens a root login just wrote become app's before the server reads them.
		own_data
		# launch.py runs upstream's garmin-mcp with this repo's extra tools
		# (ext/README.md) and the built-in sign-in, so it gets OWNER_SECRET,
		# the optional MCP_BEARER_TOKEN and, in key mode, the key.
		started=$(date +%s)
		run_as app python /opt/garmin-ext/launch.py </dev/null &
		pid=$!
		echo "$pid" >"$pidfile"
		if [ "$had_tokens" = no ]; then
			# Upstream logs in once at startup and never retries, so a server
			# started without tokens stays unready. Restart it once
			# garmin-mcp-auth has written them.
			while kill -0 "$pid" 2>/dev/null && ! has_tokens; do sleep 5; done
			if kill -0 "$pid" 2>/dev/null; then
				sleep 10 # let garmin-mcp-auth finish writing and verifying
				log "Garmin tokens found; restarting garmin-mcp to load them."
				kill "$pid" 2>/dev/null || true
				stopped_by_us=yes
			fi
		fi
		rc=0
		wait "$pid" || rc=$?
		# Our own restart is never a failure, whatever status the server exits with.
		[ "$stopped_by_us" = no ] || rc=143
		if ! after_exit "$rc" $(($(date +%s) - started)); then
			log "FATAL: garmin-mcp exited $MAX_QUICK times in a row within $HEALTHY_RUN s each (last status $rc); stopping the container so the platform restarts it."
			# SIGQUIT: Caddy exits at once with status 2, so a restart-on-failure policy applies.
			kill -QUIT "$caddy_pid"
			exit 1
		fi
		log "garmin-mcp exited (status $rc); restarting in ${delay}s."
		sleep "$delay"
	done
) &

# --- Caddy as PID 1 ----------------------------------------------------------
# Receives the platform's SIGTERM and shuts down cleanly; the container stops
# with it. Caddy is only the edge (the sign-in is in Python), so it starts
# without the secrets, the optional features' ones too (the tracing service's
# auth headers, the per-signal ones as well, the Postgres URL and password, the token key, and Langfuse's
# keys should someone set them here); env and setpriv exec it in place, so it
# stays PID 1. It keeps EDGE_CLIENT_IP_HEADER, which the Caddyfile reads.
# It runs as the user caddy, which cannot read the server's /proc entries.
# A port below 1024 then needs a platform that lets any user bind it (Docker
# does by default); compose.yaml's 8080 and the default 8080 are above.
[ "$PORT" -ge 1024 ] ||
	log "WARNING: PORT=$PORT is below 1024 and Caddy runs without root; it starts only where the platform lets any user bind such a port."
exec env -u OWNER_SECRET -u MCP_BEARER_TOKEN -u GARMIN_PERSONAL_API_KEY -u OTEL_EXPORTER_OTLP_HEADERS -u OTEL_EXPORTER_OTLP_TRACES_HEADERS -u OTEL_EXPORTER_OTLP_METRICS_HEADERS -u OTEL_EXPORTER_OTLP_LOGS_HEADERS -u DATABASE_URL -u HISTORY_TOKEN_KEY -u PGPASSWORD -u LANGFUSE_SECRET_KEY -u LANGFUSE_PUBLIC_KEY HOME=/home/caddy setpriv --reuid=caddy --regid=caddy --init-groups --no-new-privs -- caddy run --config /etc/caddy/Caddyfile --adapter caddyfile
