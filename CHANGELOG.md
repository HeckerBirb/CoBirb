# Changelog

All notable changes to CoBirb are recorded here, newest first. See
[`AGENTS.md`](./AGENTS.md) for the architecture and design reasoning behind these changes,
and its §12 decisions record for the ones that were designed and then deliberately *not* built.

## [Unreleased]

- **Fixed: approving a charter asked twice.** `/charter`, and a charter proposed mid-conversation,
  put up an approval dialog and then handed the charter to the flock — which asks the identical
  question, word for word, at its own stage 3. One decision, two dialogs, the second repeating the
  first. Only the flock's own prompt asks now: it is the design's single decision point, and a
  second dialog saying the same thing teaches people to dismiss both without reading either.
- **Fixed: `/charter` offered to re-run a flock that had already finished.** `propose_charter`
  keeps the last charter it accepted so a dismissed dialog stays recoverable, but nothing cleared
  it once a round had actually run — so `/charter` afterwards silently offered to do the whole
  thing again.

## [0.16.0]

- **The Flock tab shows Brainy Birb's working-out while it plans.** `/flock` moves you to that
  tab, but its worker panes only exist once a charter does — the end of the longest phase of the
  run — so until then it sat blank while everything happened on the tab you had just left. It now
  carries the last eight things Brainy Birb did, and `[ Waiting for LLM... ]` while it is blocked
  on a reply. The section disappears as soon as a charter is proposed and the panes take over.

## [0.15.5]

- **Fixed: a charter Brainy Birb wrote into its reply left the run dead.** CoBirb reads tool calls
  only from Ollama's native `tool_calls` field, so a model that writes the charter into its
  *answer* instead — fenced, or as plain TOML — produces no tool call at all, and the orchestrator
  reads the reply as a final answer. The flock then reported that no charter was proposed while
  the charter sat in the transcript in front of you. It is now read from there when the tool was
  not called, and the run says that is what happened. This is the "I see the proposed charter and
  nothing happens" case, and why nudging sometimes worked: native tool-calling is not reliable
  per-call on local models, and gets less so as the context fills with a skeleton.
- **Brainy Birb is told the mechanism, not just the instruction.** `BRAINY_RULES` said to call
  `propose_charter`; it never said that writing the charter into a reply *does nothing*. It does
  now, and it is told that every file it writes costs a planning turn.
- **Running out of planning turns is its own outcome.** A skeleton with more files in it than the
  budget allows used to end with `Stopped after 30 turns without a final answer.` reported
  verbatim — a synthetic string that reads exactly like a considered answer. It now says the turns
  ran out, that the skeleton is still there, and what to do next.
- **The Flock tab shows progress while Brainy Birb is planning.** `/flock` moves you to that tab,
  whose panes are only built once a charter exists — the end of the longest phase — so until then
  you watched one unchanging line while all the work rendered into the tab you had just left.
- **Fixed: streamed tool calls split across chunks lost all but the last.** Dormant with Ollama,
  which sends them together, but wrong.

## [0.15.4]

- **Fixed: a charter Brainy Birb could not get right became a loop.** Every rejection came back
  with the same twenty-five-line template, which is the strongest available hint to a model that
  the thing to send next is what it just sent. The template is now shown once; after that the
  rejection names the error and says not to resend. After five attempts the tool stops asking
  altogether and tells Brainy Birb to explain what it is stuck on instead, and planning no longer
  spends a second turn budget retrying a model that has already exhausted them. **0.15.3's retry
  made this worse before it made it better** — it doubled the turns available for failing.
- **A TOML error now names a likely cause.** `Invalid value (at line 1, column 13)` is the
  identical message for a curly quote and for an unquoted string, which tells a model correcting
  its own output nothing it can act on. Both are now identified by name, with the offending line
  quoted.

## [0.15.3]

- **Fixed: a rejected charter ended a flock as though nothing had been wrong.** When validation
  refused a charter, `charter` came back as `None` — which is also what Brainy Birb deciding the
  work should not be divided looks like. So the run reported the model's own account of it, which
  in one case was *"The charter has been finalized and submitted successfully"*, and returned
  before ever reaching the approval dialog. A charter that was attempted and rejected is now its
  own outcome, reported with the number of attempts and the reason the last one failed. Brainy
  Birb also gets one more try, with the rejection quoted back, because a model told its charter is
  invalid will otherwise often end the turn by announcing success.
