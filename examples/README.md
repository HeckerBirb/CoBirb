# Examples

Configurations you can start from instead of `~/.cobirb/config.json` empty. Each page is one
focused idea — copy the JSON block into your own config and adjust the paths and model names.

## [configuration/](configuration/)

| Example | Demonstrates |
|---|---|
| [Read-only access and file writes](configuration/read-only-access-and-file-writes.md) | Trusting CoBirb with every read and every file edit, plus a short list of shell commands that only ever look — nothing that runs code, installs anything, or rewrites history. |
| [Full file operations](configuration/full-file-operations.md) | The above, plus `mv`, `cp`, `rm`, `mkdir` and their `git` counterparts — for someone comfortable with CoBirb reorganizing a project's files, not just their contents. |
| [Flock: strong planner, four workers](configuration/flock-strong-planner-four-workers.md) | Giving Brainy Birb the most capable model you have while four Worker Birbs share a smaller-but-still-strong one, plus a worked charter showing how the worker count and file scopes come together. |
| [Autonomous loop with test verification](configuration/autonomous-loop-with-test-verification.md) | Unattended, `--headless` runs where nothing can stop to ask — `verify_command` and bounded `verify_fix_attempts` stand in for the approval prompt, backed by checkpoints and an audit log. |

Every key used here is documented in full in [`docs/manual/config.md`](../docs/manual/config.md);
[`docs/manual/permissions.md`](../docs/manual/permissions.md) covers how `allow_tools` and the
directory grants are actually checked.
