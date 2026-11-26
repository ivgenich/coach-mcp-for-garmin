#!/usr/bin/env bash
# deploy.sh: create (first run) or update the coach-mcp-for-garmin service on Railway.
# Safe to re-run: it reuses the project link, the /data volume and every secret
# already set.
#
#   GARMIN_AUTH=full REPORT_TZ=Europe/London ./scripts/deploy.sh
#                                   first deploy, full mode: you log in to Garmin once afterwards
#   GARMIN_AUTH=key REPORT_TZ=Europe/London ./scripts/deploy.sh
#                                   first deploy, key mode: asks for your Garmin Personal API key
#                                   (use your own IANA zone in both)
#   ./scripts/deploy.sh                           redeploy (keeps GARMIN_AUTH, REPORT_TZ, OWNER_SECRET
#                                                 and the Garmin key already set)
#   ./scripts/deploy.sh --rotate-secret           also replace OWNER_SECRET, which revokes every sign-in
#                                                 (not MCP_BEARER_TOKEN)
#   ./scripts/deploy.sh --rotate-token            also create or replace the optional MCP_BEARER_TOKEN
#                                                 and print it (only to a terminal; otherwise it says
#                                                 how to read it)
#   ./scripts/deploy.sh --set-garmin-key          key mode: also replace GARMIN_PERSONAL_API_KEY (asks
#                                                 for it; e.g. before Garmin's key expires)
#
# Environment: GARMIN_AUTH (required on the first deploy: full or key; when
# exported, it replaces the value on Railway; in a terminal the script asks
# when it is missing), REPORT_TZ (the same), DEPLOY_SERVICE (the Railway
# service name, and the project name on a first deploy; default
# coach-mcp-for-garmin; the folder's linked service must have this name),
# DEPLOY_WORKSPACE (optional: the exact Railway workspace ID or name for a
# first deploy; the CLI needs it when it runs outside a terminal and you have
# more than one workspace).
#
# Needs bash, the Railway CLI and python3 (to read the CLI's JSON). Before it
# changes anything it prints the linked project and service, and it stops when
# the folder's link belongs to another service or comes from a parent folder.
#
# Sign-in: the first run generates OWNER_SECRET, the secret you type on the
# server's sign-in page when Claude connects. It is never printed; read it with
#   railway variable list --kv | grep ^OWNER_SECRET=
# Secret values never go on a command line (the CLI reads them on stdin); a new
# bearer token is printed once, and only to a terminal. The Garmin Personal API
# key is read from the terminal without echo and never printed.
# Garmin email/password are never handled here; in full mode you type them once
# inside `railway ssh` (docs/garmin-login.md).
# The tools the server exposes are not set here: config/enabled-tools.txt is
# baked into the image and only read-only tools can be enabled there; only the
# PLAN_WRITES=on service variable adds the plan write tools (ext/launch.py).
set -euo pipefail

SERVICE="${DEPLOY_SERVICE:-coach-mcp-for-garmin}"

cd "$(dirname "${BASH_SOURCE[0]}")/.."

ROTATE_TOKEN=false
ROTATE_SECRET=false
SET_GARMIN_KEY=false
for arg in "$@"; do
  case "$arg" in
    --rotate-token) ROTATE_TOKEN=true ;;
    --rotate-secret) ROTATE_SECRET=true ;;
    --set-garmin-key) SET_GARMIN_KEY=true ;;
    *) echo "usage: [GARMIN_AUTH=full|key] [REPORT_TZ=Area/City] $0 [--rotate-secret] [--rotate-token] [--set-garmin-key]" >&2; exit 2 ;;
  esac
