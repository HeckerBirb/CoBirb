# cobirb-bench — v043-flock-staged

- commit `47918a2d1c`, 2 task(s) × 3 rep(s)
- max_num_ctx 32k; extra config {"flock": {"planning": "staged"}}

| Model | Pass rate | Spread across reps | Median turns | Median time |
|---|---|---|---|---|
| qwen3.5:35b-a3b | 50% | 50–50% | 68 | 419s |
| qwen3-coder:30b | 50% | 50–50% | 25 | 237s |
| gpt-oss:20b | 50% | 0–100% | 69 | 206s |
| devstral-small-2:latest | 0% | 0–0% | 0 | 856s |
| ornith-1.5:35b | 33% | 0–50% | 40 | 1105s |

## Outcomes

| Model | pass | turn_limit | no_progress | unparsed_tool_call | edit_miss | no_change | wrong_result | no_flock | error | timeout | crash |
|---|---|---|---|---|---|---|---|---|---|---|---|
| qwen3.5:35b-a3b | 3 | 0 | 0 | 0 | 0 | 0 | 1 | 2 | 0 | 0 | 0 |
| qwen3-coder:30b | 3 | 0 | 0 | 0 | 0 | 0 | 0 | 3 | 0 | 0 | 0 |
| gpt-oss:20b | 3 | 0 | 0 | 0 | 0 | 0 | 2 | 1 | 0 | 0 | 0 |
| devstral-small-2:latest | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 6 | 0 | 0 | 0 |
| ornith-1.5:35b | 2 | 0 | 0 | 0 | 0 | 0 | 2 | 1 | 0 | 1 | 0 |

## Per task

| Task | qwen3.5:35b-a3b | qwen3-coder:30b | gpt-oss:20b | devstral-small-2:latest | ornith-1.5:35b |
|---|---|---|---|---|---|
| flock-api-and-client | 1/3 (no_flock) | 0/3 (no_flock) | 2/3 (no_flock) | 0/3 (no_flock) | 2/3 (wrong_result) |
| flock-three-independent-modules | 2/3 (wrong_result) | 3/3 | 1/3 (wrong_result) | 0/3 (no_flock) | 0/3 (no_flock,timeout,wrong_result) |
