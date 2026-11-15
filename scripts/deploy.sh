#!/usr/bin/env bash
# deploy.sh: create (first run) or update the coach-mcp-for-garmin service on Railway.
# Safe to re-run: it reuses the project link, the /data volume and every secret
# already set.
#
#   REPORT_TZ=Europe/London ./scripts/deploy.sh   first deploy (use your own IANA zone)
#   ./scripts/deploy.sh                           redeploy (keeps REPORT_TZ already set)
#   ./scripts/deploy.sh --rotate-token            also replace MCP_BEARER_TOKEN and print the new one
#                                                 (only to a terminal; otherwise it says how to read it)
#
# Environment: REPORT_TZ (required on the first deploy; when exported, it
# replaces the value on Railway), DEPLOY_SERVICE (the Railway project and
# service name for a first deploy; default coach-mcp-for-garmin).
#
# Secret values never go on a command line (the CLI reads them on stdin); a new
# bearer token is printed once, and only to a terminal.
# Garmin email/password are never handled here; you type them once inside
# `railway ssh` (README step 2).
# The tools the server exposes are not set here: config/enabled-tools.txt is
# baked into the image and only read-only tools can be enabled (ext/launch.py).
set -euo pipefail

SERVICE="${DEPLOY_SERVICE:-coach-mcp-for-garmin}"

cd "$(dirname "${BASH_SOURCE[0]}")/.."

ROTATE=false
for arg in "$@"; do
  case "$arg" in
    --rotate-token) ROTATE=true ;;
    *) echo "usage: [REPORT_TZ=Area/City] $0 [--rotate-token]" >&2; exit 2 ;;
  esac
