# cobirb-bench — flock-v5-staged-locked

- commit `b4c65816f9`, 2 task(s) × 3 rep(s)
- max_num_ctx 32k; extra config {"flock_planning": "staged"}

| Model | Pass rate | Spread across reps | Median turns | Median time |
|---|---|---|---|---|
| ornith-1.5:9b | 67% | 50–100% | 48 | 532s |
| qwen3-coder:30b | 83% | 50–100% | 17 | 123s |
| gemma4:latest | 50% | 0–100% | 37 | 145s |

## Outcomes

| Model | pass | turn_limit | no_progress | unparsed_tool_call | edit_miss | no_change | wrong_result | error | timeout | crash |
|---|---|---|---|---|---|---|---|---|---|---|
| ornith-1.5:9b | 4 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 1 |
| qwen3-coder:30b | 5 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 |
| gemma4:latest | 3 | 0 | 0 | 0 | 0 | 0 | 3 | 0 | 0 | 0 |

## Per task

| Task | ornith-1.5:9b | qwen3-coder:30b | gemma4:latest |
|---|---|---|---|
| flock-api-and-client | 3/3 | 3/3 | 2/3 (wrong_result) |
| flock-three-independent-modules | 1/3 (crash,error) | 2/3 (error) | 1/3 (wrong_result) |
