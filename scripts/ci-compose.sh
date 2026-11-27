#!/usr/bin/env bash
# Docker path test: build and start the repository's real compose.yaml
# (build: .) with a test .env of fake secrets, in a throwaway Compose project,
# and check it the way Claude reaches it. CI runs it; it runs the same on any
# machine with Docker.
#   scripts/ci-compose.sh
# The Cloudflare tunnel cannot run here (it needs a real Cloudflare account).
# scripts/tests/compose-edge.yaml puts an edge in its place: Caddy on
# 127.0.0.1:COMPOSE_TEST_PORT that proxies to http://app:8080, the tunnel's
# route, and sets CF-Connecting-IP to the caller's address, as Cloudflare does.
# The app joins only an internal network: no Garmin call can be made.
#   1. full mode, no Garmin login: health, readiness, the OAuth metadata at the
#      PUBLIC_URL origin, the sign-in end to end (scripts/test-signin.py --live
#      --run-protocol) through the edge, and the caller address the app uses.
#   2. compose.yaml's restart policy, health check, no-new-privileges and named
#      volume: the sign-ins in /data/auth.sqlite survive stop and start, and a
#      crash restarts the app.
#   3. key mode with a fake key: the container is recreated (the volume stays),
#      the config check passes, it has no way out, and the sign-in works.
# Needs docker with Compose 2.24.4 or newer, curl, openssl, and a Python with
# the mcp client for scripts/test-protocol.py (COMPOSE_TEST_PYTHON, else
# SMOKE_PYTHON, else python3; after scripts/ci-local.sh --setup that is
# ~/.cache/coach-mcp-ci/venv-full/bin/python).
# Settings: COMPOSE_TEST_PORT (default 8453), COMPOSE_TEST_NAME (default
# coach-ci-compose-<pid>): the Compose project name, so the containers, the
# network, the volume and the image never touch a real coach-mcp-for-garmin
# stack. Everything the test made is removed at the end, the built image too.
# The secrets are made here, written only to the test's .env (mode 600, in a
# temporary folder that is deleted at the end), and never printed.
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT=$(pwd)

PORT="${COMPOSE_TEST_PORT:-8453}"
PY="${COMPOSE_TEST_PYTHON:-${SMOKE_PYTHON:-python3}}"
CP="${COMPOSE_TEST_NAME:-coach-ci-compose-$$}"
BASE="http://127.0.0.1:$PORT"
OVERRIDE=scripts/tests/compose-edge.yaml
WORK=$(mktemp -d)
FAILED=()
CHECKS=0
STARTED=no

dc() { docker compose -p "$CP" --project-directory "$ROOT" --env-file "$WORK/compose-vars.env" \
	-f compose.yaml -f "$OVERRIDE" "$@"; }
cleanup() {
	if [ "$STARTED" = yes ]; then dc down --volumes --rmi local --timeout 5 >/dev/null 2>&1 || true; fi
	rm -rf "$WORK"
}
trap cleanup EXIT

