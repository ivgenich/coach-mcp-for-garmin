#!/usr/bin/env bash
# Set up, check, back up, restore and upgrade the Docker Compose server behind
# a Cloudflare Tunnel (docs/deploy-docker.md). Run it from a clone of this
# repository, checked out at a release tag. Nothing is pulled from a registry
# but the pinned images compose.yaml and the Dockerfile name: the server is
# built from this checkout.
#
#   scripts/setup.sh                    install, or re-run to start again (keeps .env and tunnel.env)
#   scripts/setup.sh --check            read-only: settings by name, containers, /readyz, the public URL
#   scripts/setup.sh --backup [dir]     back up the coach-data volume (default dir: backups)
#   scripts/setup.sh --restore <file>   put a backup back on the volume (asks first)
#   scripts/setup.sh --upgrade <tag>    back up, check out the release tag, rebuild, start
#
# Without a terminal (tests, CI) set SETUP_NONINTERACTIVE=1 and the answers as
# environment variables, never as arguments: SETUP_HOST (coach.example.com),
# SETUP_AUTH (key or full), SETUP_TZ (else the machine's zone), SETUP_KEY (key
# mode), SETUP_TUNNEL_TOKEN, and SETUP_YES=1 to confirm a restore. It then never
# changes an existing .env or tunnel.env and never starts the Garmin login.
# SETUP_WAIT_S (default 240) bounds the wait for a healthy app.
#
# Secrets (the owner secret, the Garmin key, the tunnel token) are read with
# read -s, written to .env and tunnel.env (mode 600) with shell builtins, and
# never put on a command line. A new owner secret is shown once, at the end.
set -euo pipefail
set +x
# Byte-wise character classes and messages, whatever the user's locale.
LC_ALL=C
export LC_ALL

ZONEINFO="${SETUP_ZONEINFO_DIR:-/usr/share/zoneinfo}"
LOCALTIME="${SETUP_LOCALTIME:-/etc/localtime}"
ETC_TIMEZONE="${SETUP_ETC_TIMEZONE:-/etc/timezone}"
NONINTERACTIVE=0
[[ ${SETUP_NONINTERACTIVE:-} == 1 ]] && NONINTERACTIVE=1
WAIT_S="${SETUP_WAIT_S:-240}"
[[ $WAIT_S =~ ^[0-9]{1,4}$ ]] || WAIT_S=240
POLL_S="${SETUP_POLL_S:-3}"
[[ $POLL_S =~ ^[0-9]{1,2}$ ]] || POLL_S=3
TUNNEL_WAIT_S="${SETUP_TUNNEL_WAIT_S:-30}"
[[ $TUNNEL_WAIT_S =~ ^[0-9]{1,3}$ ]] || TUNNEL_WAIT_S=30
ALPHABET='23456789abcdefghjkmnpqrstuvwxyz'
PROBLEMS=0
NEW_SECRET=""
LAST_BACKUP=""

say() { printf '\n==> %s\n' "$*"; }
info() { printf '    %s\n' "$*"; }
warn() { printf '!!  %s\n' "$*"; }
problem() { # problem TEXT NEXT-STEP
	PROBLEMS=$((PROBLEMS + 1))
	printf '!!  %s\n    Next: %s\n' "$1" "$2"
}
die() {
	printf '!!  %s\n' "$*" >&2
	if [[ -n $NEW_SECRET ]]; then
		printf '    Your new owner secret is in .env; read it with: grep %s .env\n' "'^OWNER_SECRET='" >&2
	fi
	exit 1
}
usage() {
	sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}
