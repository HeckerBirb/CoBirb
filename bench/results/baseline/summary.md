# cobirb-bench — baseline

- commit `2ee866abe7`, 18 task(s) × 2 rep(s)
- max_num_ctx 32k; extra config {}

| Model | Pass rate | Spread across reps | Median turns | Median time |
|---|---|---|---|---|
| gemma4:latest | 89% | 89–89% | 6 | 11s |
| ornith-1.5:9b | 100% | 100–100% | 10 | 7s |
| qwen3-coder:30b | 94% | 94–94% | 12 | 12s |
| gpt-oss:20b | 64% | 61–67% | 12 | 7s |
| ornith-1.5:35b | 97% | 94–100% | 10 | 9s |

## Outcomes

| Model | pass | turn_limit | unparsed_tool_call | edit_miss | no_change | wrong_result | error | timeout | crash |
|---|---|---|---|---|---|---|---|---|---|
| gemma4:latest | 32 | 0 | 0 | 0 | 0 | 4 | 0 | 0 | 0 |
| ornith-1.5:9b | 36 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| qwen3-coder:30b | 34 | 0 | 2 | 0 | 0 | 0 | 0 | 0 | 0 |
| gpt-oss:20b | 23 | 8 | 0 | 1 | 0 | 3 | 1 | 0 | 0 |
| ornith-1.5:35b | 35 | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |

## Per task

| Task | gemma4:latest | ornith-1.5:9b | qwen3-coder:30b | gpt-oss:20b | ornith-1.5:35b |
|---|---|---|---|---|---|
| add-cli-flag | 2/2 | 2/2 | 2/2 | 2/2 | 2/2 |
| add-input-validation | 2/2 | 2/2 | 2/2 | 2/2 | 2/2 |
| answer-default-port | 2/2 | 2/2 | 2/2 | 2/2 | 2/2 |
| answer-which-function-raises | 2/2 | 2/2 | 2/2 | 2/2 | 2/2 |
| create-module-from-spec | 2/2 | 2/2 | 2/2 | 2/2 | 2/2 |
| document-a-flag | 2/2 | 2/2 | 2/2 | 1/2 (turn_limit) | 2/2 |
| edit-json-settings | 2/2 | 2/2 | 0/2 (unparsed_tool_call) | 0/2 (turn_limit) | 2/2 |
| fix-bash-script | 2/2 | 2/2 | 2/2 | 0/2 (error,wrong_result) | 2/2 |
| fix-from-traceback | 2/2 | 2/2 | 2/2 | 2/2 | 2/2 |
| fix-javascript-sum | 2/2 | 2/2 | 2/2 | 2/2 | 2/2 |
| fix-paginate-off-by-one | 2/2 | 2/2 | 2/2 | 2/2 | 2/2 |
| fix-to-make-tests-pass | 2/2 | 2/2 | 2/2 | 2/2 | 2/2 |
| fix-two-modules | 2/2 | 2/2 | 2/2 | 1/2 (turn_limit) | 2/2 |
| implement-slugify | 2/2 | 2/2 | 2/2 | 2/2 | 2/2 |
| multi-file-discount | 2/2 | 2/2 | 2/2 | 0/2 (turn_limit) | 2/2 |
| precise-edit-in-large-file | 0/2 (wrong_result) | 2/2 | 2/2 | 0/2 (edit_miss,wrong_result) | 2/2 |
| rename-across-files | 0/2 (wrong_result) | 2/2 | 2/2 | 0/2 (turn_limit) | 2/2 |
| write-tests-that-catch-bugs | 2/2 | 2/2 | 2/2 | 1/2 (wrong_result) | 1/2 (turn_limit) |