- **Fixed: a charter validation message stopped mid-sentence.** `worker 'w3' lists … under tests
  but does not write them — a worker's acceptance tests are its own files, so that they can be
  added to` — and that was the whole message. It now says what to do about it. This was the error
  the model was given in the run above, which is some of why it could not correct itself.
- **`propose_charter` is available for the whole session.** It used to be registered for the
  planning turn and taken away afterwards, while the planning transcript — which tells Brainy Birb
  to deliver a charter by calling it — stayed in context for the rest of the session. So "redo the
  plan" produced a call to a tool that was no longer there, an `Unknown tool` result, and a model
  reasonably concluding something had broken. It stays registered and permitted now.
- **A charter proposed outside a flock run now reaches you.** Brainy Birb can propose one at any
  point, not only while planning; the approval dialog appears as soon as the current turn finishes.
  Approving it runs the flock from that charter without planning again.
- **`/charter`** — review the last proposed charter and run it if you approve, for one that was
  dismissed or proposed while another flock was still running. `/flock` with no objective does the
  same thing.

## [0.15.2]

- **`scripts/release.sh` cuts a release.** Bump level in, version bump and CHANGELOG heading out,
  then a commit and a tag; `--push` to ship it, `--test` to run the suite first, neither by
  default. Nothing changes for anyone using CoBirb — this is a maintainer script, and it exists
  because the sequence had been run by hand five times and had grown a different set of
  unnecessary checks around it each time.

## [0.15.1]

- **Fixed: a flock round that went badly took the review down with it.** Brainy Birb's review runs
  on the same session as its planning, so `BRAINY_RULES` — which says to deliver a charter by
  calling `propose_charter` — was still in its context, as was its own successful call from
  earlier. The review prompt then asked it for "an amended charter", while `_plan` deregisters that
  tool the moment planning ends. So a round with anything to fix produced the obedient thing: a
  call to a tool that was no longer there, an `Unknown tool` result no amount of re-reading could
  argue with, and the rest of the review turns spent failing to recover. The review now asks for
  the second round in prose and says plainly that the tool is gone. Re-registering it would have
  been worse: a round ends here by design, so nothing reads a second charter and the call would
  have succeeded and been discarded in silence.

## [0.15.0]

- **A demo GIF replaces the static screenshot in the README.** `docs/images/cobirb-demo.gif`
  shows the interactive app actually being used rather than one frame of it.
- **Typing `/` lists the commands.** Five rows under the prompt box, the same shape the `@` file
  picker already has: `↑`/`↓` to move, `tab` or `enter` to pick, `escape` to dismiss, and keep
  typing to narrow. Each row carries the command's own one-line description, and a `5 of 15`
  counter says how many more matched than fit.
  - **Your own commands are in the list**, tagged `user` or `project`. They were previously
    invisible unless you already knew their names or ran `/commands`.
  - A slash only opens it as the **first word** of a message, so "remind me to /clear later" stays
    prose and a path like `/usr/bin` closes it again at the second slash. That matches what
    dispatch has always done — a command mid-sentence has never run.
  - Descriptions come from the handlers' own docstrings rather than a list kept beside them; seven
    commands that had no summary line, or led with their signature, now read properly in `/help`
    too.

## [0.14.2]

- **Fixed: `~` in a path given to a tool was treated as a directory called `~`.** "Write it to
  `~/git/c2/x.py`" produced `<cwd>/~/git/c2/x.py` and reported success. `CobirbTool._resolve` now
  expands it — and so does `Policy._resolve`, in the same change and for the same reason: the
  policy exists to say where a call will land *before* it is approved, so the two resolving
  differently would mean approving one path and writing another. A `~` path is now gated as the
  file in your home directory it will really become, which also means approving your working
  directory does not quietly carry it.
- **Fixed: a `cd` in `shell` looked like it worked.** Each call is its own process, so `cd
  somewhere` moved a shell that exited immediately after, and the next call started where the last
  one did — reported as a bare `exit=0`, indistinguishable from having stuck, with the mistake
  only surfacing when something later ran in the wrong place. A command that is *only* a `cd` now
  says so in its result, and `shell` takes an optional `cwd` argument for running one call
  somewhere else without a `cd` at all.
