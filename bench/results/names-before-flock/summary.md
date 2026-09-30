# cobirb-bench — names-before-flock

- commit `32cd45a921`, 2 task(s) × 3 rep(s)
- max_num_ctx 32k; extra config {"flock": {"planning": "staged"}}

| Model | Pass rate | Spread across reps | Median turns | Median time |
|---|---|---|---|---|
| qwen3.6:35b | 67% | 50–100% | 58 | 912s |
| ornith-1.5:35b | 83% | 50–100% | 56 | 1512s |

## Outcomes

| Model | pass | turn_limit | no_progress | unparsed_tool_call | edit_miss | no_change | wrong_result | no_flock | error | timeout | crash |
|---|---|---|---|---|---|---|---|---|---|---|---|
| qwen3.6:35b | 4 | 0 | 1 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 |
| ornith-1.5:35b | 5 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 |

## Per task

| Task | qwen3.6:35b | ornith-1.5:35b |
|---|---|---|
| flock-api-and-client | 1/3 (no_flock,no_progress) | 2/3 (no_flock) |
| flock-three-independent-modules | 3/3 | 3/3 |
