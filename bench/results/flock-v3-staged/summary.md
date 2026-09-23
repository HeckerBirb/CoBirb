# cobirb-bench — flock-v3-staged

- commit `bcb24a7921`, 2 task(s) × 3 rep(s)
- max_num_ctx 32k; extra config {}

| Model | Pass rate | Spread across reps | Median turns | Median time |
|---|---|---|---|---|
| ornith-1.5:9b | 83% | 50–100% | 116 | 496s |
| qwen3-coder:30b | 33% | 0–50% | 114 | 213s |
| gemma4:latest | 50% | 0–100% | 55 | 137s |

## Outcomes

| Model | pass | turn_limit | no_progress | unparsed_tool_call | edit_miss | no_change | wrong_result | error | timeout | crash |
|---|---|---|---|---|---|---|---|---|---|---|
| ornith-1.5:9b | 5 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 |
| qwen3-coder:30b | 2 | 0 | 0 | 0 | 0 | 0 | 4 | 0 | 0 | 0 |
| gemma4:latest | 3 | 0 | 0 | 0 | 0 | 0 | 3 | 0 | 0 | 0 |

## Per task

| Task | ornith-1.5:9b | qwen3-coder:30b | gemma4:latest |
|---|---|---|---|
| flock-api-and-client | 3/3 | 0/3 (wrong_result) | 1/3 (wrong_result) |
| flock-three-independent-modules | 2/3 (wrong_result) | 2/3 (wrong_result) | 2/3 (wrong_result) |
