#!/usr/bin/env bash
# The model-behaviour evals (docs/evals.md) with the right Python.
#   scripts/evals.sh setup                  build the evals venv once (needs network)
#   scripts/evals.sh run --smoke            live: needs ANTHROPIC_API_KEY in the environment
#   scripts/evals.sh run [--cases T01,F04] [--model ..] [--trials N] [--max-usd X] ...
#   scripts/evals.sh grade evals/runs/<id>  offline, no key
#   scripts/evals.sh compare <base> <new>   offline, no key
#   scripts/evals.sh list | label <run_dir>
# The venv is CI_CACHE/venv-evals (default ${XDG_CACHE_HOME:-$HOME/.cache}/coach-mcp-ci),
# the one scripts/ci-local.sh --setup builds; EVALS_PYTHON overrides it. The API key is
# read from the environment only: never pass it as an argument, and this script never
# prints it.
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONDONTWRITEBYTECODE=1

CACHE="${CI_CACHE:-${XDG_CACHE_HOME:-$HOME/.cache}/coach-mcp-ci}"
VENV="$CACHE/venv-evals"
PY312="${CI_PY312:-python3.12}"
PY="${EVALS_PYTHON:-$VENV/bin/python}"

if [ "${1:-}" = setup ]; then
	mkdir -p "$CACHE"
	rm -rf "$VENV"
	"$PY312" -m venv "$VENV"
	"$VENV/bin/python" -B scripts/ci-checks.py install-full
	"$VENV/bin/python" -m pip install --disable-pip-version-check --require-hashes -r evals/requirements.txt
	echo "evals: venv ready in $VENV"
	exit 0
fi

for arg in "$@"; do
	case "$arg" in
	*sk-ant-*)
		echo "evals: an argument looks like an API key; put it in ANTHROPIC_API_KEY instead" >&2
		exit 2
		;;
	esac
done

if [ "${1:-}" = run ] && [ -z "${ANTHROPIC_API_KEY:-}" ]; then
	echo "evals: a live run needs ANTHROPIC_API_KEY in the environment; refusing" >&2
	exit 2
fi

if [ ! -x "$PY" ]; then
	echo "evals: no Python at $PY; run scripts/evals.sh setup (or set EVALS_PYTHON)" >&2
	exit 1
fi

exec "$PY" -B -m evals "$@"
