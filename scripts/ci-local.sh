#!/usr/bin/env bash
# Run every CI step that needs neither Docker nor GitHub, on this machine.
#   scripts/ci-local.sh --setup    once, and again after a pin changes: build four
#                                  venvs and fetch the pinned tools (needs network)
#   scripts/ci-local.sh            run the steps; each ends PASS, FAIL or SKIP
#   scripts/ci-local.sh --strict   a step that cannot run here fails instead
# It writes nothing into the repository. Everything it builds lives in CI_CACHE
# (default ${XDG_CACHE_HOME:-$HOME/.cache}/coach-mcp-ci). Pythons: CI_PY310
# (default python3.10; it must have no mcp and no upstream: the stdlib leg) and
# CI_PY312 (default python3.12; --setup builds the venvs with it).
# The container smoke test needs Docker: scripts/ci-smoke.sh.
# The optional features: venv-full holds the image's packages, the tracing
# ones included (tests-full requires them); venv-history adds the history
# packages of an image built with HISTORY=1 (tests-history). The Postgres tests
# need a database: they run only when COACH_TEST_DATABASE_URL is set (CI's
# Postgres job sets it) and are reported as allowed skips otherwise.
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONDONTWRITEBYTECODE=1

CACHE="${CI_CACHE:-${XDG_CACHE_HOME:-$HOME/.cache}/coach-mcp-ci}"
PY310="${CI_PY310:-python3.10}"
PY312="${CI_PY312:-python3.12}"
BIN="$CACHE/bin"
FULL="$CACHE/venv-full"
TOOLS="$CACHE/venv-tools"
EVALS="$CACHE/venv-evals"
HISTORYV="$CACHE/venv-history"
SETUP=no
STRICT=no
for arg in "$@"; do
	case "$arg" in
	--setup) SETUP=yes ;;
	--strict) STRICT=yes ;;
	*)
		echo "usage: $0 [--setup] [--strict]" >&2
		exit 2
		;;
	esac
done

have() { command -v "$1" >/dev/null 2>&1; }

if [ "$SETUP" = yes ]; then
	have "$PY312" || {
		echo "ci-local: $PY312 not found; set CI_PY312" >&2
		exit 1
	}
	mkdir -p "$CACHE"
	rm -rf "$FULL" "$TOOLS" "$EVALS" "$HISTORYV"
	"$PY312" -m venv "$FULL"
	"$FULL/bin/python" -B scripts/ci-checks.py install-full
	"$PY312" -m venv "$HISTORYV"
	"$HISTORYV/bin/python" -B scripts/ci-checks.py install-full
	"$HISTORYV/bin/python" -m pip install --disable-pip-version-check --require-hashes --no-deps \
		-r config/requirements-history.txt
	"$PY312" -m venv "$EVALS"
	"$EVALS/bin/python" -B scripts/ci-checks.py install-full
	"$EVALS/bin/python" -m pip install --disable-pip-version-check --require-hashes -r evals/requirements.txt
	"$EVALS/bin/python" -m pip install --disable-pip-version-check --require-hashes --no-deps \
		-r config/requirements-history.txt
	"$PY312" -m venv "$TOOLS"
	"$TOOLS/bin/python" -m pip install --disable-pip-version-check --require-hashes -r .github/ci-requirements.txt
	scripts/ci-tools.sh "$BIN" actionlint caddy gitleaks hadolint shellcheck
	echo "ci-local: setup done in $CACHE (CI's full leg uses Python 3.12.15; this one is $("$PY312" -V 2>&1))"
	exit 0
fi

RESULTS=()
step() { # step NAME COMMAND...
	local name=$1
	shift
	echo "--- $name"
	if "$@"; then RESULTS+=("PASS  $name"); else RESULTS+=("FAIL  $name"); fi
}
skip() { # skip NAME REASON
	echo "--- $1: skipped: $2"
	if [ "$STRICT" = yes ]; then RESULTS+=("FAIL  $1 (cannot run here: $2)"); else RESULTS+=("SKIP  $1: $2"); fi
}
tool() { # the pinned binary from --setup, else the one on PATH, else nothing
	if [ -x "$BIN/$1" ]; then echo "$BIN/$1"; elif have "$1"; then command -v "$1"; fi
}
any_python() { if have "$PY310"; then echo "$PY310"; else echo python3; fi; }
files() { git ls-files -z --cached --others --exclude-standard "$@"; }

