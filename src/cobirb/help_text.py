"""The text `cobirb help` prints.

Pure data, kept out of cli.py because ~300 lines of prose between two
functions makes the module it lives in hard to read around."""
from __future__ import annotations


HELP_TEXT = """\
CoBirb — a privacy-first, local agentic CLI.

MODES
  Interactive (default): a full-screen terminal app — tabs, a live status
                         line, a boxed input, tool approval as a dialog.
  One-shot:              cobirb -p "your task" --allow-tool='shell(git)'
                         Plain stdout, so it pipes and scripts like any CLI.
  Headless (CI):         cobirb -p "task" --headless --output json
                         Never prompts; refuses anything not permitted up
                         front. Exit 0 clean, 1 failed, 2 refused something.
  Session:               cobirb -w                 (new encrypted session)
                         cobirb --session <path> -w

PRIVACY BY CONSTRUCTION
  • Zero telemetry. • No outbound network by default. • Sessions encrypted
    at rest (AES-256-GCM, keyed via scrypt). • Nothing is pre-approved: no
    tool reads, writes, or runs anything until you say so. Approving a read
    covers that directory and below; writing and running are asked every
    time unless you allow them yourself. • No local audit log unless you
    opt in ("audit_log" in config — see 'cobirb help config'), since one
    would otherwise be a second, unencrypted copy of what you write and
    run. • Everything stays on this machine.

These are enforced in code — the permission layer and the session crypto —
not by asking the model to behave. CoBirb sends no system prompt of its own
by default, so your model's own SYSTEM directive is what shapes it.

OPTIONS
  --model NAME        Model name (config/COBIRB_MODEL_NAME if omitted); pick
                      one interactively any time with /model.
  --persona NAME      Adopt a persona (default: none — the model's own voice
                      is left alone). Bundled: noah, professional, neighbor,
                      kawaii. Or set "persona" in config, or point at your
                      own <name>.json. Pick one with /persona.
  -w [PASSWORD]       Run in an encrypted session, starting one under
                      ~/.cobirb/sessions if --session names none. '-w' alone
                      prompts without echo; '-w hunter2' is visible in shell
                      history and process listings.
  --session PATH      Resume/continue the encrypted session at PATH.
  --export PATH       Decrypt --session and write it to PATH as markdown,
                      then exit. Plaintext, deliberately.
  --system-prompt     off (default) or harness. Off sends NO system message
                      at all, so the SYSTEM directive your model was built
                      with applies exactly as it does in Ollama. See
                      'cobirb help model'.
  --allow-tool SPEC   Override permission: 'name' or 'name(arg)'. Repeatable.
  --plan-mode on|off  Plan, then act, then validate with references, as 3
                      separate model phases (default: off, or "plan_mode"
                      in config). See 'cobirb help plan'.
  --headless          Never prompt: refuse anything not already permitted
                      by --allow-tool or "allow_tools". For CI. There is
                      deliberately no flag that approves everything.
  --output text|json  json prints one machine-readable object on stdout and
                      nothing else. Exit 0 clean, 1 failed, 2 refused
                      something (the last only with --headless).
  --cwd DIR           Working directory.

INTERACTIVE COMMANDS
  /model             List models available from the configured endpoint
                     and pick one for this session.
  /persona           Pick a persona from a list (including "none", the
                     default, which hands the voice back to the model).
  /persona <name>    Switch to a named persona directly.
  /export [PATH]     Write this session out as readable markdown. The
                     file is plaintext; the session stays encrypted.
  /diff              Everything the agent has changed this session, as one
                     diff. Works without git; shows only its own edits.
  /undo              Put back the files the last turn changed. Cannot
                     undo what a shell command did — see 'help tools'.
  /context           Show how much of the model's context window this
                     session is using, and what has been compacted away.
  /plan on|off       Toggle plan mode mid-conversation.
  /plan              Show whether plan mode is currently on.
  ? or /help         Open this help. '/help <topic>' opens one topic.

INTERACTIVE KEYS
  f1 help · f2 next tab · ctrl+q quit · up/down recall earlier prompts (the
  last 100, in memory only — nothing you type is written to disk).
  ctrl+c copies the transcript selection if you have dragged one out with
  the mouse, and otherwise cancels a running turn (a stuck or slow shell
  command, most usefully — quitting mid-turn tries this first too, so it
  never sits waiting on one either). In a tool-approval dialog: y allow
  once · a allow for the rest of the session · n (or escape) deny.

TOPICS
  Run 'cobirb help <topic>' for more: session, persona, plan, model,
  plugins, tools, config.
"""