- **`/clear`.** Starts the conversation over from here: clears the screen and the model's context
  together. **Nothing is deleted** — it records a marker turn, a point in the history the way
  committing an emptied file is a new commit rather than a rewrite of the ones before it. The
  earlier turns stay in the session file, so it remains a complete record for auditing, and
  `/export` still writes all of it out. Reopening a cleared session picks up from the marker
  rather than replaying a conversation you had already put behind you.

## [0.14.1]

- **Fixed: `install.sh` could not work out the latest release.** It read the tag off GitHub's
  `/releases/latest` HTML redirect, which turns out to depend on a "latest" flag that is not set
  as reliably as it looks — a repository's only published, non-draft, non-prerelease release can
  still redirect to the releases *index*, and `releases` is not a version. The one-liner failed
  with `'releases' is not a release version`. It now asks the API, which answers directly, and
  keeps the redirect as a fallback for a caller that has spent its unauthenticated 60 requests an
  hour. Naming a release explicitly (`--version v0.14.0`) was unaffected throughout.

## [0.14.0]

- **A one-line install.** `curl -fsSL .../install.sh | bash` builds a virtualenv in
  `~/.local/share/cobirb`, installs a checksum-verified release into it, and puts `cobirb` on
  your `PATH` at `~/.local/bin/cobirb`. No `sudo`, no `pipx`, no venv to activate, and nothing
  written outside your home directory. Python 3.11+ is the only prerequisite. Previously the only
  way in was a git clone plus knowing which of `pip install -e .` or `pipx` you wanted.
  - The script is wrapped in a single function it calls on its last line, so a download that dies
    halfway through executes nothing rather than the first half of an installer.
  - The wheel is verified against the release's `SHA256SUMS` before anything is installed. A
    mismatch aborts rather than retries.
  - `--uninstall` removes the virtualenv and the symlink and **leaves `~/.cobirb/` alone** —
    config, sessions and memory outlive any install.
- **`cobirb --upgrade` now knows what kind of install it is in.** Managed (put there by
  `install.sh`), a git checkout, or neither. A managed upgrade re-runs the installer that put it
  there with a different `--version`, so **upgrading and downgrading are one operation**:
  `cobirb --upgrade v0.13.0 --force` goes back the same way `cobirb --upgrade` goes forward. The
  script stays the only implementation of "move to version X", because it is also what a
  first-time user runs. An install that is neither shape is told so, and told what would work,
  instead of failing with "could not find a git checkout".
- **Releases carry artifacts.** A `vX.Y.Z` tag now builds a wheel and an sdist and attaches them,
  `SHA256SUMS` and `install.sh` to the GitHub release. The tag is checked against the packaged
  version first, so a release can't ship a wheel nobody can ask for by name.
- **`cobirb doctor` reports the install shape**, and no longer warns that a perfectly good
  managed install is "not an editable clone". It deliberately does not check whether a newer
  release exists on a managed install: that means asking GitHub, and `doctor` talks to the
  endpoint you configured and nothing else.

## [0.13.1]

- **Fixed: `cobirb doctor` reported a working model as missing when the tag was omitted.** Ollama
  treats `gemma4` and `gemma4:latest` as the same model and serves either, but `list_models` only
  ever reports the qualified form — so comparing them as raw strings told people to pull a model
  they already had. An absent tag now means `:latest` on both sides of the comparison. A
  *different* tag (`thing:70b` against `thing:9b`) is still a failure, and a genuinely absent
  model still is too.

## [0.13.0] — At hand

- **`@path` mentions.** Type `@` in the prompt box and a five-row picker appears; ↑/↓ to move,
  tab or enter to pick, escape to dismiss. The named file is sent with your message, so putting a
  file in front of the model no longer costs a `read_file` call, an approval prompt and a second
  round trip.
  - **Fuzzy matching** — subsequence, the fzf/"Goto Anything" behaviour: letters must appear in
    order but needn't be adjacent, so `gba` finds `global.py`, `general_batch.py` and `gba.py`.
    Ranking is what makes it usable: an exact name wins outright, then a prefix, then letters
    landing on word boundaries (`general_batch` above `global` for `gb`), then the rest.
  - Expanded on the way to the model, never into the transcript — the transcript shows
    `@src/main.py` as typed. Mentioned files are capped like any other read and go through the
    same secret redaction; the candidate list honours the ignore rules `glob` and `grep` use.
  - With the picker open, enter chooses rather than sends. Sending a half-typed mention is never
    what was meant.
