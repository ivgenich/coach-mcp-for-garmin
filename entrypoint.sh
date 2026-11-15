#!/bin/sh
# Container entrypoint (POSIX sh; /bin/sh is dash in the image).
#   1. Refuses to start without a usable MCP_BEARER_TOKEN, with Garmin
#      credentials in the environment, or when launch.py --check-config
#      rejects the tool settings or REPORT_TZ.
#   2. Runs garmin-mcp on 127.0.0.1:8000 in the background and restarts it if
#      it exits, and once more when first-time Garmin tokens appear.
#   3. Replaces itself with Caddy (PID 1), which owns the public $PORT.
# Only Caddy gets the bearer token; garmin-mcp gets no secret. Secret values
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

# --- Bearer token: fail closed ----------------------------------------------
# Caddy pastes this value into the Caddyfile before parsing it, both into a
# quoted header match and into a URL path. Keeping it to URL-safe characters
# stops it from closing the quoted string, turning the exact header match into
# a "*" wildcard match, or adding path segments.
[ -n "${MCP_BEARER_TOKEN:-}" ] ||
	die "MCP_BEARER_TOKEN is not set. Set it as a service variable: openssl rand -hex 32"
case "$MCP_BEARER_TOKEN" in
*[!A-Za-z0-9._~-]*)
	die "MCP_BEARER_TOKEN may only contain A-Z a-z 0-9 . _ ~ - (use: openssl rand -hex 32)"
	;;
esac
[ "${#MCP_BEARER_TOKEN}" -ge 32 ] ||
	die "MCP_BEARER_TOKEN is shorter than 32 characters (use: openssl rand -hex 32)"

# --- Garmin credentials never live in the environment ------------------------
# With them set, upstream would attempt a password login on every start.
[ -z "${GARMIN_EMAIL:-}${GARMIN_PASSWORD:-}${GARMIN_EMAIL_FILE:-}${GARMIN_PASSWORD_FILE:-}" ] ||
	die "GARMIN_EMAIL/GARMIN_PASSWORD (or a *_FILE variant) is set. Delete it; log in once with garmin-mcp-auth over railway ssh instead."

# --- Config checks: fail closed, before anything starts ----------------------
# The read-only tool policy (config/enabled-tools.txt, narrowed only by
# GARMIN_ENABLED_TOOLS; no write tool, no unknown name, no
# GARMIN_DISABLED_TOOLS) and REPORT_TZ (required, a valid IANA zone).
# launch.py prints one line per problem. This runs here, not only inside the
# supervisor loop below: there a bad setting would make launch.py exit and be
# restarted forever while Caddy keeps serving.
env -u MCP_BEARER_TOKEN python /opt/garmin-ext/launch.py --check-config </dev/null ||
	die "configuration check failed (see the launch: lines above). Fix the service variables and redeploy."

# --- Garmin token directory on the /data volume ------------------------------
case "$GARMINTOKENS" in
/data | /data/*)
	grep -qs ' /data ' /proc/mounts ||
		log "WARNING: no volume is mounted at /data; Garmin tokens will be lost on the next deploy."
	;;
esac
mkdir -p "$GARMINTOKENS"
chmod 700 "$GARMINTOKENS"

has_tokens() { [ -n "$(ls -A "$GARMINTOKENS" 2>/dev/null)" ]; }
has_tokens ||
	log "No Garmin tokens in $GARMINTOKENS yet. One-time login: railway ssh, then garmin-mcp-auth --token-path $GARMINTOKENS. garmin-mcp restarts by itself once the tokens are written."

# --- garmin-mcp, supervised in the background --------------------------------
# To reload tokens later (e.g. after garmin-mcp-auth --force-reauth), run
# inside the container:  kill "$(cat /tmp/garmin-mcp.pid)"
pidfile="${TMPDIR:-/tmp}/garmin-mcp.pid"
(
	while :; do
		had_tokens=no
		has_tokens && had_tokens=yes
		# launch.py runs upstream's garmin-mcp with this repo's extra tools
		# (ext/README.md). It never gets the bearer token: that is Caddy's
		# business.
		env -u MCP_BEARER_TOKEN python /opt/garmin-ext/launch.py </dev/null &
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
# Receives Railway's SIGTERM and shuts down cleanly; the container stops with it.
exec caddy run --config /etc/caddy/Caddyfile --adapter caddyfile
