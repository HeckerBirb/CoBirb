"""The text `cobirb help` prints.

Pure data, kept out of cli.py because ~300 lines of prose between two
functions makes the module it lives in hard to read around."""
from __future__ import annotations


HELP_TEXT = """\
CoBirb — a privacy-first, local agentic CLI.

MODES
  Interactive (default): a full-screen terminal app — tabs, a live status
                         line, a boxed input, tool approval as a dialog, and
                         a Flock tab with a pane per agent.
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
  /commands          List your own prompt files, invocable by name. Any
                     other /word is one of them — see 'help commands'.
  /flock <what>      Divide a piece of work between several agents. Opens
                     the Flock tab; you approve the charter before anything
                     runs. See 'help flock'.
  ? or /help         Open this help. '/help <topic>' opens one topic.

INTERACTIVE KEYS
  f1 help · f2 next tab · ctrl+q quit · up/down recall earlier prompts (the
  last 100, in memory only — nothing you type is written to disk).
  ctrl+c copies the transcript selection if you have dragged one out with
  the mouse, and otherwise cancels a running turn (a stuck or slow shell
  command, most usefully — quitting mid-turn tries this first too, so it
  never sits waiting on one either). In a tool-approval dialog: y allow
  once · a allow for the rest of the session · n (or escape) deny.

EXTENDING IT
  Roles          One model per job: orchestrator, worker. 'cobirb models'
                 shows how yours resolve. See 'help model'.
  Commands       A prompt in ~/.cobirb/commands/<name>.md becomes /<name>.
                 'cobirb commands' lists them. See 'help commands'.
  Hooks          Your own command at a decision point — a before_tool hook
                 can refuse a call outright. See 'help hooks'.
  MCP            Tools from a local server over stdio, subject to the same
                 permission layer as everything else. A server you add can
                 make its own network calls: READ 'help mcp' first.
  The Flock      cobirb flock -p "..." divides a piece of work between
                 several agents that cannot see each other. See 'help flock'.

  All of it is configured in ~/.cobirb/config.json. CoBirb reads no config
  from the directory you are working in — a repository cannot pre-approve a
  tool, install a hook, or start a server. See 'cobirb help config'.

TOPICS
  Run 'cobirb help <topic>' for more: session, persona, plan, model,
  commands, hooks, mcp, flock, plugins, tools, config.
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

ONE MODEL PER ROLE

A role is a job, not a name. Configure them separately when the jobs differ:

  {
    "models": {
      "default":      {"name": "qwen2.5-coder:32b",
                       "base_url": "http://gpu-box:11434"},
      "orchestrator": {"name": "qwen2.5-coder:32b"},
      "worker":       {"name": "qwen2.5-coder:7b",
                       "base_url": "http://localhost:11434"}
    }
  }

  default        Falls back to for everything else. The three older keys —
                 "model", "default_model", models.default.name — all still
                 name this one.
  orchestrator   The agent you talk to: holds the objective, decides what
                 happens next. This is what runs today.
  worker         A subagent given one bounded piece of work. Reserved for
                 the flock (v0.5.0) — nothing calls it yet. It resolves now
                 so the config can be written and checked before then.

Every role inherits field by field, so a role may name a model without
repeating the endpoint it is served from. --model outranks all of them.

  cobirb models    Print how each role resolves and where each answer came
                   from. A role you typo'd is listed too, so it is visible
                   rather than a silent fallback to the default.
""",
    "flock": """\
THE FLOCK — dividing work between agents that cannot see each other

  cobirb flock -p "add CSV export to the reporting tool"     (one-shot)
  /flock add CSV export to the reporting tool                (interactive)

Interactively this opens the Flock tab: one pane per Worker Birb, showing
what each is allowed to touch and what it is doing, updating as they work.
Ctrl+C asks before interrupting — no further workers start, and any already
talking to a model finish that turn, because a model call in flight cannot be
cut off. Whatever has been written to your files stays written; use git.

One Brainy Birb (the lead) plans the work, designs the interfaces, builds the
skeleton, and writes one ticket per Worker Birb. The workers then fill those
tickets in — each knowing only its own part, with no idea what the feature is,
how many others there are, or what they are building.

That works because the skeleton IS the communication channel. Everything the
workers would have had to agree on is already written down in the one place
all of them can see, so they never coordinate. Two engineers whose work meets
in the middle do not need to talk if their lead designed the interface they
meet at.

HOW A RUN GOES

  1. Brainy Birb plans, designs the seams, and writes the skeleton into your
     project: interfaces, typed stubs, docstrings that state semantics, and
     unit tests that fail.
  2. CoBirb checks the partition is disjoint — no two workers writing one file.
  3. YOU APPROVE THE CHARTER. The only decision you make. You see the plan,
     every seam, and every worker's exact read and write scope before anything
     runs. Approving it is what lets the workers run unattended, which is why
     there are no tool prompts afterwards.
  4. The workers fan out, two at a time by default.
  5. Brainy Birb reviews the result and reports. A second round is a new
     decision you make with that in front of you.

WHAT A WORKER CAN TOUCH

Its scope is a Policy, not a request. A Worker Birb cannot open, list or even
discover a file outside its ticket — list_dir, glob and grep all resolve to a
directory it was not granted, so it cannot enumerate its surroundings. It gets
no AGENTS.md and no repo map either: conventions reach it through the skeleton
it is filling in, which was already written in your project's style.

It CAN edit its own tests, and should add more for whatever it finds. That is
not a gap — see below.

TRUST, THEN VERIFY

Locking a worker out of its test file would ship under-tested code to prevent
a cheat that review catches anyway. Because Brainy Birb wrote the skeleton and
still has it, there is a baseline to compare against and a stub to put back:

  • The diff is read for assertions that vanished, tests turned off, and
    declarations that changed — the last being the one action that breaks
    colleagues a worker cannot see.
  • The implementation is put back to its stub and the worker's tests are run
    again. They MUST fail. A suite that passes against an unimplemented
    function is testing nothing.
  • Each behaviour a docstring claims is mutated in turn, and each must be
    caught. A surviving mutant is a promise nothing is holding — usually
    Brainy Birb's omission rather than the worker's.

WHEN A WORKER GETS STUCK

It never halts and never improvises. It implements what the contract allows,
leaves the rest visibly unfinished, and writes up what it could not do, where
it breaks, and either a proposed design change or a question. Changing the
contract itself is the one thing it must not do — that is what silently breaks
four colleagues it cannot see.

WHAT THIS CANNOT DO

  • Fan out work that will not partition into disjoint files. "This is a
    single person's job" is a correct answer and Brainy Birb will say it.
  • Survive a weak skeleton. An under-specified contract produces confident,
    plausible, incompatible code — and every worker's tests pass.
  • Refactor across the partition, or let workers catch each other's bugs.

Flock mode cannot run with --headless. The charter approval is the only place
you see what the workers will be allowed to touch, and a flock that approved
its own charter would be an agent granting itself permissions.

Undo is git's job here: a flock run touches many files across several agents,
and CoBirb does not try to own reverting that. Each engagement gets its own
session file beside the main one, paired by a token, so you can see where the
conversation branched and what the workers actually did.
""",
    "hooks": """\
HOOKS — your own commands at CoBirb's decision points

The permission layer answers "may this run?" by asking you. That works until
the answer is a rule rather than a judgement: never touch anything under
infra/, always run the formatter after an edit, tell me when a turn finishes.
Asking a human to re-enact a policy twenty times a session is how people end
up approving things unread.

Hooks live in ~/.cobirb/config.json, like everything else — CoBirb reads no
config from a working directory, so a repository cannot install one. That
costs per-project hooks, deliberately: a hook runs with no approval prompt in
the way, and a repository able to define one would make cloning it enough to
run its author's code.

  {
    "hooks": {
      "before_tool": [
        {"match": "write_file", "command": "~/.cobirb/hooks/guard-infra.sh"}
      ],
      "after_turn": ["notify-send 'CoBirb finished'"]
    }
  }

EVENTS
  before_tool   Before a tool call — before you are even asked to approve
                it. The only event that can change what happens.
  after_tool    After a call has run. Observation only.
  before_turn   Before the model is given the prompt.
  after_turn    After the turn is finished, including any verify-and-fix
                pass, so the workspace is in its final state.

THE CONTRACT
  The event arrives on stdin as one JSON object:

    {"event": "before_tool", "tool": "write_file",
     "arguments": {"path": "infra/main.tf", "content": "..."},
     "cwd": "/home/you/project"}

  Exit 0 means proceed. A non-zero exit from a before_tool hook BLOCKS the
  call, and whatever the hook printed becomes the reason the MODEL is given
  — so say something it can act on ("infra/ is generated; edit the module
  instead") rather than a flat refusal it will simply retry. On the other
  three events a non-zero exit is reported to you and nothing else.

  A hook that cannot be run, or that times out ("timeout", default 30s), is
  treated as a refusal. Failing open would mean a guard stops guarding at
  exactly the moment it breaks.

  "match" is a glob against the tool name ("write_*", "mcp__db__*"), and
  applies to the two tool events. Omit it to match everything.

A before_tool hook cannot APPROVE anything: it can only refuse. Everything
it lets past still goes to the permission layer, which still asks you.
""",
    "commands": """\
COMMANDS — a prompt you have written down, invoked by name

Everyone ends up with a handful of prompts they retype. Retyping them means
they drift, and the good version of a prompt is usually the fifth one.

Put one in a markdown file and its filename becomes the command:

  ~/.cobirb/commands/review.md            available everywhere
  <project>/.cobirb/commands/review.md    available in that project

  /review src/parser.py       Interactively.
  cobirb -p "/review src/parser.py"       One-shot; the same expansion.
  /commands  ·  cobirb commands           List what is available here.

ARGUMENTS
  $ARGUMENTS   Everything that followed the command.
  $1 … $9      Individual words, so a path and a flag can go in different
               places. A placeholder with nothing to fill it is empty, not
               an error.
  If the body has no placeholder at all, what you typed is appended — never
  silently dropped.

DESCRIPTION
  An optional frontmatter block, for the listing. The block itself is not
  sent to the model:

    ---
    description: Review a diff the way this team reviews diffs
    ---
    Read the staged diff and check it against $ARGUMENTS.

A project command is DATA, not code: it expands to a prompt and nothing
else, which is why these are read from a project directory when hooks and
MCP servers are not. Every tool call it leads to still goes through the
permission layer, and a built-in command always wins a name collision — a
custom /undo cannot quietly change what /undo does.
""",
    "mcp": """\
MCP — tools that live in someone else's process

The Model Context Protocol is how CoBirb gains tools it knows nothing about:
your issue tracker, your database, your house style guide. Each one becomes
a normal CoBirb tool named mcp__<server>__<tool>, and goes through the same
default-deny permission layer, the same approval prompt, the same redaction
pass and the same audit log as read_file does.

  Only stdio transport is supported, and deliberately so. An MCP server over
  stdio is a subprocess on this machine talking over a pipe. HTTP and SSE
  transports point at a URL, and a URL is a network call CoBirb did not make
  and cannot see inside.

READ THIS BEFORE ADDING ONE

  An MCP server is a program you are choosing to run and to show your work
  to. CoBirb's promises — no telemetry, no outbound network — are about
  CoBirb. They do not extend to a server you configure. A server CAN:

    • open its own network connections, to anywhere, at any time;
    • send the arguments of every call it receives — file paths, code
      snippets, queries, whatever the model passed it — anywhere it likes;
    • report usage, phone home, or update itself, and never mention it.

  Nothing in CoBirb can prevent this, and nothing in CoBirb tries to detect
  it. Adding a server is a decision to trust its author with the part of
  your work that touches it. Two things reduce the blast radius, both on by
  default:

    • THE ENVIRONMENT IS NOT INHERITED. A server gets PATH, HOME, LANG and
      whatever you list under "env" — not your cloud credentials, not your
      API tokens for services CoBirb has nothing to do with. Set
      "inherit_env": true only for a server you have decided you trust.
    • NOTHING IS PRE-APPROVED. MCP tools are not in the read-tool set, so
      the directory-scoped read grant does not cover them. Each one is
      asked about, and "always" grants that one tool for that session.

  Prefer servers you can read. A server that runs entirely offline — a proxy
  in front of a local database, a reader for a local archive — gives you the
  capability without the question.

CONFIGURING ONE

  In ~/.cobirb/config.json, like everything else. A repository cannot name a
  server — which matters more for this key than most, since a server entry is
  a command line.

  {
    "mcp_servers": {
      "notes": {
        "command": "python3",
        "args": ["/home/you/.cobirb/mcp/notes_server.py"],
        "env": {"NOTES_DIR": "/home/you/notes"},
        "inherit_env": false,
        "cwd": null,
        "timeout": 120,
        "startup_timeout": 30,
        "enabled": true
      }
    }
  }

  A server that fails to start is reported on stderr and skipped; the
  session runs without it rather than not running.

WRITING YOUR OWN — an offline proxy over a database

  This is the case worth building for: you want the model to answer
  questions about a real dataset, and you do not want the dataset, the
  schema, or the queries leaving the machine. A server you wrote does
  exactly what you can read, and CoBirb never sees the database at all —
  only the rows your server chose to return.

  It is a program that reads JSON-RPC 2.0 from stdin and writes it to
  stdout, one message per line. Three methods, and you are done:

    import json, sqlite3, sys

    DB = sqlite3.connect("/home/you/data/app.db")
    TOOLS = [{
        "name": "query",
        "description": "Run a read-only SQL query against the app database.",
        "inputSchema": {
            "type": "object",
            "properties": {"sql": {"type": "string"}},
            "required": ["sql"],
        },
    }]

    def reply(mid, result):
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": mid,
                                     "result": result}) + "\\n")
        sys.stdout.flush()

    def text(body, is_error=False):
        return {"content": [{"type": "text", "text": body}],
                "isError": is_error}

    for line in sys.stdin:
        if not line.strip():
            continue
        msg = json.loads(line)
        method = msg.get("method")
        if method == "initialize":
            reply(msg["id"], {"protocolVersion": "2025-06-18",
                              "capabilities": {"tools": {}},
                              "serverInfo": {"name": "appdb",
                                             "version": "1"}})
        elif method == "tools/list":
            reply(msg["id"], {"tools": TOOLS})
        elif method == "tools/call":
            sql = msg["params"].get("arguments", {}).get("sql", "")
            if not sql.lstrip().lower().startswith("select"):
                reply(msg["id"], text("only SELECT is allowed here", True))
                continue
            try:
                rows = DB.execute(sql).fetchmany(100)
            except Exception as exc:
                reply(msg["id"], text(f"query failed: {exc}", True))
            else:
                reply(msg["id"], text(json.dumps(rows)))
        elif method and method.startswith("notifications/"):
            pass          # nothing to acknowledge
        elif "id" in msg:
            reply(msg["id"], text(f"unsupported method {method}", True))

  Point CoBirb at it and it appears as mcp__appdb__query. Notes on the
  above, all of which matter in practice:

    • Never write anything but JSON-RPC to stdout. Banners, print()
      debugging and progress bars all corrupt the stream — CoBirb logs and
      skips a non-JSON line, but your own parser probably will not. Use
      stderr, which CoBirb captures and quotes back if the server fails.
    • Answer every request that has an "id". A request left unanswered is
      a client sitting on a timeout.
    • Enforce your own limits inside the server. The SELECT check and the
      fetchmany(100) above are the whole reason this is a proxy rather
      than a database connection handed to a model: the server is where
      "read-only" and "not the whole table" are facts rather than
      instructions.
    • Return errors as isError results, not as JSON-RPC errors. CoBirb
      turns those into a failed tool result, which the model reads and
      adapts to; a transport-level error reads as the server being broken.
    • Descriptions are read by the model. "Run a read-only SQL query
      against the app database" earns better calls than "query".
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

  read_file      Read a file. Large ones come back a range at a time,
                 with the offset to continue from — any size is readable.
  write_file     Create/overwrite a file.
  edit_file      Replace an exact old_str with new_str in a file.
  apply_patch    Apply a unified-diff patch to a file.
  glob           Find files matching a glob pattern, a page at a time.
  grep           Search file contents by regex.
  list_dir       List a directory's contents, a page at a time.
  repo_map       Outline the codebase: which files matter and what is
                 defined in them, most-referenced first.
  shell          Run a shell command. Highest privilege; gated. Very long
                 output keeps its start and end, dropping the middle.

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
CONFIG — one file, in your home directory

  ~/.cobirb/config.json

That is the only config file CoBirb reads. It does not read a cobirb.json
from the directory you are working in, does not merge one over this, and
does not look for one — so a repository cannot configure CoBirb at all.

That is deliberate, and it is the reason there is no project layer. Config
here is not preference: it decides what is pre-approved, which directories
may be read or written, what runs at lifecycle points, and which
subprocesses start. A repository able to contribute any of that would mean
cloning it and running CoBirb inside it lets its author influence the
permission model — before the model is asked anything, and with no prompt
able to intervene.

What a project may still do is describe itself: AGENTS.md instructions, the
repo map, and prompt files in <project>/.cobirb/commands/ are all still read.
Those are content for the model, not capability granted to it, and every
tool call they lead to still goes through the permission layer.

See config.json.example for a starting point. Keys:

  "default_model"                Model to use by default (or --model/
                                 COBIRB_MODEL_NAME). Validated at
                                 interactive startup — see 'cobirb help
                                 model'.
  "model"                        Older, equivalent name for the same thing.
  "models": {"default": {
      "name": "...", "base_url": "..."}}  Model name/endpoint (local Ollama
                                 by default; any OpenAI-compatible server
                                 works).
  "verify_command"               A command that decides whether the project
                                 is still healthy, e.g. "pytest -q". Run
                                 after any turn that changed files; if it
                                 fails, the model is told and gets one
                                 attempt to fix it. Off unless you name one
                                 — CoBirb never guesses a test command.
  "verify_timeout"               Seconds to allow it (default 120).
  "verify_fix_attempts"          How many times the model may react to a
                                 failure (default 1).
  "redact_secrets"               false to stop stripping credentials from
                                 tool output before it reaches the model,
                                 the session and the audit log. On by
                                 default. Turn it off if you need the agent
                                 to edit a credentials file — it cannot do
                                 that through a redacted read.
  "checkpoints"                  false to stop snapshotting files before
                                 the agent changes them, which is what
                                 /undo restores from. On by default.
  "instructions"                 false to stop reading the project's
                                 AGENTS.md / CoBirb.md from the working
                                 directory into the system prompt. On by
                                 default: a file in your own repo saying how
                                 you want an agent to behave is a clear
                                 enough signal not to need asking twice.
  "instructions_max_chars"       How much of it to send (default 32000 —
                                 most projects' conventions fit whole).
  "repo_map"                     false to stop putting an outline of the
                                 codebase in the system prompt at session
                                 start. The repo_map tool stays either way.
  "repo_map_max_chars"           How large that outline may be (default
                                 16000).
  "context_tokens"               Override the context window. CoBirb asks
                                 the server for one on every request, using
                                 the Modelfile's num_ctx if it sets one and
                                 otherwise what the model says it can do —
                                 so this is only needed to cap a window your
                                 VRAM would rather not hold. /context shows
                                 what is in use.
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

  "hooks"                        Your own commands at CoBirb's decision
                                 points; a before_tool hook can refuse a
                                 call outright. See 'cobirb help hooks'.
  "mcp_servers"                  Local MCP servers to start and take tools
                                 from. Each one is a program you choose to
                                 run and to show your work to — read
                                 'cobirb help mcp' before adding one.

Nothing here ever defaults to a networked provider — model/provider
settings are opt-in, matching CoBirb's no-network-by-default rule. A
configured MCP server is the one exception you can create yourself, and it
is not a CoBirb network call: it is a program you asked to run.
""",
}