HELP_TOPICS: dict[str, str] = {
    "session": """\
SESSION — encrypted, resumable conversations

  cobirb -w                            New session under ~/.cobirb/sessions.
  cobirb -w hunter2                    Same, with the password given inline.
  cobirb --session <path> -w           Resumed/created at <path>.
  cobirb -p "task" -w                  One-shot, saved to a new session.

Asking for a password is asking for a session: -w on its own starts one named
for the current time under ~/.cobirb/sessions, so there is no path to invent
up front. The same password unlocks an existing session or sets one for a
session being created.

'-w' with no value prompts for the password without echoing it. '-w hunter2'
takes it straight from the command line, which is quicker and is also visible
in your shell history and to anyone who can list processes — your call which
trade you want.

Resuming shows you the conversation you are rejoining: interactive mode
replays the saved turns into the transcript, between two dim rules, before
you type anything. The file is unlocked before anything starts, so a wrong
password reports on the terminal and exits non-zero — a session that can't
be decrypted never opens the app at all.

On exit, the command that reopens the session is printed to the terminal, so
the file is never something you have to go hunting for. In interactive mode
the Sessions tab also lists and starts sessions without needing --session at
all — see its own screen for details.

  • Encrypted at rest: AES-256-GCM, keyed via scrypt over the password. The
    plaintext session file never exists on disk.
  • Tamper-evident: each turn carries a content hash, checked on reload.
  • Saved after every turn (interactive) or once at the end (one-shot).
""",
    "persona": """\
PERSONA — how CoBirb speaks

  cobirb --persona <name>       Adopt a persona for this run.
  /persona                      Pick one from a list, "none" included.
  /persona <name>               Switch to a named persona directly.

Personas are OFF by default. A persona is a costume for the model — a name, a
species, a tone, stock phrases — and CoBirb sends it as a system message,
which replaces whatever SYSTEM directive the local model's own Modelfile
sets. Wearing one by default would silently override your model's own
configuration on every turn, so an unconfigured run sends no voice
instructions at all and the model sounds like itself.

Bundled: noah, professional, neighbor, kawaii. Set "persona" in config to
adopt one by default, or point --persona at your own <name>.json (same shape
as the bundled files — see cobirb/personas/*.json). "none" turns it back
off.

Personas are pure data: name, tone, greeting, phrasings, emoji density,
squawks. They shape tone only and can never grant permission to skip
encryption, network, or permission controls — the CLI's system prompt says
so explicitly, and no persona field can override it.
""",
    "plan": """\
PLAN MODE — explicit plan → act → validate

Off by default: the model plans, acts, and checks its own work implicitly
in one pass, which is how CoBirb behaves normally. Turned on, a run becomes
three separate model phases instead, each recorded as its own turn:

  1. plan      One reply, no tools available — a short, numbered plan for
               how the request will be fulfilled. Shown to you immediately.
  2. act       The normal tool-using loop, following that plan.
  3. validate  A bounded follow-up loop (tools available) that checks the
               work — re-reading files, re-running tests/commands — and
               reports, with concrete references, whether and how the
               request was actually fulfilled. Stored as the session's
               "validation" field alongside its usual summary.

Turn on/off:
  cobirb --plan-mode on|off     For this run (overrides config below).
  /plan on|off                  Mid-conversation, interactively.
  /plan                         Show whether it's currently on.
  "plan_mode": true             In config, as the default when neither
                                 --plan-mode nor /plan has been used yet.

Plan mode costs at least one extra model call per turn (the plan), and up
to a few more (the validate phase, bounded like the act phase); expect
slower turns in exchange for the explicit checkpoints.
""",
    "model": """\
MODEL — choosing what CoBirb talks to

CoBirb ships no model of its own; it talks to whatever OpenAI-compatible
endpoint you point it at (a local Ollama server by default). No model is
selected until you name one:

  --model NAME          For this run.
  "default_model"       In config, as the default for every run — tried at
                         interactive startup and silently ignored (not an
                         error) if that model can't be found there.
  /model                 Interactively: fetches the list of models the
                         configured endpoint currently has and lets you
                         pick one, for this session only.

If interactive mode starts with no working model — nothing configured, or
"default_model" named one that isn't there — it fetches the list itself and
opens the same picker /model would, so you're never left staring at a
session with nothing to talk to.

YOUR MODEL'S OWN SYSTEM PROMPT

Ollama accepts one system message per request, and sending one REPLACES the
SYSTEM directive the model was built with. A model you made with
'ollama create' around a custom SYSTEM is a configuration you chose, so
CoBirb does not overwrite it:

  • By default CoBirb sends no system message at all. Your model behaves
    inside CoBirb exactly as it does in 'ollama run' — same SYSTEM, same
    voice, same everything.
  • When CoBirb does have something to add (a persona, plan-mode phase
    instructions, or --system-prompt harness), it reads your model's own
    SYSTEM back via /api/show and puts it FIRST, then appends its own part.
    Yours is supplemented, never discarded.

  --system-prompt off       The default. Nothing of CoBirb's is sent.
  --system-prompt harness   Add a short description of the tool-permission
                            model. Worth trying if a model keeps retrying a
                            tool call you denied; it has no other effect.
  "system_prompt"           The same choice in config.

Nothing about CoBirb's actual guarantees depends on any of this: permissions
are enforced in policy.py and sessions are encrypted by the crypto backend,
not by asking a model to cooperate.
""",
    "plugins": """\
PLUGINS — bolt-on capabilities

Discovered from installed entry points and local cobirb/plugins/<name>/
directories each time an orchestrator is built. Never fatal: a broken
plugin, or one whose declared name collides with an existing tool, is
reported and skipped. In interactive mode, the Plugins tab shows exactly
what was discovered, what's active, and any such problems live.

  • Tool plugins are additive: every discovered one is registered
    alongside the built-ins (the permission policy still gates whether it
    can actually run).
  • Model/I/O/crypto plugins are singleton slots: a discovered one replaces
    the matching core default only when explicitly selected in config —
    "plugins": {"model": "<name>", "io": "<name>", "crypto": "<name>"}.
""",
    "tools": """\
TOOLS — what the agent can do

  read_file      Read a file's contents.
  write_file     Create/overwrite a file.
  edit_file      Replace an exact old_str with new_str in a file.
  apply_patch    Apply a unified-diff patch to a file.
  glob           Find files matching a glob pattern.
  grep           Search file contents by regex.
  list_dir       List a directory's contents.
  repo_map       Outline the codebase: which files matter and what is
                 defined in them, most-referenced first.
  shell          Run a shell command. Highest privilege; gated.

Before a tool changes a file, CoBirb copies the current version aside, so
/undo can put it back. This covers write_file, edit_file and apply_patch —
not shell, which cannot say in advance what it will touch.

Nothing is permitted up front. Every tool call you haven't already allowed
prompts you to permit it once, always, or not at all — and what "always"
covers depends on the tool:

  read_file, list_dir,   The directory the call names, and everything under
  glob, grep             it. Say yes once for a project and CoBirb can read
                         it without asking again.
  write_file, edit_file, The directory the file is in, and everything under
  apply_patch            it — a separate grant from the read one, so
                         allowing reading never allows rewriting.
  shell                  Exactly the invocation you approved — 'git' if you
                         approved a bare binary, 'python -m pytest' if you
                         approved that. Never more.

To skip the prompts for things you always want, list them yourself with
--allow-tool='name' or --allow-tool='name(arg)' (e.g. 'shell(git)'), repeat
the flag, or set "allow_tools" in config. A command CoBirb cannot fully
read — command substitution, a subshell, or find's -exec — is refused
rather than guessed at. Third-party tool plugins extend this list and are
gated identically — see 'cobirb help plugins'.
""",
    "config": """\
CONFIG — user + repo scoped settings

Read from (repo overrides user): ~/.cobirb/config.json, then ./cobirb.json
(relative to --cwd). See cobirb.json.example for a starting point. Keys:

  "default_model"                Model to use by default (or --model/
                                 COBIRB_MODEL_NAME). Validated at
                                 interactive startup — see 'cobirb help
                                 model'.
  "model"                        Older, equivalent name for the same thing.
  "models": {"default": {
      "name": "...", "base_url": "..."}}  Model name/endpoint (local Ollama
                                 by default; any OpenAI-compatible server
                                 works).
  "checkpoints"                  false to stop snapshotting files before
                                 the agent changes them, which is what
                                 /undo restores from. On by default.
  "instructions"                 false to stop reading the project's
                                 AGENTS.md / CoBirb.md from the working
                                 directory into the system prompt. On by
                                 default: a file in your own repo saying how
                                 you want an agent to behave is a clear
                                 enough signal not to need asking twice.
  "instructions_max_chars"       How much of it to send (default 2000).
                                 It rides on every request, so a long guide
                                 crowds out the conversation on a small
                                 window.
  "context_tokens"               How many tokens your endpoint actually
                                 serves. Ollama uses its own num_ctx default
                                 (4096) unless the Modelfile says otherwise,
                                 no matter how large a window the model
                                 advertises — so CoBirb only trusts num_ctx,
                                 and assumes 4096 when it can't find one.
                                 Raise this if you start Ollama with more;
                                 /context shows what is in use.
  "allow_read_dirs"              Directories CoBirb may read without asking,
                                 as a list. Covers subdirectories.
  "allow_write_dirs"             Directories CoBirb may change files in
                                 without asking. Separate from the read
                                 list on purpose.
  "allow_tools"                  Permission rules you always want, as a list
                                 in --allow-tool's syntax, e.g.
                                 ["read_file", "shell(git)",
                                  "shell(python -m pytest)"].
                                 Nothing is permitted without a rule here or
                                 an approval at the prompt.
  "persona"                      Persona to adopt by default (or --persona).
                                 Unset means none: the model keeps its voice.
  "system_prompt"                "off" (default) or "harness" — whether
                                 CoBirb sends a system prompt of its own.
                                 See 'cobirb help model'.
  "plugins": {"model"|"io"|"crypto": "<name>"}
                                 Select a discovered plugin for that slot
                                 (see 'cobirb help plugins'); the core
                                 default is kept when unset.
  "plan_mode"                    Start with plan mode on (default: false).
                                 See 'cobirb help plan'.
  "audit_log"                    Keep a local record of every tool call —
                                 name, arguments, cwd, timestamp — at
                                 ~/.cobirb/audit.jsonl (default: false).
                                 Off by default because the arguments
                                 logged are whatever a tool call actually
                                 carried, unredacted: write_file's full
                                 content, edit_file's full old/new text,
                                 apply_patch's full diff, shell's full
                                 command. Unlike sessions, this log is
                                 plain text, not encrypted — only turn it
                                 on if you want that trail and understand
                                 what ends up in it.

Nothing here ever defaults to a networked provider — model/provider
settings are opt-in, matching CoBirb's no-network-by-default rule.
""",
}
