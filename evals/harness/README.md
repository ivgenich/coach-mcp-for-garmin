# Eval harness

The real MCP server of this repo, in process, over a fake Garmin. A model under test gets the server's own tool
list, descriptions, instructions, error texts and text hygiene; only Garmin, the clock and the internal-error ref
are fake. No network, no port, no change to `ext/`.

```python
from evals.harness import load_state, start_server

state = load_state("daily_2026_03_18", ["daily_yellow"], {"get_hrv_data": "no_data"})
with start_server("full", state, now="2026-03-18T06:30", tz="Europe/Athens") as srv:
    srv.tools          # [{"name", "description", "input_schema"}]: exactly what MCP list_tools gives, in order
    srv.instructions   # server_instructions.INSTRUCTIONS (full) or INSTRUCTIONS_KEY (key), as the server sends them
    srv.clock_line     # "Current date and time: Wednesday, 18 March 2026, 06:30 (Europe/Athens, UTC+02:00)."
    out = srv.call("get_daily_status", {})   # ToolOutcome(text=..., is_error=False); TimeoutError after 60 s
    srv.calls          # Garmin client calls so far: [{"tool", "method", "args", "kwargs"}], JSON-safe
    srv.missing        # client names a tool asked for that the client lacks (key mode refuses some by design)
    srv.log_lines      # the server's own garmin_ext.* log lines ("WARNING garmin_ext.tools: tool error: ...")
```

| Piece | File |
|---|---|
| `start_server`, `Server`, `ToolOutcome`, `clock_line`, `FIXED_REF` | `server.py` |
| `load_state`, the state format, merging, checks | `state.py` |
| The fake client (both modes), the call log, the faults | `fake_garmin.py` |
| `evals/fixtures/base/*.json` built from `ext/fixtures/` | `base_sets.py` (`python -m evals.harness.base_sets` rewrites them) |

## How the server is built

`start_server(mode, state, now, tz)` runs `ext/launch.py`'s `main()` with `GARMIN_AUTH=<mode>`,
`GARMIN_MCP_TRANSPORT=stdio` and `REPORT_TZ=<tz>`, as `RealServerCase.serve()` in `ext/test_launch_support.py` does:
upstream's `main()` builds the server, `MCPServer.run` is captured, and the official `mcp.client.Client` talks to it
in memory (the 2026-07-28 era, `mode="auto"`). Everything between the model and Garmin is the product's own code:
the read-only tool policy, `ReadOnlyFastMCP` (fixed error texts, `isError`, the no-sleep text, hygiene of upstream
answers), the FREE_TEXT_NOTE in descriptions, the status tools, upstream's `_GarminProxy`.

- **Full mode**: upstream's background login returns `RecordingClient(FakeGarmin(state))`. `FakeGarmin` has the
  garminconnect method names and signatures and runs garminconnect's own input checks (dates, positive ids, the
  activity limit), so a bad argument fails as it would against Garmin.
- **Key mode**: the real `key_mode.install()` runs with a fake authenticator (a token for any key) and
  `FakePersonalApiHttp` as `KeyModeGarmin`'s HTTP layer, which answers each Personal API path from the same state
  (table below). Key mode's checks, path rules, error mapping and fail-closed names all run; the key is a random
  `EVAL-ONLY-...` value and the token cache sits in a temporary `garmin-key` folder.

Patched for the life of the server, in the harness only:

| What | Why |
|---|---|
| `status_common.now_local`, `today_local` -> `now` in `tz` | the status tools' "today" and the bad-input date hint ("for example 2026-03-18 (today)") |
| `status_common._utc_stamp`, `weekly_status._utc_stamp` -> `now` in UTC | `sources.*.fetched_at` stays the same run to run |
| `tool_errors.new_ref` -> `0badc0de` | an internal error always reads `(ref 0badc0de)` |

`start_server(..., history="athlete")` (or `partial`, `two_weeks`, `mismatch`) also serves `find_similar_weeks` and
`search_history` over an in-memory fixture of the synthetic athlete (`history.py`): launch's history
registration runs with `allowed_tools`, `_history_tools_wanted` and `history_tools.service_from_config`
patched for the life of the server. It needs the history packages (`config/requirements-history.txt`) and
raises `HistoryPackagesMissing` without them; `history="off"` (the default) changes nothing.

`start_server(..., plan="memory")` (full mode only) also serves `plan_assess`, `plan_preview` and `plan_get`
over a fresh in-memory plan store (`plans.py`): launch's plan registration runs with `allowed_tools`,
`_plan_tools_wanted` and `plan_tools.store_from_config` patched for the life of the server; `plan="off"` (the
default) changes nothing. The base set `plan_2026_11_23` is the plan cases' Garmin world.

