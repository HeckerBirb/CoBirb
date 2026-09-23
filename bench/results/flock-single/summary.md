# cobirb-bench — flock-vs-single-single

- commit `719e494862`, 2 task(s) × 1 rep(s)
- max_num_ctx 32k; extra config {}

| Model | Pass rate | Spread across reps | Median turns | Median time |
|---|---|---|---|---|
| ornith-1.5:9b | 50% | — | 27 | 196s |
| qwen3-coder:30b | 100% | — | 58 | 138s |
| gemma4:latest | 50% | — | 28 | 69s |

## Outcomes

| Model | pass | turn_limit | no_progress | unparsed_tool_call | edit_miss | no_change | wrong_result | error | timeout | crash |
|---|---|---|---|---|---|---|---|---|---|---|
| ornith-1.5:9b | 1 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 | 0 |
| qwen3-coder:30b | 2 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| gemma4:latest | 1 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 |

## Per task

| Task | ornith-1.5:9b | qwen3-coder:30b | gemma4:latest |
|---|---|---|---|
| flock-api-and-client | 1/1 | 1/1 | 1/1 |
| flock-three-independent-modules | 0/1 (no_change) | 1/1 | 0/1 (wrong_result) |
