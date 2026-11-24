#!/usr/bin/env bash
# Install pinned CI tools into one folder, each download checked by SHA-256.
#   scripts/ci-tools.sh DIR TOOL...
#   TOOL: actionlint caddy gitleaks hadolint shellcheck
# Platforms: Linux x86_64 (GitHub's ubuntu-24.04 runner) and macOS arm64.
# Add DIR to PATH afterwards. A tool already in DIR at the pinned version is
# kept. To bump a tool, change its two lines below (the release page lists
# each file's sha256); caddy must stay the Dockerfile's `FROM caddy:` version
# (scripts/ci-checks.py pin-drift checks that).
set -euo pipefail

# tool version platform sha256 url
PINS='
actionlint 1.7.12 linux-x86_64 8aca8db96f1b94770f1b0d72b6dddcb1ebb8123cb3712530b08cc387b349a3d8 https://github.com/rhysd/actionlint/releases/download/v1.7.12/actionlint_1.7.12_linux_amd64.tar.gz
actionlint 1.7.12 darwin-arm64 aba9ced2dee8d27fecca3dc7feb1a7f9a52caefa1eb46f3271ea66b6e0e6953f https://github.com/rhysd/actionlint/releases/download/v1.7.12/actionlint_1.7.12_darwin_arm64.tar.gz
caddy 2.11.7 linux-x86_64 727b91701a392de6ebc5027509f548bf39979e5216340d0faed8fa5e69c84f8b https://github.com/caddyserver/caddy/releases/download/v2.11.7/caddy_2.11.7_linux_amd64.tar.gz
caddy 2.11.7 darwin-arm64 cda3030e5d5b13eb9f0b6fb541037f633bddd958c070c5f561d7a5cccb581665 https://github.com/caddyserver/caddy/releases/download/v2.11.7/caddy_2.11.7_mac_arm64.tar.gz
gitleaks 8.30.1 linux-x86_64 551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb https://github.com/gitleaks/gitleaks/releases/download/v8.30.1/gitleaks_8.30.1_linux_x64.tar.gz
gitleaks 8.30.1 darwin-arm64 b40ab0ae55c505963e365f271a8d3846efbc170aa17f2607f13df610a9aeb6a5 https://github.com/gitleaks/gitleaks/releases/download/v8.30.1/gitleaks_8.30.1_darwin_arm64.tar.gz
hadolint 2.15.1 linux-x86_64 c7187db94eeeeca956519a6af171adc31453941a1e777961f6e680f697c8c507 https://github.com/hadolint/hadolint/releases/download/v2.15.1/hadolint-linux-x86_64
hadolint 2.15.1 darwin-arm64 5c09f3213f8e40406abe048233d985eebef336d4a6a20021be47fadb6cf480a2 https://github.com/hadolint/hadolint/releases/download/v2.15.1/hadolint-macos-arm64
shellcheck 0.11.0 linux-x86_64 b7af85e41cc99489dcc21d66c6d5f3685138f06d34651e6d34b42ec6d54fe6f6 https://github.com/koalaman/shellcheck/releases/download/v0.11.0/shellcheck-v0.11.0.linux.x86_64.tar.gz
shellcheck 0.11.0 darwin-arm64 339b930feb1ea764467013cc1f72d09cd6b869ebf1013296ba9055ab2ffbd26f https://github.com/koalaman/shellcheck/releases/download/v0.11.0/shellcheck-v0.11.0.darwin.aarch64.tar.gz
'

die() {
	echo "ci-tools: $*" >&2
	exit 1
}

[ $# -ge 2 ] || die "usage: $0 DIR TOOL... (TOOL: actionlint caddy gitleaks hadolint shellcheck)"
dir=$1
shift
case "$(uname -s)-$(uname -m)" in
Linux-x86_64) platform=linux-x86_64 ;;
Darwin-arm64) platform=darwin-arm64 ;;
*) die "no pins for $(uname -s) $(uname -m); install the tools yourself" ;;
esac
mkdir -p "$dir/.downloads"

sha256() {
	if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1"; else shasum -a 256 "$1"; fi | cut -d' ' -f1
}

for tool in "$@"; do
	line=$(printf '%s\n' "$PINS" | awk -v t="$tool" -v p="$platform" '$1 == t && $3 == p')
	[ -n "$line" ] || die "unknown tool '$tool'"
	read -r _ version _ sum url <<<"$line"
	stamp="$dir/.$tool.version"
	if [ -x "$dir/$tool" ] && [ "$(cat "$stamp" 2>/dev/null)" = "$version $sum" ]; then
		echo "ci-tools: $tool $version already in $dir"
		continue
	fi
	file="$dir/.downloads/${url##*/}"
	curl -fsSL --retry 3 --proto '=https' --tlsv1.2 -o "$file" "$url"
	got=$(sha256 "$file")
	if [ "$got" != "$sum" ]; then
		rm -f "$file"
		die "$tool $version: sha256 $got does not match the pin $sum"
	fi
	case "$file" in
	*.tar.gz)
		tmp=$(mktemp -d)
		tar -xzf "$file" -C "$tmp"
		bin=$(find "$tmp" -type f -name "$tool" | head -n 1)
		[ -n "$bin" ] || die "$tool $version: no '$tool' file in ${url##*/}"
		mv "$bin" "$dir/$tool"
		rm -rf "$tmp"
		;;
	*) cp "$file" "$dir/$tool" ;;
	esac
	chmod 0755 "$dir/$tool"
	rm -f "$file"
	echo "$version $sum" >"$stamp"
	echo "ci-tools: $tool $version installed in $dir (sha256 checked)"
done
