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
#      the background and restarts it if it exits, and (full mode) once more
#      when first-time Garmin tokens appear.
#   4. Replaces itself with Caddy (PID 1), which owns the public $PORT.
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
python /opt/garmin-ext/launch.py --check-config </dev/null ||
	die "configuration check failed (see the launch: lines above). Fix the settings (Railway: service variables; Docker: .env) and redeploy or restart."

# --- ROLE=worker or ROLE=enqueue: history search's jobs, not the server -------
# The check above refused any other ROLE, and these two without
# HISTORY_SEARCH=on. exec makes the worker PID 1, so the platform's SIGTERM
# reaches it (its leases are released, docs/history-worker.md).
role=$(printf '%s' "${ROLE:-}" | tr -d '[:space:]' | tr '[:upper:]' '[:lower:]')
case "$role" in
worker)
	log "ROLE=worker: starting the history job worker (no MCP server here)."
	exec python /opt/garmin-ext/worker.py </dev/null
	;;
enqueue)
	exec python /opt/garmin-ext/enqueue.py </dev/null
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
	mkdir -p "$GARMINTOKENS"
	chmod 700 "$GARMINTOKENS"
	has_tokens ||
		log "No Garmin tokens in $GARMINTOKENS yet. One-time login inside this container: garmin-mcp-auth --token-path $GARMINTOKENS (Railway: railway ssh first; Docker: docker compose exec app garmin-mcp-auth --token-path $GARMINTOKENS). garmin-mcp restarts by itself once the tokens are written."
fi

# --- garmin-mcp, supervised in the background --------------------------------
# To reload tokens later (e.g. after garmin-mcp-auth --force-reauth), run
# inside the container:  kill "$(cat /tmp/garmin-mcp.pid)"
pidfile="${TMPDIR:-/tmp}/garmin-mcp.pid"
(
	while :; do
		# Key mode needs no Garmin tokens: never wait for them.
		had_tokens=no
		if [ "$key_mode" = yes ] || has_tokens; then had_tokens=yes; fi
		# launch.py runs upstream's garmin-mcp with this repo's extra tools
		# (ext/README.md) and the built-in sign-in, so it gets OWNER_SECRET,
		# the optional MCP_BEARER_TOKEN and, in key mode, the key.
		python /opt/garmin-ext/launch.py </dev/null &
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
			fi
		fi
		rc=0
		wait "$pid" || rc=$?
		log "garmin-mcp exited (status $rc); restarting in 3s."
		sleep 3
	done
) &

# --- Caddy as PID 1 ----------------------------------------------------------
# Receives the platform's SIGTERM and shuts down cleanly; the container stops
# with it. Caddy is only the edge (the sign-in is in Python), so it starts
# without the secrets, the optional features' ones too (the tracing service's
# auth headers, the per-signal ones as well, the Postgres URL and password, the token key, and Langfuse's
# keys should someone set them here); env execs it in place, so it stays
# PID 1. It keeps EDGE_CLIENT_IP_HEADER, which the Caddyfile reads.
exec env -u OWNER_SECRET -u MCP_BEARER_TOKEN -u GARMIN_PERSONAL_API_KEY -u OTEL_EXPORTER_OTLP_HEADERS -u OTEL_EXPORTER_OTLP_TRACES_HEADERS -u OTEL_EXPORTER_OTLP_METRICS_HEADERS -u OTEL_EXPORTER_OTLP_LOGS_HEADERS -u DATABASE_URL -u HISTORY_TOKEN_KEY -u PGPASSWORD -u LANGFUSE_SECRET_KEY -u LANGFUSE_PUBLIC_KEY caddy run --config /etc/caddy/Caddyfile --adapter caddyfile