done

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m!!  %s\033[0m\n' "$*"; }

# GARMIN_AUTH has no default: full (the Garmin email/password login, every
# tool, unofficial interface) or key (Garmin's official Personal API key:
# read-only, fewer tools, less data). As in the server, spaces and case do
# not matter, and a wrong value is never shown. Checked before anything changes.
# Up to READY_WAIT_S seconds for $1/readyz to answer 200: the server has
# started and, in full mode, loaded the Garmin login. Silent; no curl, no wait.
# DEPLOY_READY_WAIT_S changes the limit (0: no wait).
READY_WAIT_S="${DEPLOY_READY_WAIT_S:-90}"
[[ $READY_WAIT_S =~ ^[0-9]{1,4}$ ]] || READY_WAIT_S=90
wait_ready() {
  command -v curl >/dev/null 2>&1 || return 1
  local end=$((SECONDS + READY_WAIT_S))
  while :; do
    curl -fsS -o /dev/null --max-time 5 "$1/readyz" 2>/dev/null && return 0
    ((SECONDS + 3 < end)) || return 1
    sleep 3
  done
}
lower_trim() { printf '%s' "$1" | tr -d '[:space:]' | tr '[:upper:]' '[:lower:]'; }
WANT_AUTH="$(lower_trim "${GARMIN_AUTH:-}")"
if [[ -n $WANT_AUTH && $WANT_AUTH != full && $WANT_AUTH != key ]]; then
  warn "GARMIN_AUTH must be full or key. Nothing was changed."
  exit 1
fi

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

# A new OWNER_SECRET: 30 characters from 23456789abcdefghjkmnpqrstuvwxyz (31
# symbols, no 0, 1, i, l or o; about 148 bits), printed as 6 groups of 5 joined
# by "-", so it can be typed on a phone. The pipe reads all 4096 bytes and
# the first 30 are cut in bash: a pipe closed early (a head -c 30 at its end)
# would make tr fail on the closed pipe, which pipefail turns into a failure.
# A short result, or one with fewer than 12 different characters (the server
# refuses that), is drawn again; after 5 tries it fails. It prints the secret
# on stdout, so call it only as SECRET="$(new_owner_secret)".
new_owner_secret() {
  local alphabet='23456789abcdefghjkmnpqrstuvwxyz' raw distinct i
  for _ in 1 2 3 4 5; do
    raw="$(head -c 4096 /dev/urandom | LC_ALL=C tr -dc '23456789abcdefghjkmnpqrstuvwxyz')" || raw=""
    raw="${raw:0:30}"
    distinct=0
    for ((i = 0; i < ${#alphabet}; i++)); do
      case "$raw" in *"${alphabet:i:1}"*) distinct=$((distinct + 1)) ;; esac
    done
    if [[ ${#raw} -eq 30 && $distinct -ge 12 ]]; then
      printf '%s-%s-%s-%s-%s-%s\n' "${raw:0:5}" "${raw:5:5}" "${raw:10:5}" "${raw:15:5}" "${raw:20:5}" "${raw:25:5}"
      return 0
    fi
  done
  return 1
}

# PUBLIC_URL the way the server reads it (ext/auth_config.py): surrounding
# spaces dropped, lowercase, no trailing slash, no default :443. The server's
# metadata names exactly this URL plus /mcp, and Claude needs the connector URL
# to match it. Fails, printing nothing, unless the result is an https origin
# (scheme, host, optional port): the server would refuse to start.
public_origin() {
  local url origin_re='^https://[^/?#@[:space:]]+$'
  url="$(LC_ALL=C tr '[:upper:]' '[:lower:]' <<<"$1")"
  url="${url#"${url%%[![:space:]]*}"}"
  url="${url%"${url##*[![:space:]]}"}"
  url="${url%/}"
  url="${url%:443}"
  [[ $url =~ $origin_re ]] || return 1
  printf '%s\n' "$url"
}

# Reads the Railway CLI's JSON with python3, so a reworded CLI message cannot
# change what the script decides. Only names, ids and yes/no come out. Text
# that holds secret values (the environment config) reaches it on stdin.
#   railway_json link FILE DIR    the folder the CLI takes its link from (DIR or
#                                 its closest parent in the CLI's config FILE),
#                                 the project id and the service id, one a line
#   railway_json project NAME ID  from `railway status --json`: project name,
#                                 project id, the id of service NAME, the name
#                                 of service ID, one a line
#   railway_json volume NAME      from `railway volume list --json`: exit 0 when
#                                 service NAME has a volume at /data, 3 when not
#   railway_json healthcheck ID   from `railway environment config --json`:
#                                 exit 0 when service ID checks /healthz, 3 when not
# Any other exit status means the JSON could not be read.
railway_json() {
  python3 -I -c '
import json, os, sys

def clean(value):
    return "".join(ch for ch in str(value or "") if ch.isprintable())

mode = sys.argv[1]
if mode == "link":
    try:
        with open(sys.argv[2], encoding="utf-8") as f:
            projects = json.load(f).get("projects") or {}
    except FileNotFoundError:
        projects = {}
    folder = sys.argv[3]
    while True:
        link = projects.get(folder)
        if isinstance(link, dict) and link.get("project"):
            print(clean(folder))
            print(clean(link.get("project")))
            print(clean(link.get("service")))
            break
        parent = os.path.dirname(folder)
        if parent == folder:
            break
        folder = parent
    sys.exit(0)
data = json.load(sys.stdin)
if mode == "project":
    services = [edge.get("node") or {} for edge in (data.get("services") or {}).get("edges") or []]
    print(clean(data.get("name")))
    print(clean(data.get("id")))
    print(next((clean(s.get("id")) for s in services if s.get("name") == sys.argv[2]), ""))
    print(next((clean(s.get("name")) for s in services if sys.argv[3] and s.get("id") == sys.argv[3]), ""))
elif mode == "volume":
    found = any(v.get("serviceName") == sys.argv[2] and v.get("mountPath") == "/data"
                for v in data.get("volumes") or [])
    sys.exit(0 if found else 3)
elif mode == "healthcheck":
    service = (data.get("services") or {}).get(sys.argv[2]) or {}
    sys.exit(0 if (service.get("deploy") or {}).get("healthcheckPath") == "/healthz" else 3)
else:
    sys.exit(2)
' "$@"
}

# GARMIN_AUTH and REPORT_TZ, when neither the environment nor Railway has
# them: asked for in a terminal (a wrong answer is never shown), else the
# script stops. ask_auth sets WANT_AUTH, ask_tz sets REPORT_TZ.
ask_auth() {
  local answer
  [[ -t 0 ]] || return 1
  while IFS= read -rp 'Garmin mode, full or key (see docs/garmin-login.md): ' answer; do
    answer="$(lower_trim "$answer")"
    if [[ $answer == full || $answer == key ]]; then
      WANT_AUTH="$answer"
      return 0
    fi
    echo "Type full or key."
  done
  return 1
}
ask_tz() {
  local answer
  [[ -t 0 ]] || return 1
  while IFS= read -rp 'Time zone, an IANA name such as Europe/London: ' answer; do
    answer="${answer#"${answer%%[![:space:]]*}"}"
    answer="${answer%"${answer##*[![:space:]]}"}"
    if [[ -n $answer ]] && valid_tz "$answer"; then
      REPORT_TZ="$answer"
      return 0
    fi
    echo "That is not a time zone name on this machine (match the case, e.g. Europe/London)."
  done
  return 1
}
# $1: what the stop message says about changes ("Nothing was changed." or "Nothing was deployed.").
stop_no_auth() {
  warn "GARMIN_AUTH is required: full (log in to Garmin once with your email and password; every tool)"
  warn "or key (a Garmin Personal API key; fewer tools and less data). See docs/garmin-login.md."
  warn "$1 Export it and re-run:  GARMIN_AUTH=full REPORT_TZ=Europe/London ./scripts/deploy.sh"
  exit 1
}
stop_no_tz() {
  warn "REPORT_TZ is required: the IANA time zone for the daily and weekly status, e.g. Europe/London."
  # The mode is full or key here (checked before), so it is safe to show; on a
  # first deploy it is not on Railway yet, so the re-run needs it again.
  warn "$1 Export it and re-run:  GARMIN_AUTH=$2 REPORT_TZ=Europe/London ./scripts/deploy.sh  (your own zone)"
  exit 1
}

say "Railway CLI"
if ! command -v railway >/dev/null 2>&1; then
  warn "Install the Railway CLI first:  brew install railway   (or: npm i -g @railway/cli)"
  exit 1
fi
railway --version
if ! python3 -I -c 'import json' >/dev/null 2>&1; then
  warn "deploy.sh needs python3 to read the Railway CLI's JSON. Install it (macOS: xcode-select --install;"
  warn "Linux and WSL: your package manager's python3), then re-run. Nothing was changed."
  exit 1
fi
# With these set the CLI ignores the folder's link, so the checks below would
# look at the wrong thing.
for name in RAILWAY_TOKEN RAILWAY_PROJECT_ID RAILWAY_ENVIRONMENT_ID RAILWAY_SERVICE_ID; do
  if [[ -n ${!name:-} ]]; then
    warn "$name is set: the Railway CLI then ignores this folder's link. Unset it (unset $name) and re-run. Nothing was changed."
    exit 1
  fi
done

say "Login"
railway whoami >/dev/null 2>&1 || railway login
railway whoami

say "Project and service"
# Which project and service this folder is linked to, read before anything
# changes. The CLI keeps folder links in its config file and, for a folder
# with no link of its own, uses the closest parent folder's: then `railway up`
# would upload that whole parent folder, so the script stops. It also stops
# when the linked service is not $SERVICE. Any failure to read (network,
# expired login) stops too: taking it for "not linked yet" would create a
# second project or service. Text that can hold secret values is matched
# through a pipe, never a here-string (<<<): bash 3.2, the macOS bash, writes
# here-string text to a temporary file.
REPO_DIR="$(pwd -P)"
case "$(printf '%s' "${RAILWAY_ENV:-}" | tr '[:upper:]' '[:lower:]')" in
  staging) LINK_FILE="$HOME/.railway/config-staging.json" ;;
  dev | develop) LINK_FILE="$HOME/.railway/config-dev.json" ;;
  *) LINK_FILE="$HOME/.railway/config.json" ;;
esac
line_of() { sed -n "$1p" < <(printf '%s\n' "$2"); }
# Sets LINK_DIR (empty: no link), LINK_PROJECT and LINK_SERVICE (ids).
read_link() {
  local out
  out="$(railway_json link "$LINK_FILE" "$REPO_DIR")" || return 1
  LINK_DIR="$(line_of 1 "$out")"
  LINK_PROJECT="$(line_of 2 "$out")"
  LINK_SERVICE="$(line_of 3 "$out")"
}
# Sets PROJECT_NAME, SERVICE_ID (of the service named $SERVICE; empty: none)
# and LINKED_NAME (the name of the linked service; empty: none or not found).
read_project() {
  local out
  out="$(railway status --json 2>/dev/null | railway_json project "$SERVICE" "$LINK_SERVICE")" || return 1
  PROJECT_NAME="$(line_of 1 "$out")"
  [[ "$(line_of 2 "$out")" == "$LINK_PROJECT" ]] || return 1
  SERVICE_ID="$(line_of 3 "$out")"
  LINKED_NAME="$(line_of 4 "$out")"
}
# After init, add or service link: the link must now be this folder's own.
reread_link() { read_link && [[ $LINK_DIR == "$REPO_DIR" ]] && read_project; }
cannot_read_project() {
  warn "Could not read the linked Railway project (network, an expired login, or a Railway CLI too old for this script)."
  warn "Nothing was changed. Check 'railway whoami' and 'railway status' (and update the CLI), then re-run."
  exit 1
}
if ! read_link; then
  warn "Could not read the Railway CLI's folder links in $LINK_FILE. Nothing was changed."
  exit 1
fi
if [[ -n $LINK_DIR && $LINK_DIR != "$REPO_DIR" ]]; then
  warn "This folder has no Railway link of its own: the CLI would use the link of $LINK_DIR"
  warn "and upload that whole folder. Nothing was changed. Give this folder its own link, then re-run:"
  warn "  railway link                      (an existing project; pick the $SERVICE service)"
  warn "  railway init --name $SERVICE   (a new project)"
  exit 1
fi
SERVICE_ID=""
FRESH=false   # true: no service named $SERVICE yet, so nothing is set on Railway
if [[ -n $LINK_DIR ]]; then
  read_project || cannot_read_project
  echo "Linked project: $PROJECT_NAME"
  if [[ -n $LINK_SERVICE ]]; then
    echo "Linked service: ${LINKED_NAME:-(not found in the project)}"
    if [[ $LINKED_NAME != "$SERVICE" ]]; then
      if [[ -n $LINKED_NAME ]]; then
        warn "This folder is linked to the service '$LINKED_NAME', but the script deploys '$SERVICE' (DEPLOY_SERVICE)."
        warn "Nothing was changed. If '$LINKED_NAME' is your server, re-run with DEPLOY_SERVICE='$LINKED_NAME' in front."
      else
        warn "This folder is linked to a service that is no longer in the project. Nothing was changed."
      fi
      warn "To use the $SERVICE service instead:  railway service link $SERVICE   (or railway link for another project)"
      exit 1
    fi
  else
    echo "Linked service: none"
  fi
  [[ -z $LINK_SERVICE ]] || SERVICE_ID="$LINK_SERVICE"
  [[ -n $SERVICE_ID ]] || FRESH=true
  if [[ -n ${DEPLOY_WORKSPACE:-} ]]; then
    echo "This folder is already linked to a Railway project; DEPLOY_WORKSPACE is not used."
  fi
else
  echo "This folder is not linked to a Railway project: a new project named $SERVICE will be created."
  echo "(Already deployed from another folder? Stop with Ctrl-C and run railway link here first.)"
  FRESH=true
fi
# A first deploy has nothing on Railway yet, so check its inputs before
# anything is created.
if [[ $FRESH == true ]]; then
  if [[ -z $WANT_AUTH ]] && ! ask_auth; then
    stop_no_auth "Nothing was changed."
  fi
  if [[ -z ${REPORT_TZ:-} ]] && ! ask_tz; then
    stop_no_tz "Nothing was changed." "$WANT_AUTH"
  fi
  if [[ $WANT_AUTH == key && ! -t 0 ]]; then
    warn "GARMIN_AUTH=key needs GARMIN_PERSONAL_API_KEY. Run this script in a terminal (it asks for the key"
    warn "and never shows it). Nothing was changed."
    exit 1
  fi
fi
if [[ -z $LINK_DIR ]]; then
  INIT_ARGS=(--name "$SERVICE")
  if [[ -n ${DEPLOY_WORKSPACE:-} ]]; then
    INIT_ARGS+=(--workspace "$DEPLOY_WORKSPACE")
  fi
  railway init "${INIT_ARGS[@]}"   # creates the project and links this folder to it
fi
if [[ -z $LINK_DIR || -z $LINK_SERVICE ]]; then
  reread_link || cannot_read_project
  if [[ -n $SERVICE_ID ]]; then
    railway service link "$SERVICE"   # the service exists: link this folder to it
  else
    railway add --service "$SERVICE"  # creates the service and links this folder to it
  fi
  reread_link || cannot_read_project
  if [[ -z $SERVICE_ID || $LINKED_NAME != "$SERVICE" ]]; then
    warn "Could not confirm that this folder is now linked to the $SERVICE service. Check 'railway status', then re-run."
    exit 1
  fi
  echo "Linked project: $PROJECT_NAME"
  echo "Linked service: $SERVICE"
fi
# Read the service's variables once, before changing anything. If that fails
# (network, expired login), stop: taking "could not read" for "nothing set"
# would mint a new OWNER_SECRET, which signs out every client. On a first
# deploy the service was created just above, so the list succeeds and is
# simply empty.
if ! EXISTING="$(railway variable list --kv --service "$SERVICE" 2>/dev/null)" ||
  { [[ -n $EXISTING ]] && ! grep -qE '^[A-Za-z_][A-Za-z0-9_]*=' < <(printf '%s\n' "$EXISTING"); }; then
  warn "Could not read the service variables from Railway (network, an expired login, or a Railway CLI too old for this script)."
  warn "Nothing was changed. Check 'railway whoami' and 'railway variable list' (and update the CLI), then re-run."
  exit 1
fi

say "Settings the container checks at startup"
# The container refuses to start (entrypoint.sh, launch.py --check-config) on
# these, so stop here instead of deploying a server that cannot start.
RAILWAY_AUTH="$(lower_trim "$(sed -n 's/^GARMIN_AUTH=//p' < <(printf '%s\n' "$EXISTING"))")"
AUTH_MODE="${WANT_AUTH:-$RAILWAY_AUTH}"
if [[ -z $AUTH_MODE ]]; then
  ask_auth || stop_no_auth "Nothing was deployed."
  AUTH_MODE="$WANT_AUTH"
fi
if [[ $AUTH_MODE != full && $AUTH_MODE != key ]]; then
  warn "GARMIN_AUTH on Railway is neither full nor key (the value is not shown)."
  warn "Re-run with GARMIN_AUTH=full or GARMIN_AUTH=key exported. Nothing was deployed."
  exit 1
fi
if [[ -z ${REPORT_TZ:-} ]] && ! grep -qE '^REPORT_TZ=.*[^[:space:]]' < <(printf '%s\n' "$EXISTING"); then
  ask_tz || stop_no_tz "Nothing was deployed." "$AUTH_MODE"
fi
# Key mode serves only the tools of config/enabled-tools-key.txt, and the
# server refuses to start when GARMIN_ENABLED_TOOLS names any other (launch.py
# resolve_tools). Checked here, before the key prompt, as the server reads the
# variable: split on commas, each entry trimmed and lower-cased, empty entries
# skipped. An entry is shown only when it has the shape of a tool name (as in
# the server): anything else may be a secret pasted into the wrong variable.
# The comparisons stay in bash, so no entry reaches a command line.
if [[ $AUTH_MODE == key ]]; then
  ENABLED_TOOLS="$(sed -n 's/^GARMIN_ENABLED_TOOLS=//p' < <(printf '%s\n' "$EXISTING"))"
  if [[ -n ${ENABLED_TOOLS//[[:space:]]/} ]]; then
    if ! KEY_TOOLS="$(sed -e 's/#.*//' -e 's/[[:space:]]//g' -e '/^$/d' config/enabled-tools-key.txt)" ||
      [[ -z $KEY_TOOLS ]]; then
      ENABLED_TOOLS=""
      warn "Could not read config/enabled-tools-key.txt (run this script from a full copy of the repository). Nothing was deployed."
      exit 1
    fi
    FULL_TOOLS="$(sed -e 's/#.*//' -e 's/[[:space:]]//g' -e '/^$/d' config/enabled-tools.txt 2>/dev/null || true)"
    NOT_IN_KEY=""
    HIDDEN_ENTRY=false
    ENTRY_COUNT=0
    REST="$ENABLED_TOOLS,"
    while [[ -n $REST ]]; do
      ENTRY="${REST%%,*}"
      REST="${REST#*,}"
      ENTRY="${ENTRY#"${ENTRY%%[![:space:]]*}"}"
      ENTRY="${ENTRY%"${ENTRY##*[![:space:]]}"}"
      [[ -n $ENTRY ]] || continue
      ENTRY_COUNT=$((ENTRY_COUNT + 1))
      # The shape test runs before lower-casing: an unknown entry with an
      # upper-case letter is never shown (a mixed-case secret lower-cased looks
      # like a name).
      SHOWABLE=false
      if [[ ${#ENTRY} -le 200 && $ENTRY =~ ^[a-z][a-z0-9]*(_[a-z][a-z0-9]*)+$ ]]; then
        SHOWABLE=true
      fi
      ENTRY="$(printf '%s' "$ENTRY" | LC_ALL=C tr '[:upper:]' '[:lower:]')"
      [[ $'\n'"$KEY_TOOLS"$'\n' == *$'\n'"$ENTRY"$'\n'* ]] && continue
      # A known tool name is shown however it was typed (as in the server).
      [[ $'\n'"$FULL_TOOLS"$'\n' == *$'\n'"$ENTRY"$'\n'* ]] && SHOWABLE=true
      if [[ $SHOWABLE == true ]]; then
        [[ " $NOT_IN_KEY " == *" $ENTRY "* ]] || NOT_IN_KEY="${NOT_IN_KEY:+$NOT_IN_KEY }$ENTRY"
      else
        HIDDEN_ENTRY=true
      fi
    done
    ENABLED_TOOLS=""
    ENTRY=""
    if [[ $ENTRY_COUNT -eq 0 ]]; then
      warn "GARMIN_ENABLED_TOOLS on Railway names no tools (only commas or spaces): the server would refuse to start."
      warn "Delete it (railway variable delete GARMIN_ENABLED_TOOLS) to keep every key-mode tool. Nothing was deployed."
      exit 1
    fi
    if [[ -n $NOT_IN_KEY || $HIDDEN_ENTRY == true ]]; then
      if [[ -n $NOT_IN_KEY ]]; then
        warn "GARMIN_ENABLED_TOOLS on Railway names tools that GARMIN_AUTH=key does not have: ${NOT_IN_KEY// /, }."
      fi
      if [[ $HIDDEN_ENTRY == true ]]; then
        warn "GARMIN_ENABLED_TOOLS on Railway has an entry that is not a tool name (it is not shown)."
      fi
      warn "The server would refuse to start. Remove them from GARMIN_ENABLED_TOOLS (key mode can only narrow"
      warn "config/enabled-tools-key.txt; railway variable delete GARMIN_ENABLED_TOOLS keeps every key-mode tool),"
      warn "or keep GARMIN_AUTH=full. Nothing was deployed."
      exit 1
    fi
  fi
fi
# Key mode needs GARMIN_PERSONAL_API_KEY. It is asked for here, before
# anything below changes, without echo, and only from a terminal. As in the
# server, a value that is empty or only spaces counts as unset.
HAD_GARMIN_KEY=false
if grep -qE '^GARMIN_PERSONAL_API_KEY=.*[^[:space:]]' < <(printf '%s\n' "$EXISTING"); then
  HAD_GARMIN_KEY=true
fi
if [[ $SET_GARMIN_KEY == true && $AUTH_MODE != key ]]; then
  warn "--set-garmin-key is only for GARMIN_AUTH=key. Nothing was deployed."
  exit 1
fi
if [[ $AUTH_MODE == full && $HAD_GARMIN_KEY == true ]]; then
  # The server refuses an unused secret (launch.py --check-config).
  warn "GARMIN_AUTH=full does not use GARMIN_PERSONAL_API_KEY, and the server does not start while it is set."
  warn "Delete it, then re-run:  railway variable delete GARMIN_PERSONAL_API_KEY"
  warn "Also delete the key on Garmin Connect if you no longer use it. Nothing was deployed."
  exit 1
fi
NEW_GARMIN_KEY=false
GARMIN_KEY=""
if [[ $AUTH_MODE == key && ( $HAD_GARMIN_KEY == false || $SET_GARMIN_KEY == true ) ]]; then
  if [[ ! -t 0 ]]; then
    warn "GARMIN_AUTH=key needs GARMIN_PERSONAL_API_KEY. Run this script in a terminal (it asks for the key"
    warn "and never shows it), or set the variable in the Railway dashboard. Nothing was deployed."
    exit 1
  fi
  IFS= read -rsp 'Paste your Garmin Personal API key (not shown), then press Enter: ' GARMIN_KEY || GARMIN_KEY=""
  printf '\n' >&2
  # As in the server: surrounding spaces are dropped; the key itself has none.
  GARMIN_KEY="${GARMIN_KEY#"${GARMIN_KEY%%[![:space:]]*}"}"
  GARMIN_KEY="${GARMIN_KEY%"${GARMIN_KEY##*[![:space:]]}"}"
  if [[ -z $GARMIN_KEY || $GARMIN_KEY =~ [[:space:]] || ${#GARMIN_KEY} -gt 512 ]]; then
    GARMIN_KEY=""
    warn "That key is empty, has spaces in it or is too long. Copy it again from Garmin Connect and re-run. Nothing was deployed."
    exit 1
  fi
  # The server refuses a key under 20 characters too: that is a paste that did not land, not a key.
  if [[ ${#GARMIN_KEY} -lt 20 ]]; then
    KEY_LEN=${#GARMIN_KEY}
    GARMIN_KEY=""
    warn "That key is too short ($KEY_LEN characters; a Garmin key is much longer): the paste did not land. Copy it again from Garmin Connect and re-run. Nothing was deployed."
    exit 1
  fi
  NEW_GARMIN_KEY=true
fi
if [[ -n $WANT_AUTH && -n $RAILWAY_AUTH && $WANT_AUTH != "$RAILWAY_AUTH" ]]; then
  # The old value is shown only when it is a valid mode: a pasted key must
  # never reach the log.
  if [[ $RAILWAY_AUTH == full || $RAILWAY_AUTH == key ]]; then
    echo "GARMIN_AUTH changes from $RAILWAY_AUTH to $WANT_AUTH."
  else
    echo "GARMIN_AUTH changes to $WANT_AUTH (the value on Railway was neither full nor key; it is not shown)."
  fi
  if [[ $RAILWAY_AUTH == full && $WANT_AUTH == key ]]; then
    echo "The Garmin login tokens stay on the volume until you delete them:  railway ssh -- rm -rf /data/tokens"
  fi
fi
if grep -qE '^GARMIN_DISABLED_TOOLS=.' < <(printf '%s\n' "$EXISTING"); then
  warn "GARMIN_DISABLED_TOOLS is set on Railway; it is no longer supported and the server would refuse to start."
  warn "Delete it:  railway variable delete GARMIN_DISABLED_TOOLS"
  warn "To hide tools, set GARMIN_ENABLED_TOOLS to the subset of config/enabled-tools.txt to keep. Nothing was deployed."
  exit 1
fi
if grep -qE '^GARMIN_ENABLED_TOOLS=.' < <(printf '%s\n' "$EXISTING"); then
  if [[ $AUTH_MODE == key ]]; then
    echo "GARMIN_ENABLED_TOOLS is set on Railway: it narrows config/enabled-tools-key.txt (key mode; every name must be in that file)."
  else
    echo "GARMIN_ENABLED_TOOLS is set on Railway: it narrows config/enabled-tools.txt (every name must be in that file)."
  fi
fi
# PUBLIC_URL (optional) is the server's public https origin when that is not
# the Railway domain, e.g. a custom domain. The sign-in sends Claude to it, so
# it must be https and an origin only (no path). Unset or blank, the server
# uses https://$RAILWAY_PUBLIC_DOMAIN.
PUBLIC_URL="$(sed -n 's/^PUBLIC_URL=//p' < <(printf '%s\n' "$EXISTING"))"
if [[ -n ${PUBLIC_URL//[[:space:]]/} ]]; then
  if ! PUBLIC_URL="$(public_origin "$PUBLIC_URL")"; then
    warn "PUBLIC_URL on Railway must be this server's https origin only, e.g. https://coach.example.com (no path)."
    warn "Nothing was deployed. Fix it (railway variable set PUBLIC_URL=https://...) or delete it (railway variable delete PUBLIC_URL), then re-run."
    exit 1
  fi
  echo "PUBLIC_URL is set on Railway: $PUBLIC_URL"
else
  PUBLIC_URL=""
fi
echo "OK."

say "Volume at /data (the Garmin login and the sign-ins live here)"
# Only a volume of $SERVICE counts: another service's /data volume is not this one's.
# `volume --service` takes the service id.
vol_rc=0
railway volume list --json 2>/dev/null | railway_json volume "$SERVICE" || vol_rc=$?
if [[ $vol_rc -eq 0 ]]; then
  echo "Already attached."
elif [[ $vol_rc -ne 3 ]]; then
  warn "Could not read the volumes from Railway (network, an expired login, or a Railway CLI too old for this script). Nothing was deployed."
  exit 1
elif ! railway volume --service "$SERVICE_ID" add --mount-path /data; then
  warn "Could not add the volume. In the Railway dashboard: open the $SERVICE service,"
  warn "create a volume with mount path /data, then re-run this script."
  exit 1
fi

say "Public domain"
# Before the build: the server refuses to start without its public URL (the
# sign-in names it to Claude), which is PUBLIC_URL or else the Railway domain.
DOMAIN=""
if [[ -n $PUBLIC_URL ]]; then
  echo "Using PUBLIC_URL; the Railway domain is not needed."
else
  # The domain the server will use is RAILWAY_PUBLIC_DOMAIN: take it from the
  # variable list when it is there, else from `railway domain`, which creates
  # a *.up.railway.app domain when the service has none.
  DOMAIN="$(sed -n 's/^RAILWAY_PUBLIC_DOMAIN=//p' < <(printf '%s\n' "$EXISTING"))"
  [[ $DOMAIN =~ ^[A-Za-z0-9.-]+$ ]] || DOMAIN=""
  if [[ -z $DOMAIN ]]; then
    # The host name is taken from the JSON reply by its shape, not from any message.
    DOMAIN_OUT="$(railway domain --service "$SERVICE" --json 2>&1 || true)"
    DOMAIN="$(grep -oE '[A-Za-z0-9.-]+\.up\.railway\.app' <<<"$DOMAIN_OUT" | head -n1 || true)"
    if [[ -z $DOMAIN ]]; then
      DOMAIN="$(railway domain list --service "$SERVICE" --json 2>/dev/null | grep -oE '[A-Za-z0-9.-]+\.up\.railway\.app' | head -n1 || true)"
    fi
    if [[ -z $DOMAIN ]]; then
      echo "$DOMAIN_OUT"
      warn "No public domain: create one in the dashboard (Settings > Networking > Generate Domain) or set PUBLIC_URL, then re-run. Nothing was deployed."
      warn "PUBLIC_URL is for a custom domain:  railway variable set PUBLIC_URL=https://coach.example.com"
      exit 1
    fi
  fi
  echo "$DOMAIN"
fi

say "Service variables"
# --skip-deploys everywhere: `railway up` below deploys once with all of them.
# The variables of docs/deploy-railway.md (Settings). GARMINTOKENS,
# GARMINTOKENS_BASE64 and GARMIN_MCP_STATELESS_HTTP are baked into the image,
# so this script never sets them.
if [[ -n $WANT_AUTH ]]; then
  railway variable set --skip-deploys "GARMIN_AUTH=$WANT_AUTH" --service "$SERVICE" >/dev/null
  echo "GARMIN_AUTH set to $WANT_AUTH."
else
  echo "GARMIN_AUTH already set to $AUTH_MODE; keeping it (export GARMIN_AUTH to change it)."
fi
if [[ -n ${REPORT_TZ:-} ]]; then
  railway variable set --skip-deploys "REPORT_TZ=$REPORT_TZ" --service "$SERVICE" >/dev/null
  echo "REPORT_TZ set to $REPORT_TZ."
else
  echo "REPORT_TZ already set; keeping it (export REPORT_TZ to change it)."
fi

TOKEN=""
# Shown wherever the token is not printed: the CLI can always read it back.
SHOW_TOKEN_CMD="railway variable list --kv | grep ^MCP_BEARER_TOKEN="
# Set as an EXIT trap once a secret was replaced: if anything below stops the
# script (a failed build, Ctrl-C) before "Done", say what already changed.
stopped_early() {
  if [[ $ROTATE_SECRET == true ]]; then
    warn "Stopped early. The new OWNER_SECRET is already on Railway, but the old one works until a new deployment starts: fix the problem and run ./scripts/deploy.sh again." >&2
  fi
  if [[ -n $TOKEN ]]; then
    warn "Stopped early. The new MCP_BEARER_TOKEN is already on Railway; see it with: $SHOW_TOKEN_CMD" >&2
  fi
  if [[ $NEW_GARMIN_KEY == true && $HAD_GARMIN_KEY == true ]]; then
    warn "Stopped early. The new GARMIN_PERSONAL_API_KEY is already on Railway; the running server uses the old one until a new deployment starts: fix the problem and run ./scripts/deploy.sh again." >&2
  fi
}

# GARMIN_PERSONAL_API_KEY (key mode): typed above, stored on Railway through
# stdin, never printed and never on a command line.
if [[ $NEW_GARMIN_KEY == true ]]; then
  printf '%s' "$GARMIN_KEY" | railway variable set GARMIN_PERSONAL_API_KEY --stdin --skip-deploys --service "$SERVICE" >/dev/null
  GARMIN_KEY=""
  [[ $HAD_GARMIN_KEY == false ]] || trap stopped_early EXIT
  echo "GARMIN_PERSONAL_API_KEY set (not shown)."
elif [[ $AUTH_MODE == key ]]; then
  echo "GARMIN_PERSONAL_API_KEY already set; keeping it (use --set-garmin-key to replace it, e.g. before Garmin's key expires)."
fi

# OWNER_SECRET: what you type on the sign-in page. Generated here, stored on
# Railway through stdin, never printed and never on a command line.
OWNER_SECRET_WAS_MISSING=false
if ! grep -qE '^OWNER_SECRET=.' < <(printf '%s\n' "$EXISTING"); then
  OWNER_SECRET_WAS_MISSING=true
fi
if [[ $OWNER_SECRET_WAS_MISSING == true || $ROTATE_SECRET == true ]]; then
  if ! SECRET="$(new_owner_secret)"; then
    warn "Could not generate OWNER_SECRET (reading /dev/urandom failed). Nothing was deployed."
    exit 1
  fi
  printf '%s' "$SECRET" | railway variable set OWNER_SECRET --stdin --skip-deploys --service "$SERVICE" >/dev/null
  SECRET=""
  [[ $ROTATE_SECRET == false ]] || trap stopped_early EXIT
  echo "OWNER_SECRET set (not shown). See it with: railway variable list --kv | grep ^OWNER_SECRET="
else
  echo "OWNER_SECRET already set; keeping it (use --rotate-secret to replace it, which revokes every sign-in)."
fi

# MCP_BEARER_TOKEN (optional): a static header token for Claude Code or CI.
# Created or replaced only with --rotate-token.
HAD_TOKEN=false
if grep -qE '^MCP_BEARER_TOKEN=.' < <(printf '%s\n' "$EXISTING"); then
  HAD_TOKEN=true
fi
if [[ $ROTATE_TOKEN == true ]]; then
  TOKEN="$(openssl rand -hex 32)"
  # stdin, not argv, so the token never shows up in `ps`.
  printf '%s' "$TOKEN" | railway variable set MCP_BEARER_TOKEN --stdin --skip-deploys --service "$SERVICE" >/dev/null
  # From here the new token is live: if anything below stops the script (a
  # failed build, Ctrl-C) before "Done" shows it, say where to find it.
  trap stopped_early EXIT
  echo "MCP_BEARER_TOKEN set."
elif [[ $HAD_TOKEN == true ]]; then
  echo "MCP_BEARER_TOKEN already set; keeping it (use --rotate-token to replace it)."
else
  echo "MCP_BEARER_TOKEN not set (optional; --rotate-token creates one for Claude Code or CI)."
fi

say "Healthcheck path"
# Railway waits for /healthz before a new deployment goes live. Never /readyz: in full mode it stays 503 until the
# Garmin login, so a first deployment would never go live. Best effort (the
# dashboard can set it too), set before the build so the build's deployment
# uses it. Only $SERVICE's own setting counts. Never printed: the environment
# config also holds variable values.
healthcheck_is_set() {
  railway environment config --json 2>/dev/null | railway_json healthcheck "$SERVICE_ID"
}
if healthcheck_is_set; then
  echo "Already /healthz."
else
  # Railway CLI 5.70 applies this edit only when it runs in a terminal: it
  # asks "Apply changes now?" (Enter says yes). Without one it answers "No
  # changes to apply" and still exits 0, so the setting is read back instead
  # of trusting the exit code. The --json reply
  # holds no variable values, so it is shown.
  railway environment edit --service-config "$SERVICE" deploy.healthcheckPath /healthz \
    --message "coach-mcp-for-garmin: healthcheck /healthz" --json || true
  if healthcheck_is_set; then
    echo "Set to /healthz."
  else
    warn "Could not set it (optional). Dashboard: the $SERVICE service > Settings > Deploy > Healthcheck Path: /healthz"
  fi
fi

say "Building and deploying (a few minutes; stops here if the build or the deploy fails)"
# --ci streams the build log, then waits for the deployment: it exits 0 once
# Railway reports it live (after the /healthz check when that is set) and 1
# when it failed or crashed. Plain `railway up` keeps streaming runtime logs
# forever and never reaches the summary below.
railway up --ci --service "$SERVICE"
echo "Deployed: Railway reports the new deployment as live."

trap - EXIT
say "Done"
if [[ -n $PUBLIC_URL ]]; then
  echo "  Service URL    : $PUBLIC_URL   (for test-connector.sh and /readyz)"
  echo "  Connector URL  : $PUBLIC_URL/mcp"
else
  echo "  Service URL    : https://$DOMAIN   (for test-connector.sh and /readyz)"
  echo "  Connector URL  : https://$DOMAIN/mcp"
fi
echo "                   (paste the connector URL into Claude exactly as shown, with no trailing slash)"
echo "  Sign-in        : claude.ai: Sign in now + Register automatically, then type the owner"
echo "                   secret on the page that opens. See the secret with:"
echo "                   railway variable list --kv | grep ^OWNER_SECRET="
if [[ -n $TOKEN && -t 1 ]]; then
  echo "  Header         : Authorization  =  Bearer $TOKEN"
  echo "                   (optional static bearer for Claude Code or CI; claude.ai does not need it)"
  echo
  warn "The token is shown once and saved nowhere. Put it in your password manager now."
elif [[ -n $TOKEN ]]; then
  # Output going to a file or a pipe (CI, a log) must not carry the token.
  echo "  Token          : a new MCP_BEARER_TOKEN is set, not printed because output is not a terminal."
  echo "                   See it with: $SHOW_TOKEN_CMD"
fi
if [[ $HAD_TOKEN == true && $ROTATE_TOKEN == false && $OWNER_SECRET_WAS_MISSING == true ]]; then
  # The first run after the update from the token-only version.
  warn "The https://<domain>/<token>/mcp form is gone (410). If you ever used it, the token may be in Railway's HTTP logs: run ./scripts/deploy.sh --rotate-token, or delete it: railway variable delete MCP_BEARER_TOKEN"
fi
# Full mode: offer the one-time Garmin login when this deploy is the first in
# full mode, and only in a terminal (the login asks for the email, password
# and MFA code). `railway ssh COMMAND` runs it on the server with a terminal.
# A failed or skipped login does not stop the script: the notes below follow.
LOGIN_DONE=false
LOGIN_CMD=(garmin-mcp-auth --token-path /data/tokens)
if [[ $AUTH_MODE == full && $RAILWAY_AUTH != full && -t 0 && -t 1 ]]; then
  echo
  echo "Full mode needs a one-time Garmin login on the server (docs/garmin-login.md): you type your"
  echo "Garmin email, password and MFA code into the server's login, which keeps only the tokens."
  answer=""
  IFS= read -rp 'Run the Garmin login now? [Y/n] ' answer || answer=n
  case "$(lower_trim "$answer")" in
    "" | y | yes)
      if railway ssh --service "$SERVICE" -- "${LOGIN_CMD[@]}"; then
        LOGIN_DONE=true
      else
        warn "The Garmin login did not finish. Run it again with the steps below."
      fi
      ;;
  esac
fi
# Key mode, or full mode after the login: wait for /readyz (in a terminal
# only) so "add the connector" comes when the server can answer.
READY=false
BASE_URL="${PUBLIC_URL:-${DOMAIN:+https://$DOMAIN}}"
if [[ -n $BASE_URL && -t 1 && $READY_WAIT_S -gt 0 && ($LOGIN_DONE == true || $AUTH_MODE == key) ]]; then
  echo
  echo "Waiting for $BASE_URL/readyz (up to $READY_WAIT_S s) ..."
  if wait_ready "$BASE_URL"; then
    READY=true
    echo "The server is ready."
  else
    warn "No 200 from /readyz yet; give it another minute (docs/troubleshooting.md if it stays so)."
  fi
fi
if [[ $READY == true ]]; then
  WAIT_THEN="Now add"
else
  WAIT_THEN="Wait ~30 s while the server loads, then add"
fi
if [[ $LOGIN_DONE == true ]]; then
  echo
  echo "Garmin login done. $WAIT_THEN the connector on claude.ai in a browser"
  echo "(docs/connect-claude.md). It shows up in the mobile app by itself."
elif [[ $AUTH_MODE == full ]]; then
  cat <<'EOF'

Next, first deploy only (docs/garmin-login.md):
    railway ssh
    garmin-mcp-auth --token-path /data/tokens    # Garmin email, password, MFA code
    exit
Wait ~30 s while the server reloads the tokens, then add the connector on
claude.ai in a browser (docs/connect-claude.md). It shows up in the mobile app by itself.
EOF
else
  echo
  echo "Next: key mode needs no Garmin login. $WAIT_THEN the connector on"
  echo "claude.ai in a browser (docs/connect-claude.md). It shows up in the mobile app by itself."
  echo "Before Garmin's key expires, create a new key and run:"
  echo "    ./scripts/deploy.sh --set-garmin-key"
fi
if [[ $ROTATE_SECRET == true ]]; then
  warn "Owner secret rotated: every sign-in is revoked when the new deployment starts. Every client must sign in again:"
  warn "claude.ai: Customize > Connectors > Garmin > Reconnect, then type the new secret. Claude Code: /mcp, sign in again."
  if [[ $HAD_TOKEN == true && $ROTATE_TOKEN == false ]]; then
    warn "MCP_BEARER_TOKEN still works: rotate it too (--rotate-token) or delete it (railway variable delete MCP_BEARER_TOKEN)."
  fi
fi
if [[ $ROTATE_TOKEN == true ]]; then
  warn "Token rotated: update the Authorization header wherever you use it (Claude Code: remove the server and add it again)."
fi
if [[ $NEW_GARMIN_KEY == true && $HAD_GARMIN_KEY == true ]]; then
  warn "Garmin key replaced: once the new deployment is up, delete the old key on Garmin Connect (where you created it)."
fi
