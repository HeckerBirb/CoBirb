# Tools

What the agent can do. Nothing here is pre-approved — see [Permissions](permissions.md) for how you
allow each one.

| Tool | Does |
|---|---|
| `read_file` | Read a file. A large one comes back a range at a time, with the offset to continue from. |
| `list_dir` | List one directory (not recursive). |
| `glob` | Find files by name pattern, e.g. `**/*.py`. |
| `grep` | Search inside files for a regular expression; returns `path:line: text`. |
| `repo_map` | Outline the codebase: which files matter and what they define, most-referenced first. |
| `write_file` | Create a file, or replace one's whole content. |
| `edit_file` | Replace one region of a file. Refuses when the text matches more than one place (unless `replace_all`), forgives trailing-space and indentation slips, and quotes the nearest match on a miss. |
| `apply_patch` | Apply a unified diff or a `*** Begin Patch` patch to one file. |
| `delete_file` | Delete one file (never a directory). |
| `shell` | Run a command — in the sandbox when it is available. |
| `todo` | A checklist the agent keeps for a long task; progress shows on the status bar. Changes nothing, so it never asks. |

Reads (`read_file`, `list_dir`, `glob`, `grep`, `repo_map`) and writes (`write_file`, `edit_file`,
`apply_patch`, `delete_file`) are separate grants: allowing one never allows the other.

**Every turn is undoable.** With git installed, the whole project is snapshotted before and after each
turn — shell changes included — so `/undo` puts back what the last turn changed and `/diff` shows it.
Your own repository is never touched. Without git, only the file tools' changes can be undone.

A write that leaves a Python, JSON or TOML file that no longer parses says so in its result, so the
agent hears about it straight away.

Output is bounded: very long shell output keeps its start and end, long listings and searches come a
page at a time, and a result that was cut says so. MCP servers and tool plugins add more tools, gated
the same way — see [MCP](mcp.md) and [Plugins](plugins-and-mcp.md).
