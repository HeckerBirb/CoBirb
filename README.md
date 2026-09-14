# CoBirb 🦜

A privacy-first, Copilot-like agentic CLI. It lives in your terminal, runs against your own
local models, and stays out of their way.

> **Privacy is not a feature. It's the foundation.**

CoBirb behaves like a coding agent, then subtracts every default network and telemetry behavior
and adds a hard boundary around the rest. **No data leaves the CPU process unless you explicitly
opt a capability in.**

- 🔒 No telemetry. No analytics. No pings.
- 🔒 No outbound network by default. Models are yours to configure.
- 🔒 Sessions are encrypted at rest (AES-256-GCM, keyed via scrypt).
- 🔒 Credentials are stripped from tool output before the model, the session or the
  audit log ever see them — private keys, `AKIA…`, `ghp_…`, `sk-…` and friends.
- 🔒 Nothing is pre-approved. No tool reads, writes, or runs anything until you say so.
  Approving a read covers that directory and below; writing and running ask every time,
  unless you allow them yourself in config.
- 🔒 Your model's own `SYSTEM` prompt is left alone. CoBirb sends no system message by
  default, so a model you built with `ollama create` behaves inside CoBirb exactly as it
  does in `ollama run`. When CoBirb does add something, yours goes first.
- 🦜 Optional personas — various twists on the replies. Off by default.

## Status

🚧 **v0.7.0.** The core loop, built-in tools, the permission model,
encrypted sessions and a local Ollama provider — plus the things that make it usable on real
work: context compaction so long sessions don't degrade, project instructions, `.gitignore`
awareness, a `repo_map` tool so it can find its way around, a diff shown before any write,
`/undo` and `/diff`, credential redaction, an opt-in "run my tests after you change something"
loop, and a headless mode for CI.

**0.7 is the hardening release** — the last one before 1.0. The plugin SPI is now **frozen and
versioned**: within a version changes are additive only, a plugin declares what it was written
against with `COBIRB_SPI = 1`, and one that needs a newer CoBirb is refused with a message saying
so instead of failing later. Session files carry a schema version with a migration path, and one
written by a *newer* CoBirb is declined rather than misread and saved back wrong. Session
encryption now records its own KDF parameters, which is what made it possible to raise the scrypt
cost to OWASP's current recommendation without orphaning the files already on disk — old sessions
still open. See §12.3 of [`AGENTS.md`](./AGENTS.md) for the full security review.

**New in 0.6 — a running session stops being a one-shot commitment.** Type while the model is
answering and it **redirects the turn in progress** rather than queuing behind it — mid-stream,
where the model supports being cut off. **Branch a conversation** into a new file to try a
different direction (`--branch`, or the Sessions tab) without disturbing the original. And
`cobirb plugin install ./my-plugin` makes a plugin on your disk something CoBirb actually
discovers — local only, no registry, nothing fetched.

**New in 0.5 — the Flock.** `cobirb flock -p "add CSV export"` divides a piece of work between
several agents that cannot see each other. One *Brainy Birb* plans it, designs the interfaces and
writes the skeleton — typed stubs, semantic docstrings, failing tests — then hands one ticket to
each *Worker Birb*. A worker knows only its own part: not what the feature is, not how many others
there are, not what they are building. It works because the skeleton is the communication channel,
so nobody has to coordinate. You approve the charter once, see every worker's exact scope before
anything runs, and the work is reviewed against the skeleton afterwards. See `cobirb help flock`.

In 0.4: **one model per role** (`cobirb models`), **hooks** that can refuse a tool call before you
are even asked about it, **custom commands** — a prompt you wrote down, invoked by name — and an
**MCP client** over stdio, so tools from a local server become CoBirb tools under the same
permission layer as everything else. See [`AGENTS.md`](./AGENTS.md) for the architecture, the
plugin SPI, and the reasoning behind all of it.

## Quick start

