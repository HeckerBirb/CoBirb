# cobirb-bench — opt-pass-after-single

- commit `32cd45a921`, 24 task(s) × 2 rep(s)
- max_num_ctx 32k; extra config {}

| Model | Pass rate | Spread across reps | Median turns | Median time |
|---|---|---|---|---|
| qwen3-coder:30b | 100% | 100–100% | 20 | 42s |
| qwen3.5:35b-a3b | 100% | 100–100% | 9 | 12s |

## Outcomes

| Model | pass | turn_limit | no_progress | unparsed_tool_call | edit_miss | no_change | wrong_result | no_flock | error | timeout | crash |
|---|---|---|---|---|---|---|---|---|---|---|---|
| qwen3-coder:30b | 48 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| qwen3.5:35b-a3b | 48 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |

## Per task

| Task | qwen3-coder:30b | qwen3.5:35b-a3b |
|---|---|---|
| add-cli-flag | 2/2 | 2/2 |
| add-input-validation | 2/2 | 2/2 |
| answer-default-port | 2/2 | 2/2 |
| answer-which-function-raises | 2/2 | 2/2 |
| create-module-from-spec | 2/2 | 2/2 |
| document-a-flag | 2/2 | 2/2 |
| edit-json-settings | 2/2 | 2/2 |
| fix-bash-script | 2/2 | 2/2 |
| fix-from-traceback | 2/2 | 2/2 |
| fix-javascript-sum | 2/2 | 2/2 |
| fix-paginate-off-by-one | 2/2 | 2/2 |
| fix-to-make-tests-pass | 2/2 | 2/2 |
| fix-two-modules | 2/2 | 2/2 |
| hard-api-migration | 2/2 | 2/2 |
| hard-bug-in-long-file | 2/2 | 2/2 |
| hard-cascading-failures | 2/2 | 2/2 |
| hard-extract-module | 2/2 | 2/2 |
| hard-feature-tests-and-docs | 2/2 | 2/2 |
| hard-implement-ttl-cache | 2/2 | 2/2 |
| implement-slugify | 2/2 | 2/2 |
| multi-file-discount | 2/2 | 2/2 |
| precise-edit-in-large-file | 2/2 | 2/2 |
| rename-across-files | 2/2 | 2/2 |
| write-tests-that-catch-bugs | 2/2 | 2/2 |
