# cobirb-bench — run

- commit `d37227c69d`, 1 task(s) × 1 rep(s)
- max_num_ctx 32k; extra config {"flock": {"planning": "staged"}}

| Model | Pass rate | Spread across reps | Median turns | Median time |
|---|---|---|---|---|
| qwen3-coder:30b | 0% | — | 254 | 1179s |

## Outcomes

| Model | pass | turn_limit | no_progress | unparsed_tool_call | edit_miss | no_change | wrong_result | no_flock | error | timeout | crash |
|---|---|---|---|---|---|---|---|---|---|---|---|
| qwen3-coder:30b | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 | 0 |

## Per task

| Task | qwen3-coder:30b |
|---|---|
| golden-snake | 0/1 (wrong_result) |