Starting from nothing, on a machine with [Ollama](https://ollama.com) installed:

```bash
ollama pull qwen2.5-coder:14b     # any local model works; this one is a reasonable default
                                  # for coding work. See `cobirb help model` for choosing by
                                  # VRAM budget, and for the num_ctx trap worth knowing about.
pip install -e .                  # or ".[dev]" if you intend to run the test suite

# Interactive: the full-screen app.
COBIRB_MODEL_NAME="qwen2.5-coder:14b" cobirb
```

⚠️ **Name the tag.** A bare `qwen2.5-coder` means `qwen2.5-coder:latest` to Ollama, which is a
*different model* from `qwen2.5-coder:14b` and may not be pulled at all. CoBirb checks before a
Flock run and tells you which tags the endpoint actually has; elsewhere you'll get a 404 with the
server's own explanation quoted.

Everything else:

```bash
# Interactive, with the model set in config instead of the environment.
cobirb

# One-shot: plain stdout, pipes and scripts like any CLI.
COBIRB_MODEL_NAME="llama3.1" cobirb -p "list the files in this directory" --allow-tool=list_dir

# Unattended, for CI: never prompts, refuses anything not permitted up front.
# Exit 0 clean, 1 failed, 2 completed but something was refused.
cobirb -p "check the tests pass" --headless --output json \
  --allow-tool='shell(python -m pytest)'

# In an encrypted session: -w starts one under ~/.cobirb/sessions and the
# command to resume it is printed when you exit. Give the password inline
# (visible in shell history) or leave it off to be prompted without echo.
cobirb -w hunter2
cobirb -w
cobirb --session ~/.cobirb/sessions/session-20260912-185817.json -w

# Read one back as markdown. Plaintext, deliberately — that's what sharing is.
cobirb --session ~/.cobirb/sessions/session-20260912-185817.json -w --export out.md

# Branch a conversation to try a different direction. The original is untouched
# and still resumable; --branch-at N forks from turn N instead of the end.
cobirb --session ~/.cobirb/sessions/session-20260912-185817.json -w \
  --branch ~/.cobirb/sessions/other-approach.json

# Plugins you have on disk. Local only — nothing is fetched, there is no registry.
cobirb plugin install ./my-plugin
cobirb plugin list
cobirb plugin remove my-plugin
```

Run the test suite with `pytest`.

Three runtime dependencies, all local-only: `rich` (rendering), `cryptography` (session
encryption), and `textual` (the interactive app — imported lazily, so one-shot mode never
loads it).

## Interactive mode

Running `cobirb` with no `-p` opens a full-screen terminal app, with three tabs:

- **Current** — the conversation. A **live status line** shows persona · model · plan mode ·
  working directory, kept current as you change it. A **boxed input** that stays open while the
  turn runs: send another message and it **steers the turn already in flight** rather than queuing
  behind it — the model is cut off mid-sentence where it supports that, keeps what it had already
  said, and takes your correction into account immediately. Steering messages are marked `»` in
  the transcript so they read as the interjection they were. There's no "continue? [y/N]" to
  answer either; you just keep typing. The **transcript** reads as one column: your prompt and the model's reply
  each carry a `>` marker, in different colours, with the reply still rendered as markdown.
  Tool calls, errors and plan/validation phases stay in panels — they aren't conversation.
  Drag to select any of it and `ctrl+c` copies it. **Tool approval is a dialog** — `y` allow once, `a` allow for the rest of the session,
  `n` or escape to deny. The dialog says what "always" would actually grant — for a read
  that is a whole directory tree, so it names it rather than letting you agree blind.
- **Sessions** — lists encrypted session files found in `~/.cobirb/sessions/` (or wherever
  `COBIRB_HOME` points), lets you resume one (prompts for its password) or start a new one
  (prompts for a name and password), all without needing `--session` on the command line.
  Resuming replays the saved conversation into the transcript and drops you back on the
  Current tab, so you rejoin a thread you can actually read. `cobirb --session <path> -w`
  does the same at startup — and unlocks the file *before* the app starts, so a wrong
  password prints on the terminal and exits non-zero rather than opening an empty session.
- **Plugins** — a live view of every registered tool, which model/I/O/crypto implementation is
  active for each slot, and any plugin discovery problems — the same information a broken
  `plugins.*` config selection would otherwise only report to a stderr the full-screen app hides.

In the input: `/model` lists the models the configured endpoint currently has and lets you pick
one for this session; `/persona` opens the same kind of picker for personas —
including `none`, the default — and `/persona <name>` switches directly; `/diff` shows everything the agent has changed this session and `/undo` puts the last turn back;
`/context` shows how much of the model's window this session is using;
`/plan on|off` toggles plan mode (`/plan` alone reports it); `?` or `/help` opens the help screen
(`/help <topic>` for one topic). Keys: `f1` help, `f2` next tab, `ctrl+q` quit, `up`/`down`
recall earlier prompts (the last 100, in memory only), `ctrl+c` copies the transcript
selection if you've dragged one out with the mouse and otherwise cancels a running turn —
most useful against a stuck or slow `shell` command; quitting mid-turn tries this first too,
so it's never stuck waiting on one either.

If no model is configured, or the configured one isn't actually available, interactive mode
fetches the endpoint's model list itself and opens the same picker `/model` would — set
`"default_model"` in config to skip that when it resolves to a real model, and don't worry about
it when it doesn't: an unavailable `default_model` is ignored, not an error.

## Your model's own system prompt

Ollama takes one system message per request, and sending one **replaces** the `SYSTEM`
directive the model was built with. A model you created with `ollama create` around a custom
`SYSTEM` is a configuration you chose deliberately, so CoBirb doesn't overwrite it:

- **By default CoBirb sends no system message at all.** Same model, same `SYSTEM`, same
  behaviour as `ollama run`.
- When CoBirb *does* have something to add — a persona, plan-mode phase instructions, or
  `--system-prompt harness` — it reads your model's own prompt back via `/api/show` and places
  it **first**, then appends its own part. Yours is supplemented, never discarded.

```bash
cobirb --system-prompt off      # the default: nothing of CoBirb's is sent
cobirb --system-prompt harness  # adds a short note about the tool-permission model,
                                # which stops some models retrying a denied tool call
```

Or set `"system_prompt"` in config. None of CoBirb's actual guarantees depend on this —
permissions are enforced in `policy.py` and sessions are encrypted by the crypto backend, not
by asking a model to cooperate.

## What CoBirb does not protect you from

Being straight about the edges, since the rest of this page makes strong claims:

- **Your model server is a separate program you didn't write.** CoBirb's guarantees end at
  the socket. Ollama binds an *unauthenticated* local port — any process running as you can
  read what you send it or issue its own requests — fetches weights from a registry, and
  makes outbound requests on its own schedule. None of that is malicious; all of it is trust
  rather than enforcement. `llama-server`, LM Studio and vLLM have the same shape. **CoBirb will
  not close this itself** — it is a client of an OpenAI-compatible endpoint and deliberately does
  not run models (an embedded GGUF runtime was designed and dropped; see §12.1 of
  [`AGENTS.md`](./AGENTS.md)). Where inference happens is yours to choose, including an endpoint
  you wrote. If the endpoint's trustworthiness matters to you, that is a property to fix in the
  endpoint, and it is fixable — `llama-server` will bind a Unix socket instead of a port
  (`--host /path/to.sock`), which removes the port any local process can reach.
- **An approved shell command runs with your full user privileges.** CoBirb decides
  *whether* a command runs, not what it can reach once it does. Approve `npm test` and
  that command can read `~/.ssh` and open a socket like any other program you'd run.
  There is no sandbox. Approve narrowly, and prefer `shell(git status)`-style rules over
  trusting a bare binary.
- **`/undo` does not cover what a shell command did.** CoBirb copies a file aside before
  `write_file`, `edit_file` or `apply_patch` changes it, so `/undo` puts those back. A
  shell command cannot say in advance what it will touch, so anything it does is outside
  that. Keep your work in git as well.
- **Context is finite.** Long sessions are compacted to fit the model's window (see
  `/context`); old tool results are summarised away first, and a large file is read a
  range at a time rather than whole. Nothing is lost from your session file, only from
  what the model is shown at once.
