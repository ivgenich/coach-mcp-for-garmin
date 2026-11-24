# Weekly status

`get_weekly_status` covers the 7 days ending `end_date` (default yesterday) against the 7
days before: HRV 7-day against 28-day average, average sleep and nights under 6 h, volume
per sport, load (acute, chronic, ACWR), plan completion, the week's long session (aerobic
decoupling), VO2max and training status, weight, and gear mileage.

Plan completion is counted over the whole week, because planned sessions often move a day
or two: a planned workout is done when Garmin linked an activity to it or an activity of
the same sport was recorded on any day of that week (same-day matches first, one activity
per workout). The plan line gives how many were done and how many of those on the planned
day; in the JSON, `plan` holds `scheduled`, `completed`, `on_planned_day`, `linked`
(Garmin's own links) and `completion_pct`. The daily status keeps the same-day rule.

With the [morning report recipe](../recipes/claude-ai-morning-report/README.md), the cloud
routine adds the weekly report to the page's weekly tab on Sundays.

## Weekly status rules v1

| Flag | Fires when | Level |
|---|---|---|
| `hrv_down` | 7-day HRV average ≤ 28-day average − 5 ms, or below your balanced range | yellow |
| `short_nights` | 3 or more nights under 6 h | yellow |
| `volume_jump` | total hours ≥ 1.3 × the week before, which had at least 3 h | yellow |
| `load_high` | Garmin's ACWR status is HIGH or VERY_HIGH | red |
| `low_completion` | under 70% of planned workouts done (on any day of the week), with at least 3 planned | yellow |
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
