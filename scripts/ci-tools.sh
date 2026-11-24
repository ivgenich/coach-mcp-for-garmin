#!/usr/bin/env bash
# Install pinned CI tools into one folder, each download checked by SHA-256.
#   scripts/ci-tools.sh DIR TOOL...
#   TOOL: actionlint caddy gitleaks hadolint shellcheck
# Platforms: Linux x86_64 (GitHub's ubuntu-24.04 runner) and aarch64, macOS
# arm64 and x86_64.
# Add DIR to PATH afterwards. A tool already in DIR at the pinned version is
# kept. To bump a tool, change its four lines below; take each sha256 from the
# publisher's checksum file on the release page (caddy lists sha512 there:
# check the download against it, then pin its sha256); caddy must stay the Dockerfile's `FROM caddy:` version
# (scripts/ci-checks.py pin-drift checks that).
set -euo pipefail

# tool version platform sha256 url
PINS='
actionlint 1.7.12 linux-x86_64 8aca8db96f1b94770f1b0d72b6dddcb1ebb8123cb3712530b08cc387b349a3d8 https://github.com/rhysd/actionlint/releases/download/v1.7.12/actionlint_1.7.12_linux_amd64.tar.gz
actionlint 1.7.12 darwin-arm64 aba9ced2dee8d27fecca3dc7feb1a7f9a52caefa1eb46f3271ea66b6e0e6953f https://github.com/rhysd/actionlint/releases/download/v1.7.12/actionlint_1.7.12_darwin_arm64.tar.gz
actionlint 1.7.12 linux-aarch64 325e971b6ba9bfa504672e29be93c24981eeb1c07576d730e9f7c8805afff0c6 https://github.com/rhysd/actionlint/releases/download/v1.7.12/actionlint_1.7.12_linux_arm64.tar.gz
actionlint 1.7.12 darwin-x86_64 5b44c3bc2255115c9b69e30efc0fecdf498fdb63c5d58e17084fd5f16324c644 https://github.com/rhysd/actionlint/releases/download/v1.7.12/actionlint_1.7.12_darwin_amd64.tar.gz
caddy 2.11.9 linux-x86_64 5b8cb7faf8726a2028ad96a66e54e6e81e8f361bc6175e5d22bbd997cc5177fb https://github.com/caddyserver/caddy/releases/download/v2.11.9/caddy_2.11.9_linux_amd64.tar.gz
caddy 2.11.9 darwin-arm64 d075afc620376679a77b453399bf7ada264cab3280b2833e4ed1372abc0c1c13 https://github.com/caddyserver/caddy/releases/download/v2.11.9/caddy_2.11.9_mac_arm64.tar.gz
caddy 2.11.9 linux-aarch64 5e377cff994fc9362a6fcf695c53f226cfca3a3ef85bd3a3687cf252d6310165 https://github.com/caddyserver/caddy/releases/download/v2.11.9/caddy_2.11.9_linux_arm64.tar.gz
caddy 2.11.9 darwin-x86_64 22af256a2cc45606f6bfa8786cd89130529331319665926f4e972234a658fb30 https://github.com/caddyserver/caddy/releases/download/v2.11.9/caddy_2.11.9_mac_amd64.tar.gz
gitleaks 8.30.1 linux-x86_64 551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb https://github.com/gitleaks/gitleaks/releases/download/v8.30.1/gitleaks_8.30.1_linux_x64.tar.gz
gitleaks 8.30.1 darwin-arm64 b40ab0ae55c505963e365f271a8d3846efbc170aa17f2607f13df610a9aeb6a5 https://github.com/gitleaks/gitleaks/releases/download/v8.30.1/gitleaks_8.30.1_darwin_arm64.tar.gz
gitleaks 8.30.1 linux-aarch64 e4a487ee7ccd7d3a7f7ec08657610aa3606637dab924210b3aee62570fb4b080 https://github.com/gitleaks/gitleaks/releases/download/v8.30.1/gitleaks_8.30.1_linux_arm64.tar.gz
gitleaks 8.30.1 darwin-x86_64 dfe101a4db2255fc85120ac7f3d25e4342c3c20cf749f2c20a18081af1952709 https://github.com/gitleaks/gitleaks/releases/download/v8.30.1/gitleaks_8.30.1_darwin_x64.tar.gz
hadolint 2.15.1 linux-x86_64 c7187db94eeeeca956519a6af171adc31453941a1e777961f6e680f697c8c507 https://github.com/hadolint/hadolint/releases/download/v2.15.1/hadolint-linux-x86_64
hadolint 2.15.1 darwin-arm64 5c09f3213f8e40406abe048233d985eebef336d4a6a20021be47fadb6cf480a2 https://github.com/hadolint/hadolint/releases/download/v2.15.1/hadolint-macos-arm64
hadolint 2.15.1 linux-aarch64 f6198ef8090f404dbb771abfee086eb8c48ac177f30da7fd3510aca35b344b5d https://github.com/hadolint/hadolint/releases/download/v2.15.1/hadolint-linux-arm64
hadolint 2.15.1 darwin-x86_64 ffe9bb18b23d5ed1eae50237aecdbb523d016e96da0bd4e7aa432040acfc3fde https://github.com/hadolint/hadolint/releases/download/v2.15.1/hadolint-macos-x86_64
shellcheck 0.11.0 linux-x86_64 b7af85e41cc99489dcc21d66c6d5f3685138f06d34651e6d34b42ec6d54fe6f6 https://github.com/koalaman/shellcheck/releases/download/v0.11.0/shellcheck-v0.11.0.linux.x86_64.tar.gz
shellcheck 0.11.0 darwin-arm64 339b930feb1ea764467013cc1f72d09cd6b869ebf1013296ba9055ab2ffbd26f https://github.com/koalaman/shellcheck/releases/download/v0.11.0/shellcheck-v0.11.0.darwin.aarch64.tar.gz
shellcheck 0.11.0 linux-aarch64 68a8133197a50beb8803f8d42f9908d1af1c5540d4bb05fdfca8c1fa47decefc https://github.com/koalaman/shellcheck/releases/download/v0.11.0/shellcheck-v0.11.0.linux.aarch64.tar.gz
shellcheck 0.11.0 darwin-x86_64 c2c15e08df0e8fbc374c335b230a7ee958c313fa5714817a59aa59f1aa594f51 https://github.com/koalaman/shellcheck/releases/download/v0.11.0/shellcheck-v0.11.0.darwin.x86_64.tar.gz
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
Linux-aarch64 | Linux-arm64) platform=linux-aarch64 ;;
Darwin-arm64) platform=darwin-arm64 ;;
Darwin-x86_64) platform=darwin-x86_64 ;;
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
