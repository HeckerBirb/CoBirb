# cobirb-bench — opt-pass-before-flock

- commit `aef147c472`, 2 task(s) × 2 rep(s)
- max_num_ctx 32k; extra config {"flock": {"planning": "staged"}}

| Model | Pass rate | Spread across reps | Median turns | Median time |
|---|---|---|---|---|
| qwen3-coder:30b | 0% | 0–0% | 0 | 358s |
| qwen3.5:35b-a3b | 100% | 100–100% | 54 | 604s |

## Outcomes

| Model | pass | turn_limit | no_progress | unparsed_tool_call | edit_miss | no_change | wrong_result | no_flock | error | timeout | crash |
|---|---|---|---|---|---|---|---|---|---|---|---|
| qwen3-coder:30b | 0 | 0 | 1 | 0 | 0 | 0 | 0 | 3 | 0 | 0 | 0 |
| qwen3.5:35b-a3b | 4 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |

## Per task

| Task | qwen3-coder:30b | qwen3.5:35b-a3b |
|---|---|---|
| flock-api-and-client | 0/2 (no_flock) | 2/2 |
| flock-three-independent-modules | 0/2 (no_flock,no_progress) | 2/2 |
