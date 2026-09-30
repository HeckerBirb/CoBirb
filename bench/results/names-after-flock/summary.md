# cobirb-bench — names-after-flock

- commit `981951868c`, 2 task(s) × 3 rep(s)
- max_num_ctx 32k; extra config {"flock": {"planning": "staged"}}

| Model | Pass rate | Spread across reps | Median turns | Median time |
|---|---|---|---|---|
| qwen3.6:35b | 100% | 100–100% | 104 | 1278s |
| ornith-1.5:35b | 83% | 50–100% | 28 | 848s |

## Outcomes

| Model | pass | turn_limit | no_progress | unparsed_tool_call | edit_miss | no_change | wrong_result | no_flock | error | timeout | crash |
|---|---|---|---|---|---|---|---|---|---|---|---|
| qwen3.6:35b | 6 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| ornith-1.5:35b | 5 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 0 |

## Per task

| Task | qwen3.6:35b | ornith-1.5:35b |
|---|---|---|
| flock-api-and-client | 3/3 | 3/3 |
| flock-three-independent-modules | 3/3 | 2/3 (timeout) |
