# cobirb-bench — run

- commit `f1bb7498c4`, 2 task(s) × 3 rep(s)
- max_num_ctx 32k; extra config {"flock": {"planning": "staged"}}

| Model | Pass rate | Spread across reps | Median turns | Median time |
|---|---|---|---|---|
| qwen3.5:35b-a3b | 83% | 50–100% | 116 | 1028s |
| qwen3-coder:30b | 33% | 0–50% | 48 | 252s |

## Outcomes

| Model | pass | turn_limit | no_progress | unparsed_tool_call | edit_miss | no_change | wrong_result | no_flock | error | timeout | crash |
|---|---|---|---|---|---|---|---|---|---|---|---|
| qwen3.5:35b-a3b | 5 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 | 0 |
| qwen3-coder:30b | 2 | 0 | 1 | 0 | 0 | 0 | 0 | 3 | 0 | 0 | 0 |

## Per task

| Task | qwen3.5:35b-a3b | qwen3-coder:30b |
|---|---|---|
| flock-api-and-client | 3/3 | 1/3 (no_flock,no_progress) |
| flock-three-independent-modules | 2/3 (wrong_result) | 1/3 (no_flock) |
