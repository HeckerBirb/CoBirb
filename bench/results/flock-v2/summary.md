# cobirb-bench — flock-v2

- commit `8f7377d191`, 2 task(s) × 3 rep(s)
- max_num_ctx 32k; extra config {}

| Model | Pass rate | Spread across reps | Median turns | Median time |
|---|---|---|---|---|
| ornith-1.5:9b | 33% | 0–50% | 0 | 726s |
| qwen3-coder:30b | 100% | 100–100% | 72 | 221s |
| gemma4:latest | 50% | 0–100% | 41 | 153s |

## Outcomes

| Model | pass | turn_limit | no_progress | unparsed_tool_call | edit_miss | no_change | wrong_result | error | timeout | crash |
|---|---|---|---|---|---|---|---|---|---|---|
| ornith-1.5:9b | 2 | 0 | 0 | 0 | 0 | 0 | 0 | 4 | 0 | 0 |
| qwen3-coder:30b | 6 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| gemma4:latest | 3 | 0 | 0 | 0 | 0 | 0 | 3 | 0 | 0 | 0 |

## Per task

| Task | ornith-1.5:9b | qwen3-coder:30b | gemma4:latest |
|---|---|---|---|
| flock-api-and-client | 1/3 (error) | 3/3 | 1/3 (wrong_result) |
| flock-three-independent-modules | 1/3 (error) | 3/3 | 2/3 (wrong_result) |
