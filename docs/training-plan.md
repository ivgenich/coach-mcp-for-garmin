# Training plans (preview)

With `PLAN_TOOLS=on` the server builds a marathon or half-marathon plan from your own Garmin numbers by fixed,
versioned rules, and shows it: weeks, sessions, pace bands, warnings and the checks that every rule holds.
The read tools only preview. With `PLAN_WRITES=on` too, `plan_commit` and `plan_cancel` write the plan's own
workouts to the Garmin calendar after the user approved (see [How plan writes behave](#how-plan-writes-behave)).
With `PLAN_TRACKING=on` the server also tracks the plan's sessions against your activities and stores RPE and pain
(`log_feedback`), in its own database only, never in Garmin ([Tracking](#tracking)), and reviews each week
(`plan_review_week`): proposals that only hold or reduce load, applied only after the user approves
([Weekly review](#weekly-review)).

## Turning it on

| Variable | Value |
|---|---|
| `PLAN_TOOLS` | `on` (default `off`) |
| `PLAN_WRITES` | `off` (default); `on` (it needs `PLAN_TOOLS=on`) adds `plan_commit` and `plan_cancel` |
| `PLAN_TRACKING` | `on` (default `off`; it needs `PLAN_TOOLS=on`): links finished activities to the plan's sessions and serves `log_feedback` ([Tracking](#tracking)) and `plan_review_week` ([Weekly review](#weekly-review)); with `PLAN_WRITES=on` too, also `plan_apply_changes` |

`PLAN_TOOLS=on` needs `HISTORY_SEARCH=on` (the previews are kept in the same Postgres, schema `history`) and
`GARMIN_AUTH=full` (Garmin's Personal API key does not serve the lactate threshold, the race predictions or
the calendar). Set it on the app service only: `--check-config` refuses it on `ROLE=worker` and
`ROLE=enqueue`. With `HISTORY_FORGET=yes` the plan tools are off, like the history tools. The default
configuration is unchanged: without `PLAN_TOOLS=on` the server has exactly the tools it had before.

## The three tools

| Tool | Use it for | Reads |
|---|---|---|
| `plan_assess` | Is the goal realistic? The last 8 weeks, the start values and a verdict | Garmin |
| `plan_preview` | The whole plan to race day, stored as a draft | Garmin, then Postgres |
| `plan_get` | The stored draft again, without Garmin (only this account's plans) | Postgres only |

The arguments are plain text: `race_date` (YYYY-MM-DD), `race_distance` (`marathon`, `half`), `goal_time`
(H:MM:SS), `b_races` (`2027-01-29:tri_half,...`; kinds `tri_full`, `tri_half`, `run`), `run_days` and
`rest_days` (`mon,tue,...`), `long_run_day`, `quality_per_week` (0-2), `max_long_km` (10-32);
`plan_preview` also takes `blocked_dates`, `overrides` (a JSON list, see [Overrides](#overrides)),
`plan_id` and `course_id` (the A race's course from `get_courses`, for [race pacing](#plan-rules-v2)). A bad
argument answers `Invalid input: ...` with a fixed text, never the value.

What the tools read from Garmin: the run activities of the last 8 full weeks (distance and date only), the
lactate-threshold speed, Garmin's race predictions, VO2max, today's stats (the profile id, only for the
account fingerprint) and the calendar from today to the race (each workout's date, sport and whether
`get_daily_status` would call it hard or long, and its estimated duration). The plan's own workouts
(named `Coach ...`, written by `plan_commit`) are not counted as fixed sessions. Since plan rules v2
they also read, for the planned load: every sport's activities of those 8 weeks (date, sport, duration,
distance and Garmin's training load; never names) and Garmin's acute and chronic load today, 14 and 28
days ago (three training-status reads). `plan_preview` with a `course_id` reads that one course too;
without one it reads no course.

The answer is compact JSON: `weeks` (each with `kind`, `phase`, `target_km`, `planned_km` and the sessions
with `key`, `date`, `role`, `km`, `pace` and a `structure` text), `warnings` (ids with dates), `feasibility`,
`what_if`, `invariants` (`ok`, `failed`, `checked`), `missing`, `overrides` (`applied`, `refused`),
`window` (the next 7 days rendered from the exact Garmin workout JSON `plan_commit` sends),
`acwr` (the planned load, see rule 12), `race_pacing` (only with a `course_id`, rule 13),
`stored` (`plan_id`, `version`, `changed`) and `content_hash`.

## AI roles

The tool descriptions give the chat model two roles; neither decides a number.

- Intake interviewer (`plan_assess`): asks once for what it does not know (race, goal, B races, run days,
  rest days, long-run day, blocked dates) and never invents a value.
- Coach critic (`plan_preview`): reads the warnings, feasibility and invariants and explains the risks. It
  may propose edits only as `overrides`; the server refuses any edit that adds load or breaks a rule, and the
  critic reports the refusal instead of working around it.

The Claude project instructions repeat this ([recipes/coach-project/instructions.md](../recipes/coach-project/instructions.md)).

## Plan rules v2

`RULES_VERSION = 2` in `ext/plan_engine.py`; every number is in its `RULES`, and the numbers of rules 12 and
13 are echoed in the answer (`acwr.rules`, `race_pacing.rules`). Rules v2 are rules 1-11 of
[v1](#plan-rules-v1) unchanged, plus:

12. Planned all-sport ACWR. Each day's load is its minutes x the sport's load per minute, measured on the
    recorded activities of the last 8 weeks (Garmin's training load over their duration). Days up to today
    count what was recorded; later days count the plan's runs (minutes from the pace bands, or from the
    recorded runs' mean speed without an LT speed) and the fixed calendar sessions (their estimated
    duration, else the sport's mean session); a fixed session today counts while nothing is recorded today.
    A B race counts its estimated duration x the all-sport load per minute: a full triathlon 750 minutes, a
    half 330, a run race a half marathon at the recorded runs' mean speed (120 minutes without one); these
    are `acwr.b_races` (date, kind, minutes, load). The A race is not counted. The ratio of a day is the
    load of the last 7 days over the load of the last 28 days / 4, like Garmin's acute and chronic load. The
    model is checked against Garmin's reported ratio today, 14 and 28 days ago (unrounded): when it is within 0.15 on at
    least 2 of those days, the ratio is enforced. The cap acts on the days of a week a cut can lower: from
    the week's first easy run on (a B race day itself excepted). When one of them is above 1.3, the easy runs
    on or before the highest such day are cut in steps of 5% of the week's km (the long run and the quality
    sessions stay; a quality session goes too if the week would fall under 80% easy); a step that would end
    the week under 0.6 x its target is made smaller or skipped (warning `acwr_cut`). It stops with
    `acwr_floor` when no step is left or a step no longer lowers the highest ratio (that step is undone). A
    cut is not a shortfall: the next weeks grow from the km before it, and they are checked again. Load is
    never raised. When the model does not track Garmin, or there is no recorded load or Garmin ratio to
    compare, nothing is cut: `acwr.enforced` is false with the reason (warning `acwr_not_enforced`) and days
    above 1.3 only warn (`acwr_high`). After an override, the weeks are cut again where the edit raised a
    later day's ratio. `acwr.planned_max` and `days_over` cover every day after today and before the A race.
    Invariant `acwr_cap`: while enforced, no day the cap acts on is above 1.3 unless its week is at its
    floor or stopped with `acwr_floor`.
13. Race pacing (with `course_id`): the course's climbs as `get_course_profile` finds them; each climb is run
    3% slower per 1% of its average grade, the rest of the course at one faster pace, so that the course
    takes the goal time (else Garmin's prediction) scaled to its length. `race_pacing.segments` gives each
    segment's km, kind, grade, pace, time (the times add up to the finish) and the clock at its end. A course
    more than 5% longer or shorter than the race warns `course_distance_mismatch`. No course or no goal:
    `race_pacing.missing`.

What changed from v1: the answer has the `acwr` block and the `acwr_cap` invariant; plans whose recorded load
tracks Garmin can have smaller weeks (only easy runs shorter or dropped); everything else is the same, so a
plan without recorded load (`missing: training_load`) has v1's weeks.

## Plan rules v1

Plans stored with `rules_version` 1 were built by these rules alone. The current rules,
`RULES_VERSION = 2` in `ext/plan_engine.py`, keep every rule below and add the ACWR cap ([Plan rules
v2](#plan-rules-v2)); every number below is in its `RULES`.

1. Weeks run Monday to Sunday. Week 1 starts next Monday; the rest of this week (week 0) has easy runs only.
   The last week is the race week. Phases from the end: taper (the race week, and the week before it in
   plans of 10 weeks or more), peak (1 week from 8 weeks, 2 from 11), then base (4/9 of the rest) and build.
2. Weekly km: the start (km0) is the mean of the last 4 full weeks. A normal week grows by at most 8% on
   the last normal week, counted as planned: when the easy-run cap (or the run days) holds a week below its
   target, the next weeks grow from the km it got, not from the target. Days lost to blocked dates and fixed
   calendar sessions are not a shortfall (the week counts what it would have had without them), nor is the
   100 m rounding of each session. No weekly target goes above the peak ceiling, max(1.3 x the biggest full
   week of the last 8 weeks, km0 + 25 km); once reached it holds, deloads still apply, and the warning
   `peak_capped` gives the numbers. After every 3 normal weeks comes a deload at 0.78 x the last normal
   week. A reduced week is never made up afterwards. Taper: 0.70 x the peak week; race week: 0.45 x the
   peak week, pro rata to the days before the race, the race not counted (the peak week as planned too).
3. Long run in a normal week: at most min(max(0.50 x the week's km, the longest run of the last 8 weeks),
   3 h at the slow edge of its pace band, the last normal week's long run as placed + 2 km (21 or more days
   out, a shortened one included, so a week cut to 23 km is followed by at most 25 km; one rule 7 holds around a
   B race does not count; before any, the longest run of the last 8 weeks), `max_long_km`, 32 km).
   Reduced weeks scale it. Within 21 days of the A race no long run is longer than 0.80 x the peak long run
   (the longest long run of a normal week 21 days or more out; before there is one, the longest run so far:
   the longest of the last 8 weeks or a long run placed before it). The cap applies on the day the long run
   lands, also when it moved a day, before the 80%-easy check and the easy runs fill the week. After a gap,
   a normal or deload week with no long run that has a blocked run day or no run at all (travel, a week
   off), the +2 km step counts from the last long run actually planned, whatever its week, until a normal
   week's long run: a travel week then a deload with an 18.5 km long run gives at most 20.5 km the week
   after, not the longest so far + 2 km. `long_step` checks the same sequence on the finished plan.
4. Quality by phase: threshold, intervals, marathon pace, with dose caps (threshold 10%, intervals 8%,
   marathon pace 20% of the week's km) and at least 80% of the week's time easy. Strides stay at 6 x 100 m,
   which `get_daily_status` classifies as easy.
5. Pace bands are fractions of the lactate-threshold speed: easy and long 0.70-0.80, threshold 0.97-1.03,
   intervals and strides 1.05-1.10, marathon pace from the goal (clamped to 0.85-0.96 of LT, with a
   warning). Without an LT speed the steps have no pace target and `lt_speed` is listed as missing.
6. Placement: long run first, then Q1 and Q2, so that no two hard-or-long days touch, counting fixed
   calendar sessions, the races and the weeks before and after. When a session does not fit, a fallback
   (Q2 dropped, long run moved, Q1 as strides, long run dropped) warns with its dates; a long run off its
   long-run day for any reason (a blocked or rest day, a B window, hard days) warns `long_moved`. A week whose
   run days are all blocked gets no run and one warning, `no_runs_blocked`, instead of the fallbacks.
7. B races: a 4-day mini-taper (0.6, strides only), the race, then easy recovery at 0.5 for 6 days after the
   race day (a 7-day window with the race: a Friday half-distance triathlon is followed by easy runs only
   through Thursday; 7 days after it and a warning after a full-distance triathlon). A peak week inside a B block becomes a B week (warning
   `b_race_in_peak`); the A taper never moves. The factors are per day: an easy run on a mini-taper or recovery
   day is 0.6 or 0.5 of a full easy day of the same week (also when the recovery reaches into the next
   week); a window day that would fall under the easy minimum gets no run. A B race in the A-race week (or
   before week 1) is not planned around (`b_race_ignored`); one in the week before it is, with
   `b_race_in_taper`, and no session is placed on a B race day. Three limits hold for every kind of B race
   (`rules.b_race`), and each lowers only, warning `b_race_capped` with the dates:
   - a long run 5 to 13 days before a B race is at most 0.70 x the peak long run so far (rule 3's reference:
     the longest long run of a normal week 21 or more days before the A race placed before it; before one,
     the longest run so far; with neither, as in rule 3, no limit);
   - each run on a mini-taper day is at most 8.0 km (still 0.6 of a full day and strides; the cap only
     lowers), also when the mini-taper starts in week 0;
   - the first long run after a B race is at most 0.70 x that reference (0.60 after a full-distance
     triathlon), and after a full-distance triathlon it is 10 or more days later: a long run sooner becomes an
     easy run (at most the easy cap) and the next long run is the first.

   After a half-distance triathlon (`tri_half`) the first long run is also at most 0.75 x the last long run
   before the race (`long_after_of_last`; the longest run of the last 8 weeks before any), a ceiling in 100 m
   steps: 20.8 km before gives at most 15.6 km. Two fixed notes (server text, the session's `note`): that long
   run says "Run up to <its km> km only if your legs feel normal and your HRV is back in its usual range;
   otherwise run <lo>-<hi> km easy.", lo and hi being 0.65 and 0.75 x its km in whole km
   (`long_after_fallback_of_cap`; 15.6 km gives 10-12 km); the first run after the race says "Skip it if
   you're still sore." `tri_full` and `run` B races keep the limits above and get no notes.

   The km these limits take off are not made up by other days, and the weeks after still grow from the
   week's target (the cut is no shortfall). Overrides that would break a limit (`move`, `swap`,
   `long_run_day`) are refused with `b_window`; the 0.70 and 0.60 reference is the plan as built, so an
   override that shortens an earlier long run never tightens those limits. The 0.75 limit after a
   half-distance triathlon follows the plan as it is: shortening the long run before the race with an override
   is refused (`b_windows`) while the first long run after it would then be over 0.75 x it, so cap that one
   first. `b_windows` checks all of them.
8. Feasibility: `feasible`, `ambitious` or `unrealistic` from the weeks to race, the start km, the planned
   peak and Garmin's prediction. The plan never grows faster to meet a goal; `corrected_goal_s` says what is
   in reach.
9. Invariants: every rule above is checked again on the finished plan, from the placed sessions (the
   planned weeks and the long run the 2 km step counts from, recomputed by the check) (`growth_cap`, `no_makeup`,
   `deload_cadence`, `deload_depth`, `long_cap`, `long_step`, `peak_long_timing`, `easy_share`,
   `no_adjacent_hard`, `bands_match_roles`, `b_windows`, `a_taper`, `last_quality`, `rest_days`,
   `one_run_per_day`, `peak_ceiling`), plus `dto_valid` (upstream's workout checks pass on every workout JSON) and
   `cross_engine` (every session `get_daily_status` would call hard or long is one the plan marks so).
10. What-if: the goal 5 minutes faster and slower, and a long-run share of 0.35 and 0.45, each with its peak,
    long run, verdict and warnings.
11. Blocked dates get no run and their volume is not made up: the run a blocked date would have had is
    dropped, and the other days keep their km. Overrides only move or reduce load, and never put a run after
   the A race (`after_race`).

The same inputs and the same Garmin numbers give the same plan, byte for byte (`content_hash`).

## Overrides

`overrides` is a JSON list; each edit is checked by its own rule, then every invariant runs, and an edit that
breaks one is undone and listed in `overrides.refused` with the rule, the value and the limit. A refused edit
is never stored: the stored version's inputs, `content_hash` and body keep only the edits that applied. When
every edit for a stored `plan_id` is refused, no new version is stored and the answer (`kind`
`plan_overrides`) names the current version with `stored.changed` false. Name a session by its `date` where
the edit takes one.

| kind | fields | does |
|---|---|---|
| `move` | `key`, `date` | moves a session to another day of its week |
| `swap` | `date`, `date_b` | swaps two days of one week |
| `rest` | `date` | makes a day a rest day (its km is not made up) |
| `easy` | `key` or `date` | turns a quality session or long run into an easy run of at most the same km |
| `cap_km` | `key` or `date`, `km` | shortens one easy run, strides run or long run (never lengthens it; at least the easy-run minimum) |
| `cap_long` | `date` (or `key`), `km` | lowers that day's long run to at most `km`; the rest of its week keeps its km. Refused: `not_long_run` (no long run that day), `raises_load` (above the planned km), `easy_min_km` (under 4 km), `unknown_session` |
| `cap_week_km` | `week_start`, `km` | lowers a week's km (never raises it) |
| `long_run_day` | `week_start`, `weekday` | moves that week's long run to another day |

## Tracking

With `PLAN_TRACKING=on` the server follows one plan: your active plan, or, while no plan is active (no
Garmin writes yet), your latest draft. It tracks the plan's own sessions (date, sport, role), so it works
without any Garmin write; a session's Garmin workout id (`plan_commit`, `PLAN_WRITES=on`) is only an extra
link. Rules (`ext/plan_tracking.py`):

- An activity completes a session of the same sport from 1 day before to 1 day after its date
  (`status_common.match_completion_pairs`), never an activity that started before the plan was activated
  (a draft: created). It completes as many sessions as it can and, among those ways, has as many on their
  own day as it can (same-day matches first), so a moved session never takes the run of a session done on its
  day when another run keeps the count. Each activity completes at most one session (an activity listed twice
  counts once). On one day
  the bigger run (distance, then time) is offered first, so a warm-up jog before the long run does not make
  it partial; with two sessions on one day the long or quality one gets the run first, unless it is too
  short for it and a full run for the other (the two swap runs when each is a full run for the other's
  session).
- A link already counted stays when the plan is activated later (`plan_commit` on a tracked draft): the
  activation only keeps runs from before it from counting anew.
- Garmin's own link (`associatedActivityId` on the calendar entry) wins; it is usually empty, so matching
  does most of the work.
- A matched run under 80% of the planned distance or of the planned time is `partial`, otherwise `done`
  (exactly 80% is `done`: compared to the metre and the millisecond, not in floating point);
  a day early or late adds `_early` or `_late` (`done_late`, `partial_early`, ...).
- A session still without a run is settled only from data covering its whole window (the day before, the
  day itself and the day after, which must be over): then it is `replaced` when other sports that day add
  up to 80% of its planned time, else `missed`. The daily status's two days never settle one; the weekly
  status's 14 days do, but not when its activity search failed (nothing is settled then), and not for days
  a full page of 100 activities may lack (only from the day after its oldest activity). A later run can
  still turn `missed` into done.
- B races (triathlons) are not tracked.

`get_daily_status` refreshes the links each time it runs, from yesterday's and today's activities (an
activity without a local start time dated by the day Garmin listed it under, as the rest of the report does), and
`get_weekly_status` from its 14 days of activities and the week's calendar; a database failure never fails
either report. The status reports then show the plan: `get_daily_status` today's
session, its role and whether feedback is due; `get_weekly_status` the week's counts
([daily status](daily-status.md), [weekly status](weekly-status.md)). `plan_get` adds `tracking.links`, each
session's status as of its last check (`as_of`, local time), and `tracking.feedback`. Nothing is written to
Garmin. The weekly review (`plan_review_week`) calls the same refresh (`plan_tracking.refresh_plan`) over its
week and the week before.

## Feedback

`log_feedback` stores how a session felt: `workout_key` (the session's key, from `plan_get` or
`get_daily_status`), `rpe` (1-10), `pain` (0-10), `pain_location` (`none`, `foot`, `ankle`, `achilles`,
`calf`, `shin`, `knee`, `hamstring`, `quad`, `hip`, `glute`, `lower_back`, `other`) and `sharp` (sharp or
localised pain); `plan_id` is optional. Pain 0 goes with location `none`; pain above 0 needs a location.
Calling it again for the same session replaces the numbers. A session after tomorrow is refused. The answer
says `stored` and `progression_hold`: true for pain 4 or more, or sharp pain. Bad input answers
`Invalid input: ...` with a fixed text, never the value; a database failure answers a fixed `No data:` text.
No free text is stored or logged. It is the only plan tool that writes, and only to the app's own database:
annotated additive, idempotent and local-only, never read-only.

## Weekly review

With `PLAN_TRACKING=on`, `plan_review_week(plan_id, week_start)` reviews one week of the tracked plan
(`plan_id` empty: the tracked plan; `week_start` a Monday that has begun, empty: the last week that has ended,
or this week on a Sunday). It reads from Garmin the activities from two weeks before the week to today, the
calendar, four weeks of HRV, the week's nights and Garmin's training status at the week's end (today's daily
status only when the plan has a quality session today), refreshes the plan's links, reads the stored
feedback and applies [Plan review rules v1](#plan-review-rules-v1). The answer: `metrics` (planned, due and
done km, completion, session counts, the long run, easy RPE of both weeks, pain, HRV, short nights, ACWR,
flags, next week's km), `changes` (each with `change_id`, `rule`, `kind`, `from`, `to`, a `reason` sentence
that cites its number, `evidence` {`metric`, `value`, `threshold`, `also`}, `status`, and `advice` where a
rule has a fixed text), `rules` (every threshold) and a `note`. The same week reviewed again gives the same
change ids.

Nothing in the plan changes until the user approves:

- `plan_preview(plan_id, accept_changes="c...,c...")` patches the stored version the user reviewed: every
  stored session stays as it is (the rest of the current week too), and only the sessions after today a change
  targets are shortened, dropped or turned into easy runs (today's only for A8), at the pace bands the stored
  plan already uses; the easy-band slowdown slows the later easy, strides and long runs at the same distance (an easy run
  longer than 74 minutes at the slower band is shortened to 74 minutes, plan_engine's own cap).
  A week cap counts the km already run that week (the linked runs); a shorter long run caps the later long
  runs at the 2 km step. The patch is checked again (see the rules below); `changes.accepted`,
  `changes.applied` and `changes.refused` (each with the rule that blocks it, or `unknown_change`) say what
  happened. When no accepted change patches a session (advice only, or the plan already at or below them) no
  new version is stored. The other arguments are not used. It writes nothing to Garmin.
- With `PLAN_WRITES=on` too, `plan_review_week` answers `apply`: a token (`at1.`, 30 minutes to first use)
  bound to the plan, its current version, the accepted change ids, the new version's content and the exact
  ops (the plan's own days on Garmin from tomorrow on, rewritten as a commit would; today only for an A8
  change). `plan_apply_changes(plan_id, apply_token, change_ids)` checks the write scope, asks the user to
  confirm (form elicitation, as `plan_commit` below) where the client supports it, stores that version and writes the ops with the same
  helper and statuses as `plan_commit` (`done` replays, `partial`, `unknown`, `refused`, `declined`).
  `change_ids` must be exactly the token's (`changes_mismatch`); a version stored after the review, or a
  change no longer stored as proposed or approved (a later review of the week), makes the token `superseded`.
  No token when no change patches a session, an invariant fails or nothing is on Garmin from today on.

A change's status only moves forward: `proposed`, `approved` (accepted in `plan_preview` or at the start of
an apply), `applied` (every write of the apply done). A new review of the same week replaces the proposed
changes and keeps approved and applied ones as they were.

Annotations: `plan_review_week` is read-only. What it stores, the proposals (`plan_changes`), the refreshed
links and, with `PLAN_WRITES=on`, an apply token row (`plan_tokens`, its SHA-256 only), is the server's own
bookkeeping in its database, like `get_daily_status` refreshing the links: nothing the user sees in Garmin or
in the plan changes, and calling it again changes nothing but the proposals' numbers and a new token.
`plan_apply_changes` is destructive (it replaces the plan's own workouts) and idempotent (a done token
replays), and joins only with both `PLAN_WRITES=on` and `PLAN_TRACKING=on`.

## Plan review rules v1

`ext/plan_review.py` (`REVIEW_RULES_VERSION = 1`, the numbers in `REVIEW_RULES`). A week is Monday to Sunday;
every change targets the week after the reviewed one (A8: today), only while that week has not ended. A
session counts as due once it is settled (done, partial, replaced or missed); a run counts up to its planned
km.

| Rule | When | Proposal |
|---|---|---|
| A1 | done km under 70% of the due km | next week repeats this week's planned km |
| A2 | HRV down (7-day average 5 ms or more under the 28-day) or 3 or more nights under 6 h (`get_weekly_status` flags) | next week becomes a deload: 0.78 x this week's planned km |
| A2 | Garmin's ACWR above 1.3 together with a mean easy-run RPE of 6 or more | the same deload; ACWR alone never triggers a change, and beside HRV or nights it is listed in `evidence.also` |
| A3 | the week's long run missed | next week's long run at most the missed length; later long runs keep the 2 km step from it |
| A4 | mean easy-run RPE 6 or more in this week and the week before | easy and long pace bands 3% slower (strides keep their distance), and next week's km 10% lower than in the version reviewed |
| A5 | a quality session run 2% or more above its planned average speed at RPE 5 or less | advice only: a threshold retest (no session changes) |
| A6 | pain 4 or more, or sharp pain | next week holds this week's km and its quality sessions become easy runs, with the fixed text "see a physiotherapist or doctor"; when there is nothing to hold or drop (or no next week), that advice alone (`pain_advice`, no edit) |
| A7 | a missed week (no due km done) | next week at most 1.10 x the km of the last week that was done |
| A8 | today's daily level red and a quality session today | today's session becomes an easy run of the same distance |

A proposal is made only when its value is below the plan's. Accepted changes are applied one at a time to the
stored version; each patch is checked twice and undone when it fails, with the rule: the plan rules a
shortening can break (`patch_problems`: `long_step`, `easy_min_km`, `easy_share`, only where the stored
version kept them), and `raises_load` when the patched version has, after today, a week with more km, a
longer long run, more quality sessions or a hard day the stored version does not have. Two caps on one week
keep the lower one that passes: a refused lower cap leaves the other to apply.

## What is stored

Migration `003_training_plans.sql` creates every table the plan stages need (`plans`, `plan_versions`,
`plan_workouts`, `plan_tokens`, `plan_feedback`, `plan_changes`); the previews fill `plans` and
`plan_versions`, `plan_commit` and `plan_cancel` fill `plan_workouts` (our own Garmin workout and calendar-entry ids, dates, roles, states) and `plan_tokens` (SHA-256 only); with `PLAN_TRACKING=on` tracking fills `plan_workouts` too (a `local` row per tracked session without a Garmin workout: date, role, link status and the linked activity's id) and `log_feedback` fills `plan_feedback` (numbers and the location only); `plan_review_week` fills `plan_changes` (the change's kind, from and to values, the machine edits, the fixed reason text and the evidence numbers; its status). Stored: the account fingerprint (the history's HMAC, never the profile id), dates, the
rules version, the inputs and the plan JSON (numbers, enums and dates; no names or notes). A preview with
the same content as the current version stores nothing new (it only marks the draft as used).
`plan_get` shows only the plans of the Garmin account the history index recorded; until it has recorded one
(or while Garmin is signed in to another account) it answers `No data`. Retention (the worker's daily `sweep`): drafts
untouched for 30 days, cancelled or finished plans 392 days after they ended, tokens 7 days after they
expired. An active plan becomes `finished` the day after its A race (the daily `sweep`, and before a
`plan_commit` activates another plan of the account), so a finished plan never blocks the next one.
`HISTORY_FORGET=yes` removes them with the rest of the schema; it is refused (nothing removed) while the
plans still have workouts on Garmin from today on: run `--plan-cleanup` first.

## Garmin facts

- Workout ids come from upstream `garmin_mcp/workouts.py` (end conditions time 2, distance 3, lap button 1,
  iterations 7; targets no target 1, pace zone 6; running sport 1), checked at build time by
  `plan_dto.selfcheck()`.
- Pace targets are speeds in m/s; the workout JSON puts the slower speed in `targetValueOne` and the faster
  in `targetValueTwo`, and Garmin keeps them in that order (spike S0.2).
- A distance step's `endConditionValue` is in meters in every pool, a yard pool too (even with
  `poolLengthUnit` and the step's `preferredEndConditionUnit` both `yard`): the distance
  steps add up exactly to the workout's `estimatedDistanceInMeters`. Verified by spike S0.3 on real 25 yd
  pool workouts; `status_common.step_distance_m` reads the value as meters, with no conversion.

Verified by spike S0.2 (2026-11-26, one test workout on the owner's account, scheduled, seen in Garmin
Connect, then removed):

- Upload: `POST workout-service/workout` answers 200 with the workout JSON, `workoutId` included; reading
  it back returns the steps, pace targets (same order) and `description` exactly as sent.
- The library listing (`workout-service/workouts`) lists the newest workout first and carries each
  workout's `description`, so a create whose answer was lost is found again by its marker.
- Schedule: `POST workout-service/schedule/<workoutId>` with `{"date": ...}` answers 200 with the entry id
  as `workoutScheduleId`. A second POST for the same workout and date makes a second calendar entry,
  which is why the helper reads the calendar first.
- Unschedule: `DELETE workout-service/schedule/<entry id>` answers 204 and the entry leaves the calendar;
  delete: `DELETE workout-service/workout/<id>` answers 204. Afterwards a GET or a second DELETE fails
  with garminconnect's `GarminConnectConnectionError("API Error 404 - ...")` and no response attached;
  `plan_writes.classify` reads the status from that text (404 = gone).
- Each request took about 0.3 s; nine writes about 1.2 s apart met no rate limit.

Still unverified, and coded to these assumptions behind `PLAN_WRITES` (default off):

- The GraphQL calendar read (`workoutScheduleSummariesScalar`) carries `workoutId` and the same entry id
  for a workout we scheduled (the spike checked the REST calendar, `calendar-service/year/<y>/month/<m>`,
  whose month is 0-based and whose entry `id` is the `workoutScheduleId`).
- Deleting a workout also removes its calendar entries (the spike unscheduled first).
- garminconnect's re-send after a 401 does not duplicate a write; a read timeout or a 5xx may have
  written.
- Garmin's write rate limits under load, and the client's tool-call timeout: the budget (8 writes, 15 s
  to start them, 1 s apart, 8 s read timeout) is a guess.
- The workout reaches the watch (Fenix) after a sync with its pace targets (it showed in Garmin Connect).
- claude.ai (web, mobile) and Claude Desktop with a connector were not tested for the confirmation form
  (spike S0.5 ran from Claude Code); nor whether they prompt before a tool whose hints say it writes.

Verified by spike S0.5 (2026-11-26, a throwaway server behind a temporary custom connector, called from
Claude Code 2.1.332, protocol 2026-07-28):

- The client declares form and URL elicitation, but on the 2026 protocol the SDK's
  `can_send_request` is False: a server-to-client `elicitation/create` during a tool call cannot be
  sent, so `plan_confirm.confirm`, which then used only `session.elicit_form`, answered "unsupported".
  It now asks through the input-required round on that protocol (below, "How plan writes behave").
- The 2026 protocol's input-required round works: the tool returns an `InputRequiredResult` whose
  `input_requests` hold a form `ElicitRequest`; the client shows the form, then calls the tool again with
  the answer in `input_responses` (read with `Context.input_responses`). The user saw the form and
  answered within 20 to 30 s.
- A boolean checkbox comes back `false` even when the user ticked it, so it cannot carry consent. A
  required string `enum` ("Add them" / "Don't add") comes back exactly as chosen. Declining the form
  answers `action: "decline"` with no content.

## How plan writes behave

With `PLAN_WRITES=on` (full mode, the app only), and only for a sign-in whose consent page granted Garmin
writes (scope `plan:write`; the static `MCP_BEARER_TOKEN` never may):

1. `plan_preview` answers `commit`: a token (`ct1.`, 30 minutes to first use) and its ops, the runs of the
   next `commit_days` days (1 to 21, default 7) not on Garmin yet, and the plan's own days whose workout
   changed (the new workout is created first, then the old one unscheduled and deleted). No token while an
   invariant fails.
2. `plan_commit(plan_id, commit_token)`: when the client declares MCP form elicitation, the user confirms a
   summary the server builds (the workouts to add and those to remove, each with dates and roles) by
   choosing one of two fixed labels ("Add them" / "Don't add"; "Remove them" / "Don't remove"; "Make these
   changes" / "Don't change anything"): only the approving label is a yes, never a checkbox. On protocol
   2026-07-28 this is the input-required round: the first call reads the token and writes nothing (no Garmin
   call, the token stays unused) and answers the form with a state the server minted (bound to the action,
   plan, token and summary, under a key of this process, and sealed again by the SDK for the same tool,
   arguments and sign-in, 10 minutes); the client shows it and calls the tool again with the same arguments
   and the answer. An answer without that state (also one sent on a first call), with another tool,
   arguments or summary, or after a restart of the server is not taken: the form is asked again (or the SDK
   refuses the call), and nothing is written. On older protocols the form is an elicitation request during
   the call (no answer in 3 minutes: declined); a client without form elicitation keeps the token alone
   (`confirmed_by: "token"`). Declined, cancelled, the other label or no answer: nothing written, the token
   stays valid. Then
   the server checks the Garmin account, reads the calendar (no read, no write), refuses when a new hard or
   long session now sits next to a planned hard or long day (`calendar_changed`), and writes one request at
   a time: upload, then schedule unless the calendar already has it. A day with another run on it is a
   `conflict` and is skipped. A session Garmin linked to an activity (the calendar's `associatedActivityId`,
   also with `PLAN_TRACKING=off`) is done: never written again nor deleted (`kept[]`). A workout uploaded but
   not scheduled (Garmin refused the schedule, or the token expired after a 429) is offered again by the next
   preview, and scheduled without a second upload. The plan becomes `active` (one per account).
3. Answers: `done` (the same token again replays it with no Garmin call; when Garmin refused deleting a
   replaced workout, `failed[]` lists it and the note says so: a later commit tries again), `partial` (an
   upload or schedule Garmin refused (4xx) is in `failed[]`, `retry_after_s` null: the same token tries it
   once more; otherwise call again with the
   same token after `retry_after_s`: 0 for the write budget, 60 after a write Garmin did not answer, 180
   while an earlier write is still being checked, 900 after a 429, when the call guard refuses live Garmin
   calls for 15 minutes), `unknown` (a write may have landed: call plan_get first, then the same token after
   `retry_after_s` 180; the next call settles it from
   Garmin once the request's thread has ended and 2 minutes have passed; a create is adopted by its
   marker in the library, a schedule from the calendar), `refused` (`reason`), `declined`. A started token
   resumes for 24 hours; a new plan version makes it `superseded`. One write call per plan runs at a time
   (`busy`), also after the client cancelled the call: the lock belongs to the thread doing the writes. A
   replaced workout always goes: one left on Garmin by an earlier call is deleted by the next commit.
4. `plan_cancel(plan_id)` answers what it would delete (the plan's own workouts from today on, not linked to
   an activity), what it keeps, and a cancel token (`xt1.`); with the token it deletes them (404 counts as
   gone) and cancels the plan. A cancel token covers only the workouts it listed: after a commit in between
   it is `superseded` (a workout it listed while its upload was `unknown` is the same one once settled). A
   delete Garmin refuses answers `partial` with `failed[]`, and the plan stays open.
5. Each workout this server writes is named `Coach <role>` and carries `coach-mcp <install tag> <plan_id>
   <key> h<hash>` in its description. A workout is ours only when its Garmin id is in `plan_workouts`;
   TrainingPeaks, Garmin Coach, your own workouts and another install's are never touched.
6. `python ext/launch.py --plan-cleanup [--dry-run] [--orphans]` (the owner, e.g. `railway run`; needs
   `DATABASE_URL`, `GARMIN_AUTH=full` and the Garmin login) deletes this install's own workouts from today on,
   of the plans of the Garmin account logged in, also with `PLAN_WRITES=off`. With `--orphans` it also
   deletes library workouts with this install's marker that no row owns, only those on the calendar today or
   later, never on an earlier day, never linked to an activity. A workout of today that Garmin linked to
   an activity is kept. It prints counts only.

Every Garmin request passes a method allowlist: GET, one anonymous GraphQL read query (no `mutation` or
`subscription` keyword outside its strings and comments, a comment ending at a carriage return or a line
feed, no `operationName`, and no `data` or `files` body beside the checked `json`), and only with
`PLAN_WRITES=on` the four
workout-service write paths above. `plan_apply_changes` (the weekly review's write, [Weekly review](#weekly-review))
uses the same paths, helper and statuses. The history worker never writes.

## Golden plans

`ext/fixtures/plan/` holds five plans the tests rebuild byte for byte: G1 (Tel Aviv Marathon 2027-02-26 with
Israman 2027-01-29 as a B race, from representative numbers; the same rules were checked on the owner's real data and signed off on
2026-11-25, with Israman as a half),
G2 (a 15 km a week runner), G3 (race in 5 weeks), G4 (no LT speed) and G5 (a heavy fixed TrainingPeaks
week). Since rules v2, G1 and G5 carry representative recorded loads that track Garmin (G1 models
Israman's load: its race day peaks at 1.39, and the ratio cuts the day after (week 9) and the recovery
week (week 10) back toward 1.3; G5's heavy week cuts its easy runs to
the floor and stops there), G4's
loads differ from Garmin's ratio by 0.3 (only reported), and G2 and G3 have none.
