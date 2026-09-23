# cobirb-bench — v0.31.0

- commit `2835e18ea6`, 18 task(s) × 2 rep(s)
- max_num_ctx 32k; extra config {}

| Model | Pass rate | Spread across reps | Median turns | Median time |
|---|---|---|---|---|
| gemma4:latest | 97% | 94–100% | 7 | 14s |
| ornith-1.5:9b | 100% | 100–100% | 10 | 6s |
| qwen3-coder:30b | 100% | 100–100% | 11 | 11s |
| gpt-oss:20b | 83% | 83–83% | 10 | 9s |
| ornith-1.5:35b | 100% | 100–100% | 8 | 8s |

## Outcomes

| Model | pass | turn_limit | unparsed_tool_call | edit_miss | no_change | wrong_result | error | timeout | crash |
|---|---|---|---|---|---|---|---|---|---|
| gemma4:latest | 35 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 |
| ornith-1.5:9b | 36 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| qwen3-coder:30b | 36 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| gpt-oss:20b | 30 | 0 | 0 | 0 | 0 | 4 | 2 | 0 | 0 |
| ornith-1.5:35b | 36 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |

## Per task

| Task | gemma4:latest | ornith-1.5:9b | qwen3-coder:30b | gpt-oss:20b | ornith-1.5:35b |
|---|---|---|---|---|---|
| add-cli-flag | 2/2 | 2/2 | 2/2 | 2/2 | 2/2 |
| add-input-validation | 2/2 | 2/2 | 2/2 | 2/2 | 2/2 |
| answer-default-port | 2/2 | 2/2 | 2/2 | 2/2 | 2/2 |
| answer-which-function-raises | 2/2 | 2/2 | 2/2 | 2/2 | 2/2 |
| create-module-from-spec | 2/2 | 2/2 | 2/2 | 2/2 | 2/2 |
| document-a-flag | 2/2 | 2/2 | 2/2 | 2/2 | 2/2 |
| edit-json-settings | 2/2 | 2/2 | 2/2 | 2/2 | 2/2 |
| fix-bash-script | 2/2 | 2/2 | 2/2 | 1/2 (wrong_result) | 2/2 |
| fix-from-traceback | 2/2 | 2/2 | 2/2 | 2/2 | 2/2 |
| fix-javascript-sum | 2/2 | 2/2 | 2/2 | 2/2 | 2/2 |
| fix-paginate-off-by-one | 2/2 | 2/2 | 2/2 | 2/2 | 2/2 |
| fix-to-make-tests-pass | 2/2 | 2/2 | 2/2 | 2/2 | 2/2 |
| fix-two-modules | 2/2 | 2/2 | 2/2 | 2/2 | 2/2 |
| implement-slugify | 2/2 | 2/2 | 2/2 | 1/2 (error) | 2/2 |
| multi-file-discount | 2/2 | 2/2 | 2/2 | 0/2 (error,wrong_result) | 2/2 |
| precise-edit-in-large-file | 2/2 | 2/2 | 2/2 | 1/2 (wrong_result) | 2/2 |
| rename-across-files | 1/2 (wrong_result) | 2/2 | 2/2 | 1/2 (wrong_result) | 2/2 |
| write-tests-that-catch-bugs | 2/2 | 2/2 | 2/2 | 2/2 | 2/2 |