done

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m!!  %s\033[0m\n' "$*"; }

# A light shape check; the container validates REPORT_TZ fully at startup
# (launch.py --check-config). Where this machine has a zoneinfo database, the
# zone must exist in it with the exact case: macOS ignores case in file names
# (so -f accepts europe/london), the container's Linux does not. find matches
# the name case-exactly and, with no -type, keeps zones that are symlinks.
valid_tz() {
  [[ $1 =~ ^[A-Za-z][A-Za-z0-9_+-]*(/[A-Za-z0-9_+-]+)*$ ]] || return 1
  [[ -d /usr/share/zoneinfo ]] || return 0
  [[ -f /usr/share/zoneinfo/$1 && -n $(find /usr/share/zoneinfo/ -path "/usr/share/zoneinfo/$1" -print -quit 2>/dev/null) ]]
}
if [[ -n ${REPORT_TZ:-} ]] && ! valid_tz "$REPORT_TZ"; then
  warn "REPORT_TZ='$REPORT_TZ' is not an IANA time zone name (e.g. Europe/London). Nothing was changed."
  exit 1
fi

say "Railway CLI"
if ! command -v railway >/dev/null 2>&1; then
  warn "Install the Railway CLI first:  brew install railway   (or: npm i -g @railway/cli)"
  exit 1
fi
railway --version

say "Login"
railway whoami >/dev/null 2>&1 || railway login
railway whoami

say "Project and service"
# Create a project only when none is linked, and a service only when none is.
# Any other failure (network, expired login) stops here: taking it for "not
# linked yet" would create a second project or service. The CLI's output is
# not shown: a variable listing holds secret values.
cli_rc=0
CLI_OUT="$(railway status 2>&1)" || cli_rc=$?
if grep -q 'No linked project' <<<"$CLI_OUT"; then
  railway init --name "$SERVICE"
elif [[ $cli_rc -ne 0 ]]; then
  warn "Could not read the Railway project status (network, or an expired login)."
  warn "Nothing was changed. Check 'railway whoami' and 'railway status', then re-run."
  exit 1
fi
# railway init links an empty project; volumes and variables need a linked service.
cli_rc=0
CLI_OUT="$(railway variable list 2>&1)" || cli_rc=$?
if grep -q 'No service linked' <<<"$CLI_OUT"; then
  CLI_OUT=""
  railway add --service "$SERVICE"   # creates the service and links this directory to it
elif [[ $cli_rc -ne 0 ]]; then
  CLI_OUT=""
  warn "Could not read the service variables from Railway (network, or an expired login)."
  warn "Nothing was changed. Check 'railway whoami' and 'railway variable list', then re-run."
  exit 1
fi
CLI_OUT=""
# Read the service's variables once, before changing anything. If that fails
# (network, expired login), stop: taking "could not read" for "nothing set"
# would mint a new MCP_BEARER_TOKEN and cut off every client that holds the
# current one. On a first deploy the service was linked just above, so the
# list succeeds and is simply empty.
if ! EXISTING="$(railway variable list --kv 2>/dev/null)" ||
  { [[ -n $EXISTING ]] && ! grep -qE '^[A-Za-z_][A-Za-z0-9_]*=' <<<"$EXISTING"; }; then
  warn "Could not read the service variables from Railway (network, or an expired login)."
  warn "Nothing was changed. Check 'railway whoami' and 'railway variable list', then re-run."
  exit 1
fi

say "Settings the container checks at startup"
# The container refuses to start (entrypoint.sh, launch.py --check-config) on
# these, so stop here instead of deploying a server that cannot start.
if [[ -z ${REPORT_TZ:-} ]] && ! grep -qE '^REPORT_TZ=.' <<<"$EXISTING"; then
  warn "REPORT_TZ is required: the IANA time zone for the daily and weekly status, e.g. Europe/London."
  warn "Nothing was deployed. Export it and re-run:  REPORT_TZ=Europe/London ./scripts/deploy.sh  (your own zone)"
  exit 1
fi
if grep -qE '^GARMIN_DISABLED_TOOLS=.' <<<"$EXISTING"; then
  warn "GARMIN_DISABLED_TOOLS is set on Railway; it is no longer supported and the server would refuse to start."
  warn "Delete it:  railway variable delete GARMIN_DISABLED_TOOLS"
  warn "To hide tools, set GARMIN_ENABLED_TOOLS to the subset of config/enabled-tools.txt to keep. Nothing was deployed."
  exit 1
fi
if grep -qE '^GARMIN_ENABLED_TOOLS=.' <<<"$EXISTING"; then
  echo "GARMIN_ENABLED_TOOLS is set on Railway: it narrows config/enabled-tools.txt (every name must be in that file)."
fi
echo "OK."

say "Volume at /data (the Garmin login lives here)"
# Capture first: piped straight into `grep -q`, which exits at the first match,
# the CLI aborts writing to the closed pipe and pipefail fails the test.
VOLUMES="$(railway volume list 2>/dev/null || true)"
if grep -q '/data' <<<"$VOLUMES"; then
  echo "Already attached."
elif ! railway volume add --mount-path /data; then
  warn "Could not add the volume. In the Railway dashboard: open the $SERVICE service,"
  warn "create a volume with mount path /data, then re-run this script."
  exit 1
fi

say "Service variables"
# --skip-deploys everywhere: `railway up` below deploys once with all of them.
railway variable set --skip-deploys GARMIN_MCP_STATELESS_HTTP=true GARMINTOKENS=/data/tokens >/dev/null
if [[ -n ${REPORT_TZ:-} ]]; then
  railway variable set --skip-deploys "REPORT_TZ=$REPORT_TZ" >/dev/null
  echo "REPORT_TZ set to $REPORT_TZ."
else
  echo "REPORT_TZ already set; keeping it (export REPORT_TZ to change it)."
fi
TOKEN=""
# Shown wherever the token is not printed: the CLI can always read it back.
SHOW_TOKEN_CMD="railway variable list --kv | grep ^MCP_BEARER_TOKEN="
token_not_shown() {
  warn "Stopped early. The new MCP_BEARER_TOKEN is already on Railway; see it with: $SHOW_TOKEN_CMD" >&2
}
if [[ $ROTATE == false ]] && grep -q '^MCP_BEARER_TOKEN=' <<<"$EXISTING"; then
  echo "MCP_BEARER_TOKEN already set; keeping it (use --rotate-token to replace it)."
else
  TOKEN="$(openssl rand -hex 32)"
  # stdin, not argv, so the token never shows up in `ps`.
  printf '%s' "$TOKEN" | railway variable set MCP_BEARER_TOKEN --stdin --skip-deploys >/dev/null
  # From here the new token is live: if anything below stops the script (a
  # failed build, Ctrl-C) before "Done" shows it, say where to find it.
  trap token_not_shown EXIT
  echo "MCP_BEARER_TOKEN set."
fi

say "Building (a few minutes; stops here if the build fails)"
# --ci exits once the build finishes; plain `railway up` keeps streaming runtime
# logs forever and never reaches the summary below.
railway up --ci
echo "Build done; Railway is starting the container (~30 s)."

say "Public domain"
DOMAIN_OUT="$(railway domain 2>&1 || true)"
DOMAIN="$(grep -oE '[A-Za-z0-9.-]+\.up\.railway\.app' <<<"$DOMAIN_OUT" | head -n1 || true)"
if [[ -z $DOMAIN ]]; then
  DOMAIN="$(railway domain list 2>/dev/null | grep -oE '[A-Za-z0-9.-]+\.up\.railway\.app' | head -n1 || true)"
fi
if [[ -z $DOMAIN ]]; then
  echo "$DOMAIN_OUT"
  warn "Could not read the domain above; find it in the dashboard under the service's Settings > Networking."
  DOMAIN="<your-app>.up.railway.app"
fi

trap - EXIT
say "Done"
echo "  Service URL    : https://$DOMAIN   (for test-connector.sh and /readyz)"
echo "  Connector URL  : https://$DOMAIN/mcp"
if [[ -n $TOKEN && -t 1 ]]; then
  echo "  Header         : Authorization  =  Bearer $TOKEN"
  echo "  URL, no header : https://$DOMAIN/$TOKEN/mcp"
  echo "                   (use only if claude.ai shows no Request headers section)"
  echo
  warn "The token is shown once and saved nowhere. Put it in your password manager now."
elif [[ -n $TOKEN ]]; then
  # Output going to a file or a pipe (CI, a log) must not carry the token.
  echo "  Token          : a new MCP_BEARER_TOKEN is set, not printed because output is not a terminal."
  echo "                   See it with: $SHOW_TOKEN_CMD"
  echo "  URL, no header : https://$DOMAIN/<token>/mcp"
else
  echo "  Token unchanged: use the one you saved."
  echo "                   Lost it, or an earlier run stopped before showing it? See it with:"
  echo "                   $SHOW_TOKEN_CMD"
fi
cat <<'EOF'

Next, first deploy only (README step 2):
    railway ssh
    garmin-mcp-auth --token-path /data/tokens    # Garmin email, password, MFA code
    exit
Wait ~30 s while the server reloads the tokens, then add the connector on
claude.ai in a browser (README step 3). It shows up in the mobile app by itself.
EOF
if [[ $ROTATE == true ]]; then
  warn "Token rotated: remove the Garmin connector in claude.ai and add it again with the new token."
fi
