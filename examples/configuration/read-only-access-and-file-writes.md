# Read-only access and file writes

For someone who trusts CoBirb to read anything, edit files, and poke around with safe,
non-destructive shell commands — but does not want it running anything that could change
history, delete something, or reach the network.

```json
{
  "allow_read_dirs": ["~/code/myproject"],
  "allow_write_dirs": ["~/code/myproject"],
  "allow_tools": [
    "read_file",
    "list_dir",
    "glob",
    "grep",
    "repo_map",
    "write_file",
    "edit_file",
    "apply_patch",
    "shell(git status)",
    "shell(git diff)",
    "shell(git log)",
    "shell(git show)",
    "shell(git blame)",
    "shell(rg)",
    "shell(grep)",
    "shell(find)",
    "shell(sed -n)",
    "shell(cat)",
    "shell(wc)"
  ]
}
```

## What this grants

- Every **read** tool (`read_file`, `list_dir`, `glob`, `grep`, `repo_map`) and every **write**
  tool (`write_file`, `edit_file`, `apply_patch`), scoped to the directories you name.
- A handful of `shell` prefixes that only ever look — `git status`, `git diff`, `git log`,
  `rg`, `grep`, `find`, `cat`, `wc` — plus `sed -n`, the read-only form of `sed` (no `-i`, so it
  can't edit in place).

## What it does not grant

Nothing that runs code, installs anything, or touches version control history: no bare `shell`,
no `git commit`, `git push`, `npm install`, `pip install`, `curl`, or `rm`. Anything outside this
list — including `sed -i` or `git add` — still stops and asks.

This is a good default for exploring an unfamiliar codebase, or for letting CoBirb refactor
files while you keep your hands on the git history yourself.
