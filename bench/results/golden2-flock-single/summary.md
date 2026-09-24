# cobirb-bench — golden2-flock-single

- commit `5906e6c448`, 1 task(s) × 1 rep(s)
- max_num_ctx 32k; extra config {}

| Model | Pass rate | Spread across reps | Median turns | Median time |
|---|---|---|---|---|
| qwen3.5:35b-a3b | 0% | — | 182 | 1468s |

## Outcomes

| Model | pass | turn_limit | no_progress | unparsed_tool_call | edit_miss | no_change | wrong_result | no_flock | error | timeout | crash |
|---|---|---|---|---|---|---|---|---|---|---|---|
| qwen3.5:35b-a3b | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 | 0 |

## Per task

| Task | qwen3.5:35b-a3b |
|---|---|
| golden-snake | 0/1 (wrong_result) |
