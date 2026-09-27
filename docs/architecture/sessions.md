# Sessions, crypto and undo

`session.py`, `plugins/core/crypto.py`, `checkpoints.py`, `memory.py`, `runtime/catalogues.py`.

## Sessions

- `Turn` and `Session` are the data; `SessionManager` owns a session file — creating, loading and
  saving it through the configured `SessionCrypto`. `runtime/sessions.py` holds what the CLI and the
  TUI share for finding and opening one: the most recent session (`--continue`), the password prompt,
  open errors.
- `SCHEMA_VERSION = 1`. `Session.from_dict` is the only read path and always calls `migrate()`; a
  future schema is refused. Fields since schema 1 are optional, so `_MIGRATIONS` is empty — add
  optional fields, not migrations, unless a change makes an old payload *wrong*
  (`test_every_schema_step_has_a_migration` enforces the pairing).
- `Turn.digest()` hashes role + content + `tool_use` + `phase` — deliberately not `images` — and
  `load` verifies every hash. Never extend the formula — older sessions would fail.
- Session files and the audit log are created `0600` via `os.open` (no chmod window).
- `fork_session()` only reads the source; a truncated branch drops `summary`/`validation`, keeps
  `flock`, records `forked_from = "<path>@turn<N>"`, and carries only the images its turns reference.

## Crypto

AES-256-GCM, key from scrypt `N=2**17, r=8, p=1`. Blob = `b"cobirb1"` + base64 JSON header naming
the KDF parameters + `\n` + base64(`salt||nonce||ciphertext`). Headerless blobs are pre-header files
read at `N=2**14` and rewritten on save. No PQ KEM: a password-encrypted local file has no key
exchange to protect.

## Undo and diff

- **`TreeCheckpoints` snapshots the whole tree before and after every turn**, so `/undo` and `/diff`
  cover shell changes. A separate git dir with the project as work tree: the project need not be a
  repository, its own `.git` is never read or written, its `.gitignore` holds, `ALWAYS_IGNORED`
  (`plugins/core/ignores.py`) and `.git` are excluded, and the user's git config is kept out (no
  hooks, no signing, fixed identity). `0700`, lives for the session (`close()`), dead-process stores
  swept. **Undo restores only files the last turn changed that are still as it left them.** Chosen
  when `git` is installed.
- Per-file `Checkpoints` (the paths a tool's `writes()` declares, `DEFAULT_KEEP_TURNS = 20`) is the
  fallback without git, and what Worker Birbs use — concurrent workers would capture each other's
  work in a tree snapshot.

## Memory catalogues

Named lists of facts fed into the system prompt: `<name>.md` (plaintext) or `<name>.md.enc`
(password-protected, same crypto as sessions) under `paths.memories_dir()`. `public.md` always
exists. Bodies are a flat `- fact` list — no metadata, since it is read straight into a prompt.
Created `0600` via `os.open`: a plaintext catalogue feeds the prompt, so a write window is a way to
put words in the model's mouth.

- `/memories` manages catalogues and `/remember <fact>` adds one — slash commands, not a model tool,
  so they fire exactly once whatever the model can do.
- Which are open is `runtime.catalogues.CatalogueStore`, session-lifetime, never auto-loaded. The TUI
  composes `CatalogueStore.system_prompt()` into the system prompt of every turn; one-shot `-p` runs
  do not use catalogues.
- **Never reaches a Worker Birb or a planning stage**: both run with `system=""`, and workers with
  `project_context=""` too.

## Attached images

`/image <path> [message]` attaches a file to the next prompt (or sends at once with a message); a
quoted path wins, and an unquoted argument naming an existing file is taken whole.

- **The bytes live in `Session.images` (`{id: base64}`, keyed by content hash) inside the encrypted
  blob; `Turn.images` holds only references.** Do not move them out: nothing retains a password, so
  a separate file could never be decrypted to build context.
- `_build_context` resolves every image turn (a missing id degrades to a marker). Pricing and
  elision are in [the loop](loop.md).
- Data is sent only when `supports_vision()` says the model can see.
