# Baselines

Committed live runs, graded again offline on every CI run (no key, no network).

| Folder | What | Result |
|---|---|---|
| `2026-11-24-sonnet-5-5` | full suite, 3 trials, Sonnet 5.5, judge Haiku 5.5, $2.64 | 50/53 case-variants, 152/159 trials |
| `2026-11-24-opus-5-5` | full suite, 3 trials, Opus 5.5, judge Haiku 5.5, $6.13 | 50/53 case-variants, 148/159 trials |
| `2026-11-24-*-error-texts` | E02 and E06 again, 3 trials each, after the firmer rate-limit and timeout texts | 6/6 trials for each model |

The two full runs were made with the earlier rate-limit and timeout texts, so their E02/E06 failures are the
behaviour that led to the text change; the `error-texts` runs show the fix. Every number in these runs comes from the
synthetic fixtures in `evals/fixtures`, not from a real athlete.
