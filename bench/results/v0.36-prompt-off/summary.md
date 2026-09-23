# cobirb-bench — v0.36-prompt-off

- commit `603c292020`, 24 task(s) × 2 rep(s)
- max_num_ctx 32k; extra config {}

| Model | Pass rate | Spread across reps | Median turns | Median time |
|---|---|---|---|---|
| gemma4:latest | 81% | 79–83% | 7 | 14s |
| ornith-1.5:9b | 100% | 100–100% | 10 | 8s |
| qwen3-coder:30b | 100% | 100–100% | 14 | 15s |
| gpt-oss:20b | 92% | 88–96% | 12 | 7s |
| ornith-1.5:35b | 96% | 92–100% | 10 | 11s |

## Outcomes

| Model | pass | turn_limit | no_progress | unparsed_tool_call | edit_miss | no_change | wrong_result | error | timeout | crash |
|---|---|---|---|---|---|---|---|---|---|---|
| gemma4:latest | 39 | 0 | 0 | 0 | 0 | 0 | 9 | 0 | 0 | 0 |
| ornith-1.5:9b | 48 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| qwen3-coder:30b | 48 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| gpt-oss:20b | 44 | 1 | 0 | 0 | 0 | 1 | 2 | 0 | 0 | 0 |
| ornith-1.5:35b | 46 | 0 | 0 | 0 | 0 | 0 | 1 | 1 | 0 | 0 |

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
| fix-bash-script | 2/2 | 2/2 | 2/2 | 2/2 | 2/2 |
| fix-from-traceback | 2/2 | 2/2 | 2/2 | 2/2 | 2/2 |
| fix-javascript-sum | 2/2 | 2/2 | 2/2 | 2/2 | 2/2 |
| fix-paginate-off-by-one | 2/2 | 2/2 | 2/2 | 2/2 | 2/2 |
| fix-to-make-tests-pass | 2/2 | 2/2 | 2/2 | 2/2 | 2/2 |
| fix-two-modules | 2/2 | 2/2 | 2/2 | 2/2 | 2/2 |
| hard-api-migration | 0/2 (wrong_result) | 2/2 | 2/2 | 0/2 (no_change,wrong_result) | 2/2 |
| hard-bug-in-long-file | 2/2 | 2/2 | 2/2 | 2/2 | 2/2 |
| hard-cascading-failures | 2/2 | 2/2 | 2/2 | 2/2 | 2/2 |
| hard-extract-module | 1/2 (wrong_result) | 2/2 | 2/2 | 1/2 (wrong_result) | 2/2 |
| hard-feature-tests-and-docs | 0/2 (wrong_result) | 2/2 | 2/2 | 1/2 (turn_limit) | 1/2 (wrong_result) |
| hard-implement-ttl-cache | 1/2 (wrong_result) | 2/2 | 2/2 | 2/2 | 2/2 |
| implement-slugify | 2/2 | 2/2 | 2/2 | 2/2 | 1/2 (error) |
| multi-file-discount | 2/2 | 2/2 | 2/2 | 2/2 | 2/2 |
| precise-edit-in-large-file | 2/2 | 2/2 | 2/2 | 2/2 | 2/2 |
| rename-across-files | 0/2 (wrong_result) | 2/2 | 2/2 | 2/2 | 2/2 |
| write-tests-that-catch-bugs | 1/2 (wrong_result) | 2/2 | 2/2 | 2/2 | 2/2 |
