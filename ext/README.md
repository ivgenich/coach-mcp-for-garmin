# Extra tools

Tools this repo adds to upstream garmin-mcp without forking it. `launch.py` wraps
upstream's `courses.register_tools`, so the extra tools register on the same app: the
tool policy and the effect annotations apply to them like any upstream tool.
`entrypoint.sh` starts the server through `launch.py`, and the Dockerfile calls
`launch.patch()` at build time (which also runs the status modules' `selfcheck()`), so
an upstream bump that breaks a hook or removes a helper they import fails the build, not
the live server.

Tool policy (read-only, enforced in `launch.py`): the allowed set is the read-only tools
(upstream's `tool_annotations.READ_ONLY_TOOLS` plus the three below, minus any name
upstream lists as a write tool) that are listed in `config/enabled-tools.txt`, which is
baked into the image. `GARMIN_ENABLED_TOOLS` can only narrow that set: every name in it
must be in the file, so a write tool, an unknown name or a read-only tool the file leaves
out is a startup error. `GARMIN_DISABLED_TOOLS` is refused: a set value stops startup. A
second check at registration time drops any tool outside the set, so a write tool is
never registered even if upstream changes.

Startup checks: `entrypoint.sh` runs `launch.py --check-config` before it starts Caddy.
It checks the tool policy and `REPORT_TZ` (`status_common.check_report_tz`: required, a
valid IANA zone, no default) and exits non-zero with a one-line message, so a bad
setting stops the container instead of looping restarts. If a tool still runs without
`REPORT_TZ` (started by hand, or in a test), it returns `Error: REPORT_TZ is not set: ...`
instead of guessing a zone.

| Tool | What it adds |
|---|---|
| `get_course_profile` | A course's climbs (start/end km, length, gain, average and steepest-100 m grade) and an elevation profile thinned to ~500 points. Upstream's `get_course_details` counts the geo points but returns none. |
| `get_daily_status` | One day's status: a level (green, yellow, red, incomplete), a recommendation for today's planned workout, recovery metrics each beside your own baseline, training load, today's and tomorrow's planned workouts classed hard (relative to your lactate threshold and FTP, reported under `thresholds_athlete`) or long and marked done when a linked or same-day same-sport activity exists, the flags that fired and what data is missing. Done workouts no longer count for the hard-day rules; when all of today's are done the recommendation is `done_today`. Every number and decision is computed on the server by fixed rules (README "Daily status rules v1"). `probe=true` only checks whether last night's sleep and Training Readiness have synced (2 Garmin calls); `text_format="markdown"` adds the English report text. Read-only. |
| `get_weekly_status` | The 7 days ending `end_date` (default yesterday) against the 7 before: HRV trend, sleep, volume per sport, load, planned-workout completion (linked by Garmin or matched by a same-sport activity on any day of the week, with how many were done on the planned day), the week's long session, fitness, weight, gear mileage, flags. `text_format="markdown"` adds the English report text. Read-only. |

Climbs on a course are found by distance (a course has no timing), using the same 100 m
grade window and 3% start threshold as upstream's time-based detection on recorded rides;
a climb ends after 300 m below the threshold.

| File | Role |
|---|---|
| `launch.py` | Hooks the tools into upstream; `patch()`; the tool policy and `--check-config` |
| `mcp2_compat.py` | Runs upstream, written for mcp 1.x, on mcp 2.x (below) |
| `course_profile.py` | `get_course_profile` |
| `status_common.py` | Shared helpers: time zone (`REPORT_TZ`, required; `check_report_tz`), parallel Garmin calls with timeouts, rounding, error cleaning, matching planned workouts to activities |
| `daily_status.py` | `get_daily_status`: the rules (`THRESHOLDS`, `RULES_VERSION`) and `render_text` |
| `weekly_status.py` | `get_weekly_status`, same layout |
| `phrasing.py` | Checks for English text written from a status; nothing calls them yet. Each returns every problem it finds, an empty list when the text passes. `check_text` is the numbers check: every number must trace back to the status (a threshold only inside its flag label or the template's own wording), within the word limit, with no tag but `<b>`. `check_all(text, status, kind="daily")` (or `"weekly"`) adds the rules a full report message must meet: the level word, every flag label and the recommendation verbatim, no other level, no Markdown, links, @mentions or /commands, no emoji but the level marker, no number words. A one- or two-sentence summary such as the page's `summary_en` fails `check_all` by design; check it with `check_text`, `check_style` and `check_number_words`. |

## mcp 2.x adapter

claude.ai and Claude Code open with `server/discover` at protocol 2026-07-28, which only
mcp 2.x serves; upstream still imports mcp 1.x's `mcp.server.fastmcp.FastMCP`, which mcp
2.x renamed to `mcp.server.mcpserver.MCPServer` (and moved host, port and stateless_http
from the constructor to `run()`). `mcp2_compat.install()` puts a module in
`sys.modules["mcp.server.fastmcp"]` whose `FastMCP` subclasses `MCPServer`, takes the 1.x
constructor arguments and passes the HTTP ones to `run("streamable-http", ...)`. It keeps
1.x's defaults: SSE-capable responses (`json_response=False`) and, bound to 127.0.0.1,
DNS-rebinding protection (Caddy sends `Host: 127.0.0.1:8000`, so a public Host gets 421
when it bypasses Caddy). Its `tool()` wraps each tool so an exception's message reaches the
client (`Error executing tool <name>: <message>`, as in 1.x; mcp 2.x alone drops it), with
names, docstrings and input schemas unchanged. It raises `ShimError` unless mcp is 2.x with
the expected API.

- `launch.py` calls `install()` before importing upstream; the Dockerfile's build check
  (`launch.patch()`) therefore fails the build if the adapter does not fit.
- `set_server_overrides()` sets constructor arguments that win over upstream's own:
  `launch.py` uses it so `serverInfo.name` is `coach-mcp-for-garmin`, not upstream's
  "Garmin Connect v1.0" (`serverInfo.version` stays the mcp version).
- Other entry points, above all `garmin-mcp-auth`, import upstream too, so the Dockerfile
  writes `garmin_mcp2_compat.pth` into site-packages. It only calls `register_finder()`, an
  import hook that runs `install()` on the first import of `mcp.server.fastmcp`.
- The tests that import upstream call `register_finder()` themselves (a no-op on mcp
  1.x), so they need no `.pth`. Under mcp 1.x the `launch.patch()` tests are skipped.
- `constraints.txt` pins mcp 2.3.0 against upstream's `mcp<2` cap; garmin-mcp is
  installed with `--no-deps`, so `pip check` reports that one conflict by design.
- When upstream ports to mcp 2.x itself (issue #344), delete the adapter, the `.pth`
  step and the `register_finder()` calls.

Tests (standard library only; the ones that need upstream garmin_mcp are skipped without
it; each test sets its own time zone): `python3 -m unittest discover -s ext -v`
