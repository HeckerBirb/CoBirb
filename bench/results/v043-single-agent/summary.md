# cobirb-bench — v043-single-agent

- commit `47918a2d1c`, 2 task(s) × 3 rep(s)
- max_num_ctx 32k; extra config {}

| Model | Pass rate | Spread across reps | Median turns | Median time |
|---|---|---|---|---|
| qwen3.5:35b-a3b | 100% | 100–100% | 21 | 55s |
| qwen3-coder:30b | 100% | 100–100% | 41 | 69s |
| gpt-oss:20b | 100% | 100–100% | 21 | 30s |
| ornith-1.5:35b | 100% | 100–100% | 34 | 120s |

## Outcomes

| Model | pass | turn_limit | no_progress | unparsed_tool_call | edit_miss | no_change | wrong_result | no_flock | error | timeout | crash |
|---|---|---|---|---|---|---|---|---|---|---|---|
| qwen3.5:35b-a3b | 6 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| qwen3-coder:30b | 6 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| gpt-oss:20b | 6 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| ornith-1.5:35b | 6 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |

## Per task

| Task | qwen3.5:35b-a3b | qwen3-coder:30b | gpt-oss:20b | ornith-1.5:35b |
|---|---|---|---|---|
| flock-api-and-client | 3/3 | 3/3 | 3/3 | 3/3 |
| flock-three-independent-modules | 3/3 | 3/3 | 3/3 | 3/3 |
