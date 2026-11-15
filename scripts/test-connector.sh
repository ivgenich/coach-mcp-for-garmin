#!/usr/bin/env bash
# Verify the deployed Garmin MCP connector end to end.
# Usage: scripts/test-connector.sh <base-url>
#   e.g. scripts/test-connector.sh https://<your-app>.up.railway.app
# The token is read from $MCP_BEARER_TOKEN or prompted for, so it never lands
# in shell history or on a command line that `ps` can see.
set -u

if [ $# -ne 1 ]; then
  echo "Usage: $0 <base-url>" >&2
  exit 2
fi

BASE="${1%/}"
BASE="${BASE%/mcp}"   # accept either https://domain or https://domain/mcp
TOKEN="${MCP_BEARER_TOKEN:-}"
if [ -z "$TOKEN" ]; then
  printf 'Bearer token: ' >&2
  read -rs TOKEN
  echo >&2
fi
FAIL=0
INIT='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"test-connector","version":"1.0.0"}}}'

pass() { echo "PASS: $1"; }
fail() { echo "FAIL: $1"; FAIL=1; }

# Both framings of a Streamable HTTP reply (application/json or an SSE event)
# carry these two fields in an InitializeResult.
is_init_result() { grep -q '"serverInfo"' "$1" && grep -q '"protocolVersion"' "$1"; }
show_body() { echo "--- response body ---" >&2; head -c 2000 "$1" >&2; echo >&2; }

# 1. Liveness, no auth.
code=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 15 "$BASE/healthz")
if [ "$code" = "200" ]; then pass "GET /healthz returned 200"; else fail "GET /healthz returned $code (expected 200)"; fi

# 2. Garmin login state, no auth.
code=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 15 "$BASE/readyz")
if [ "$code" = "200" ]; then
  pass "GET /readyz returned 200 (Garmin login OK)"
else
  fail "GET /readyz returned $code: Garmin login not done yet, or still loading (wait 30 s after garmin-mcp-auth and retry)"
fi

# 3. No token -> 401.
code=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 15 -X POST "$BASE/mcp" \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' -d "$INIT")
if [ "$code" = "401" ]; then pass "POST /mcp without a token returned 401"; else fail "POST /mcp without a token returned $code (expected 401)"; fi

body=$(mktemp)
trap 'rm -f "$body"' EXIT

# 4. Header mode: the header is fed on stdin, not argv.
code=$(curl -sS -o "$body" -w '%{http_code}' --max-time 30 -X POST "$BASE/mcp" \
  -H @- -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -d "$INIT" <<<"Authorization: Bearer $TOKEN")
if [ "$code" = "200" ] && is_init_result "$body"; then
  pass "Header mode (Authorization: Bearer) returned an InitializeResult"
else
  fail "Header mode: HTTP $code, no InitializeResult"; show_body "$body"
fi

# 5. URL mode: the URL is fed to curl as a config file on stdin, not argv.
code=$(printf 'url = "%s/%s/mcp"\n' "$BASE" "$TOKEN" | curl -sS -K - -o "$body" -w '%{http_code}' \
  --max-time 30 -X POST -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' -d "$INIT")
if [ "$code" = "200" ] && is_init_result "$body"; then
  pass "URL mode (https://<domain>/<token>/mcp) returned an InitializeResult"
else
  fail "URL mode: HTTP $code, no InitializeResult"; show_body "$body"
fi

if [ "$FAIL" -ne 0 ]; then
  echo "RESULT: FAIL"
  exit 1
fi
echo "RESULT: PASS"