- **`cobirb --continue`.** Reopens the session you were last in, and implies `--password` —
  continuing a session means unlocking one, so asking for both flags would be asking twice. An
  explicit `-w <password>` is still honoured, so it can be scripted.
- **`cobirb doctor`** (also `cobirb --doctor`). One command for "am I ready to go", replacing five
  failures that each surfaced somewhere different.
  - **Config**: parses, every key is one CoBirb actually reads, values are the right type, and
    what it references exists. The key check is the one that earns its keep — `Config.get` is a
    plain lookup, so `redact_secret` for `redact_secrets` currently reads as redaction *off* while
    it stays *on*, in total silence.
  - **Environment**: endpoint reachable, every configured role's model actually pulled, and
    whether it can see images. These are the checks that otherwise fail mid-turn.
  - **Install**: version against the latest release, whether `--upgrade` can work here, and
    whether the checkout is on a branch — a detached `HEAD` swallows the next commit made in it.
  - Exits non-zero only on a real failure. A warning is worth knowing, not a broken install.

## [0.12.7]

- **Status badges in the README.** The tests badge is live from the Actions workflow; the Python
  row matches `requires-python` and the CI matrix, the licence matches `LICENSE`, and the two
  privacy badges restate invariants the codebase holds. No version badge until a release badge
  would be telling the truth.

## [0.12.6]

- **Fixed: the upgrade tests failed in CI.** They created throwaway git repositories and then
  named the `main` branch by hand, which only works on a machine whose git config sets
  `init.defaultBranch`. `git init` otherwise produces `master`, so the tests passed locally and
  failed on the runner. Each test repository now names its initial branch explicitly, so the
  suite no longer depends on whoever is running it.

## [0.12.5]

- **Fixed: `cobirb --upgrade` left the checkout on a detached `HEAD`.** It checked the tag out
  directly, which is harmless for someone only running CoBirb and a trap for anyone who also
  commits to it — the next commit belongs to no branch, so `git push` silently has nothing to
  send and the work is easy to lose. The current branch is now fast-forwarded onto the tagged
  commit instead, leaving you where you were.
  - Detaching remains the fallback where there is nothing else honest to do: already detached,
    or a branch carrying commits the tag doesn't have. A branch with its own work is never moved.
  - The result says which happened. `Upgraded v0.7.0 → v0.8.0 (v0.8.0). Still on main.` or, when
    detached, what to run to get back on a branch.
  - A clone that has never fetched upgrades fine: `upgrade()` fetches before it resolves
    anything, and fetching a tag brings the commit it points to, so the fast-forward has a local
    ref to move onto. Pinned by a test, since narrowing or reordering that fetch would break it
    silently — the branch would simply stay behind and the checkout detach.
- `AGENTS.md` §15 now states that a behaviour change is not finished until the matching page in
  `docs/` is updated in the same change.

## [0.12.4]

- **Fixed: closing an unresponsive MCP server blocked for as long as that server lived.**
  `StdioClient.close()` closed the pipes before ending the process, and a reader thread sitting
  in `for line in process.stdout` holds that stream's lock — so the close waited for a read that
  was never going to return. Terminating first makes the blocked read hit EOF immediately.
  A 30-second shutdown is now 2.
- **Docs.** `docs/` gains eleven short how-to pages — install, first run, commands, CLI, config,
  permissions, sessions, memory, images, the Flock, plugins & MCP — linked from the README. The
  screenshot moved to `docs/images/`.
- **Tests run in half the time** (207s to 98s, ~5/s to ~10/s), with no coverage removed:
  - Session keys derive at scrypt's interactive cost across the suite. `test_crypto.py` restores
    the shipped cost, since there the KDF is the subject. The cipher, blob format and round trip
    are still exercised for real everywhere.
  - The probe's give-up path is asserted in 1.2s instead of 60 — the fake endpoint stalls for
    longer than the probe waits, which is the whole condition, and needs no more than that.
    `probe_concurrency(timeout=...)` is typed `float` now, since it is a duration.
- **Restored seven `/image` argument-parsing tests** that were removed by accident along with a
  debug test in v0.12.1. The behaviour shipped correctly; its tests did not.
