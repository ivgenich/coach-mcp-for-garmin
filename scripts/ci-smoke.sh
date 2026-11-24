#!/usr/bin/env bash
# Container smoke test: build the image, start it with fake secrets and no
# Garmin login, and check it. CI runs it; it runs the same on any machine with
# Docker. No Garmin call is ever made: full mode has no tokens, and key mode
# runs with a fake key and no network at all.
#   scripts/ci-smoke.sh
# Needs docker (with a running daemon), curl, openssl, and a Python with the
# mcp client for scripts/test-protocol.py (SMOKE_PYTHON; after
# `scripts/ci-local.sh --setup` that is ~/.cache/coach-mcp-ci/venv-full/bin/python).
# Settings: SMOKE_PORT (default 8443), SMOKE_IMAGE (default
# coach-mcp-for-garmin:smoke), SMOKE_PYTHON (default python3), SMOKE_BUILD=0
# to test SMOKE_IMAGE as it is, SMOKE_NAME (default coach-mcp-smoke-<pid>): the
# prefix of the container names and the Compose project name.
# The expected tools come from config/enabled-tools.txt (full mode) and
# config/enabled-tools-key.txt (key mode); the version from ext/version.py.
# The secrets are made here, handed to docker by name only, and never printed.
set -euo pipefail
cd "$(dirname "$0")/.."

PORT="${SMOKE_PORT:-8443}"
IMAGE="${SMOKE_IMAGE:-coach-mcp-for-garmin:smoke}"
PY="${SMOKE_PYTHON:-python3}"
FULL_TOOLS=config/enabled-tools.txt
KEY_TOOLS=config/enabled-tools-key.txt
BASE="http://127.0.0.1:$PORT"
RUN="${SMOKE_NAME:-coach-mcp-smoke-$$}"
INIT='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"ci-smoke","version":"1"}}}'
WORK=$(mktemp -d)
NAMES=()
FAILED=()
CHECKS=0

cleanup() {
	# The compose leg's own project, if it started (dc is defined with CP).
	if [ -n "${CP:-}" ]; then dc down --volumes --timeout 5 >/dev/null 2>&1 || true; fi
	# ${a[@]+...}: an empty array under set -u is an error in bash 3.2 (macOS).
	for n in ${NAMES[@]+"${NAMES[@]}"}; do docker rm -f "$n" >/dev/null 2>&1 || true; done
	rm -rf "$WORK"
}
trap cleanup EXIT

