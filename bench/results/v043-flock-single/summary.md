# cobirb-bench — v043-flock-single

- commit `47918a2d1c`, 2 task(s) × 3 rep(s)
- max_num_ctx 32k; extra config {}

| Model | Pass rate | Spread across reps | Median turns | Median time |
|---|---|---|---|---|
| qwen3.5:35b-a3b | 100% | 100–100% | 60 | 294s |
| qwen3-coder:30b | 83% | 50–100% | 60 | 163s |
| gpt-oss:20b | 83% | 50–100% | 44 | 71s |
| devstral-small-2:latest | 50% | 0–100% | 9 | 972s |
| ornith-1.5:35b | 100% | 100–100% | 48 | 466s |

## Outcomes

| Model | pass | turn_limit | no_progress | unparsed_tool_call | edit_miss | no_change | wrong_result | no_flock | error | timeout | crash |
|---|---|---|---|---|---|---|---|---|---|---|---|
| qwen3.5:35b-a3b | 6 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| qwen3-coder:30b | 5 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 |
| gpt-oss:20b | 5 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 | 0 |
| devstral-small-2:latest | 3 | 0 | 0 | 0 | 0 | 0 | 0 | 2 | 0 | 1 | 0 |
| ornith-1.5:35b | 6 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |

## Per task

| Task | qwen3.5:35b-a3b | qwen3-coder:30b | gpt-oss:20b | devstral-small-2:latest | ornith-1.5:35b |
|---|---|---|---|---|---|
| flock-api-and-client | 3/3 | 3/3 | 3/3 | 1/3 (no_flock) | 3/3 |
| flock-three-independent-modules | 3/3 | 2/3 (no_flock) | 2/3 (wrong_result) | 2/3 (timeout) | 3/3 |