- Comments and docstrings across `src/` and `tests/` now describe what the code does rather than
  what it once did.

## [0.12.3]

`tui/app.py` was 1648 lines and the home of everything the interactive app could do. It is now
1225, and three of the things it was doing have somewhere of their own to live.

- **`tui/slash_commands.py`** — what each `/command` does. Fourteen `_cmd_*` methods made the app
  the home of exporting markdown, toggling plan mode and starting a flock, on top of being the
  Textual application. They were already plain functions of `(app, argument)` — the dispatch table
  held *unbound* methods and called them `handler(self, argument)` — so this changed how they are
  stored, not how they work. The app keeps `_dispatch_command`, which is a routing decision about
  input rather than the behaviour of any one command.
- **`tui/transcript.py`** — `TranscriptView`: everything written to the transcript, and the
  ordering rule they all depend on (flush the streamed reply before writing anything else, or a
  tool-call panel lands above the reasoning that led to it). Takes the persona name and a message's
  attachments as arguments rather than reading them off the app, so it changes when the transcript
  changes and not when the application's fields do.
- **`tui/attachments.py`** — `PendingAttachments`: the images `/image` has queued for the next
  message, with the reading, format sniffing and payload shaping that were spread across two app
  methods. `AttachmentError` carries a sentence, because every failure it can have is one a person
  should read.
- **Fixed: the flock's activity roll-up could name a worker that wasn't there.** `WorkerPane`
  rendered its state without remembering it, so the app kept a parallel `_flock_states` dict to
  build the roll-up from — the same fact in two places, and only one of them noticed when a pane
  was gone. The pane that shows a state now has it, `FlockPane.activity_summary()` reads off the
  panes, and the dict is deleted.

## [0.12.2]

Housekeeping pass: two real bugs, and the catalogue bookkeeping moved out of the app.

- **Fixed: the always-present public catalogue was never created.** `memory.ensure_public_exists`
  had no callers outside the tests, so a fresh install opened `/memories` on an empty list and
  `/remember` offered nowhere to put the fact. Both commands now create it on first use, which is
  what "lazily, on first use" claimed all along.
- **Fixed: a catalogue name was used as a filename unchecked.** `../../escaped` wrote a catalogue
  outside `~/.cobirb/memories` entirely, and a name containing a separator wrote into a
  subdirectory `discover_catalogues` would never list again. A name must now be a name.
- **`CatalogueStore` (`runtime/catalogues.py`), extracted from `CoBirbApp`.** Ten methods of
  catalogue bookkeeping were living among tabs, modals and worker threads without touching a single
  widget. They have one reason to change and it is not "the screen changed", so they now have their
  own home; the app keeps thin forwarders for the modals, and `tui/app.py` loses 145 lines.
- **`MemoryCataloguesModal` and `RememberModal` share a base class.** Reading the catalogues,
  rendering them loaded-first, showing an inline error and the unlock-then-continue dance were the
  same code twice.
- `policy._read_target` is now `_target_path` — it has always served the write tools too, so the
  name claimed a narrower job than it does, in the one module where being exact matters most.
- Removed seven genuinely dead imports; the two that survive are marked as the re-exports they are.

## [0.12.1]

- **`/image <path> <message>` now works.** Typing the path and the question on one line is what
  people reach for, and reading the whole line as a filename failed with
  `No such file or directory: 'docs/cobirb.png What is this image?'` — which blames the file for a
  parsing rule. The first word is the path and the rest is the message, sent with the image in one
  go. A quoted path wins outright, and an unquoted path that *does* name an existing file is still
  taken whole, so paths containing spaces keep working.
- **`/image` resolves a relative path against the session's working directory**, not the directory
  CoBirb happened to be launched from — `--cwd` exists precisely so those can differ.
- **Switching model with `/model` no longer keeps the previous model's context window.**
  `_context_budget` asks the provider once and remembers, which is true within a run and wrong
  across a session: `/model` swaps the provider under it. Switching away from a small-window model
  left every later turn compacting against the old budget — enough on its own to elide an attached
  image, since one is priced at 1500 tokens against a stale 2048-token budget.

## [0.12.0]

