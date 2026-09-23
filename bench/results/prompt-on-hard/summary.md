# cobirb-bench — prompt-on-hard

- commit `603c292020`, 6 task(s) × 2 rep(s)
- max_num_ctx 32k; extra config {"system_prompt": "harness"}

| Model | Pass rate | Spread across reps | Median turns | Median time |
|---|---|---|---|---|
| gemma4:latest | 42% | 33–50% | 18 | 38s |
| ornith-1.5:9b | 100% | 100–100% | 19 | 22s |
| qwen3-coder:30b | 92% | 83–100% | 30 | 33s |
| gpt-oss:20b | 75% | 67–83% | 27 | 23s |
| ornith-1.5:35b | 100% | 100–100% | 18 | 27s |

## Outcomes

| Model | pass | turn_limit | no_progress | unparsed_tool_call | edit_miss | no_change | wrong_result | error | timeout | crash |
|---|---|---|---|---|---|---|---|---|---|---|
| gemma4:latest | 5 | 0 | 1 | 0 | 0 | 1 | 5 | 0 | 0 | 0 |
| ornith-1.5:9b | 12 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| qwen3-coder:30b | 11 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 |
| gpt-oss:20b | 9 | 1 | 0 | 0 | 0 | 0 | 2 | 0 | 0 | 0 |
| ornith-1.5:35b | 12 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |

## Per task

| Task | gemma4:latest | ornith-1.5:9b | qwen3-coder:30b | gpt-oss:20b | ornith-1.5:35b |
|---|---|---|---|---|---|
| hard-api-migration | 0/2 (no_progress,wrong_result) | 2/2 | 1/2 (error) | 0/2 (wrong_result) | 2/2 |
| hard-bug-in-long-file | 2/2 | 2/2 | 2/2 | 1/2 (turn_limit) | 2/2 |
| hard-cascading-failures | 1/2 (no_change) | 2/2 | 2/2 | 2/2 | 2/2 |
| hard-extract-module | 1/2 (wrong_result) | 2/2 | 2/2 | 2/2 | 2/2 |
| hard-feature-tests-and-docs | 0/2 (wrong_result) | 2/2 | 2/2 | 2/2 | 2/2 |
| hard-implement-ttl-cache | 1/2 (wrong_result) | 2/2 | 2/2 | 2/2 | 2/2 |