die() { # this machine cannot run the test
	echo "ci-compose: $*" >&2
	exit 2
}
fatal() { # the repository is not ready for the test
	echo "ci-compose: $*" >&2
	exit 1
}
check() { # check NAME OK DETAIL
	CHECKS=$((CHECKS + 1))
	if [ "$2" = yes ]; then echo "PASS  $1: $3"; else
		echo "FAIL  $1: $3"
		FAILED+=("$1")
	fi
}
yes_if() { if "$@"; then echo yes; else echo no; fi; }
indent() { sed 's/^/      /' "$1"; }
http_code() { curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$@" || true; }
tool_count() { sed -e 's/#.*//' -e 's/[[:space:]]//g' "$1" | grep -v '^$' | sort -u | wc -l | tr -d ' '; }

# --- preflight ------------------------------------------------------------------
command -v docker >/dev/null || die "docker is not installed"
docker info >/dev/null 2>&1 || die "the Docker daemon is not reachable"
docker compose version >/dev/null 2>&1 || die "docker compose is not installed"
command -v curl >/dev/null || die "curl is not installed"
command -v openssl >/dev/null || die "openssl is not installed"
"$PY" -c 'import mcp, httpx2' 2>/dev/null ||
	die "$PY has no mcp client; set COMPOSE_TEST_PYTHON to a Python with it (after scripts/ci-local.sh --setup: ~/.cache/coach-mcp-ci/venv-full/bin/python)"
[ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "$BASE/" || true)" = 000 ] ||
	die "port $PORT is in use; set COMPOSE_TEST_PORT"
[ -f compose.yaml ] || fatal "compose.yaml is missing"
VERSION=$("$PY" -B scripts/ci-checks.py version --print) || fatal "cannot read VERSION from ext/version.py: $VERSION"
# The edge runs the Caddy image the Dockerfile pins, so Dependabot's bump moves both.
EDGE_IMAGE=$(sed -n 's/^FROM \(caddy:[^ ]*@sha256:[0-9a-f]\{64\}\) AS caddy$/\1/p' Dockerfile)
[ -n "$EDGE_IMAGE" ] || fatal "no 'FROM caddy:<tag>@sha256:<digest> AS caddy' line in the Dockerfile"

OWNER_SECRET=$(openssl rand -hex 32)
MCP_BEARER_TOKEN=$(openssl rand -hex 32)
# A fake key: there is no network, so it never reaches Garmin.
GARMIN_PERSONAL_API_KEY="fk!$(openssl rand -hex 12)"
export OWNER_SECRET MCP_BEARER_TOKEN # test-signin.py reads them from the environment
if [ "${GITHUB_ACTIONS:-}" = true ]; then
	echo "::add-mask::$OWNER_SECRET"
	echo "::add-mask::$MCP_BEARER_TOKEN"
	echo "::add-mask::$GARMIN_PERSONAL_API_KEY"
fi
# log_hits FILE: lines of FILE that hold a secret value of this run.
log_hits() { grep -cF -e "$OWNER_SECRET" -e "$MCP_BEARER_TOKEN" -e "$GARMIN_PERSONAL_API_KEY" "$1" || true; }

# write_env MODE: the app's .env for full or key mode (only the variable names reach the logs).
write_env() {
	(
		umask 077
		{
			printf 'OWNER_SECRET=%s\nMCP_BEARER_TOKEN=%s\n' "$OWNER_SECRET" "$MCP_BEARER_TOKEN"
			printf 'PUBLIC_URL=%s\nREPORT_TZ=UTC\nGARMIN_AUTH=%s\n' "$BASE" "$1"
			# Single quotes: the key holds a '!' (compose.yaml's .env rule for special characters).
			if [ "$1" = key ]; then printf "GARMIN_PERSONAL_API_KEY='%s'\n" "$GARMIN_PERSONAL_API_KEY"; fi
		} >"$WORK/app.env"
	)
}
write_env full
printf 'TUNNEL_TOKEN=ci-placeholder\n' >"$WORK/tunnel.env"
# Compose's own variables, so the repository's .env (a real one, on a maintainer's machine) is never read.
printf 'CI_COMPOSE_ENV=%s\nCI_COMPOSE_TUNNEL_ENV=%s\nCI_EDGE_IMAGE=%s\nCI_EDGE_PORT=%s\n' \
	"$WORK/app.env" "$WORK/tunnel.env" "$EDGE_IMAGE" "$PORT" >"$WORK/compose-vars.env"
dc config --quiet 2>"$WORK/config.txt" || {
	indent "$WORK/config.txt"
	die "docker compose cannot read compose.yaml with $OVERRIDE (Compose 2.24.4 or newer is needed)"
}

# app_id: the app container's id. inspect FORMAT: docker inspect of the app container.
app_id() { dc ps -q app 2>/dev/null; }
inspect() { docker inspect -f "$1" "$(app_id)" 2>/dev/null || echo "inspect failed"; }
# wait_up: the app healthy (compose.yaml's health check) and /healthz 200 through the edge.
wait_up() {
	local i health=none code=000
	for i in $(seq 1 150); do
		health=$(inspect '{{.State.Health.Status}}')
		code=$(http_code "$BASE/healthz")
		[ "$health" = healthy ] && [ "$code" = 200 ] && break
		sleep 1
	done
	echo "app health $health, GET /healthz through the edge $code after ${i}s"
	[ "$health" = healthy ] && [ "$code" = 200 ]
}
# clients: how many OAuth clients /data/auth.sqlite holds (read only, inside the app).
clients() {
	dc exec -T app python -c 'import sqlite3
print(sqlite3.connect("file:/data/auth.sqlite?mode=ro", uri=True).execute("select count(*) from clients").fetchone()[0])' \
		2>/dev/null || echo none
}
# signin LABEL TOOLS_FILE: the real sign-in through the edge, then test-protocol.py with its token.
signin() {
	if "$PY" -B scripts/test-signin.py --live --base "$BASE" --tools-file "$2" --run-protocol \
		--protocol-python "$PY" --protocol-args "--expect-version $VERSION" >"$WORK/$1-signin.txt" 2>&1; then
		check "$1/signin" yes "test-signin.py --live --run-protocol through the edge: $(tail -n 1 "$WORK/$1-signin.txt")"
	else
		check "$1/signin" no "test-signin.py --live --run-protocol through the edge: $(tail -n 1 "$WORK/$1-signin.txt")"
	fi
	indent "$WORK/$1-signin.txt"
}

# --- 1. build and start: full mode, no Garmin login -------------------------------
STARTED=yes
if dc build app >"$WORK/build.log" 2>&1; then
	check build yes "docker compose build app (compose.yaml's build: .; the Dockerfile's self-checks passed)"
else
	check build no "docker compose build failed; the end of its log:"
	tail -n 40 "$WORK/build.log"
	exit 1
fi
dc up -d --no-build app edge >"$WORK/up.log" 2>&1 || indent "$WORK/up.log"
detail=$(wait_up) && ok=yes || ok=no
check full/up "$ok" "$detail"
code=$(http_code -o "$WORK/readyz" "$BASE/readyz")
check full/readyz "$(yes_if test "$code" = 503)" "GET /readyz $code (want 503: no Garmin login)"

# The OAuth metadata names the PUBLIC_URL origin, not the app's inside address.
curl -s --max-time 10 "$BASE/.well-known/oauth-authorization-server" >"$WORK/as.json" || true
curl -s --max-time 10 "$BASE/.well-known/oauth-protected-resource/mcp" >"$WORK/pr.json" || true
got=$("$PY" -c 'import json, sys
a, p = (json.load(open(f)) for f in sys.argv[1:3])
print(a.get("issuer"), a.get("authorization_endpoint"), p.get("resource"), *p.get("authorization_servers", []))' \
	"$WORK/as.json" "$WORK/pr.json" 2>/dev/null || echo "unreadable")
want="$BASE $BASE/authorize $BASE/mcp $BASE"
check full/metadata "$(yes_if test "$got" = "$want")" "issuer, authorization_endpoint, resource, authorization_servers: $got (want $want)"
want="resource_metadata=\"$BASE/.well-known/oauth-protected-resource/mcp\""
code=$(http_code -D "$WORK/h401" -X POST "$BASE/mcp" -H 'Content-Type: application/json' \
	-H 'Accept: application/json, text/event-stream' -d '{"jsonrpc":"2.0","id":1,"method":"ping"}')
ok=no
[ "$code" = 401 ] && grep -i '^www-authenticate:' "$WORK/h401" | grep -qF "$want" && ok=yes
check full/no-token "$ok" "POST /mcp without a token $code (want 401 with $want)"
signin full config/enabled-tools.txt

# The caller's address: the edge replaces a planted CF-Connecting-IP, and the app
# ignores X-Real-IP and X-Client-IP from the caller. The probe path is unique, so
# its line in the app's access log is easy to find (Caddy keeps the path).
probe="/ci-edge-probe-$(openssl rand -hex 4)"
curl -s -o /dev/null -D "$WORK/hprobe" --max-time 10 -H 'CF-Connecting-IP: 198.51.100.7' \
	-H 'X-Real-IP: 198.51.100.8' -H 'X-Client-IP: 198.51.100.9' "$BASE$probe" || true
edge_ip=$(sed -n 's/^[Xx]-[Cc][Ii]-[Ee]dge-[Cc]lient-[Ii][Pp]: *\([^[:space:]]*\).*/\1/p' "$WORK/hprobe")
sleep 1
dc logs --no-color --no-log-prefix app >"$WORK/app.log" 2>&1 || true
seen=$(grep -F "\"uri\":\"$probe\"" "$WORK/app.log" | sed -n 's/.*"client_ip":"\([^"]*\)".*/\1/p' | head -n 1)
ok=no
[ -n "$edge_ip" ] && [ "$seen" = "$edge_ip" ] && ok=yes
check full/client-ip "$ok" "planted CF-Connecting-IP, X-Real-IP, X-Client-IP; the app logged client_ip ${seen:-none} (want the edge's ${edge_ip:-none})"
# By design (Caddyfile): every peer that reaches app:8080 is trusted for
# CF-Connecting-IP, since no port is published and only the tunnel gets there.
# A peer on the Compose network that skips the edge can name any address.
probe2="/ci-direct-probe-$(openssl rand -hex 4)"
dc exec -T edge wget -q -O /dev/null --header 'CF-Connecting-IP: 198.51.100.10' "http://app:8080$probe2" \
	>/dev/null 2>&1 || true
sleep 1
dc logs --no-color --no-log-prefix app >"$WORK/app.log" 2>&1 || true
seen=$(grep -F "\"uri\":\"$probe2\"" "$WORK/app.log" | sed -n 's/.*"client_ip":"\([^"]*\)".*/\1/p' | head -n 1)
check full/direct-peer "$(yes_if test "$seen" = 198.51.100.10)" \
	"a peer on the Compose network that skips the edge is trusted for CF-Connecting-IP (by design, no port is published): client_ip ${seen:-none}"
# shellcheck disable=SC2016 # a Go template, not a shell expansion
port=$(inspect '{{range $p, $b := .NetworkSettings.Ports}}{{range $b}}{{$p}}->{{.HostIp}}:{{.HostPort}} {{end}}{{end}}')
port=$(printf '%s' "$port" | sed 's/ *$//')
check full/no-port "$(yes_if test -z "$port")" "host ports published for app: ${port:-none} (only the test's edge publishes one)"
grep -F "launch: config" "$WORK/app.log" >"$WORK/config-lines.txt" || true
want="launch: config OK ($(tool_count config/enabled-tools.txt) read-only tools)"
check full/config "$(yes_if grep -qF "$want" "$WORK/app.log")" "the log says: $want"

# --- 2. compose.yaml's restart, health check, no-new-privileges and volume ------------
got=$(inspect '{{.HostConfig.RestartPolicy.Name}}|{{json .HostConfig.SecurityOpt}}|{{json .Config.Healthcheck.Test}}')
ok=no
case "$got" in unless-stopped\|*'"no-new-privileges:true"'*\|*'/healthz'*) ok=yes ;; esac
check settings/app "$ok" "restart policy, security options, health check: $got"
got=$(inspect '{{range .Mounts}}{{.Type}}:{{.Name}}:{{.Destination}} {{end}}' | sed 's/ *$//')
check settings/volume "$(yes_if test "$got" = "volume:${CP}_coach-data:/data")" "mounts: $got (want volume:${CP}_coach-data:/data)"
before=$(clients)
ok=no
[ "$before" != none ] && [ "$before" -ge 3 ] && ok=yes
check restart/signins "$ok" "/data/auth.sqlite holds $before OAuth clients after the sign-in (want 3 or more)"
dc stop --timeout 10 edge app >/dev/null 2>&1 || true
dc start app edge >"$WORK/start.log" 2>&1 || indent "$WORK/start.log"
detail=$(wait_up) && ok=yes || ok=no
after=$(clients)
[ "$after" = "$before" ] || ok=no
check restart/stop-start "$ok" "after docker compose stop and start: $detail; OAuth clients $after (want $before)"
# A crash: Caddy (PID 1) exits on SIGQUIT with status 2, and Docker restarts the container.
count=$(inspect '{{.RestartCount}}')
dc exec -T app sh -c 'kill -QUIT 1' >/dev/null 2>&1 || true
sleep 3
detail=$(wait_up) && ok=yes || ok=no
now=$(inspect '{{.RestartCount}}')
[ "$now" != "inspect failed" ] && [ "$count" != "inspect failed" ] && [ "$now" -gt "$count" ] || ok=no
after=$(clients)
[ "$after" = "$before" ] || ok=no
check restart/crash "$ok" "after PID 1 exited: restarts $count -> $now; $detail; OAuth clients $after (want $before)"
dc logs --no-color --no-log-prefix app edge >"$WORK/full.log" 2>&1 || true
check full/log-secrets "$(yes_if test "$(log_hits "$WORK/full.log")" = 0)" \
	"no secret value in $(wc -l <"$WORK/full.log" | tr -d ' ') log lines of app and edge"

# --- 3. key mode with a fake key: a new container on the same volume -----------------
write_env key
dc up -d --no-build --force-recreate app edge >"$WORK/up-key.log" 2>&1 || indent "$WORK/up-key.log"
detail=$(wait_up) && ok=yes || ok=no
check key/up "$ok" "recreated with GARMIN_AUTH=key: $detail"
after=$(clients)
check key/volume "$(yes_if test "$after" = "$before")" "the new container reads the same /data/auth.sqlite: OAuth clients $after (want $before)"
code=$(http_code "$BASE/readyz")
check key/readyz "$(yes_if test "$code" = 200)" "GET /readyz $code (want 200: key mode needs no Garmin login)"
# No way out: neither a name lookup nor a connection to an outside address works.
out=$(dc exec -T app python -c 'import socket
for target in (("connectapi.garmin.com", 443), ("1.1.1.1", 443)):
    try:
        socket.create_connection(target, timeout=3).close()
        print("reached", target[0])
    except OSError:
        pass' 2>&1 || echo "exec failed")
check key/no-network "$(yes_if test -z "$out")" "the app cannot reach connectapi.garmin.com or 1.1.1.1: ${out:-none reached}"
dc logs --no-color --no-log-prefix app >"$WORK/key-app.log" 2>&1 || true
want="launch: config OK ($(tool_count config/enabled-tools-key.txt) read-only tools, Garmin personal API key)"
check key/config "$(yes_if grep -qiF "$want" "$WORK/key-app.log")" "the log says: $want (any letter case)"
signin key config/enabled-tools-key.txt
dc logs --no-color --no-log-prefix app edge >"$WORK/key.log" 2>&1 || true
check key/log-secrets "$(yes_if test "$(log_hits "$WORK/key.log")" = 0)" \
	"no secret value (the fake key included) in $(wc -l <"$WORK/key.log" | tr -d ' ') log lines of app and edge"

# --- 4. clean up, and prove it --------------------------------------------------------
dc down --volumes --rmi local --timeout 10 >/dev/null 2>&1 || true
STARTED=no
left=$(docker ps -aq --filter "label=com.docker.compose.project=$CP" | wc -l | tr -d ' ')
vols=$(docker volume ls -q --filter "label=com.docker.compose.project=$CP" | wc -l | tr -d ' ')
nets=$(docker network ls -q --filter "label=com.docker.compose.project=$CP" | wc -l | tr -d ' ')
imgs=$(docker images -q --filter "label=com.docker.compose.project=$CP" | wc -l | tr -d ' ')
check down "$(yes_if test "$left$vols$nets$imgs" = 0000)" \
	"docker compose down --volumes --rmi local left $left containers, $vols volumes, $nets networks, $imgs images (want 0 each)"

if [ ${#FAILED[@]} -ne 0 ]; then
	echo "ci-compose: $((CHECKS - ${#FAILED[@]}))/$CHECKS checks passed; FAILED: ${FAILED[*]}"
	exit 1
fi
echo "ci-compose: $CHECKS/$CHECKS checks passed"
