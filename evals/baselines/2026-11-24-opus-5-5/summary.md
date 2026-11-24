# Eval run 20261124T090917Z-claude-opus-5-5

Model: claude-opus-5-5; judge: claude-haiku-5-5 (report only).
Trials: 159 (0 errors, 0 truncated, 0 refusals). Cost: $6.13 (model $6.09, judge $0.04). Judge errors: 0. Tool calls: 344.

**Cases passed: 50/53 = 94% (85-98%); pass@1 93% (88-96%); pass^k of all-trials cases 90%.**

## Per group

| | cases | passed | pass rate | pass@1 | pass^k (all-trials) | input tok | output tok | cost |
|---|---|---|---|---|---|---|---|---|
| efficiency | 3 | 2 | 67% | 67% | - | 66 | 16731 | $0.67 |
| failure_texts | 9 | 7 | 78% | 81% | 78% | 196 | 13961 | $0.49 |
| faithfulness | 9 | 9 | 100% | 96% | - | 170 | 21436 | $0.83 |
| key_mode | 7 | 7 | 100% | 95% | 100% | 130 | 13253 | $0.57 |
| morning_report | 5 | 5 | 100% | 100% | 100% | 208 | 32898 | $1.62 |
| safety | 9 | 9 | 100% | 100% | 100% | 174 | 26692 | $1.01 |
| tool_choice | 11 | 11 | 100% | 97% | - | 210 | 26596 | $0.90 |

## Per variant

| | cases | passed | pass rate | pass@1 | pass^k (all-trials) | input tok | output tok | cost |
|---|---|---|---|---|---|---|---|---|
| bare | 24 | 22 | 92% | 90% | 82% | 582 | 75457 | $3.28 |
| coach | 4 | 3 | 75% | 75% | 100% | 90 | 15417 | $0.51 |
| instr | 25 | 25 | 100% | 99% | 100% | 482 | 60693 | $2.30 |

## Tokens

Model: 1154 in, 151567 out, 5056980 cache read, 408364 cache write. Judge: 333429 in, 9371 out.

## Worst cases

| case | variant | status | pass@1 | failed checks |
|---|---|---|---|---|
| E06 | bare | fail | 0% | max_tool_calls x3 |
| P03 | coach | fail | 0% | max_tool_calls x3, no_invented_numbers x3 |
| E02 | bare | fail | 33% | no_call_after_error:rate_limited x2 |