- **Installing a plugin runs that plugin's code.** `cobirb plugin install` hands the directory to
  `pip`, and pip executes the package's own build backend — so the install is arbitrary code
  execution at your privileges, before CoBirb has looked at a single class and before any
  permission prompt exists to ask you about it. No prompt can cover this; it is what installing any
  Python package means. A plugin is code you chose to run, and choosing it *is* the security
  decision. Afterwards its tools are gated like any others; the install itself is not.
- **An MCP server you configure is a program you chose to run.** CoBirb's "no telemetry, no
  outbound network" promises are about CoBirb. A configured server can open its own network
  connections and send the arguments of every call it receives — file paths, code, queries —
  anywhere it likes, and nothing in CoBirb detects or prevents that. Two defaults reduce the
  blast radius: a server does **not** inherit your environment (no cloud credentials, no API
  tokens for unrelated services), and its tools are pre-approved by nothing. Read
  `cobirb help mcp` before adding one; it also has a worked example of writing your own
  offline server, which is the case worth building for.

## Configuration

Copy [`config.json.example`](./config.json.example) to `~/.cobirb/config.json` and edit it —
nothing in it is loaded until you do, and nothing defaults to a networked provider. See
`cobirb help config` for what each key does.

**That is the only config file CoBirb reads.** It does not read a `cobirb.json` from the
directory you are working in, does not merge one over yours, and does not look for one — so a
repository cannot pre-approve a tool, install a hook, or start a server. Configuration here is
not preference: it decides what runs without asking, and a project able to contribute to it
would mean cloning a project is enough to influence the permission model.

A project can still *describe itself*: `AGENTS.md`, the repo map, and prompt files in
`<project>/.cobirb/commands/` are all read. Those are content for the model rather than
capability granted to it, and every tool call they lead to still goes through the permission
layer.

## Documentation

- [AGENTS.md](./AGENTS.md) — the single source of truth: architecture, the plugin SPI,
  the security design, and the working conventions.

## License

MIT — see [LICENSE](./LICENSE).
