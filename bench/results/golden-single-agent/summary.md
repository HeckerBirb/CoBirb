# cobirb-bench — golden-single-agent

- commit `e1127a89ba`, 1 task(s) × 1 rep(s)
- max_num_ctx 32k; extra config {}

| Model | Pass rate | Spread across reps | Median turns | Median time |
|---|---|---|---|---|
| qwen3.5:35b-a3b | 0% | — | 66 | 615s |
| qwen3-coder:30b | 0% | — | 84 | 510s |

## Outcomes

| Model | pass | turn_limit | no_progress | unparsed_tool_call | edit_miss | no_change | wrong_result | no_flock | error | timeout | crash |
|---|---|---|---|---|---|---|---|---|---|---|---|
| qwen3.5:35b-a3b | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 | 0 |
| qwen3-coder:30b | 0 | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |

## Per task

| Task | qwen3.5:35b-a3b | qwen3-coder:30b |
|---|---|---|
| golden-snake | 0/1 (wrong_result) | 0/1 (turn_limit) |
