# Full file operations

Everything in [read-only access and file writes](read-only-access-and-file-writes.md), plus the
shell commands that move, copy, create and remove files and directories — for someone
comfortable with CoBirb reorganizing a project's files, not just editing their contents.

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
    "shell(git add)",
    "shell(git mv)",
    "shell(git rm)",
    "shell(mkdir)",
    "shell(mv)",
    "shell(cp)",
    "shell(rm)",
    "shell(rmdir)",
    "shell(touch)"
  ]
}
```

## What this adds over read-only + writes

`mkdir`, `mv`, `cp`, `rm`, `rmdir` and `touch` — the everyday filesystem operations most tools
assume are already there — plus `git add`, `git mv` and `git rm` so the working tree and the
index move together instead of drifting apart.

## What is still missing on purpose

`git commit`, `git push`, and anything that runs project code (`npm install`, `pytest`, a build
script) are deliberately absent. Renaming and deleting files is one kind of trust; recording
history or executing arbitrary project commands is another, and this example only grants the
first. Add commands as narrow prefixes — `shell(git commit -m)` rather than bare `shell(git)` —
when you decide you want the second.
