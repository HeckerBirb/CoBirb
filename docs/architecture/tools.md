# Tools

`plugins/core/tools.py`, `patches.py`.

Built-ins: `read_file`, `write_file`, `edit_file`, `apply_patch`, `delete_file`, `glob`, `grep`,
`list_dir`, `repo_map`, `shell`, `todo`. Subclasses of `CobirbTool` declare `NAME: ClassVar[str]`;
the SPI declares `name` as a **method** and `ToolRegistry.register` rejects anything else.
Descriptions say when to use a tool and which neighbour fits better — small models lean on them.

- **`edit_file` changes exactly one region or refuses** (`_plan_edit`): several matches are refused
  with their line numbers (`replace_all` opts in); a miss tries a unique whitespace-tolerant
  whole-line match (trailing space, CRLF, an indent missing uniformly, re-added to `new_str`); a true
  miss quotes the closest region. Success shows the edited lines.
- **`apply_patch`** takes unified diffs, diffs with bare `@@` (placed by context), and
  `*** Begin Patch` (`patches.py`), single-file only. Context placement refuses a block that matches
  twice without an `@@` anchor.
- **`delete_file`** removes files, never directories; scoped, previewed and undoable like any write.
- **`todo`** is a checklist replaced whole on each call; progress shows on the status bar.
- **A write that leaves Python, JSON or TOML unparseable says so** (`_syntax_note`, in-process). A
  note, not a refusal. No per-edit lint command — `verify_command` covers the user's own check.
- **Results are bounded**: 256 KiB per read (paged, saying how to continue), 500 grep matches, 300
  chars per line, 1000 list/glob entries, 64 KiB of shell output (head **and** tail), 8 KiB of
  preview.
- **`shell`**: own process group, default timeout 300 s (max 600), `cancel_running()` for Ctrl+C,
  runs in the sandbox when active (see [permissions](permissions.md)). Each call is its own process,
  so a `cd` does not persist: `cwd` is the stateless way, and `_changes_directory_only()` notes a
  `cd`-only line. That detector is advisory and fail-open, so it must not share code with
  `policy._segments`, which fails closed.
- **A tool panel names what the call was aimed at** (`render.build_tool_call_panel`): a `shell` panel
  is headed by its command (bounded), a `read_file`/`write_file`/`delete_file` panel by its path —
  refused or not. `edit_file` and `apply_patch` show their diff, which names the file.
