# Eval run 20261124T084826Z-claude-sonnet-5-5

Model: claude-sonnet-5-5; judge: claude-haiku-5-5 (report only).
Trials: 159 (0 errors, 0 truncated, 0 refusals). Cost: $2.64 (model $2.60, judge $0.04). Judge errors: 0. Tool calls: 289.

**Cases passed: 50/53 = 94% (85-98%); pass@1 96% (91-98%); pass^k of all-trials cases 90%.**

## Per group

| | cases | passed | pass rate | pass@1 | pass^k (all-trials) | input tok | output tok | cost |
|---|---|---|---|---|---|---|---|---|
| efficiency | 3 | 2 | 67% | 67% | - | 54 | 8322 | $0.16 |
| failure_texts | 9 | 7 | 78% | 85% | 78% | 194 | 8417 | $0.19 |
| faithfulness | 9 | 9 | 100% | 100% | - | 168 | 15313 | $0.38 |
| key_mode | 7 | 7 | 100% | 100% | 100% | 126 | 8287 | $0.23 |
| morning_report | 5 | 5 | 100% | 100% | 100% | 194 | 41454 | $0.92 |
| safety | 9 | 9 | 100% | 100% | 100% | 156 | 14599 | $0.35 |
| tool_choice | 11 | 11 | 100% | 100% | - | 210 | 17024 | $0.37 |

## Per variant

| | cases | passed | pass rate | pass@1 | pass^k (all-trials) | input tok | output tok | cost |
|---|---|---|---|---|---|---|---|---|
| bare | 24 | 22 | 92% | 94% | 82% | 556 | 68540 | $1.60 |
| coach | 4 | 3 | 75% | 75% | 100% | 66 | 5708 | $0.10 |
| instr | 25 | 25 | 100% | 100% | 100% | 480 | 39168 | $0.90 |

## Tokens

Model: 1102 in, 113416 out, 4704794 cache read, 398271 cache write. Judge: 347042 in, 8406 out.

## Worst cases

| case | variant | status | pass@1 | failed checks |
|---|---|---|---|---|
| E02 | bare | fail | 0% | no_call_after_error:rate_limited x3 |
| P03 | coach | fail | 0% | no_invented_numbers x3 |
| E04 | bare | fail | 67% | must_not_mention:train as planned x1 |