evals_offline() { # the eval tests, then each committed baseline graded into a temp folder
	local tmp d rc=0
	# The history eval tests skip without the history packages: here a missing package fails.
	"$EVALS/bin/python" -B -c 'import evals.harness.history as h; assert h.available(), "history packages missing"' ||
		return 1
	"$EVALS/bin/python" -B -m unittest discover -s evals/tests -t . || return 1
	tmp=$(mktemp -d)
	for d in evals/baselines/*/; do
		[ -d "$d/transcripts" ] || continue
		"$EVALS/bin/python" -B -m evals grade "$d" --out "$tmp/$(basename "$d")" || rc=1
	done
	rm -rf "$tmp"
	return "$rc"
}
shellcheck_all() { files -- '*.sh' | xargs -0 "$1"; }
gitleaks_files() { # the files git would commit: tracked and new, ignored ones left out
	local tmp f rc=0
	tmp=$(mktemp -d)
	while IFS= read -r -d '' f; do
		[ -f "$f" ] || continue
		mkdir -p "$tmp/$(dirname "$f")"
		cp -p "$f" "$tmp/$f"
	done < <(files)
	"$1" dir --redact --no-banner --no-color --verbose "$tmp" || rc=$?
	rm -rf "$tmp"
	return "$rc"
}

PY=$(any_python)
if have "$PY310"; then
	step tests-stdlib env -u REQUIRE_UPSTREAM "$PY310" -B scripts/ci-unittest.py
else
	skip tests-stdlib "$PY310 not found (set CI_PY310)"
fi

if [ -x "$FULL/bin/python" ]; then
	caddy=$(tool caddy)
	step verify-env "$FULL/bin/python" -B scripts/ci-checks.py verify-env
	step tests-full env REQUIRE_UPSTREAM=1 REQUIRE_TELEMETRY=1 CADDY="${caddy:-}" "$FULL/bin/python" -B scripts/ci-unittest.py
	if [ -n "$caddy" ]; then
		step e2e "$FULL/bin/python" -B scripts/e2e-local.py --work "$CACHE/e2e" --caddy "$caddy" \
			--server-port "${CI_E2E_SERVER_PORT:-8340}" --edge-port "${CI_E2E_EDGE_PORT:-8341}"
	else
		skip e2e "no caddy (run --setup)"
	fi
else
	skip verify-env "no full venv (run scripts/ci-local.sh --setup)"
	skip tests-full "no full venv (run scripts/ci-local.sh --setup)"
	skip e2e "no full venv (run scripts/ci-local.sh --setup)"
fi

if [ -x "$HISTORYV/bin/python" ]; then
	# With COACH_TEST_DATABASE_URL set, the Postgres tests must run too.
	require_db=0
	if [ -n "${COACH_TEST_DATABASE_URL:-}" ]; then require_db=1; fi
	step tests-history env REQUIRE_UPSTREAM=1 REQUIRE_TELEMETRY=1 REQUIRE_HISTORY=1 REQUIRE_DATABASE="$require_db" \
		CADDY="$(tool caddy)" "$HISTORYV/bin/python" -B scripts/ci-unittest.py
else
	skip tests-history "no history venv (run --setup)"
fi

if [ -x "$EVALS/bin/python" ]; then
	step evals-offline evals_offline
else
	skip evals-offline "no evals venv (run --setup)"
fi

if [ -x "$TOOLS/bin/ruff" ]; then
	step ruff "$TOOLS/bin/ruff" check --no-cache .
elif have ruff; then
	step ruff ruff check --no-cache .
else
	skip ruff "no ruff (run --setup)"
fi

for t in shellcheck hadolint actionlint gitleaks; do
	path=$(tool "$t")
	if [ -z "$path" ]; then
		skip "$t" "no $t (run --setup)"
		continue
	fi
	case "$t" in
	shellcheck) step shellcheck shellcheck_all "$path" ;;
	hadolint) step hadolint "$path" --no-color Dockerfile ;;
	actionlint) step actionlint env PATH="$BIN:$PATH" "$path" -no-color ;;
	gitleaks)
		if git rev-parse --verify -q HEAD >/dev/null; then
			step gitleaks-history "$path" git --redact --no-banner --no-color --verbose .
		else
			skip gitleaks-history "no commit yet"
		fi
		step gitleaks-files gitleaks_files "$path"
		;;
	esac
done

step text-scan "$PY" -B scripts/ci-checks.py text-scan
step pin-drift "$PY" -B scripts/ci-checks.py pin-drift
step version "$PY" -B scripts/ci-checks.py version
# The history venv holds every feature package (telemetry and history), so their metadata is checked too.
if [ -x "$HISTORYV/bin/python" ]; then
	step licenses "$HISTORYV/bin/python" -B scripts/ci-checks.py licenses
else
	step licenses "$PY" -B scripts/ci-checks.py licenses
fi
if have docker; then # the CLI only: no daemon is needed
	step compose "$PY" -B scripts/ci-checks.py compose
else
	skip compose "no docker CLI"
fi
if [ -x "$TOOLS/bin/python" ]; then
	step pip-audit "$TOOLS/bin/python" -B scripts/ci-checks.py audit
else
	skip pip-audit "no tools venv (run --setup)"
fi

echo
echo "ci-local summary (container smoke test: scripts/ci-smoke.sh, needs Docker):"
printf '  %s\n' "${RESULTS[@]}"
for r in "${RESULTS[@]}"; do
	case "$r" in FAIL*) exit 1 ;; esac
done
