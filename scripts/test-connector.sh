#!/usr/bin/env bash
# Verify the deployed Garmin MCP connector from the outside: the probes, the
# sign-in discovery Claude starts with (401, protected-resource metadata,
# authorization server metadata), the optional static bearer, and the 410 for
# the removed https://<domain>/<token>/mcp form.
# Usage: scripts/test-connector.sh [--expect-unready] <base-url>
#   e.g. scripts/test-connector.sh https://your-app.up.railway.app
#   scripts/test-connector.sh --expect-unready <base-url>   before the Garmin login (and in CI): /readyz must be 503
# The static bearer check runs only when MCP_BEARER_TOKEN is set in the
# environment; it is never prompted for and never put on a command line.
# The whole sign-in, end to end (it asks for the owner secret):
#   python3 scripts/test-signin.py --live --base <base-url>
set -u

USAGE="Usage: $0 [--expect-unready] <base-url>   (https://..., or http:// only for 127.0.0.1, localhost or [::1])"
case "${1:-}" in -h|--help) echo "$USAGE"; exit 0 ;; esac
EXPECT_UNREADY=0
if [ "${1:-}" = "--expect-unready" ]; then EXPECT_UNREADY=1; shift; fi
if [ $# -ne 1 ]; then
  echo "$USAGE" >&2
  exit 2
fi

BASE="${1%/}"
BASE="${BASE%/mcp}"   # accept either https://domain or https://domain/mcp
# The static bearer goes to this host: only https, or plain http on loopback.
case "$BASE" in
  *@*|*\\*|*[[:space:]]*) echo "$USAGE" >&2; exit 2 ;;  # no user info: it would move the host
  https://?*) ;;
  http://127.0.0.1|http://127.0.0.1:*|http://localhost|http://localhost:*|http://\[::1\]|http://\[::1\]:*) ;;
  *) echo "$USAGE" >&2; exit 2 ;;
esac
TOKEN="${MCP_BEARER_TOKEN:-}"
FAIL=0
INIT='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"test-connector","version":"1.0.0"}}}'

pass() { echo "PASS: $1"; }
fail() { echo "FAIL: $1"; FAIL=1; }

# Both framings of a Streamable HTTP reply (application/json or an SSE event)
# carry these two fields in an InitializeResult.
is_init_result() { grep -q '"serverInfo"' "$1" && grep -q '"protocolVersion"' "$1"; }
show_body() { echo "--- response body ---" >&2; head -c 2000 "$1" >&2; echo >&2; }
# json_has FILE KEY VALUE: the JSON in FILE has "KEY": "VALUE", whitespace
# allowed around the colon, VALUE matched exactly (regex characters escaped).
re_escape() { printf '%s' "$1" | sed 's/[][\.*^$+?(){}|]/\\&/g'; }
json_has() { tr -d '\r\n' <"$1" | grep -qE "\"$2\"[[:space:]]*:[[:space:]]*\"$(re_escape "$3")\""; }

body=$(mktemp)
hdrs=$(mktemp)
trap 'rm -f "$body" "$hdrs"' EXIT

# 1. Liveness, no auth.
code=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 15 "$BASE/healthz")
if [ "$code" = "200" ]; then pass "GET /healthz returned 200"; else fail "GET /healthz returned $code (expected 200)"; fi

# 2. Garmin login state, no auth. With --expect-unready (before the Garmin
#    login, and in CI) it must be 503.
code=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 15 "$BASE/readyz")
if [ "$EXPECT_UNREADY" -eq 1 ]; then
  if [ "$code" = "503" ]; then
    pass "GET /readyz returned 503 (not ready: no Garmin login yet, or Garmin refused the key; as --expect-unready expects)"
  else
    fail "GET /readyz returned $code (--expect-unready expects 503: no Garmin login yet, or a refused key)"
  fi
elif [ "$code" = "200" ]; then
  pass "GET /readyz returned 200 (Garmin login or key accepted)"
else
  fail "GET /readyz returned $code: Garmin login not done yet, or still loading (wait 30 s after garmin-mcp-auth and retry); in key mode, Garmin refused the key at startup (make a new key, set it, redeploy)"
fi

# 3. No token: 401, pointing Claude to the sign-in (resource_metadata).
want="resource_metadata=\"$BASE/.well-known/oauth-protected-resource/mcp\""
code=$(curl -sS -o /dev/null -D "$hdrs" -w '%{http_code}' --max-time 15 -X POST "$BASE/mcp" \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' -d "$INIT")
if [ "$code" = "401" ] && grep -i '^www-authenticate:' "$hdrs" | grep -qF "$want"; then
  pass "POST /mcp without a token returned 401 with $want"
else
  fail "POST /mcp without a token returned $code (expected 401 with a WWW-Authenticate header carrying $want)"
fi

# 4. Protected-resource metadata: "resource" must be exactly the connector URL
#    you paste into Claude.
code=$(curl -sS -o "$body" -w '%{http_code}' --max-time 15 "$BASE/.well-known/oauth-protected-resource/mcp")
if [ "$code" = "200" ] && json_has "$body" resource "$BASE/mcp"; then
  pass "Protected-resource metadata names $BASE/mcp"
else
  fail "GET /.well-known/oauth-protected-resource/mcp returned $code (expected 200 with \"resource\": \"$BASE/mcp\"; on a custom domain, set PUBLIC_URL to it)"
  show_body "$body"
fi

# 5. Authorization server metadata: this server is its own issuer, and Claude
#    registers itself at registration_endpoint.
code=$(curl -sS -o "$body" -w '%{http_code}' --max-time 15 "$BASE/.well-known/oauth-authorization-server")
if [ "$code" = "200" ] && json_has "$body" issuer "$BASE" && grep -q '"registration_endpoint"' "$body"; then
  pass "Authorization server metadata: issuer $BASE, with registration_endpoint"
else
  fail "GET /.well-known/oauth-authorization-server returned $code (expected 200 with \"issuer\": \"$BASE\" and a registration_endpoint)"
  show_body "$body"
fi

# 6. Optional static bearer: the header is fed on stdin, not argv, through a
#    pipe from printf (a bash builtin, so no argv either), never a here-string
#    (<<<): bash 3.2, the macOS bash, writes here-string text to a temporary file.
if [ -z "$TOKEN" ]; then
  echo "SKIP: MCP_BEARER_TOKEN not set (optional)"
else
  code=$(printf 'Authorization: Bearer %s\n' "$TOKEN" | curl -sS -o "$body" -w '%{http_code}' --max-time 30 \
    -X POST "$BASE/mcp" -H @- -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
    -d "$INIT")
  if [ "$code" = "200" ] && is_init_result "$body"; then
    pass "Static bearer (Authorization: Bearer) returned an InitializeResult"
  else
    fail "Static bearer: HTTP $code, no InitializeResult"; show_body "$body"
  fi
fi

# 7. The removed https://<domain>/<token>/mcp form: 410 for any token-shaped
#    first segment (64 zeros here).
code=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 15 -X POST "$BASE/$(printf '%064d' 0)/mcp" \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' -d "$INIT")
if [ "$code" = "410" ]; then
  pass "POST /<64 zeros>/mcp returned 410 (the old URL-token form is gone)"
else
  fail "POST /<64 zeros>/mcp returned $code (expected 410)"
fi

echo "INFO: to test the whole sign-in (it asks for the owner secret): python3 scripts/test-signin.py --live --base $BASE"
if [ "$FAIL" -ne 0 ]; then
  echo "RESULT: FAIL"
  exit 1
fi
echo "RESULT: PASS"