- **An attached image now survives a resume.** Attach one, quit, reopen the session, and the model
  can still see it — which is what v0.11.0 was supposed to do and didn't. Its images lived as
  separately-encrypted files in a `<session>.images/` sibling directory, and that could never
  work: neither `SessionManager` nor `Orchestrator` retains a password, so the layer that builds
  the model's context had no way to decrypt them. A resumed session showed a `[image: x.png]`
  marker where the picture had been, and nothing ever read those files back at all.
  - The bytes now live in `Session.images` (`{id: base64}`, deduplicated by content hash) **inside
    the session's own encrypted blob**. Same AES-256-GCM, same password, one mechanism instead of
    two — and already decrypted by the time `load()` returns. `crypto.py`'s `derive_key`/
    `encrypt_bytes`/`decrypt_bytes`, `SessionManager.attach_image`/`read_image`/`images_dir` and
    `Session.image_key_salt` are all deleted; the SPI was never touched, so no plugin breaks.
  - Every image-bearing turn is resolved against that table, not just the newest, so an image stays
    visible for as long as its turn does. Old ones fall back to a `[image: filename]` marker in
    compaction pass 1; recent ones are never elided. Compaction prices an image at a flat 1500
    tokens rather than its base64 length (a 1 MB screenshot would otherwise look like 350k tokens).
  - `fork_session` carries exactly the images the branch's kept turns reference.
- **Fixed: `/image` crashed the turn in the default (no `--session`) configuration.** From the
  second turn onward it raised `'NoneType' object has no attribute 'encrypt'` — the orchestrator
  manufactures its own `SessionManager` after turn one, and that one has no crypto backend. The
  error was swallowed by the TUI's catch-all and the user's message never reached the model.
- **Fixed: `/image` created a stray `..images/` directory in the working tree** in that same
  configuration, writing into the user's repository.
- **Fixed: a multi-line `/remember` silently lost every line after the first.** The prompt box is a
  multi-line editor; continuation lines are now indented in the `.md` and read back correctly.
- **Fixed: memory catalogues existed world-writable (`0666`) for a window** between creation and
  `chmod`. Created with `os.open(..., 0o600)` now, matching `session._write_blob`.

## [0.11.0]

- **Vision: attach an image with `/image <path>`.** No caption argument — nobody types a
  description of their own screenshot; the message typed and sent next is the caption, when there
  is one, the same as attaching a file anywhere else.
  - Persisted reference only (`{id, filename}`); the encrypted bytes live in a sibling
    `<session>.images/` directory, keyed by a session-password-derived key that's derived once and
    reused (`crypto.py`'s new `derive_key`/`encrypt_bytes`/`decrypt_bytes` — duck-typed extras, not
    added to the frozen `SessionCrypto` ABC, so a third-party crypto plugin without them still
    works, just slower).
  - **Only the newest turn's image is ever sent as bytes.** Every earlier image-bearing turn is
    rewritten to a plain `[image: filename]` marker the moment a later turn exists — nothing is
    ever resent, and `context.py`'s compaction needs no changes to know images exist.
  - `supports_vision()` is real now (was a hardcoded `False`): reads `capabilities` off the same
    cached `/api/show` payload the context-window lookup already uses. Most local models can't see
    images at all, so CoBirb asks rather than assuming.
  - `/export` shows a `📎 filename` marker, never the bytes — an export is plaintext by design, and
    an attached image riding along unasked in it isn't the same explicit act.

## [0.10.0]

- **Memory catalogues.** Named lists of facts fed into the system prompt, each its own file under
  `~/.cobirb/memories/`: `<name>.md` (plaintext) or `<name>.md.enc` (password-protected, reusing
  the same AES-256-GCM + scrypt backend sessions already use). A `public.md` catalogue always
  exists; anything else is created explicitly.
  - `/memories` — load, unload, rename, delete, or create a catalogue, from a TUI dialog. Loaded
    catalogues are listed first, separated from the rest.
  - `/remember <fact>` — save a fact into a catalogue you pick on the spot, prompting for a
    password there if the chosen one is locked. **Deliberately a plain slash command, not a model
    tool**: a tool would either be unreachable for a model without tool-calling support, or get
    called on every turn with no memory of already having asked. This fires exactly once, exactly
    when typed, regardless of what the model can do.
  - Loaded catalogues are composed into the system prompt fresh on every turn (never baked into
    the orchestrator at construction time, which is what made the old header panel go stale) and
    are never sent to a Worker Birb — same "nothing but its brief" boundary the Flock already
    enforces for `AGENTS.md` and the repo map.