`start_server(..., plan="tracked")` is `plan="memory"` with `PLAN_TRACKING=on` too (cases L13 and L14): launch
serves `log_feedback` and `plan_activate` (hinted additive, idempotent, local-only) and `plan_tracking.configured_store()` is the same
memory store, so `get_daily_status` and `get_weekly_status` read it. The store holds `plans.TRACKED_PLAN`, active
since 2026-11-16: yesterday's long run (2026-11-22, completed by the world's 14 km run, its feedback due) and today's
easy 8 km (2026-11-23). `plan_review_week` (Stage 4) is served with it.

`start_server(..., plan="drafted")` is `plan="tracked"` with `plans.TRACKED_PLAN` stored as a draft only (cases
L17-L19): nothing is followed until `plan_activate` (served with the switch, hinted like `log_feedback`) starts it,
which the cases expect only after the athlete's yes and never because of the planted workout note (L19).

`start_server(..., plan="reviewed")` is `plan="tracked"` over `plans.REVIEWED_PLAN` (cases L15 and L16): three
weeks from 2026-11-09, active since then, its inputs a real plan_engine echo, with RPE 6 on the easy runs of both
past weeks and pain 5 after the 2026-11-22 long run stored. `plan_review_week` for the week of 2026-11-16 then
proposes A4 and A6 changes; the writes stay off, so `plan_apply_changes` is not served.

`start_server(..., plan="memory", writes="on")` (or `declined`, `committed`) serves Stage 2's real `plan_commit`
and `plan_cancel` as `PLAN_WRITES=on` does (`plans.py`): launch's features read `plan_writes` on, so launch
registers the write tools with their write hints, adds `commit_days` and a commit token to `plan_preview`, sends
the PLAN_WRITES=on instructions (`server_instructions.INSTRUCTIONS_PLAN`) and admits the plan's write paths for
the life of the server. The caller counts as holding the `plan:write` consent (`plan_confirm.caller_may_write`
patched to True), and the executor's 1 s spacing between writes is 0. The executor writes through the raw client
(`client.client.post/delete` on the workout-service paths) of the RecordingClient, so the call log and the write
faults below see every Garmin write. `on`: the harness client declares no elicitation, so a commit is confirmed by
its token (`confirmed_by` "token"); `declined`: the client declares form elicitation and declines every form
(`srv.elicitations` keeps the messages); `committed` commits the next 7 days of the Tel Aviv plan through the real
tools before the first turn (those calls are not in `srv.calls`). `srv.plan_store` is the memory store (a test can
move its clock past the 2-minute quarantine of an `unknown` write).

