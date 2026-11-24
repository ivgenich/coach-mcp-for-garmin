# Daily status

## What it returns

Ask "How am I today?" and Claude calls `get_daily_status` (the
[coach Project](../recipes/coach-project/README.md)'s instructions make it the first call).
The tool returns JSON with:

- `level`: `green`, `yellow`, `red`, or `incomplete` when neither sleep nor Training
  Readiness has synced yet; `recommendation` for today's planned workout.
- `recovery`: sleep (duration, score, stages), HRV beside its 7-day average and Garmin's
  status, resting HR beside its 7-day average, Training Readiness, recovery time, Body
  Battery, stress, SpO2, respiration.
- `load`: acute and chronic load, ACWR with Garmin's status, yesterday's sessions.
- `today` and `tomorrow`: planned workouts, each classed `hard_or_long` or easy and
  marked `completed` once done.
- `flags` (which rules fired) and `missing` (which values Garmin had none for).

Every number, flag, level and recommendation is computed on the server by the rules
below; Claude only phrases them. A value Garmin doesn't have is `null` and listed in
`missing`, never guessed, and a rule that needs it is skipped. Arguments: `date`
(YYYY-MM-DD, default today in the time zone of the required service variable
`REPORT_TZ`; see [Settings](deploy-railway.md#settings)), `text_format` (`markdown` adds the
report text, in English), `probe` (only "have last night's sleep and Training Readiness
synced?", 2 Garmin calls) and `include_sources` (adds trimmed raw Garmin payloads and
their key paths, for debugging). A full status takes about a dozen Garmin calls, made in
parallel with per-call timeouts; a call that fails marks its values missing instead of
failing the status. The exception: when the first call fails because the Garmin login is
missing or expired, Garmin's rate limit, or the network (no other call is then made), the
tool answers with that error instead of a status. A rate limit on any later call still
starts the server's 15-minute call guard ([Troubleshooting](troubleshooting.md)). The calibration recipe then ends with
"calibration failed: no thresholds": fix the Garmin login and run it again.

## Daily status rules v1

Baselines are your own: Garmin's 7-day averages. The thresholds are starting values (see
"Calibration"); each status carries them under `thresholds`, and the lactate threshold
and FTP values the hard-workout rule used under `thresholds_athlete`, with `rules_version`.

| Flag | Fires when | Level |
|---|---|---|
| `low_hrv` | last night's HRV ≤ its 7-day average − 10 ms | yellow |
| `hrv_low_trend` | Garmin's 7-day HRV average below the low end of your balanced range on each of the last 3 days (needs all three) | red |
| `high_rhr` | resting HR ≥ its 7-day average + 5 bpm | yellow |
| `short_sleep` | under 6 h of sleep (not when `sleep_debt` fires) | yellow |
| `sleep_debt` | under 6 h last night and the night before | red |
| `low_readiness` | Training Readiness under 40 | yellow |
| `load_spike` | Garmin's ACWR status is HIGH or VERY_HIGH; with no status, ACWR > 1.5 | red |
| `load_rising` | ACWR > 1.3 while Garmin's status is not HIGH or VERY_HIGH (never together with `load_spike`) | yellow |
| `recovery_incomplete` | a hard or long workout still to do today and Garmin's recovery time is still ≥ 6 h | yellow |

Garmin's schedule has a date but no time of day, so "recovery time longer than the time
until the workout" is approximated by the 6 h rule.

**Level:** any red flag → red; otherwise sleep and readiness both missing → incomplete;
otherwise 3 or more yellow flags → red, 1–2 → yellow, none → green.

**Recommendation:** nothing planned today → `no_planned_workout`; everything planned today
already done → `done_today`; level incomplete → `no_recovery_data` (decide by feel);
otherwise, by the workouts still to do:

| Level | Yellow flags | A hard or long one left today | Only easy ones left |
|---|---|---|---|
| green | 0 | `as_planned` | `as_planned` |
| yellow | 1 | `as_planned_may_stop`: as planned, stop if it feels wrong | `as_planned` |
| yellow | 2 | `zone2_or_shorten`: make it Zone 2 or shorten it | `as_planned` |
| red | any | `short_zone2_or_rest`: short Zone 2, or rest | `shorten` |

**Hard or long:** a workout is hard when its steps at intensity add up to at least 300 s
or 1000 m of work (`hard_min_work_s`, `hard_min_work_m`), so a few strides never make a
run hard. A step other than warm-up, cool-down, recovery or rest is at intensity when it
has:

- a heart rate, power or pace zone target of 4 or higher (primary or secondary target);
- a pace or speed range whose midpoint is at least 0.90 × your running lactate-threshold
  speed (`hard_pace_of_lt_speed`);
- a power range whose midpoint is at least 0.91 × your cycling FTP (`hard_power_of_ftp`);
- a heart rate range (bpm) whose midpoint is at least 0.95 × your lactate-threshold heart
  rate (`hard_hr_of_lthr`).

Without the threshold a range needs (and always for swim pace), a range counts only on an
interval step inside a repeat of 2 or more. A range alone proves little: workouts synced
from TrainingPeaks put pace ranges even on easy single-step runs. Your lactate threshold
and FTP are read from Garmin only when a workout needs them. A workout without steps
counts as hard when its name says so, in English (interval, tempo, threshold, VO2, race,
hard, brick). A name in another language does not count. Phrases that say the opposite do
not count either: "pre-race" and "post-race", "race" in a shakeout's name, "not hard" or "no
hard", and a keyword with "-free" ("tempo-free"); any other keyword in the same name still
does ("Intervals with recovery jogs" is hard).

A workout is long when its estimated duration or distance reaches run 75 min / 15 km,
bike 150 min / 70 km, swim 75 min / 3.5 km, other sports 90 min. The duration is Garmin's
estimate when there is one; otherwise it is added up from the steps only when every step
is timed. A workout set by distance, like most swims, then has no duration, and only its
distance can make it long.

**Done:** a planned workout counts as done when Garmin linked an activity to it or, since
Garmin rarely does, when an activity of the same sport (run, bike, swim, other) was
recorded on the same local date; each activity completes one workout at most. Done
workouts drop out of the hard-day rules: the table's hard column and
`recovery_incomplete` look only at workouts still to do.

## Calibration

The thresholds are general starting values. After about four weeks of reports, compare
the flags with how you actually felt: on the report page
([morning report recipe](../recipes/claude-ai-morning-report/README.md)), tap
good / ok / tired each morning. The one-time routine in
`recipes/claude-ai-morning-report/calibration.md` then counts how
often each flag fired on days you felt fine or tired, and proposes at most five one-step
threshold changes in the page's calibration tab (it needs at least 14 days with a feel
pick). It changes nothing itself. To apply a proposal, edit `THRESHOLDS` in
`ext/daily_status.py` (or `ext/weekly_status.py`), bump `RULES_VERSION`, run the tests and
deploy. A changed threshold changes the public API: see
[Updates and versions](updates.md#the-public-api).

## In key mode

With a Garmin Personal API key, Garmin shares no training readiness, training status or load,
and no planned workouts, so the daily status never asks for them. `recovery.readiness`,
`recovery.readiness_level`, `recovery.recovery_hours`, `load.acwr`, `load.acwr_status`,
`load.acwr_percent`, `today` and `tomorrow` are always null (those the report text shows are
listed in `missing`), and with
`include_sources` each skipped source reads
`skipped: not available with a Garmin Personal API key`. The rules `low_readiness`,
`load_spike`, `load_rising` and `recovery_incomplete` never fire; the HRV, resting heart rate
and sleep rules work as in `full` mode. Without the planned workout the status treats every day
as a hard one, the cautious reading: on yellow and red days you get the advice for a hard
workout, and `no_planned_workout` and `done_today` never come. With `probe`, `readiness_ready`
is `null` (not applicable): a routine of your own that waits for readiness must stop on `null`.
Every answer carries a `key_mode_note` field, and its markdown text ends with that note. The
rules and `RULES_VERSION` are the same in both modes.

The morning report recipe stores these statuses on a private claude.ai page:
[recipes/claude-ai-morning-report/README.md](../recipes/claude-ai-morning-report/README.md).