die() { # this machine cannot run the test
	echo "ci-smoke: $*" >&2
	exit 2
}
fatal() { # the repository is not ready for the test
	echo "ci-smoke: $*" >&2
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
quiet() { "$@" >/dev/null 2>&1; }
indent() { sed 's/^/      /' "$1"; }
# tool_names FILE: the names in a tool list, sorted (text after '#', spaces and
# blank lines dropped, as ext/launch.py reads config/enabled-tools.txt).
tool_names() { sed -e 's/#.*//' -e 's/[[:space:]]//g' "$1" | grep -v '^$' | sort -u; }

# --- preflight ------------------------------------------------------------------
command -v docker >/dev/null || die "docker is not installed"
docker info >/dev/null 2>&1 || die "the Docker daemon is not reachable"
command -v curl >/dev/null || die "curl is not installed"
command -v openssl >/dev/null || die "openssl is not installed"
"$PY" -c 'import mcp, httpx2' 2>/dev/null ||
	die "$PY has no mcp client; set SMOKE_PYTHON to a Python with it (after scripts/ci-local.sh --setup: ~/.cache/coach-mcp-ci/venv-full/bin/python)"
[ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "$BASE/" || true)" = 000 ] ||
	die "port $PORT is in use; set SMOKE_PORT"
for f in "$FULL_TOOLS" "$KEY_TOOLS"; do [ -s "$f" ] || fatal "$f is missing or empty"; done
FULL_N=$(tool_names "$FULL_TOOLS" | wc -l | tr -d ' ')
KEY_N=$(tool_names "$KEY_TOOLS" | wc -l | tr -d ' ')
KEY_NAMES=$(tool_names "$KEY_TOOLS" | paste -sd, -)
VERSION=$("$PY" -B scripts/ci-checks.py version --print) || fatal "cannot read VERSION from ext/version.py: $VERSION"

OWNER_SECRET=$(openssl rand -hex 32)
MCP_BEARER_TOKEN=$(openssl rand -hex 32)
# A fake key (key mode runs with no network). 27 characters with a '!', so no
# generic long-token pattern of the log redaction matches it: only the exact
# value that launch.py registers can hide it.
GARMIN_PERSONAL_API_KEY="fk!$(openssl rand -hex 12)"
export OWNER_SECRET MCP_BEARER_TOKEN GARMIN_PERSONAL_API_KEY
if [ "${GITHUB_ACTIONS:-}" = true ]; then
	echo "::add-mask::$OWNER_SECRET"
	echo "::add-mask::$MCP_BEARER_TOKEN"
	echo "::add-mask::$GARMIN_PERSONAL_API_KEY"
fi
SECRETS=(-e OWNER_SECRET -e MCP_BEARER_TOKEN) # names only: docker reads the values from this environment

# --- 1. build (the Dockerfile's own self-checks run here) ------------------------
if [ "${SMOKE_BUILD:-1}" != 0 ]; then
	if docker build -t "$IMAGE" . >"$WORK/build.log" 2>&1; then
		check build yes "docker build -t $IMAGE (the Dockerfile's self-checks passed)"
	else
		check build no "docker build failed; the end of its log:"
		tail -n 40 "$WORK/build.log"
		exit 1
	fi
fi

# --- 2. the image's packages are exactly constraints.txt --------------------------
docker run --rm --entrypoint python "$IMAGE" -m pip freeze --all >"$WORK/freeze.txt"
docker run --rm --entrypoint python "$IMAGE" -m pip check >"$WORK/pipcheck.txt" || true # exits 1 on the deliberate conflict
if "$PY" -B scripts/ci-checks.py verify-env --freeze "$WORK/freeze.txt" --pip-check "$WORK/pipcheck.txt" >"$WORK/verify.txt" 2>&1; then
	check image-pins yes "$(tail -n 2 "$WORK/verify.txt" | head -n 1 | sed 's/^ci-checks: //')"
else
	check image-pins no "the image's packages differ from constraints.txt:"
	indent "$WORK/verify.txt"
fi

# --- 3. fail closed: each bad setting stops the container --------------------------
# start_fails LABEL TEXT DOCKER_RUN_ARGS...: the container must exit non-zero
# within 60 s, and its log must hold TEXT.
start_fails() {
	local label=$1 text=$2 name="$RUN-$1" status=created code i
	shift 2
	NAMES+=("$name")
	docker run -d --name "$name" "$@" "$IMAGE" >/dev/null
	for i in $(seq 1 60); do
		status=$(docker inspect -f '{{.State.Status}}' "$name")
		[ "$status" = exited ] && break
		sleep 1
	done
	code=$(docker inspect -f '{{.State.ExitCode}}' "$name")
	docker logs "$name" >"$WORK/$label.log" 2>&1
	named=$(yes_if grep -qF -- "$text" "$WORK/$label.log")
	ok=no
	[ "$status" = exited ] && [ "$code" != 0 ] && [ "$named" = yes ] && ok=yes
	check "fail-closed/$label" "$ok" "status $status, exit $code; the log names the problem: $named"
	[ "$named" = yes ] || { echo "      want: $text"; indent "$WORK/$label.log"; }
	docker rm -f "$name" >/dev/null 2>&1 || true
}
ok_env=(-e "PUBLIC_URL=$BASE" -e REPORT_TZ=UTC -e GARMIN_AUTH=full)
start_fails no-report-tz "launch: config error: REPORT_TZ is not set" \
	"${SECRETS[@]}" -e "PUBLIC_URL=$BASE" -e GARMIN_AUTH=full
start_fails garmin-password "GARMIN_EMAIL/GARMIN_PASSWORD (or a *_FILE variant) is set" \
	"${SECRETS[@]}" "${ok_env[@]}" -e GARMIN_PASSWORD=not-a-real-password
start_fails no-garmin-auth "launch: config error: GARMIN_AUTH is not set" \
	"${SECRETS[@]}" -e "PUBLIC_URL=$BASE" -e REPORT_TZ=UTC
start_fails key-without-key "launch: config error: GARMIN_AUTH=key needs GARMIN_PERSONAL_API_KEY" \
	"${SECRETS[@]}" -e "PUBLIC_URL=$BASE" -e REPORT_TZ=UTC -e GARMIN_AUTH=key
# Empty, Caddy would fall back to X-Forwarded-For, which the caller writes.
start_fails edge-header-empty "launch: config error: EDGE_CLIENT_IP_HEADER is set but empty" \
	"${SECRETS[@]}" "${ok_env[@]}" -e 'EDGE_CLIENT_IP_HEADER='

# pid1_secrets NAME: how many secret variables Caddy (PID 1) holds; want 0.
pid1_secrets() {
	docker exec "$1" sh -c 'tr "\0" "\n" </proc/1/environ | grep -cE "^(OWNER_SECRET|MCP_BEARER_TOKEN|GARMIN_PERSONAL_API_KEY)="' || true
}
# log_hits FILE: lines of FILE that hold a secret value of this run.
log_hits() { grep -cF -e "$OWNER_SECRET" -e "$MCP_BEARER_TOKEN" -e "$GARMIN_PERSONAL_API_KEY" "$1" || true; }

# --- 4. full mode, no Garmin login, checked from outside ----------------------------
MAIN="$RUN-full"
NAMES+=("$MAIN")
# The key variable is set but blank, as a deploy that left the optional field
# empty does: full mode must treat it as unset.
docker run -d --name "$MAIN" -p "127.0.0.1:$PORT:8080" "${SECRETS[@]}" "${ok_env[@]}" \
	-e 'GARMIN_PERSONAL_API_KEY=' "$IMAGE" >/dev/null
code=000
for i in $(seq 1 90); do
	code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$BASE/healthz" || true)
	[ "$code" = 200 ] && break
	sleep 1
done
check full/healthz "$(yes_if test "$code" = 200)" "GET /healthz $code after ${i}s (want 200)"
code=$(curl -s -o "$WORK/readyz" -w '%{http_code}' --max-time 10 "$BASE/readyz" || true)
check full/readyz "$(yes_if test "$code" = 503)" "GET /readyz $code (want 503: no Garmin login) $(head -c 80 "$WORK/readyz")"
want="resource_metadata=\"$BASE/.well-known/oauth-protected-resource/mcp\""
code=$(curl -s -o /dev/null -D "$WORK/h401" -w '%{http_code}' --max-time 10 -X POST "$BASE/mcp" \
	-H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' -d "$INIT" || true)
ok=no
[ "$code" = 401 ] && grep -i '^www-authenticate:' "$WORK/h401" | grep -qF "$want" && ok=yes
check full/no-token "$ok" "POST /mcp without a token $code (want 401 with $want)"
if "$PY" -B scripts/test-protocol.py --url "$BASE/mcp" --expect-version "$VERSION" \
	--expect-name coach-mcp-for-garmin >"$WORK/protocol.txt" 2>&1; then
	check full/test-protocol yes "$(tail -n 1 "$WORK/protocol.txt")"
else
	check full/test-protocol no "$(tail -n 1 "$WORK/protocol.txt")"
fi
indent "$WORK/protocol.txt"
if bash scripts/test-connector.sh --expect-unready "$BASE" >"$WORK/connector.txt" 2>&1; then
	check full/test-connector yes "$(tail -n 1 "$WORK/connector.txt")"
else
	check full/test-connector no "$(tail -n 1 "$WORK/connector.txt")"
fi
indent "$WORK/connector.txt"
check full/caddy-env "$(yes_if test "$(pid1_secrets "$MAIN")" = 0)" "Caddy (PID 1) holds no secret variable"
docker logs "$MAIN" >"$WORK/full.log" 2>&1
want="launch: config OK ($FULL_N read-only tools)"
check full/config "$(yes_if grep -qF "$want" "$WORK/full.log")" "the log says: $want (with the key variable blank)"
grep -F "launch: config" "$WORK/full.log" | sed 's/^/      /' || true
check full/log-secrets "$(yes_if test "$(log_hits "$WORK/full.log")" = 0)" \
	"no secret value in $(wc -l <"$WORK/full.log" | tr -d ' ') log lines"
restarts=$(grep -c "garmin-mcp exited" "$WORK/full.log" || true)
running=$(docker inspect -f '{{.State.Running}}' "$MAIN")
ok=no
[ "$restarts" = 0 ] && [ "$running" = true ] && ok=yes
check full/no-restarts "$ok" "running: $running; launch.py restarts: $restarts"
docker rm -f "$MAIN" >/dev/null 2>&1 || true

# --- 5. key mode with a fake key and no network, checked from inside -----------------
KEYC="$RUN-key"
NAMES+=("$KEYC")
# EDGE_CLIENT_IP_HEADER as compose.yaml sets it, so the image's Caddyfile is
# also proven with the Cloudflare Tunnel value (the full run uses the default).
docker run -d --name "$KEYC" --network none "${SECRETS[@]}" -e GARMIN_PERSONAL_API_KEY \
	-e PUBLIC_URL=http://127.0.0.1:8080 -e REPORT_TZ=UTC -e GARMIN_AUTH=key \
	-e EDGE_CLIENT_IP_HEADER=CF-Connecting-IP "$IMAGE" >/dev/null
up=no
for i in $(seq 1 90); do
	if docker exec "$KEYC" python -c 'import urllib.request; urllib.request.urlopen("http://127.0.0.1:8080/healthz", timeout=3)' >/dev/null 2>&1; then
		up=yes
		break
	fi
	sleep 1
done
check key/up "$up" "GET /healthz inside the container answered after ${i}s"
docker logs "$KEYC" >"$WORK/key.log" 2>&1
want="launch: config OK ($KEY_N read-only tools, Garmin personal API key)"
check key/config "$(yes_if grep -qiF "$want" "$WORK/key.log")" "the log says: $want (any letter case)"
if docker exec -i "$KEYC" python - --expect-ready 200 --expect-names "$KEY_NAMES" --expect-version "$VERSION" \
	--call get_stats --call-args '{"date": "2026-01-15"}' --expect-error "Garmin Connect did not answer" \
	<scripts/ci-smoke-probe.py >"$WORK/probe.txt" 2>&1; then
	check key/probe yes "$(tail -n 1 "$WORK/probe.txt")"
else
	check key/probe no "$(tail -n 1 "$WORK/probe.txt")"
fi
indent "$WORK/probe.txt"
check key/caddy-env "$(yes_if test "$(pid1_secrets "$KEYC")" = 0)" "Caddy (PID 1) holds no secret variable"
docker logs "$KEYC" >"$WORK/key.log" 2>&1
check key/log-secrets "$(yes_if test "$(log_hits "$WORK/key.log")" = 0)" \
	"no secret value (the fake key included) in $(wc -l <"$WORK/key.log" | tr -d ' ') log lines"
docker rm -f "$KEYC" >/dev/null 2>&1 || true

# --- 6. compose.yaml's app service, as a Docker Compose user runs it -----------------
# The image built above, a test .env, and a project name of its own, so it never
# touches a user's coach-mcp-for-garmin stack or volume. Only the app starts:
# the tunnel needs a real Cloudflare token. Full mode, no Garmin login.
if [ -f compose.yaml ]; then
	CD="$WORK/compose"
	mkdir -m 700 "$CD"
	cp compose.yaml "$CD/compose.yaml"
	printf 'services:\n  app:\n    image: %s\n    pull_policy: never\n' "$IMAGE" >"$CD/smoke.yaml"
	(
		umask 077
		printf 'OWNER_SECRET=%s\nMCP_BEARER_TOKEN=%s\nPUBLIC_URL=https://coach.example.com\nREPORT_TZ=UTC\nGARMIN_AUTH=full\n' \
			"$OWNER_SECRET" "$MCP_BEARER_TOKEN" >"$CD/.env"
	)
	printf 'TUNNEL_TOKEN=ci-placeholder\n' >"$CD/tunnel.env"
	dc() { docker compose -p "$CP" --project-directory "$CD" -f "$CD/compose.yaml" -f "$CD/smoke.yaml" "$@"; }
	CP="$RUN"
	dc up -d --no-build app >"$WORK/compose-up.log" 2>&1 || indent "$WORK/compose-up.log"
	health=none
	for i in $(seq 1 120); do
		health=$(docker inspect -f '{{.State.Health.Status}}' "$(dc ps -q app 2>/dev/null)" 2>/dev/null || echo none)
		[ "$health" = healthy ] && break
		sleep 1
	done
	check compose/healthy "$(yes_if test "$health" = healthy)" "app health: $health after ${i}s (compose.yaml's /healthz check)"
	# PORT|EDGE_CLIENT_IP_HEADER|GARMINTOKENS_BASE64|TUNNEL_TOKEN set or not; never a value of a secret.
	# shellcheck disable=SC2016 # expanded by the container's shell, on purpose
	envs=$(dc exec -T app sh -c 'printf "%s|%s|%s|%s" "$PORT" "$EDGE_CLIENT_IP_HEADER" "${GARMINTOKENS_BASE64:-}" "${TUNNEL_TOKEN:+set}"' 2>/dev/null || echo "exec failed")
	want="8080|CF-Connecting-IP|/data/tokens/garmin_tokens.base64|"
	check compose/env "$(yes_if test "$envs" = "$want")" "PORT|EDGE_CLIENT_IP_HEADER|GARMINTOKENS_BASE64|TUNNEL_TOKEN = $envs (want $want)"
	# The host ports Docker bound for the app container. Not `docker compose port`:
	# with nothing published, Compose v5 prints "invalid IP:0" instead of nothing.
	# shellcheck disable=SC2016 # a Go template, not a shell expansion
	port=$(docker inspect -f '{{range $p, $b := .NetworkSettings.Ports}}{{range $b}}{{$p}}->{{.HostIp}}:{{.HostPort}} {{end}}{{end}}' \
		"$(dc ps -q app 2>/dev/null)" 2>/dev/null || echo "inspect failed")
	port=$(printf '%s' "$port" | sed 's/ *$//')
	check compose/no-port "$(yes_if test -z "$port")" "host ports published for app: ${port:-none}"
	check compose/login-cli "$(yes_if quiet dc exec -T app garmin-mcp-auth --help)" "garmin-mcp-auth --help runs in docker compose exec"
	dc exec -T app python -c 'import urllib.request as u
for h, v in (("CF-Connecting-IP", "203.0.113.7"), ("X-Real-IP", "203.0.113.8")):
    u.urlopen(u.Request("http://127.0.0.1:8080/healthz", headers={h: v}), timeout=5).read()' >/dev/null 2>&1 || true
	sleep 1
	dc logs --no-color app >"$WORK/compose.log" 2>&1 || true
	cf=$(grep -c '"client_ip":"203.0.113.7"' "$WORK/compose.log" || true)
	xr=$(grep -c '"client_ip":"203.0.113.8"' "$WORK/compose.log" || true)
	ok=no
	[ "$cf" -ge 1 ] && [ "$xr" = 0 ] && ok=yes
	check compose/client-ip "$ok" "CF-Connecting-IP logged as client_ip: $cf; X-Real-IP logged: $xr (want 1 or more, and 0)"
	check compose/log-secrets "$(yes_if test "$(log_hits "$WORK/compose.log")" = 0)" \
		"no secret value in $(wc -l <"$WORK/compose.log" | tr -d ' ') log lines"
	dc down --volumes --timeout 10 >/dev/null 2>&1 || true
	left=$(docker volume ls -q --filter "name=${CP}_coach-data" | wc -l | tr -d ' ')
	check compose/down "$(yes_if test "$left" = 0)" "docker compose down --volumes left $left ${CP}_coach-data volumes (want 0)"
	CP=""
else
	echo "NOTE  compose: no compose.yaml, the compose leg is skipped"
fi

if [ ${#FAILED[@]} -ne 0 ]; then
	echo "ci-smoke: $((CHECKS - ${#FAILED[@]}))/$CHECKS checks passed; FAILED: ${FAILED[*]}"
	exit 1
fi
echo "ci-smoke: $CHECKS/$CHECKS checks passed"