## [0.9.2]

- **`--cwd` (and its default) now resolves to a real, absolute path** instead of passing the
  literal string `.` around when it wasn't given — which used to show up verbatim in the header
  panel and status bar (`CoBirb · model · .`). Resolved once in `cli.main()` rather than at every
  call site.
- **Removed the interactive app's header panel.** It named the model configured at startup, not
  necessarily the one actually running — if the configured model wasn't available and the startup
  picker changed it, the panel kept naming the original for the rest of the session, because the
  transcript it lived in is append-only and can't be corrected in place. A same-day fix attempt
  (writing a second, corrected panel) produced a visibly duplicated banner instead, which is worse
  than the original bug. Removed rather than patched further: persona/model/plan/cwd/session are
  already all on the live status line, which self-corrects because it isn't an append-only log.

## [0.9.1]

- Removed every named mention of other AI coding tools from docstrings, `AGENTS.md`, `README.md`
  and package metadata (`pyproject.toml`), rephrased to keep the same meaning. Prompted by a local
  model echoing one of those names back unprompted — CoBirb's own description of itself is
  something a model can read (project instructions, package metadata) and repeat, so it
  shouldn't name a product it isn't.
- `cobirb.__version__` (used in the MCP client handshake) had drifted out of sync with
  `pyproject.toml`'s version since v0.8.0; corrected and kept in this release.

## [0.9.0] — "Noah", the default theme

The parrot's own colours, replacing the stock teal/orange/navy scheme app-wide: one accent
(tail red — was two colours in two places), a canvas, structural chrome, and two tiers of text.
Defined once in `plugins.core.render` so the plain one-shot CLI and the full-screen app render
identically, same as everything else that module builds. An error now gets bold text on a
tinted background strip rather than plain coloured text, so it doesn't read as the same accent
used for the active-tab underline everywhere else.

## [0.8.0] — Installable

A stranger can get a working `cobirb` binary onto their `PATH`, and keep it current.