trim() {
	local s=$1
	s="${s#"${s%%[![:space:]]*}"}"
	s="${s%"${s##*[![:space:]]}"}"
	printf '%s' "$s"
}
lower() { printf '%s' "$1" | tr '[:upper:]' '[:lower:]'; }

# --- questions ----------------------------------------------------------------
REPLY_TEXT=""
REPLY_SECRET=""
ask() { # ask PROMPT DEFAULT -> REPLY_TEXT
	local a=""
	if [[ -n $2 ]]; then printf '%s [%s]: ' "$1" "$2" >&2; else printf '%s: ' "$1" >&2; fi
	IFS= read -r a || a=""
	a="$(trim "$a")"
	REPLY_TEXT="${a:-$2}"
}
ask_secret() { # ask_secret PROMPT -> REPLY_SECRET; the terminal does not show what is typed
	local s=""
	printf '%s (not shown): ' "$1" >&2
	IFS= read -rs s || s=""
	printf '\n' >&2
	REPLY_SECRET="$(trim "$s")"
}
yes_no() { # yes_no PROMPT DEFAULT(y|n); without a terminal: the default
	local a d=$2
	if [[ $NONINTERACTIVE == 1 ]]; then [[ $d == y ]]; return; fi
	if [[ $d == y ]]; then printf '%s [Y/n]: ' "$1" >&2; else printf '%s [y/N]: ' "$1" >&2; fi
	IFS= read -r a || a=""
	a="$(lower "$(trim "$a")")"
	[[ -z $a ]] && a=$d
	[[ $a == y || $a == yes ]]
}

# --- checks of values ---------------------------------------------------------
# The same case-exact check as scripts/deploy.sh: the name must be a file under
# the zoneinfo folder, spelled exactly (find -path is case-sensitive even on a
# case-insensitive disk).
valid_tz() {
	[[ $1 =~ ^[A-Za-z][A-Za-z0-9_+-]*(/[A-Za-z0-9_+-]+)*$ ]] || return 1
	[[ -d $ZONEINFO ]] || return 0
	[[ -f $ZONEINFO/$1 && -n $(find "$ZONEINFO/" -path "$ZONEINFO/$1" -print -quit 2>/dev/null) ]]
}
# The machine's own zone: /etc/localtime's link (macOS: /var/db/timezone/zoneinfo/...,
# Linux: /usr/share/zoneinfo/...), else timedatectl, else /etc/timezone.
detect_tz() {
	local t=""
	if [[ -L $LOCALTIME ]]; then
		t="$(readlink "$LOCALTIME" 2>/dev/null)" || t=""
		case "$t" in
		*/zoneinfo/*) t="${t#*/zoneinfo/}" ;;
		*) t="" ;;
		esac
		t="${t#posix/}"
	fi
	if [[ -z $t ]] && command -v timedatectl >/dev/null 2>&1; then
		t="$(timedatectl show -p Timezone --value 2>/dev/null)" || t=""
	fi
	if [[ -z $t && -r $ETC_TIMEZONE ]]; then
		t="$(head -n 1 "$ETC_TIMEZONE" 2>/dev/null)" || t=""
	fi
	t="$(trim "$t")"
	if [[ -n $t ]] && valid_tz "$t"; then printf '%s' "$t"; fi
}
# A public host name: lower case letters, digits and '-', at least two labels,
# no scheme but https://, no port, path, user or IP address. Sets PARSED_HOST,
# or HOST_ERROR and returns 1 (call it directly, not in $(...)).
HOST_ERROR=""
PARSED_HOST=""
parse_host() {
	local h
	h="$(lower "$(trim "$1")")"
	HOST_ERROR=""
	PARSED_HOST=""
	if [[ -z $h ]]; then HOST_ERROR="missing"; return 1; fi
	case "$h" in
	http://*) HOST_ERROR="use https:// (Claude needs HTTPS; Cloudflare gives it), or just the host name"; return 1 ;;
	https://*) h="${h#https://}"; h="${h%/}" ;;
	*://*) HOST_ERROR="give a host name such as coach.example.com"; return 1 ;;
	esac
	if [[ $h == *:* ]]; then HOST_ERROR="no port: the tunnel serves HTTPS on 443 (an IPv6 address is not a host name either)"; return 1; fi
	if [[ $h == */* || $h == *\?* || $h == *#* ]]; then HOST_ERROR="no path: only the host name, such as coach.example.com"; return 1; fi
	if [[ $h == *@* ]]; then HOST_ERROR="no user name: only the host name"; return 1; fi
	if [[ $h =~ ^[0-9.]+$ ]]; then HOST_ERROR="an IP address cannot have a Cloudflare tunnel route: use a host name on your Cloudflare domain"; return 1; fi
	if ((${#h} > 253)) || ! [[ $h =~ ^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]([a-z0-9-]{0,61}[a-z0-9])?$ ]]; then
		HOST_ERROR="not a host name such as coach.example.com (letters, digits, '-' and dots)"
		return 1
	fi
	PARSED_HOST=$h
}
# The server's own rules (ext/auth_config.py): 32 to 256 printable ASCII
# characters, no space at either end, at least 12 different characters.
secret_ok() {
	local s=$1 seen="" c i
	((${#s} >= 32 && ${#s} <= 256)) || return 1
	local printable='^[ -~]+$'
	[[ $s =~ $printable ]] || return 1
	[[ $s == "$(trim "$s")" ]] || return 1
	for ((i = 0; i < ${#s}; i++)); do
		c=${s:i:1}
		[[ $seen == *"$c"* ]] || seen+=$c
	done
	((${#seen} >= 12))
}
# Garmin's key as the server checks it (ext/key_mode.py): 20 to 512 printable
# ASCII characters, no space. .env holds it in single quotes, so no ' either.
KEY_ERROR=""
key_ok() {
	KEY_ERROR=""
	if ((${#1} < 20)); then KEY_ERROR="too short (${#1} characters): copy the whole key from Garmin Connect"; return 1; fi
	if ((${#1} > 512)); then KEY_ERROR="too long (over 512 characters): copy the key again"; return 1; fi
	local visible='^[!-~]+$'
	if ! [[ $1 =~ $visible ]]; then KEY_ERROR="it has spaces or characters that are not plain ASCII"; return 1; fi
	if [[ $1 == *"'"* ]]; then KEY_ERROR="it has a ' in it, which .env cannot hold here: add it by hand (docs/deploy-docker.md, Manual setup)"; return 1; fi
}
# A Cloudflare tunnel token: base64 text.
token_ok() { ((${#1} >= 20 && ${#1} <= 4096)) && [[ $1 =~ ^[A-Za-z0-9+/=_-]+$ ]]; }

# A new owner secret: 40 characters from $ALPHABET (31 symbols, no 0, 1, i, l
# or o; about 198 bits) in 8 groups of 5 joined by '-'. Prints it on stdout:
# call it only as S="$(new_owner_secret)".
new_owner_secret() {
	local raw seen c i
	for _ in 1 2 3 4 5; do
		raw="$(head -c 4096 /dev/urandom | LC_ALL=C tr -dc "$ALPHABET")" || raw=""
		raw="${raw:0:40}"
		seen=""
		for ((i = 0; i < ${#raw}; i++)); do
			c=${raw:i:1}
			[[ $seen == *"$c"* ]] || seen+=$c
		done
		if [[ ${#raw} -eq 40 && ${#seen} -ge 12 ]]; then
			printf '%s-%s-%s-%s-%s-%s-%s-%s\n' "${raw:0:5}" "${raw:5:5}" "${raw:10:5}" "${raw:15:5}" \
				"${raw:20:5}" "${raw:25:5}" "${raw:30:5}" "${raw:35:5}"
			return 0
		fi
	done
	return 1
}

# percent-encode a URL for a query value.
pct() {
	local s=$1 out="" c e i
	for ((i = 0; i < ${#s}; i++)); do
		c=${s:i:1}
		case "$c" in
		[A-Za-z0-9.~_-]) out+=$c ;;
		*) printf -v e '%%%02X' "'$c"; out+=$e ;;
		esac
	done
	printf '%s' "$out"
}

# --- the settings files -------------------------------------------------------
# env_get FILE NAME: the value of the last NAME= line, outer quotes removed.
env_get() {
	local file=$1 name=$2 line v="" found=1
	[[ -f $file ]] || return 1
	while IFS= read -r line || [[ -n $line ]]; do
		line="${line%$'\r'}"
		if [[ $line == "$name="* ]]; then
			v="${line#"$name="}"
			found=0
		fi
	done <"$file"
	((found == 0)) || return 1
	if [[ $v == \'*\' && ${#v} -ge 2 ]]; then v="${v:1:${#v}-2}"; elif [[ $v == \"*\" && ${#v} -ge 2 ]]; then v="${v:1:${#v}-2}"; fi
	printf '%s' "$v"
}
file_mode() { stat -c %a "$1" 2>/dev/null || stat -f %Lp "$1" 2>/dev/null || echo "?"; }
# write_private FILE: stdin goes to FILE with mode 600 (made under umask 077, then moved in place).
write_private() {
	local tmp="$1.setup-tmp"
	(umask 077 && cat >"$tmp") || { rm -f "$tmp"; return 1; }
	chmod 600 "$tmp" && mv -f "$tmp" "$1"
}
# env_set FILE NAME VALUE: replace NAME's lines in FILE, keeping every other line.
env_set() {
	local file=$1 name=$2 value=$3 line
	{
		while IFS= read -r line || [[ -n $line ]]; do
			[[ $line == "$name="* ]] || printf '%s\n' "$line"
		done <"$file"
		printf '%s=%s\n' "$name" "$value"
	} | write_private "$file"
}
env_unset() {
	local file=$1 name=$2 line
	{
		while IFS= read -r line || [[ -n $line ]]; do
			[[ $line == "$name="* ]] || printf '%s\n' "$line"
		done <"$file"
	} | write_private "$file"
}

# --- docker -------------------------------------------------------------------
check_docker() {
	local v major minor rest arch
	arch="$(uname -m 2>/dev/null || echo unknown)"
	case "$arch" in
	x86_64 | amd64 | aarch64 | arm64) ;;
	armv6* | armv7* | armhf | i?86) die "This computer is $arch: the image needs a 64-bit system (amd64 or arm64)." ;;
	esac
	command -v docker >/dev/null 2>&1 || die "Docker is not installed. Install Docker Engine (Linux) or Docker Desktop, then run this again."
	if ! v="$(docker compose version --short 2>/dev/null)" || [[ -z $v ]]; then
		if command -v docker-compose >/dev/null 2>&1; then
			die "Only docker-compose (Compose v1) is installed, and it is not supported. Install the Docker Compose plugin 2.20 or newer (docker compose, with a space): https://docs.docker.com/compose/install/"
		fi
		die "docker compose is missing. Install the Docker Compose plugin 2.20 or newer: https://docs.docker.com/compose/install/"
	fi
	v="$(trim "${v#v}")"
	major="${v%%.*}"
	rest="${v#*.}"
	minor="${rest%%.*}"
	if ! [[ $major =~ ^[0-9]+$ && $minor =~ ^[0-9]+$ ]]; then
		die "Could not read the Docker Compose version ($v). This setup needs 2.20 or newer."
	fi
	if ((major < 2 || (major == 2 && minor < 20))); then
		die "Docker Compose $v is too old: this setup needs 2.20 or newer. Update Docker (Desktop) or the docker-compose-plugin package."
	fi
	docker info >/dev/null 2>&1 || die "Docker is installed but not running (or this user may not use it). Start Docker, then run this again."
}
# The state and health of one service: "running healthy", "running", "restarting", "exited", or "" when it has no container.
service_state() {
	docker compose ps --all --format '{{.Service}} {{.State}} {{.Health}}' 2>/dev/null |
		awk -v s="$1" '$1 == s { print ($3 == "" ? $2 : $2 " " $3); exit }'
}
wait_healthy() {
	local end=$((SECONDS + WAIT_S)) s
	while :; do
		s="$(service_state app)"
		case "$s" in
		"running healthy") return 0 ;;
		exited* | dead*) return 1 ;;
		esac
		((SECONDS >= end)) && return 1
		sleep "$POLL_S"
	done
}
# /readyz from inside the app container: prints the HTTP status (0: no answer).
readyz_code() {
	docker compose exec -T app python -c '
import urllib.error, urllib.request
try:
    print(urllib.request.urlopen("http://127.0.0.1:8080/readyz", timeout=5).status)
except urllib.error.HTTPError as e:
    print(e.code)
except Exception:
    print(0)' 2>/dev/null | tr -dc '0-9' || true
}
# grep -c reads the whole log: grep -q would stop early, and under pipefail the
# SIGPIPE it gives docker compose logs would make a connected tunnel look down.
tunnel_connected() {
	local n
	n="$(docker compose logs --no-color --tail 2000 tunnel 2>/dev/null | grep -c 'Registered tunnel connection' || true)"
	[[ ${n:-0} -gt 0 ]]
}
# Waits up to TUNNEL_WAIT_S for the tunnel; reports a problem when it is down.
check_tunnel() {
	local end=$((SECONDS + TUNNEL_WAIT_S)) s
	while :; do
		s="$(service_state tunnel)"
		if [[ $s == running* ]] && tunnel_connected; then
			info "tunnel: connected to Cloudflare"
			return 0
		fi
		((SECONDS >= end)) && break
		sleep "$POLL_S"
	done
	case "$s" in
	running*)
		warn "tunnel: running, but its log does not show a connection yet (docker compose logs tunnel)." ;;
	*)
		problem "tunnel: not running (${s:-no container}); it did not connect to Cloudflare." \
			"docker compose logs tunnel shows why. A wrong token: put the right one in tunnel.env (scripts/setup.sh asks for it when tunnel.env is gone: rm tunnel.env), then docker compose up -d --force-recreate tunnel." ;;
	esac
}

# --- git ----------------------------------------------------------------------
in_git() { git rev-parse --git-dir >/dev/null 2>&1; }
current_tag() { git describe --tags --exact-match HEAD 2>/dev/null || true; }
local_changes() { git status --porcelain --untracked-files=no 2>/dev/null || true; }

# --- install ------------------------------------------------------------------
HOST=""
AUTH=""
TZNAME=""
KEY=""
TOKEN=""

ask_host() {
	local def=$1 h
	while :; do
		ask "Public host name for the server (a subdomain of your domain on Cloudflare, e.g. coach.example.com)" "$def"
		if parse_host "$REPLY_TEXT"; then HOST=$PARSED_HOST; return 0; fi
		echo "Not usable: $HOST_ERROR." >&2
	done
}
ask_auth() {
	local def=${1:-key} a
	echo "Garmin mode: key = a Garmin Personal API key (recommended: nothing to log in to, fewer tools);" >&2
	echo "             full = log in to Garmin once with your email and password (every tool). docs/garmin-login.md" >&2
	while :; do
		ask "Garmin mode, key or full" "$def"
		a="$(lower "$REPLY_TEXT")"
		if [[ $a == key || $a == full ]]; then AUTH=$a; return 0; fi
		echo "Type key or full." >&2
	done
}
ask_tz() {
	local def=$1
	while :; do
		ask "Time zone, an IANA name such as Europe/London" "$def"
		if [[ -n $REPLY_TEXT ]] && valid_tz "$REPLY_TEXT"; then TZNAME=$REPLY_TEXT; return 0; fi
		echo "That is not a time zone name on this machine (match the case, e.g. Europe/London)." >&2
	done
}
ask_key() { # ask_key KEEP(1: Enter keeps the current key)
	while :; do
		if [[ $1 == 1 ]]; then ask_secret "Garmin Personal API key (Enter keeps the current one)"; else ask_secret "Garmin Personal API key"; fi
		if [[ -z $REPLY_SECRET && $1 == 1 ]]; then KEY=""; return 0; fi
		if key_ok "$REPLY_SECRET"; then KEY=$REPLY_SECRET; REPLY_SECRET=""; return 0; fi
		echo "Not used: $KEY_ERROR." >&2
	done
}
ask_token() {
	echo "The tunnel token: in Cloudflare, the install command for your tunnel ends with --token and the token. Paste only the token." >&2
	while :; do
		ask_secret "Cloudflare tunnel token"
		if token_ok "$REPLY_SECRET"; then TOKEN=$REPLY_SECRET; REPLY_SECRET=""; return 0; fi
		echo "That does not look like a tunnel token (base64 text, no spaces). Copy only the part after --token." >&2
	done
}

write_new_env() {
	{
		printf '%s\n' "# Settings for this server, written by scripts/setup.sh (docs/deploy-docker.md)." \
			"# It holds secrets: keep it private (mode 600); never commit, share or paste it." \
			"# .env.example lists every other setting. After a change, run:" \
			"#   docker compose up -d --force-recreate app" \
			"# A value that contains \$ goes in single quotes, e.g. NAME='a\$b'." \
			"" \
			"# The owner secret you type on the server's sign-in page when Claude connects." \
			"# Read it with: grep '^OWNER_SECRET=' .env"
		printf 'OWNER_SECRET=%s\n' "$1"
		printf '%s\n' "" "# How this server reads Garmin: key or full (docs/garmin-login.md)."
		printf 'GARMIN_AUTH=%s\n' "$AUTH"
		if [[ $AUTH == key ]]; then
			printf '%s\n' "# The Garmin Personal API key: a secret like a password."
			printf "GARMIN_PERSONAL_API_KEY='%s'\n" "$KEY"
		fi
		printf '%s\n' "" "# This server's public https origin: https:// and the tunnel's host name."
		printf 'PUBLIC_URL=https://%s\n' "$HOST"
		printf '%s\n' "" "# Your IANA time zone, for \"today\" in the daily and weekly status."
		printf 'REPORT_TZ=%s\n' "$TZNAME"
	} | write_private .env
}

do_install() {
	local e_secret="" e_auth="" e_tz="" e_url="" e_key="" e_host="" missing=() keep_env=0 change=0 t_token="" def_tz=""
	say "Checks"
	check_docker
	info "docker compose: OK"
	if in_git; then
		[[ -n $(current_tag) ]] || warn "This checkout is not at a release tag. Use one: git fetch --tags && git checkout vX.Y.Z (the newest on the Releases page)."
		[[ -z $(local_changes) ]] || warn "This checkout has local changes (git status). The build uses them as they are."
	else
		warn "This folder is not a git clone: --upgrade will not work. Clone the repository instead (docs/deploy-docker.md)."
	fi
	if [[ $NONINTERACTIVE != 1 && ! -t 0 ]]; then
		die "Run this in a terminal, or set SETUP_NONINTERACTIVE=1 with SETUP_HOST, SETUP_AUTH, SETUP_TZ, SETUP_KEY and SETUP_TUNNEL_TOKEN (scripts/setup.sh, top)."
	fi

	say "Settings (.env)"
	if [[ -f .env ]]; then
		e_secret="$(env_get .env OWNER_SECRET || true)"
		e_auth="$(lower "$(trim "$(env_get .env GARMIN_AUTH || true)")")"
		e_tz="$(trim "$(env_get .env REPORT_TZ || true)")"
		e_url="$(trim "$(env_get .env PUBLIC_URL || true)")"
		e_key="$(trim "$(env_get .env GARMIN_PERSONAL_API_KEY || true)")"
		secret_ok "$e_secret" || missing+=(OWNER_SECRET)
		[[ $e_auth == key || $e_auth == full ]] || missing+=(GARMIN_AUTH)
		{ [[ -n $e_tz ]] && valid_tz "$e_tz"; } || missing+=(REPORT_TZ)
		if [[ $e_url == https://* ]] && parse_host "$e_url"; then e_host=$PARSED_HOST; else e_host=""; missing+=(PUBLIC_URL); fi
		if [[ $e_auth == key ]] && ! key_ok "$e_key"; then missing+=(GARMIN_PERSONAL_API_KEY); fi
		if [[ $e_auth == full && -n $e_key ]]; then missing+=("GARMIN_PERSONAL_API_KEY (must be empty in full mode)"); fi
		if ((${#missing[@]} == 0)); then
			info ".env has OWNER_SECRET, GARMIN_AUTH, REPORT_TZ and PUBLIC_URL (host $e_host, mode $e_auth, zone $e_tz)."
			if yes_no "Keep the settings in .env as they are?" y; then keep_env=1; fi
		else
			info ".env exists; missing or not valid: ${missing[*]}."
			if [[ $NONINTERACTIVE == 1 ]]; then
				die "SETUP_NONINTERACTIVE=1 never changes an existing .env. Fix those settings, or move .env away and run this again. Nothing was changed."
			fi
			yes_no "Fill in those settings in .env (other lines stay)?" y || die "Nothing was changed."
		fi
		change=$((1 - keep_env))
	fi

	if [[ $keep_env == 1 ]]; then
		HOST=$e_host AUTH=$e_auth TZNAME=$e_tz
		[[ $(file_mode .env) == 600 ]] || { chmod 600 .env; info "Made .env private (mode 600)."; }
	else
		def_tz="$(detect_tz)"
		if [[ $NONINTERACTIVE == 1 ]]; then
			parse_host "${SETUP_HOST:-}" || die "SETUP_HOST: $HOST_ERROR. Nothing was changed."
			HOST=$PARSED_HOST
			AUTH="$(lower "$(trim "${SETUP_AUTH:-}")")"
			[[ $AUTH == key || $AUTH == full ]] || die "SETUP_AUTH must be key or full. Nothing was changed."
			TZNAME="$(trim "${SETUP_TZ:-$def_tz}")"
			{ [[ -n $TZNAME ]] && valid_tz "$TZNAME"; } || die "SETUP_TZ is not a time zone name on this machine (match the case, e.g. Europe/London). Nothing was changed."
			if [[ $AUTH == key ]]; then
				KEY="$(trim "${SETUP_KEY:-}")"
				key_ok "$KEY" || die "SETUP_KEY: $KEY_ERROR. Nothing was changed."
			fi
		else
			ask_host "${e_host:-}"
			ask_auth "${e_auth:-key}"
			ask_tz "${e_tz:-$def_tz}"
			if [[ $AUTH == key ]]; then
				if [[ -n ${e_key:-} ]] && key_ok "$e_key"; then ask_key 1; else ask_key 0; fi
			fi
		fi
		local dots=${HOST//[!.]/}
		if ((${#dots} > 2)); then
			warn "$HOST is deeper than a first-level subdomain: Cloudflare's free certificate does not cover it (it needs an Advanced Certificate)."
		fi
		if [[ $change == 1 ]]; then
			secret_ok "$e_secret" || { NEW_SECRET="$(new_owner_secret)" || die "Could not make an owner secret (/dev/urandom)."; env_set .env OWNER_SECRET "$NEW_SECRET"; }
			env_set .env GARMIN_AUTH "$AUTH"
			env_set .env PUBLIC_URL "https://$HOST"
			env_set .env REPORT_TZ "$TZNAME"
			if [[ $AUTH == key && -n $KEY ]]; then env_set .env GARMIN_PERSONAL_API_KEY "'$KEY'"; fi
			if [[ $AUTH == full && -n ${e_key:-} ]]; then env_unset .env GARMIN_PERSONAL_API_KEY; info "Removed GARMIN_PERSONAL_API_KEY: full mode does not start with it."; fi
			info "Updated .env (mode 600); other lines are kept."
		else
			NEW_SECRET="$(new_owner_secret)" || die "Could not make an owner secret (/dev/urandom)."
			write_new_env "$NEW_SECRET"
			info "Wrote .env (mode 600) with a new owner secret."
		fi
		KEY=""
	fi

	say "Tunnel token (tunnel.env)"
	t_token="$(trim "$(env_get tunnel.env TUNNEL_TOKEN 2>/dev/null || true)")"
	local keep_token=0
	if [[ -f tunnel.env ]] && token_ok "$t_token"; then
		yes_no "Keep the tunnel token in tunnel.env?" y && keep_token=1
	elif [[ -f tunnel.env ]]; then
		[[ $NONINTERACTIVE == 1 ]] && die "tunnel.env has no usable TUNNEL_TOKEN line, and SETUP_NONINTERACTIVE=1 never changes it. Fix it or remove it."
		yes_no "tunnel.env has no usable TUNNEL_TOKEN line. Replace it with a new token?" y || die "Kept tunnel.env as it is; the tunnel cannot start without a token."
	fi
	if [[ $keep_token == 1 ]]; then
		[[ $(file_mode tunnel.env) == 600 ]] || { chmod 600 tunnel.env; info "Made tunnel.env private (mode 600)."; }
		info "Kept tunnel.env."
	else
		if [[ $NONINTERACTIVE == 1 ]]; then
			TOKEN="$(trim "${SETUP_TUNNEL_TOKEN:-}")"
			token_ok "$TOKEN" || die "SETUP_TUNNEL_TOKEN is missing or does not look like a tunnel token."
		else
			ask_token
		fi
		printf 'TUNNEL_TOKEN=%s\n' "$TOKEN" | write_private tunnel.env
		TOKEN=""
		info "Wrote tunnel.env (mode 600)."
	fi
	t_token=""

	say "Build and start (docker compose up -d --build; the first build takes a few minutes)"
	if ! docker compose up -d --build; then
		die "docker compose up failed. If app is unhealthy, docker compose logs app names the setting to fix in .env; then run this again."
	fi
	if wait_healthy; then
		info "app: healthy"
	else
		die "app is not healthy ($(service_state app)). docker compose logs app shows why (a launch: config error line names the setting). Fix .env and run this again."
	fi

	local code
	code="$(readyz_code)"
	if [[ $AUTH == full ]]; then
		if [[ $code == 200 ]]; then
			info "/readyz: ready (a Garmin login is already on the volume)"
		else
			info "/readyz: not ready ($code): full mode needs the one-time Garmin login."
			if [[ $NONINTERACTIVE != 1 ]] && yes_no "Run the Garmin login now (your Garmin email, password and MFA code)?" y; then
				docker compose exec app garmin-mcp-auth --token-path /data/tokens || warn "The login did not finish. Run it again later: docker compose exec app garmin-mcp-auth --token-path /data/tokens"
				info "The server picks the login up by itself in about 30 seconds."
			else
				info "Run it later, in this folder: docker compose exec app garmin-mcp-auth --token-path /data/tokens"
			fi
		fi
	elif [[ $code == 200 ]]; then
		info "/readyz: ready (Garmin did not refuse the key, or a saved access token was reused)"
	else
		problem "/readyz: not ready ($code): Garmin refused the key, or could not be reached." \
			"docker compose logs app; make a new key (docs/maintenance.md, Renew the Garmin key), then run this again with a fresh .env or follow Manual setup."
	fi

	say "Tunnel"
	check_tunnel

	local url="https://$HOST/mcp"
	say "Connect Claude"
	info "Connector URL (exactly, no trailing slash): $url"
	info "Add it in claude.ai with one click:"
	info "  https://claude.ai/customize/connectors?modal=add-custom-connector&connectorName=Garmin%20coach&connectorUrl=$(pct "$url")"
	info "Then Connect, check the address bar shows https://$HOST, type the owner secret and Approve (docs/connect-claude.md)."
	info "In Cloudflare, for this host: no Access application, Bot Fight Mode off, the AI agent policy not on Block,"
	info "no Under Attack mode (docs/deploy-docker.md, Cloudflare settings that block Claude)."
	info "Check everything later: scripts/setup.sh --check"
	if [[ -n $NEW_SECRET ]]; then
		say "Your owner secret (save it now in your password manager; it is not shown again)"
		printf '    %s\n' "$NEW_SECRET"
		info "You type it on the server's sign-in page when Claude connects. Later: grep '^OWNER_SECRET=' .env"
		NEW_SECRET=""
	else
		info "The owner secret is unchanged: grep '^OWNER_SECRET=' .env"
	fi
	if ((PROBLEMS > 0)); then
		say "Set up with $PROBLEMS problem(s) above."
		return 1
	fi
	say "Done."
}

# --- check --------------------------------------------------------------------
do_check() {
	local auth="" url="" host="" secret="" tz="" key="" code="" s="" t_token="" v="" major="" minor="" rest=""
	say "Settings (names only)"
	if [[ ! -f .env ]]; then
		problem ".env is missing." "Run scripts/setup.sh."
	else
		[[ $(file_mode .env) == 600 ]] || problem ".env is readable by others (mode $(file_mode .env))." "chmod 600 .env"
		secret="$(env_get .env OWNER_SECRET || true)"
		if secret_ok "$secret"; then info "OWNER_SECRET: set"; else problem "OWNER_SECRET: missing or not valid (32-256 characters, 12 different)." "Run scripts/setup.sh and let it fill it in."; fi
		secret=""
		auth="$(lower "$(trim "$(env_get .env GARMIN_AUTH || true)")")"
		if [[ $auth == key || $auth == full ]]; then info "GARMIN_AUTH: set"; else problem "GARMIN_AUTH: missing or not key/full." "Set GARMIN_AUTH=key or GARMIN_AUTH=full in .env."; fi
		key="$(trim "$(env_get .env GARMIN_PERSONAL_API_KEY || true)")"
		if [[ $auth == key ]]; then
			if key_ok "$key"; then info "GARMIN_PERSONAL_API_KEY: set"; else problem "GARMIN_PERSONAL_API_KEY: missing or not valid." "Put the key in .env (docs/deploy-docker.md, Manual setup)."; fi
		elif [[ $auth == full && -n $key ]]; then
			problem "GARMIN_PERSONAL_API_KEY: set in full mode; the server does not start with it." "Delete that line in .env, then docker compose up -d --force-recreate app."
		fi
		key=""
		tz="$(trim "$(env_get .env REPORT_TZ || true)")"
		if [[ -n $tz ]] && valid_tz "$tz"; then info "REPORT_TZ: set"; else problem "REPORT_TZ: missing or not a time zone name." "Set REPORT_TZ in .env, such as Europe/London."; fi
		url="$(trim "$(env_get .env PUBLIC_URL || true)")"
		if [[ $url == https://* ]] && parse_host "$url"; then host=$PARSED_HOST; info "PUBLIC_URL: set"; else host=""; problem "PUBLIC_URL: missing or not https:// plus a host name." "Set PUBLIC_URL=https://<the tunnel's host name> in .env."; fi
	fi
	if [[ ! -f tunnel.env ]]; then
		problem "tunnel.env is missing." "Run scripts/setup.sh."
	else
		[[ $(file_mode tunnel.env) == 600 ]] || problem "tunnel.env is readable by others (mode $(file_mode tunnel.env))." "chmod 600 tunnel.env"
		t_token="$(trim "$(env_get tunnel.env TUNNEL_TOKEN || true)")"
		if token_ok "$t_token"; then info "TUNNEL_TOKEN: set"; else problem "TUNNEL_TOKEN: missing or not a token." "rm tunnel.env, then scripts/setup.sh asks for the token."; fi
		t_token=""
	fi

	say "Docker"
	if ! command -v docker >/dev/null 2>&1; then
		problem "Docker is not installed." "Install Docker, then run scripts/setup.sh."
	elif ! v="$(docker compose version --short 2>/dev/null)" || [[ -z $v ]]; then
		problem "docker compose is missing (docker-compose v1 is not supported)." "Install the Docker Compose plugin 2.20 or newer."
	else
		v="$(trim "${v#v}")"; major="${v%%.*}"; rest="${v#*.}"; minor="${rest%%.*}"
		if [[ $major =~ ^[0-9]+$ && $minor =~ ^[0-9]+$ ]] && ((major > 2 || (major == 2 && minor >= 20))); then
			info "docker compose $v: OK"
		else
			problem "Docker Compose $v is older than 2.20." "Update Docker or the docker-compose-plugin package."
		fi
		if ! docker info >/dev/null 2>&1; then
			problem "Docker is not running." "Start Docker."
		else
			s="$(service_state app)"
			case "$s" in
			"running healthy") info "app: running, healthy" ;;
			"") problem "app: no container." "scripts/setup.sh (or docker compose up -d --build)." ;;
			*) problem "app: $s." "docker compose logs app shows why; after fixing .env: docker compose up -d --force-recreate app." ;;
			esac
			s="$(service_state tunnel)"
			if [[ $s == running* ]] && tunnel_connected; then
				info "tunnel: running, connected"
			elif [[ $s == running* ]]; then
				warn "tunnel: running; its log shows no connection to Cloudflare (docker compose logs tunnel)."
			else
				problem "tunnel: ${s:-no container}." "docker compose logs tunnel; a wrong token: rm tunnel.env and run scripts/setup.sh."
			fi
			if [[ $(service_state app) == running* ]]; then
				code="$(readyz_code)"
				if [[ $code == 200 ]]; then
					info "/readyz: 200, ready"
				elif [[ $code == 503 && $auth == full ]]; then
					problem "/readyz: 503: no Garmin login yet (or it is still loading)." "docker compose exec app garmin-mcp-auth --token-path /data/tokens, then wait 30 seconds."
				elif [[ $code == 503 ]]; then
					problem "/readyz: 503: Garmin refused the key." "Make a new key (docs/maintenance.md, Renew the Garmin key)."
				else
					problem "/readyz: no answer (${code:-none})." "docker compose logs app"
				fi
			fi
		fi
	fi

	say "Public URL"
	if [[ -z ${host:-} ]]; then
		info "Skipped: no usable PUBLIC_URL."
	elif ! command -v curl >/dev/null 2>&1; then
		info "Skipped: curl is not installed."
	elif ! curl -sS -o /dev/null --max-time 10 "https://$host/healthz" 2>/dev/null; then
		problem "This computer cannot reach the server's public URL." "Check the tunnel (above), the route in Cloudflare (Networking, Tunnels, your tunnel, Routes: http://app:8080) and the DNS record."
	else
		local unready=()
		[[ ${auth:-} == full && ${code:-} == 503 ]] && unready=(--expect-unready)
		if ./scripts/test-connector.sh ${unready[@]+"${unready[@]}"} "https://$host"; then
			info "test-connector.sh: PASS"
		else
			problem "test-connector.sh found problems (above)." "docs/troubleshooting.md; Cloudflare settings: docs/deploy-docker.md."
		fi
	fi
	echo
	if ((PROBLEMS > 0)); then
		echo "check: $PROBLEMS problem(s)"
		return 1
	fi
	echo "check: OK"
}

# --- backup and restore -------------------------------------------------------
need_files() {
	[[ -f .env && -f tunnel.env ]] || die "Run this in the project folder, after scripts/setup.sh (it needs .env and tunnel.env)."
}
do_backup() { # do_backup DIR; sets LAST_BACKUP
	local dir=${1:-backups} stamp file tmp running=0
	need_files
	(umask 077 && mkdir -p "$dir") || die "Cannot make the folder $dir."
	stamp="$(date -u +%Y%m%d-%H%M%S)"
	file="$dir/coach-data-$stamp.tar.gz"
	local n=2
	while [[ -e $file ]]; do
		file="$dir/coach-data-$stamp-$n.tar.gz"
		n=$((n + 1))
	done
	tmp="$file.part"
	say "Backup of the coach-data volume (the Garmin login or key cache, and the sign-ins)"
	[[ $(service_state app) == running* ]] && running=1
	if ((running)); then
		info "Stopping app for a moment, so the sign-in database is copied whole."
		docker compose stop app >/dev/null || die "Could not stop app."
	fi
	# The app's own image (no other image), with tar as its entry point; it writes the archive to stdout.
	if (umask 077 && docker compose run --rm --no-deps -T --entrypoint tar app -czf - -C /data . >"$tmp"); then
		:
	else
		rm -f "$tmp"
		((running)) && docker compose up -d >/dev/null 2>&1
		die "The backup failed; nothing was written."
	fi
	((running)) && { docker compose up -d >/dev/null || warn "Could not start the server again: docker compose up -d"; }
	if [[ ! -s $tmp ]] || ! tar -tzf "$tmp" >/dev/null 2>&1; then
		rm -f "$tmp"
		die "The backup is empty or broken; nothing was written."
	fi
	chmod 600 "$tmp" && mv -f "$tmp" "$file"
	LAST_BACKUP=$file
	info "Wrote $file (mode 600). It holds your Garmin login and sign-ins: keep it private, like .env."
	info "It does not hold .env and tunnel.env: keep copies of them too (your password manager is a good place)."
	info "Restore it (this computer, or a new one with the same .env): scripts/setup.sh --restore $file"
}
do_restore() {
	local file=$1 bad
	[[ -n $file ]] || die "Usage: scripts/setup.sh --restore <backup file>"
	[[ -f $file && -r $file ]] || die "Cannot read $file."
	need_files
	if ! tar -tzf "$file" >/dev/null 2>&1; then die "$file is not a .tar.gz backup."; fi
	bad="$(tar -tzf "$file" | grep -E '(^/|(^|/)\.\.(/|$))' | head -n 1 || true)"
	[[ -z $bad ]] || die "$file has a path outside the volume; it was not made by setup.sh. Nothing was changed."
	say "Restore $file to the coach-data volume"
	info "This replaces everything on the volume (the Garmin login or key cache, and the sign-ins)."
	if [[ $NONINTERACTIVE == 1 ]]; then
		[[ ${SETUP_YES:-} == 1 ]] || die "Set SETUP_YES=1 to confirm the restore. Nothing was changed."
	else
		yes_no "Stop the server and replace the volume's contents?" n || die "Nothing was changed."
	fi
	docker compose stop app >/dev/null 2>&1 || true
	if ! docker compose run --rm --no-deps -T --entrypoint sh app -c 'find /data -mindepth 1 -delete && tar -xzf - -C /data' <"$file"; then
		die "The restore failed. The volume may be half restored: run scripts/setup.sh --restore again."
	fi
	info "Restored. Starting the server."
	docker compose up -d || die "docker compose up -d failed: docker compose logs app"
	if wait_healthy; then info "app: healthy"; else die "app is not healthy: docker compose logs app"; fi
	info "Check it: scripts/setup.sh --check"
}

# --- upgrade ------------------------------------------------------------------
do_upgrade() {
	local tag=$1 prev prev_ref
	[[ -n $tag ]] || die "Usage: scripts/setup.sh --upgrade <tag>, e.g. v1.2.0 (the Releases page lists them)."
	[[ $tag =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || die "Not a tag name: $tag"
	in_git || die "This folder is not a git clone; --upgrade needs one."
	[[ -z $(local_changes) ]] || die "This checkout has local changes (git status); --upgrade does not overwrite them. Commit, stash or undo them first."
	check_docker
	prev="$(current_tag)"
	prev_ref="${prev:-$(git rev-parse --short HEAD)}"
	do_backup backups
	say "Fetch the release tags"
	git fetch --tags --quiet || die "git fetch --tags failed (network?). Nothing was changed but the backup."
	git rev-parse -q --verify "refs/tags/$tag^{commit}" >/dev/null || die "There is no tag $tag. Nothing was changed but the backup."
	say "Check out $tag (from $prev_ref)"
	git -c advice.detachedHead=false checkout --quiet "refs/tags/$tag" || die "git checkout $tag failed."
	say "Build and start"
	local ok=1
	docker compose up -d --build || ok=0
	if ((ok)) && wait_healthy; then info "app: healthy"; else ok=0; warn "app is not healthy: docker compose logs app."; fi
	say "Roll back, if you need to"
	if [[ -n $prev ]]; then
		info "scripts/setup.sh --upgrade $prev"
	else
		info "git -c advice.detachedHead=false checkout $prev_ref && docker compose up -d --build"
	fi
	# Only when the old version's heading is in the new CHANGELOG: without it the
	# whole file would be read and the database note would always show.
	if [[ -n $prev ]] && grep -qF "## [${prev#v}]" CHANGELOG.md 2>/dev/null &&
		awk -v v="## [${prev#v}]" 'index($0, v) == 1 { exit } { print }' CHANGELOG.md 2>/dev/null | grep -c 'sign-in database' >/dev/null; then
		info "The CHANGELOG since $prev says the sign-in database changed: after the line above, also run"
		info "scripts/setup.sh --restore $LAST_BACKUP"
	else
		info "If the CHANGELOG says the sign-in database changed, also run: scripts/setup.sh --restore $LAST_BACKUP"
	fi
	((ok)) || return 1
	info "If the CHANGELOG says tools were added or changed, choose Refresh tools list on the connector in claude.ai."
	say "Done: now at $tag."
}

main() {
	cd "$(dirname "${BASH_SOURCE[0]}")/.."
	case "${1:-}" in
	"") do_install ;;
	--check) [[ $# -eq 1 ]] || { usage >&2; exit 2; }; do_check ;;
	--backup)
		[[ $# -le 2 ]] || { usage >&2; exit 2; }
		check_docker
		do_backup "${2:-backups}" ;;
	--restore)
		[[ $# -eq 2 ]] || { usage >&2; exit 2; }
		check_docker
		do_restore "$2" ;;
	--upgrade) [[ $# -eq 2 ]] || { usage >&2; exit 2; }; do_upgrade "$2" ;;
	-h | --help) usage ;;
	*) usage >&2; exit 2 ;;
	esac
}

# All of the script is read before it runs: --upgrade checks out a new version of this file.
main "$@"; exit $?
