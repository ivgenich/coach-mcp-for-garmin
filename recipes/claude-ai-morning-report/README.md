# Recipe: morning report on a Claude artifact page

**Experimental, claude.ai only.** It needs two claude.ai features: Claude Code cloud
routines and the artifact database (an artifact's `db` capability). Other AI clients
don't have them. Any client can still get the same report text by calling
`get_daily_status` with `text_format="markdown"`.

In `key` mode the report has less in it: no Training Readiness, training load or planned
workouts, so those tiles stay empty, and each status ends with a note that says so
([Daily status in key mode](../../docs/daily-status.md#in-key-mode)). The routine needs no
change: it never waits for readiness.

The page has four tabs: today, a 28-day history, weekly and calibration. On today's
report you can tap how you felt (good / ok / tired). The page only renders; the data
lives in the page's own small database. A cloud routine (a scheduled Claude session on
claude.ai's servers) calls `get_daily_status` through your connector each morning and
stores the result there. Nothing extra runs on your server, and it needs no repository
and no extra secrets. Cloud routines can't use the Claude Docs connector, which is why
this is a page and not a doc.

| File | What it is |
|---|---|
| `index.html` | The page |
| `daily-report.md` | Settings and prompt of the daily routine (adds the weekly report on Sundays) |
| `calibration.md` | Settings and prompt of a one-time routine that proposes threshold changes |

## Placeholders

| Placeholder | Replace with | In |
|---|---|---|
| `{{TZ}}` | Your IANA time zone, the same as the server's `REPORT_TZ`, e.g. `Europe/London` | `index.html`, `daily-report.md`, `calibration.md` |
| `{{CRON}}` | The daily run times, e.g. `30 5,7,10 * * *` (05:30, 07:30, 10:30) | `daily-report.md` |
| `{{REPORT_URL}}` | Your page's link, `https://claude.ai/artifact/<id>` | `daily-report.md`, `calibration.md` |

## Setup

1. Replace `{{TZ}}` in `index.html`. In Claude Code, publish `index.html` as an
   artifact with the `db` capability (one access rule at the root: read `view`, write
   `owner`, so only you can write) and the `user` capability. Keep it private. Its link
   is your `{{REPORT_URL}}`.
2. Create the daily routine from `daily-report.md`. The top of the file lists its
   settings (connector, allowed tools, schedule, model).
3. About four weeks after the first report, create the one-time routine from
   `calibration.md` (see [Calibration](../../docs/daily-status.md#calibration)). It needs at least 14 days
   with a feeling marked.

## Privacy

The page's database holds your daily and weekly statuses, which are health data. Keep
the page private, and keep its one write rule for the owner only.
