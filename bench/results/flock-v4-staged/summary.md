# cobirb-bench — flock-v4-staged-briefs-after

- commit `16f57f36fb`, 2 task(s) × 3 rep(s)
- max_num_ctx 32k; extra config {"flock_planning": "staged"}

| Model | Pass rate | Spread across reps | Median turns | Median time |
|---|---|---|---|---|
| ornith-1.5:9b | 50% | 0–100% | 80 | 576s |
| qwen3-coder:30b | 33% | 0–50% | 90 | 241s |
| gemma4:latest | 50% | 0–100% | 58 | 188s |

## Outcomes

| Model | pass | turn_limit | no_progress | unparsed_tool_call | edit_miss | no_change | wrong_result | error | timeout | crash |
|---|---|---|---|---|---|---|---|---|---|---|
| ornith-1.5:9b | 3 | 0 | 0 | 0 | 0 | 0 | 1 | 2 | 0 | 0 |
| qwen3-coder:30b | 2 | 0 | 0 | 0 | 0 | 0 | 4 | 0 | 0 | 0 |
| gemma4:latest | 3 | 0 | 0 | 0 | 0 | 1 | 2 | 0 | 0 | 0 |

## Per task

| Task | ornith-1.5:9b | qwen3-coder:30b | gemma4:latest |
|---|---|---|---|
| flock-api-and-client | 2/3 (wrong_result) | 0/3 (wrong_result) | 1/3 (no_change,wrong_result) |
| flock-three-independent-modules | 1/3 (error) | 2/3 (wrong_result) | 2/3 (wrong_result) |
