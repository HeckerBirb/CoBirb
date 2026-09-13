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

🚧 **v0.3.0, with v0.4.0 landing.** The core loop, built-in tools, the permission model,
encrypted sessions and a local Ollama provider — plus the things that make it usable on real
work: context compaction so long sessions don't degrade, project instructions, `.gitignore`
awareness, a `repo_map` tool so it can find its way around, a diff shown before any write,
`/undo` and `/diff`, credential redaction, an opt-in "run my tests after you change something"
loop, and a headless mode for CI.

Newly in: **one model per role** (`cobirb models`), **hooks** that can refuse a tool call before
you are even asked about it, **custom commands** — a prompt you wrote down, invoked by name — and
an **MCP client** over stdio, so tools from a local server become CoBirb tools under the same
permission layer as everything else. See [`AGENTS.md`](./AGENTS.md) for the architecture, the
plugin SPI, and the reasoning behind all of it.

## Quick start

Requires a local [Ollama](https://ollama.com) server with a model pulled.

```bash
pip install -e ".[dev]"

# Interactive: the full-screen app.
COBIRB_MODEL_NAME="llama3.1" cobirb

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
```

Run the test suite with `pytest`.

Three runtime dependencies, all local-only: `rich` (rendering), `cryptography` (session
encryption), and `textual` (the interactive app — imported lazily, so one-shot mode never
loads it).

## Interactive mode

Running `cobirb` with no `-p` opens a full-screen terminal app, with three tabs:

- **Current** — the conversation. A **live status line** shows persona · model · plan mode ·
  working directory, kept current as you change it. A **boxed input**: submit and it greys out
  while the turn runs, then comes back — there's no "continue? [y/N]" to answer, you just keep
  typing. The **transcript** reads as one column: your prompt and the model's reply
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
  rather than enforcement. `llama-server`, LM Studio and vLLM have the same shape. The fix is
  not a better server but no server at all: an embedded GGUF runtime is planned for v0.4.0.
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
