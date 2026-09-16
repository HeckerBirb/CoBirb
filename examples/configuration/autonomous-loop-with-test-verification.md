# Autonomous loop with test verification

For unattended runs — a CI job, a scheduled task, `--headless` — where nothing can stop to ask,
so the safety net has to be the test suite instead: every change is checked, and CoBirb gets a
bounded number of tries to fix what it broke before giving up.

```json
{
  "models": { "default": { "name": "qwen2.5-coder:14b" } },
  "allow_read_dirs": ["~/code/myproject"],
  "allow_write_dirs": ["~/code/myproject/src"],
  "allow_tools": [
    "read_file", "list_dir", "glob", "grep", "repo_map",
    "write_file", "edit_file", "apply_patch",
    "shell(pytest)"
  ],
  "verify_command": "pytest -q",
  "verify_timeout": 180,
  "verify_fix_attempts": 2,
  "checkpoints": true,
  "audit_log": true,
  "redact_secrets": true
}
```

## What each key is doing here

- **`allow_write_dirs` scoped to `src`** — tests and config stay out of reach even
  unattended; only the code the tests exercise can change.
- **`verify_command`** runs after any turn that changed a file. A failure is handed back to the
  model as the reason to try again, up to **`verify_fix_attempts`** times, bounded so a stubborn
  failure can't loop forever.
- **`verify_timeout`** is raised past the default 120s — a real suite can be slower than the
  smoke tests a CI box is used to.
- **`checkpoints`** keeps a snapshot behind every change, so a run that ends badly is still one
  `/undo` away from clean, even with nobody watching.
- **`audit_log`** records every tool call to `~/.cobirb/audit.jsonl` — the record you'd want to
  read after an unattended run, since there was no prompt to see at the time. It is plain text;
  keep it out of anything that syncs off the machine.

## Running it

```bash
cobirb -p "add input validation to the report parser" \
  --headless --output json \
  --allow-tool='shell(pytest)'
```

`--headless` never prompts and refuses anything not already permitted; exit code `2` means it
finished but had to refuse something outside this config's grants.
