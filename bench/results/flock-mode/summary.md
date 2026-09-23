# cobirb-bench — flock-vs-single-flock

- commit `5df3509884`, 2 task(s) × 1 rep(s)
- max_num_ctx 32k; extra config {}

| Model | Pass rate | Spread across reps | Median turns | Median time |
|---|---|---|---|---|
| ornith-1.5:9b | 0% | — | 0 | 464s |
| qwen3-coder:30b | 50% | — | 0 | 254s |
| gemma4:latest | 0% | — | 0 | 180s |

## Outcomes

| Model | pass | turn_limit | no_progress | unparsed_tool_call | edit_miss | no_change | wrong_result | error | timeout | crash |
|---|---|---|---|---|---|---|---|---|---|---|
| ornith-1.5:9b | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 2 | 0 | 0 |
| qwen3-coder:30b | 1 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 | 0 |
| gemma4:latest | 0 | 0 | 0 | 0 | 0 | 2 | 0 | 0 | 0 | 0 |

## Per task

| Task | ornith-1.5:9b | qwen3-coder:30b | gemma4:latest |
|---|---|---|---|
| flock-api-and-client | 0/1 (error) | 0/1 (no_change) | 0/1 (no_change) |
| flock-three-independent-modules | 0/1 (error) | 1/1 | 0/1 (no_change) |