- **Global install via [pipx](https://pipx.pypa.io).** `pipx install --editable .` puts a
  `cobirb` binary on `PATH` with its three dependencies isolated in their own environment — no
  venv to activate. `--editable` keeps it pointed at the git clone, so a source change still
  needs nothing further; see README "Installing" for the plain-venv alternative.
- **`cobirb --upgrade [tag] [--force]`.** Fetches tags, checks out the latest release by default
  or a named one, and reinstalls to refresh metadata. Refuses to move to an older release than
  the one currently running unless `--force` says so, and refuses outright on a checkout with
  uncommitted changes rather than guessing what to do with them. The one CoBirb command that
  talks to a network by default — because it's what was just typed, the same justification
  `cobirb plugin install` already relies on.

## [0.7.0] — Hardened

The last release before the plugin and session-file formats were frozen.

- **Plugin SPI frozen and versioned.** A plugin declares `COBIRB_SPI = 1` (absent means 1, so
  nothing existing breaks); changes within a version are additive-only. A plugin written against
  a newer CoBirb is refused at the loader boundary with a message naming which side to upgrade,
  and one bad plugin no longer stops the others from loading.
- **Session schema migrations.** `migrate()` runs inside `from_dict`, on every read path. A
  session file written by a *newer* CoBirb is declined rather than read optimistically and saved
  back with the wrong content.
- **Security review** (see AGENTS.md §12.3): the encrypted session header now carries its own KDF
  parameters, which let the scrypt cost move from N=2¹⁴ to 2¹⁷ (OWASP's current recommendation)
  without orphaning files already on disk — old sessions still open, at the old cost, from a
  headerless blob. Corrupt session files used to report as "Incorrect padding" or fail inside the
  cipher, both of which read as *wrong password*; they're now distinguished. The plugin installer
  put its directory at the front of `sys.path`, ahead of the standard library; it's now appended.
- **Cold-start README pass.**

## [0.6.0] — Interactive

A running session stops being a one-shot commitment.

- **Plugin distribution.** `cobirb plugin install/list/remove` turns a plugin's source directory
  on disk into something the loader discovers, via a real `pip install -e` round trip. Local
  only — no registry, no fetching.
- **Mid-turn steering.** A message sent while a turn is running redirects it instead of queuing
  behind it, cutting the model off mid-stream where it supports that.
- **Session branching.** `session.fork_session()` forks a saved conversation into a new,
  independent file; the source is only ever read. `--branch` / `--branch-at`, or one click from
  the Sessions tab.

## [0.5.1] — Field notes

Everything the first real Flock run turned up.

- Worker reads opened to the whole project (writes stayed exactly as strict) — reads scoped to
  exact brief files had left workers unable to orient.
- Force-stop for a worker stuck on the model — streaming moved onto a tracked `http.client`
  connection so a hung request can actually be interrupted.
- A 404 no longer misreported as "is Ollama running?" — `HTTPError` is a subclass of `URLError`,
  and the two were being conflated.
- Pre-flight, tag-insensitive model check, to catch the `:latest` trap before a Flock plans
  around a model that will 404.
- Charter dialog colour, the dialog showing the charter it's asking about, multi-line prompt
  input, a scrolling Flock tab, selectable pane text, a starter config on first run, an activity
  line.

## [0.5.0] — The Flock

Multi-agent subtasking. `cobirb flock -p "..."` divides a piece of work between several agents
that cannot see each other.

- **Charter.** A TOML document *Brainy Birb* proposes and you approve: objective, seams,
  per-worker read/write scope, and an acceptance check per worker.
- **Skeleton.** Brainy Birb writes the interfaces and failing tests into the tree before anyone
  fans out — ordinary tool use, no code generation in core.
- **Fan-out.** *Worker Birbs* run under a charter-derived policy, two at a time by default, each
  knowing only its own part of the work.
- **Three-pass review.** Baseline diff, stub reversion, behaviour mutation, cheapest first.
- **Flock tab.** A pane per worker, live status, force-stop.
- **Session pairing.** A GUID pairs the main session with its own flock session file.

See the "The Flock" design record for the full decision history.

## [0.4.0] — Extensible

- **MCP client (stdio).** Tools from a local MCP server become CoBirb tools under the same
  permission layer as everything else — no outbound socket, since stdio servers are local
  subprocesses.
- **Hooks.** Four lifecycle points; a `before_tool` hook can refuse a call before you're even
  asked about it.
- **Custom commands.** Markdown files under a project become slash commands.
- **Per-role model selection.** `models.default/orchestrator/worker`, each inheriting from
  `default` field by field.
- **Documented the shell privilege gap** rather than building a sandbox — an approved shell
  command still runs with your full privileges.
- An embedded GGUF runtime was designed for this release and then deliberately cut; see
  AGENTS.md §12.1.

## [0.3.0] — Grounded

- **`repo_map` tool** — a ranked outline of the codebase (Python symbols via `ast`, regexes
  elsewhere), so the agent can answer "where is X" without grepping blind.
- **`/diff`** — everything the agent changed this session, built on the undo snapshots rather
  than git, so it works in a directory that was never a repository. (Auto-commit deferred; see
  AGENTS.md §12.1.)
- **Self-verification loop** — `verify_command` runs after a turn that changed files; a failure
  goes back to the model for one bounded attempt.
- **Write-scope grants**, mirroring the existing read grant.
- **Secret redaction** — high-confidence formats only (private keys, `AKIA…`, `ghp_…`, `sk-…`),
  nothing matching on a variable name.
- **Session export** — `--export` / `/export` to markdown, on explicit request only.

## [0.2.0] — Trustworthy

- **Context compaction** against the model's real context window (via `/api/show`) — the fix
  for the single most damaging failure mode an agentic tool has: a session that quietly gets
  dumber the longer it runs. Old tool results are elided first, then the oldest turns, then the
  working set is trimmed; a short session takes an unchanged fast path. `/context` shows the
  budget.
- **Project instructions** — `AGENTS.md` (and `CoBirb.md`) read from the working directory into
  the system prompt.
- **`.gitignore` awareness** in `glob` / `grep`.
- **Diff preview before write** — the approval dialog shows the actual diff before it applies,
  not after.
- **Checkpoint & `/undo`** — touched files are snapshotted before each turn.
- **Headless mode** — `--headless`, a policy file, `--output json`, real exit codes.
- **Malformed tool-call repair** — a bad call gets one corrective round trip instead of burning
  a turn.

## [0.1.0] — Initial

Core agent loop, built-in tools, the permission model, a local Ollama provider, and encrypted
sessions (AES-256-GCM, keyed via scrypt).