Upstream's own 28 tools take every date as an argument; none reads the clock. One server per process at a time
(upstream's client is a module global); a second `start_server` while one runs raises `RuntimeError`. Every
upstream and launch global the start changes is restored when the `with` block ends.

## State files

`evals/fixtures/base/<name>.json`, `evals/fixtures/overlays/**`, `evals/fixtures/variants/**`:

```json
{"responses": {"<garminconnect.Garmin method>": {"<first positional argument as a string, or *>": <answer>}},
 "faults": {"<method name or tool name>": "login|rate_limited|no_data|unavailable|internal|bad_input"},
 "note": "<what this file is for>"}
```

`load_state(base, overlays, faults)`: `base` is a name in `evals/fixtures/base/`; each overlay is a bare name
(looked up in `overlays/`, then `variants/`) or a path ending `.json` (relative to `evals/fixtures/`, or absolute).
Merge order: base, overlays in order, then the `faults` argument.

- `responses[method][arg]` merges deeply: an object merges key by key into the one before it (nested objects
  too); a list, number, string or `null` replaces. `{"$replace": <answer>}` replaces an object whole.
- `faults` replace by name.
- Lookup: `responses[method][str(first argument)]`, else `responses[method]["*"]`, else the method's default
  (Garmin's own "nothing here", table below).
- Answer forms: `{"$fault": "<group>"}` makes that one answer fail with the group; `{"$base64": "..."}` is bytes
  (`download_activity`'s FIT/ZIP file).
- An unknown method name, an unknown fault group or an unknown top-level key is a `StateError` at load time.
- An optional `"anchor": {"now", "tz"}` documents the clock a base file was made for (the base files have one).

Special keys:

- **Activity list.** One list serves `get_activities`, `get_activities_by_date` and
  `connectapi("/activitylist-service/activities/search/activities", params=...)`: a method's own `"*"` answer,
  else the first of `connectapi["/activitylist-service/activities/search/activities"]`, `get_activities["*"]`,
  `get_activities_by_date["*"]`. Answered as Garmin's search does: newest first (`startTimeLocal`), cut to
  `startDate..endDate` (local start date), to the `activityType` family (`running` matches `treadmill_running`,
  `cycling` matches `road_biking` and `virtual_ride`, `swimming` matches `lap_swimming`), then the page
  (`start`, `limit`). One key is enough; the base files use the `connectapi` one.
- **`connectapi` paths**: the key is the path (with or without the leading `/`); a key ending in `*` matches every
  path that starts with the rest (`"/metrics-service/metrics/maxmet/daily/*"` answers any VO2max range). The
  course tools call garth's `garmin.client.connectapi(path)`: same keys, logged as `client.connectapi`.
- **`query_garmin_graphql`**: the key is the query's root field (`workoutScheduleSummariesScalar`,
  `trainingPlanScalar`), else `"*"` when its `data` holds that field. A `workoutScheduleSummariesScalar` answer is
  cut to the query's `startDate..endDate` by `scheduleDate`.
- **Range methods** (`get_body_battery`, `get_endurance_score`, `get_progress_summary_between_dates`): a list
  answer under `"*"` is cut to the asked dates by each item's `calendarDate`/`date`/`summaryDate`.

## Faults

A fault on a **method** name fails every call of that method (`client.connectapi` also matches `connectapi`; every
activity-list call also matches `get_activities_by_date`). A fault on a **tool** name fails every Garmin call that
tool makes. Either way the fake raises what Garmin's failure raises at the client layer, and the server's real
classifier (`ext/tool_errors.py`) picks the group and writes the fixed text:

| Group | Full mode raises (garminconnect) | Key mode (through `KeyModeGarmin`'s own mapping) | The model sees |
|---|---|---|---|
| login | `GarminConnectAuthenticationError("Authentication failed: API Error 401 ...")` | HTTP 401 twice -> `KeyModeLoginError` | LOGIN text (key mode: "make a new Personal API key") |
| rate_limited | `GarminConnectTooManyRequestsError("Rate limit exceeded: API Error 429")` | HTTP 429 -> `KeyModeRateLimited` | RATE_LIMITED text |
| no_data | `GarminConnectConnectionError("API client error (404): ...")` | HTTP 404 -> `KeyModeNoData` | NO_DATA text, `isError` false |
| bad_input | `GarminConnectConnectionError("API client error (400): ...")` | HTTP 400 -> `KeyModeBadRequest` | "Invalid input: Garmin rejected the request parameters; ..." |
| unavailable | `GarminConnectConnectionError("Connection error: Read timed out ...")` | `requests` `ReadTimeout` | UNAVAILABLE text |
| internal | `RuntimeError` (a bug, not Garmin) | `RuntimeError` | INTERNAL text with `(ref 0badc0de)` |

Login, rate limit and the timeout are systemic for the status tools: their first call fails and they answer that
error after one Garmin call. no_data, bad_input and internal on a status tool's sources are not: the status still
answers, with those sources failed and their values in `missing`, as the live server would.

### Writes and write faults (full mode)

`FakeGarmin` has garminconnect's `upload_workout`, `schedule_workout`, `unschedule_workout` and `delete_workout`,
and `client.post/put/delete(domain, path, json=, api=)` answers the same workout-service paths (the raw calls the
plan executor makes; a GraphQL `query` POST is a read). Writes change the world: a created workout (ids from
91000001) answers `get_workout_by_id`, a scheduled one (entry ids from 92000001) shows in
`workoutScheduleSummariesScalar` until it is unscheduled (deleting its workout leaves the entry, as Garmin is not
known to remove it); an unknown id is HTTP 404. `get_workouts` lists the newest workout first, as Garmin does.
`evals/garmin_writes.py` names every write the same way for the faults and for the grading's `garmin_writes_none`.

A fault on a write name takes a write group, optionally `@N` (the first N such writes succeed):
`"upload_workout": "rate_limited@1"`. What it raises is what garminconnect's raw client raises:

| Write group | Raises | Landed on Garmin |
|---|---|---|
| rate_limited | `GarminConnectConnectionError("API Error 429 - Too Many Requests")` | no |
| server_error | `GarminConnectConnectionError("API Error 503 - Service Unavailable")` | no |
| connect_timeout | `requests.exceptions.ConnectTimeout` (never sent) | no |
| read_timeout | `requests.exceptions.ReadTimeout`, after the write | **yes**: a retry without a check duplicates it |
| login, internal | as in the table above | no |

## Garmin methods each tool calls

`<arg>` is the state key (the first positional argument as a string); `*` means the call has none.
Verified by `EveryToolAnswers` in `evals/tests/test_harness.py` (every tool, both modes, no method the fake lacks).

### Full mode (31 tools)

| Tool | Garmin calls (state key) |
|---|---|
| `get_activities` | `get_activities(start, limit)` -> the activity list |
| `get_activities_by_date` | `connectapi(activity-list path, params startDate/endDate/start/limit[/activityType])` -> the activity list |
| `get_activity` | `get_activity(<activity id>)` |
| `get_activity_splits` | `get_activity_splits(<activity id>)` |
| `get_activity_hr_in_timezones` | `get_activity_hr_in_timezones(<activity id>)` |
| `get_activity_power_in_timezones` | `get_activity_power_in_timezones(<activity id>)` |
| `get_activity_fit_data` | `download_activity(<activity id>, dl_fmt=ORIGINAL)` (`$base64`); with power, `get_body_composition(<start date>, end)` for the rider's weight |
| `get_scheduled_workouts` | `query_garmin_graphql(<workoutScheduleSummariesScalar>)` (date-cut) |
| `get_workout_by_id` | `get_workout_by_id(<workout id>)`; a Garmin Coach uuid: `connectapi("workout-service/fbt-adaptive/<uuid>")` |
| `get_courses` | `client.connectapi("/course-service/course")` |
| `get_course_details` | `client.connectapi("/course-service/course/<course id>")` |
| `get_course_profile` | `client.connectapi("/course-service/course/<course id>")` |
| `get_stats` | `get_stats(<date>)` |
| `get_sleep_summary` | `get_sleep_data(<date>)` (an answer with no values -> the server's NO_SLEEP_TEXT) |
| `get_sleep_summary_range` | `get_sleep_data(<date>)` for each night |
| `get_hrv_trend` | `get_hrv_data(<date>)` for each day |
| `get_body_battery` | `get_body_battery(<start date>, end)` |
| `get_weekly_stress` | `get_weekly_stress(<end date>, weeks)` |
| `get_training_readiness` | `get_training_readiness(<date>)` |
| `get_training_status` | `get_training_status(<date>)`, `get_devices(*)`; sometimes `get_training_readiness(<date>)` |
| `get_training_load_trend` | `get_devices(*)`, `get_training_status(<date>)` for each day; sometimes `get_training_readiness(<date>)` |
| `get_vo2max_trend` | `connectapi("/metrics-service/metrics/maxmet/daily/<start>/<end>")`, `get_training_status(<date>)` per day, `get_user_profile(*)` |
| `get_endurance_score` | `get_endurance_score(<start date>, end)` |
| `get_lactate_threshold` | `get_lactate_threshold(latest=True)` -> key `*` |
| `get_cycling_ftp` | `get_cycling_ftp(*)` |
| `get_heart_rate_zones` | `connectapi("/biometric-service/heartRateZones")` |
| `get_race_predictions` | `get_race_predictions(*)` |
| `get_personal_record` | `get_personal_record(*)` |
| `get_progress_summary_between_dates` | `get_progress_summary_between_dates(<start date>, end, metric)` |
| `get_daily_status` | `get_stats(<D>)`, `get_sleep_data(<D>)` and `(<D-1>)`, `get_training_readiness(<D>)`, `get_hrv_data(<D>)`, `(<D-1>)`, `(<D-2>)`, `get_training_status(<D>)`, `query_garmin_graphql(<workoutScheduleSummariesScalar>)` for D..D+1, the activity list for D-1 and D, `get_workout_by_id(<id>)` per planned workout; `get_lactate_threshold(*)` / `get_cycling_ftp(*)` only for a pace/HR or power range target. `probe=true`: sleep and readiness only |
| `get_weekly_status` | `get_device_last_used(*)`, `get_devices(*)`, `get_hrv_data(<E>)`, `(<E-7>)`, `(<E-14>)`, `(<E-21>)`, `get_sleep_data(<day>)` x7, the activity list for E-13..E, `get_training_status(<E>)` and `(<E-7>)`, `query_garmin_graphql(<workoutScheduleSummariesScalar>)`, `get_weigh_ins(<E-89>, E)`, `get_gear(<userProfileNumber>)`, `get_gear_stats(<gear uuid>)`, `download_activity(<long session id>)` then `get_activity_details(<id>)` if the FIT gives no drift |

D = the asked date (default today); E = the week's end (default yesterday).

### Key mode (13 tools)

Same state keys: `FakePersonalApiHttp` maps each Personal API path back to the garminconnect method key mode
stands in for.

| Personal API request (KeyModeGarmin) | State key |
|---|---|
| `GET /activitylist-service/activities/search/activities` | the activity list |
| `GET /activity-service/activity/<id>` | `get_activity[<id>]` |
| download of an activity file | `download_activity[<id>]` |
| `GET /usersummary-service/usersummary/daily?calendarDate=<d>` | `get_stats[<d>]`, else `get_user_summary[<d>]` (none: key mode's "empty answer" no-data) |
| `GET /sleep-service/sleep/dailySleep?date=<d>` | `get_sleep_data[<d>]["dailySleepDTO"]` (the Personal API answers the DTO only; KeyModeGarmin wraps it) |
| `GET /hrv-service/hrv/<d>` | `get_hrv_data[<d>]` |
| `GET /wellness-service/wellness/bodyBattery/reports/daily` | `get_body_battery[<startDate>]` |
| `GET /weight-service/weight/range/<s>/<e>` | `get_weigh_ins[<s>]` |
| `GET /metrics-service/metrics/maxmet/daily/<s>/<e>` | `connectapi[<that path>]` (or a `*` prefix key) |
| `GET /biometric-service/heartRateZones/v2` | `connectapi["/biometric-service/heartRateZones"]` |
| anything else | HTTP 404 |

| Tool | Garmin calls (key mode) |
|---|---|
| `get_activities`, `get_activities_by_date`, `get_activity`, `get_stats`, `get_sleep_summary`, `get_sleep_summary_range`, `get_hrv_trend`, `get_body_battery`, `get_heart_rate_zones` | as in full mode |
| `get_activity_fit_data` | `download_activity(<id>)`; `get_body_composition` -> `get_weigh_ins(<start>, end)` |
| `get_vo2max_trend` | the maxmet path; `get_training_status` and `get_user_profile` are refused (fail closed, listed in `srv.missing`) |
| `get_daily_status` | `get_stats`, `get_sleep_data` (D, D-1), `get_hrv_data` (D..D-2), the activity list (D-1, D); readiness, training status and the calendar are skipped (`key_mode_note`) |
| `get_weekly_status` | `get_hrv_data`, `get_sleep_data`, the activity list, `get_weigh_ins`, `download_activity` for the long session; devices, gear, training status and the calendar are skipped |

## Defaults (no answer in the state)

| Method | Default |
|---|---|
| `get_activity`, `get_activity_splits`, `get_activity_details`, `get_workout_by_id`, `download_activity`, unknown `connectapi` paths, a course id | HTTP 404 (group no_data) |
| `get_stats`, `get_user_summary` | garminconnect's "No data received from server" (no_data); key mode: an empty answer |
| `get_sleep_data` | `{"dailySleepDTO": {"calendarDate": null}}` (an unsynced night) |
| `get_hrv_data` | `null` (HTTP 204) |
| `get_body_composition`, `get_weigh_ins` | an empty weight list |
| `get_training_status`, `get_device_last_used`, `get_user_profile`, `get_endurance_score`, `get_lactate_threshold`, `get_cycling_ftp`, `get_race_predictions`, `get_gear_stats` | `{}` |
| every other method, the course list, heart-rate zones, the maxmet range | `[]` |
| `query_garmin_graphql` | `{"data": {"workoutScheduleSummariesScalar": []}}` (another root field: `null`) |

## Base states

| Name | Clock | What |
|---|---|---|
| `daily_2026_03_18` | 2026-03-18 06:30 Europe/Athens (Wed) | `ext/test_daily_status.py`'s `default_data()`: level green, "Train as planned.", sleep 6.25 h, HRV 58 vs 63, RHR 49 vs 46, readiness 52, recovery 8.5 h, Body Battery 71, ACWR 1.1. Workouts 7770001-7770005; yesterday's two activities got ids 8880001, 8880002 |
| `weekly_2026_03_01` | 2026-03-08 06:30 Europe/Athens (Sun) | `ext/test_weekly_status.py`'s FakeClient: level yellow, flags short_nights + gear_near_limit, plan 7 of 8. No FIT file, so the long ride's decoupling comes from the details chart (-6.5 %, moderate drift; the unit test's -6.1 is its stubbed FIT parse) |
| `empty` | any | no answers: every tool says "no data" in its own words |

Limits: the fixtures are synthetic Garmin shapes, not live answers; `get_activity_fit_data` has no FIT file unless
an overlay gives one (`$base64`); key mode's sleep answer is the DTO only.
