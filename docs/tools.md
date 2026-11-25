# Tools

The server exposes only read-only tools, and the list is baked into the image from
`config/enabled-tools.txt`. At startup it checks every name against the read-only tools
(upstream's plus this repo's), and it never registers a tool that changes data, whatever
the settings say. No setting can turn on a write tool. In `key` mode the server exposes
only the tools marked `yes` in the `key` column below, the names in
`config/enabled-tools-key.txt`; `GARMIN_ENABLED_TOOLS` can narrow that list further.

- **Add or remove a tool for good:** edit `config/enabled-tools.txt` (only read-only
  tools are accepted), then redeploy ([How to update](updates.md#how-to-update)).
- **Turn tools off without a rebuild:** set the service variable `GARMIN_ENABLED_TOOLS`
  to a comma-separated list of tool names. It can only narrow the baked list, never add
  to it. Every name must be in `config/enabled-tools.txt`: a write tool, an unknown name
  or a read-only tool the file leaves out stops the server at startup, with a one-line
  error in the server log. Delete the variable to go back to the whole baked list. If it
  is set when you add a tool to the file, add the tool to the variable too, and when you
  remove a tool from the file, remove it from the variable too.

`GARMIN_DISABLED_TOOLS` is no longer supported: if it is set, the server does not start
(and `deploy.sh` stops before deploying). Delete it:
`railway variable delete GARMIN_DISABLED_TOOLS`.

`get_course_profile`, `get_daily_status` and `get_weekly_status` are not upstream's: this
repo adds them from `ext/` (see [ext/README.md](../ext/README.md)). After adding a tool, open the Garmin
connector on claude.ai (Customize → Connectors), choose **Refresh tools list** from the
menu next to Disconnect, and start a new chat; claude.ai keeps the tool list it saw when
the connector was added. After a deploy that changes tool descriptions, use **Refresh tools
list** the same way, since claude.ai keeps the old descriptions too.

## Tools in each mode

| Tool | What it answers | `full` | `key` |
|---|---|---|---|
| `get_activities` | Your recent activities, newest first | yes | yes |
| `get_activities_by_date` | Activities in a date range | yes | yes |
| `get_activity` | One activity: training effect, load, power | yes | yes |
| `get_activity_splits` | One activity's laps, or a swim's strokes and SWOLF (large) | yes | no |
| `get_activity_hr_in_timezones` | Time in each heart-rate zone, for one activity | yes | no |
| `get_activity_power_in_timezones` | Time in each power zone, for one ride | yes | no |
| `get_activity_fit_data` | One activity's detailed records, from its FIT file (large) | yes | yes |
| `get_scheduled_workouts` | Planned workouts on your Garmin calendar | yes | no |
| `get_workout_by_id` | The steps of one planned workout | yes | no |
| `get_courses` | Your saved courses: distance, climb, sport | yes | no |
| `get_course_details` | One course's waypoints | yes | no |
| `get_course_profile` | One course's climbs and elevation profile (this project) | yes | no |
| `get_daily_status` | One day's level, flags and recommendation (this project) | yes | yes |
| `get_weekly_status` | A week against the week before (this project) | yes | yes |
| `get_stats` | One day's summary: resting heart rate, stress, Body Battery | yes | yes |
| `get_sleep_summary` | One night's sleep | yes | yes |
| `get_sleep_summary_range` | Sleep over several nights | yes | yes |
| `get_hrv_trend` | HRV over a range of days | yes | yes |
| `get_body_battery` | Body Battery | yes | yes |
| `get_weekly_stress` | Stress by week | yes | no |
| `get_training_readiness` | Training Readiness, newest first | yes | no |
| `get_training_status` | Training status and VO2max | yes | no |
| `get_training_load_trend` | Training load over time | yes | no |
| `get_vo2max_trend` | VO2max over time | yes | yes |
| `get_endurance_score` | Endurance score | yes | no |
| `get_lactate_threshold` | Lactate threshold | yes | no |
| `get_cycling_ftp` | Cycling FTP | yes | no |
| `get_heart_rate_zones` | Your heart-rate zones | yes | yes |
| `get_race_predictions` | Race time predictions | yes | no |
| `get_personal_record` | Personal records | yes | no |
| `get_progress_summary_between_dates` | Totals per sport between two dates | yes | no |

With history search on (`HISTORY_SEARCH=on`, Railway only for now), the server also has
`find_similar_weeks` (past weeks most like a given week) and `search_history` (stored days and
weeks that match words, flags or dates), in both modes. They read only your own Postgres and
never call Garmin. They are not in `config/enabled-tools.txt`: the switch adds them, and
`GARMIN_ENABLED_TOOLS`, when set, can narrow them away like any other tool.
[History search](history-search.md).

## What the server adds to upstream's answers

Upstream's answers are Garmin's data as JSON and are not this project's public API
([updates](updates.md)). Where they are easy to misread, the server adds fields (never removes or
renames one, and skips a field upstream already sends) and ends the tool's description with a short
note (`TOOL_NOTES` and `ANSWER_ADDITIONS` in `ext/launch.py`):

- `get_progress_summary_between_dates`: `unit`, Garmin's raw unit of `sum`, `avg`, `min` and `max`
  (`cm` for `distance` and `elevationGain`, `ms` for `duration` and `movingDuration`, `kcal` for
  `calories`), and per sport the same values converted: `sum_m` ... `max_m` (meters), `sum_s` ...
  `max_s` (seconds), or `sum_rounded` ... `max_rounded` (whole kcal).
- `get_training_status`, `get_training_load_trend`: `optimal_acute_load_min` and
  `optimal_acute_load_max`, the same values as `optimal_chronic_load_min/max`: the band is where
  acute load should sit, not a target for chronic load. Garmin's ratio (`load_ratio`, `acwr`) may be cut
  to one decimal; `get_daily_status` computes it to two.
- Description notes only: `get_body_battery` event and `get_activity_splits` lap `start_time` are
  GMT; `get_activities` and `get_activities_by_date` `start_time` and `get_training_readiness`
  `timestamp` are local time; `get_vo2max_trend` returns one sport (the one with more readings), and
  the running VO2max is `vo2_max` in `get_training_status`.
