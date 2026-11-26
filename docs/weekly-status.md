# Weekly status

`get_weekly_status` covers the 7 days ending `end_date` (default yesterday) against the 7
days before: HRV 7-day against 28-day average, average sleep and nights under 6 h, volume
per sport, load (acute, chronic, ACWR), plan completion, the week's long session (aerobic
decoupling), VO2max and training status, weight, and gear mileage.

Planned sessions often move a day or two, so a planned workout is done when Garmin linked
an activity to it, or an activity of the same sport was recorded on the planned day, up to
1 day earlier or up to 2 days later (as many workouts as possible and, among those ways, as
many on the planned day as possible: same-day matches first; one activity per workout, and
only activities of the report's week up to today). The week has one set of edges for both
sides: workouts dated in the week, activities started from its first day at 00:00 up to
the end of the week or today, whichever comes first (by the watch's local start time, else
the GMT start read in `REPORT_TZ`). A workout dated after today is not due yet: it is
never matched to an activity, the plan line says how many are still ahead, and it is left
out of `completed`, `completion_pct` and `low_completion`; if Garmin already linked an
activity to it, it counts in `done_early` instead, so the share never passes 100%. Garmin's
schedule list carries no date the workout was created, so the 1-day early limit is what
keeps a session done days before the plan from counting. The plan line gives how many of
the due workouts were done and how many of those on the planned day; in the JSON, `plan`
holds `scheduled`, `due` (dated up to today), `completed`, `on_planned_day`, `moved` (done
on another day), `upcoming` (dated after today), `done_early` (upcoming, already linked by
Garmin), `linked` (Garmin's own links) and `completion_pct` (completed of due; null when
nothing is due yet). The daily status keeps the same-day rule.

With plan tracking on (`PLAN_TRACKING=on`, see [Training plans](training-plan.md)), the
answer also has `training_plan`, right after `plan`: the stored training plan's sessions of
the 7 days from `start`, as `planned`, `done`, `partial`, `missed`, `replaced`, `moved` and
`open` (not settled yet), or `null` when there is no active plan (a draft is never counted), it has no session in
those days, or its database read failed. Before counting, the report refreshes the plan's
links from its 14 days of activities and the week's calendar (the daily status's two days
cannot settle a session as missed; a failed activity search settles nothing, and a full page
of 100 activities settles nothing before the day after its oldest one), then counts from that same read; the text adds one
"Training plan" line. Best effort: when the database does not answer within 3 seconds, or no
account id is found (in the week's HRV or sleep answers, else the last used device's profile
number, else an activity's owner), `training_plan` is left out, exactly as with plan
tracking off. It feeds no flag and no level, `RULES_VERSION` stays the same,
and key mode has none.

The long session's aerobic decoupling compares heart rate against power (or speed) in the
first and second half, so it only means something when the effort was even. `long_session.steady`
says whether it was: `true` for an indoor session (virtual ride, indoor cycling, treadmill), or
outdoors when a ride's variability index (normalized ÷ average power, from the FIT file) is at
most 1.10 and the climbing is below 300 m per hour (a run: below 100 m per hour). When
`steady` is `false`, `decoupling_pct` is still given but `interpretation_code` is
`not_applicable` ("Uneven effort (hills or coasting): drift is not meaningful"); `null`
means the data could not tell (no ascent in the file or chart) and the reading is the usual
one. When the drift comes from the activity's chart instead of the FIT file, only the
climbing in its elevation samples is checked.

With the [morning report recipe](../recipes/claude-ai-morning-report/README.md), the cloud
routine adds the weekly report to the page's weekly tab on Sundays.

## Weekly status rules v3

v3 (from v2): `completed` and `completion_pct` count only the workouts already due
(`completion_pct` = completed of due, v2: completed of scheduled), and a workout still
ahead that Garmin already linked counts in the new `plan.done_early`, not in `completed`
(v2 counted it, so the share could pass the due workouts); `plan.due` is added. Workouts
and activities share the same week edges.

v2 (from v1): a planned workout counts only when done on its day, 1 day earlier or 2 days
later, never after today (v1: on any day of the week), with `plan.moved` and
`plan.upcoming` added; the long session's decoupling reads `not_applicable` when the effort
was uneven (`long_session.steady` added).

| Flag | Fires when | Level |
|---|---|---|
| `hrv_down` | 7-day HRV average ≤ 28-day average − 5 ms, or below your balanced range | yellow |
| `short_nights` | 3 or more nights under 6 h | yellow |
| `volume_jump` | total hours ≥ 1.3 × the week before, which had at least 3 h | yellow |
| `load_high` | Garmin's ACWR status is HIGH or VERY_HIGH | red |
| `low_completion` | under 70% of the workouts due by today done (on the day, 1 day earlier or 2 days later), with at least 3 due | yellow |
| `gear_near_limit` | a gear item at 90% or more of its maximum distance | yellow |

The level counts flags as the daily one does; it is `incomplete` only when HRV, sleep and
volume are all missing.

The 14 days of activities come in one request of at most 100. When Garmin's answer holds
100, some may be cut, so the comparison with the week before is not trusted: `volume` is
listed in `missing`, `volume.change_pct` is null and `volume_jump` does not fire. The other
volume numbers are those of the activities received, the text answer adds a line saying last
week may be incomplete, and for the `incomplete` level volume then counts as missing.

## In key mode

In `key` mode the weekly status still reads HRV, sleep, activities and weigh-ins. Load, fitness
(VO2max and training status) and the plan (scheduled, completed, on the planned day) are
always null; `gear` is an empty list, and `gear` is listed in `missing`. The long session's
decoupling comes only from its FIT file. The flags `load_high`, `low_completion` and
`gear_near_limit` never fire; HRV, sleep, volume and weight work as in `full` mode. The answer
carries a `key_mode_note` field, and its markdown text ends with that note. The rules and
`RULES_VERSION` are the same in both modes.
