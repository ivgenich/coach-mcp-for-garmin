# Contributing

Thanks for helping. This is a personal project, maintained on a best-effort basis: small, focused
pull requests are the easiest to review.

## Before you open an issue

- **Never paste health data, tokens, secrets, keys, emails or logs with personal data** into an
  issue or a pull request. Garmin answers hold names, places and GPS tracks; server logs can hold
  your host name. Replace them with made-up values.
- A security problem goes to a private report, not an issue: see [SECURITY.md](SECURITY.md).
- First look at [Troubleshooting](docs/troubleshooting.md).

## Run the tests

Python 3.10 or newer. The stdlib leg needs nothing installed:

```bash
python3 -B scripts/ci-unittest.py
```

The other CI steps (the full test leg with upstream garmin_mcp and the pinned mcp, the linters,
the secret scan) run on your computer with `scripts/ci-local.sh`: run
`scripts/ci-local.sh --setup` once (it needs network and `python3.10` and `python3.12`), then
`scripts/ci-local.sh`. It writes nothing into the repository. The container smoke test needs
Docker: `scripts/ci-smoke.sh`; so does the Docker Compose path test, `scripts/ci-compose.sh`
(`scripts/ci-local.sh --compose`). More: [ext/README.md](ext/README.md).

## Style

- Plain, short English in docs, comments and messages. Say what the code does.
- Python 3.10 compatible; `ruff check .` passes (`ruff.toml`). Shell scripts pass `shellcheck`.
- Every behaviour change comes with a test, and with the doc page that describes it.
- Every tool stays read-only, except the plan write tools behind `PLAN_WRITES=on` (`plan_commit`,
  `plan_cancel`): they write only through a token's ops and only the server's own workouts
  ([SECURITY.md](SECURITY.md#how-plan-writes-behave)). A new write needs its own switch, default
  off. No secret, key or Garmin value may reach a log line, an error text or a trace.
- Add a line under `## [Unreleased]` in [CHANGELOG.md](CHANGELOG.md) for anything a user notices.
