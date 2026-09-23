# cobirb-bench — smoke

- commit `2ee866abe7`, 1 task(s) × 1 rep(s)
- max_num_ctx 32k; extra config {}

| Model | Pass rate | Spread across reps | Median turns | Median time |
|---|---|---|---|---|
| gemma4:latest | 100% | — | 4 | 15s |

## Outcomes

| Model | pass | turn_limit | unparsed_tool_call | edit_miss | no_change | wrong_result | error | timeout | crash |
|---|---|---|---|---|---|---|---|---|---|
| gemma4:latest | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |

## Per task

| Task | gemma4:latest |
|---|---|
| answer-default-port | 1/1 |
