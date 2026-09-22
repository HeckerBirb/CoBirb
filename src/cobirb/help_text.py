"""The text `cobirb help` prints.

Pure data, kept out of cli.py because ~300 lines of prose between two
functions makes the module it lives in hard to read around."""
from __future__ import annotations


HELP_TEXT = """\
CoBirb — a privacy-first, local agentic CLI.

FIRST RUN
  cobirb setup           Choose your model server and model; saved to your
                         config. Only the address you give is contacted.

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
  -w [PASSWORD]       Run in an encrypted session, starting one under
                      ~/.cobirb/sessions if --session names none. '-w' alone
                      prompts without echo; '-w hunter2' is visible in shell
                      history and process listings.
  --session PATH      Resume/continue the encrypted session at PATH.
  --export PATH       Decrypt --session and write it to PATH as markdown,
                      then exit. Plaintext, deliberately.
  --branch PATH       Fork --session into a new, independent encrypted
                      session at PATH, then exit. The original is untouched
                      and stays resumable exactly as it was. Add
                      --branch-at N to fork from turn N instead of the end.
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
  /export [PATH]     Write this session out as readable markdown. The
                     file is plaintext; the session stays encrypted.
  /diff              Everything the agent has changed this session, as one
                     diff. Works without git; shows only its own edits.
  /undo              Put back the files the last turn changed. Cannot
                     undo what a shell command did — see 'help tools'.
  /context           Show how much of the model's context window this
                     session is using, and what has been compacted away.
  /clear             Start over from here: clears the screen and the
                     model's context. Nothing is deleted — the turns
                     before it stay in the session file.
  /autopilot on|off  Work unattended: files in the project and commands in the
                     sandbox run without asking; anything else is refused.
  /plan on|off       Toggle plan mode mid-conversation.
  /plan              Show whether plan mode is currently on.
  /commands          List your own prompt files, invocable by name. Any
                     other /word is one of them — see 'help commands'.
  /flock <what>      Divide a piece of work between several agents. Opens
                     the Flock tab; you approve the charter before anything
                     runs. See 'help flock'.
  /charter           Review the charter Brainy Birb last proposed and run
                     it if you approve. '/flock' with no objective does the
                     same. Useful if you dismissed the dialog.
  /memories          Load, create, rename, or delete memory catalogues —
                     named lists of facts fed into the system prompt.
  /remember <fact>   Save a fact into a catalogue you pick. A plain command,
                     not a model tool, so it works the same regardless of
                     what the model can do, and only fires when typed.
  /image <path>      Attach an image to the next message you send — type
                     your message normally, right after, the same as
                     attaching a file anywhere else.
  /image <path> <message>
                     Or say both at once, and it sends straight away.
                     Quote a path that contains spaces.
  ? or /help         Open this help. '/help <topic>' opens one topic.

INTERACTIVE KEYS
  enter sends · shift+enter starts a new line (alt+enter or ctrl+j if your
  terminal cannot tell shift+enter from enter — many cannot). The box wraps
  and grows to 8 lines, then scrolls.
  Typing '/' at the start of a message lists the commands, your own included;
  '@' lists files. Both: up/down to move, tab or enter to pick, escape to
  dismiss. A '/' anywhere but the start is just text.
  f1 help · f2 next tab · ctrl+q quit · up/down recall earlier prompts (the
  last 100, in memory only — nothing you type is written to disk); in a
  multi-line message they move the cursor, and recall from the first/last
  line.
  ctrl+c copies the transcript selection if you have dragged one out with
  the mouse, and otherwise cancels a running turn (a stuck or slow shell
  command, most usefully — quitting mid-turn tries this first too, so it
  never sits waiting on one either). In a tool-approval dialog: y allow
  once · a allow for the rest of the session · n (or escape) deny. A Worker
  Birb's request is not a dialog — it appears in that worker's own pane on the
  Flock tab, with its own buttons, so two workers asking at once can never be
  answered by the same click. See 'help flock'.

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
  Run 'cobirb help <topic>' for more: session, plan, model,
  commands, hooks, mcp, flock, plugin, plugins, tools, config.
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

BRANCHING — trying a different direction without losing the original

  cobirb --session <path> -w --branch <new-path>            Fork it whole.
  cobirb --session <path> -w --branch <new-path> --branch-at 3
                                                     Fork from turn 3 only.

Forking copies a session's turns into a brand-new file; the source is only
ever read and comes out completely unchanged, still resumable exactly as it
was. In interactive mode, the Sessions tab's "Branch…" button forks the
whole of the selected session and switches straight into it — the same way
"Resume" does. Branching from an earlier point (--branch-at) rather than the
end is CLI-only for now.
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

CoBirb ships no model of its own; it talks to the model server you point it
at — Ollama by default, or any OpenAI-compatible one (llama.cpp, LM Studio,
vLLM) with "api": "openai". No model is selected until you name one:

  cobirb setup           Choose the server and model; saved to your config.
  --model NAME           For this run.
  models.default.name    In config, the default for every run.
  /model                 Interactively: lists what the server has and lets
                         you pick one; offers to save it if none is set.

If interactive mode starts with no working model — nothing configured, or a
configured one that isn't there — it fetches the list itself and opens the
same picker /model would.

YOUR MODEL'S OWN SYSTEM PROMPT

Ollama accepts one system message per request, and sending one REPLACES the
SYSTEM directive the model was built with. A model you made with
'ollama create' around a custom SYSTEM is a configuration you chose, so
CoBirb does not overwrite it:

  • By default CoBirb sends no system message at all. Your model behaves
    inside CoBirb exactly as it does in 'ollama run' — same SYSTEM, same
    voice, same everything.
  • When CoBirb does have something to add (plan-mode phase
    instructions, or --system-prompt harness), it reads your model's own
    SYSTEM back via /api/show and puts it FIRST, then appends its own part.
    Yours is supplemented, never discarded.

  --system-prompt off       The default. Nothing of CoBirb's is sent.
  --system-prompt harness   Add a short block on how to work as a coding
                            agent — look before changing, check your work,
                            keep going until done, don't retry a denied
                            call. Placed after your model's own SYSTEM.
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

  default        Falls back to for everything else. The older top-level
                 "model" and "default_model" keys still name this one when
                 models.default.name is unset; doctor calls them deprecated.
  orchestrator   The agent you talk to, and Brainy Birb in a flock.
  worker         What each Worker Birb runs on — see 'cobirb help flock'.

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
Ctrl+C asks before interrupting — no further workers start, no further reviews
run, and any worker already talking to a model finishes that turn, because a
model call in flight cannot be cut off. A review already under way finishes too:
it puts a file back to its stub for a moment, and stopping mid-way would leave
it there. Whatever has been written to your files stays written; use git.

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
  2. It builds the charter a ticket at a time, and each one is checked as it
     lands — a file another ticket already writes, or a file that is a seam, is
     refused there and then. So the partition comes out disjoint: no two
     workers writing one file, nobody reading a file that is still moving.
  3. YOU APPROVE THE CHARTER. The only decision you make. You see the plan,
     every seam, and every worker's exact read and write scope before anything
     runs. Approving it is what lets the workers run unattended, which is why
     there are no tool prompts afterwards.
  4. The workers fan out, two at a time by default.
  5. Brainy Birb reviews the result and reports. A second round is a new
     decision you make with that in front of you.

WHAT A WORKER CAN TOUCH

Its scope is a Policy, not a request. A Worker Birb may CHANGE only the files
its ticket names — nothing else, and a write into a file another worker owns is
refused outright rather than asked about. It may READ anything in the project,
because one that cannot orient cannot work; what isolates it is that it gets no
AGENTS.md, no repo map and no plan, so it sees code rather than the shape of the
whole. Conventions reach it through the skeleton it is filling in, which was
already written in your project's style.

It may run the programs its own acceptance check names, with any arguments — so
it can run one test file at a time and converge on green instead of writing
blind. Nothing else: a pipe is a second program, so 'pytest -q | head' still
asks you, because shell grants carry no path scoping and 'head' would reach
outside the worker's read scope. Requests appear in that worker's own pane.

Your config does NOT apply to Worker Birbs. allow_tools, allow_read_dirs and
allow_write_dirs are ignored for them: the charter you approved is the only
thing that grants a worker anything up front. So "shell(pytest)" in your config
grants a worker nothing, and that one rule — not a special case per command —
is why it asks about find, pwd or ls.

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
    function is testing nothing. Where that cannot be judged — the worker
    changed nothing, or its ticket never said which of its files hold the
    tests — it reports "could not be checked" rather than a pass.

Neither check uses a model, so a review costs you no tokens and cannot be
talked out of a finding.

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
    "plugin": """\
PLUGIN — a registry that is a plain file

  cobirb plugin install <path>    Install a local plugin's source directory.
  cobirb plugin list              What's installed, and what's active.
  cobirb plugin remove <name>     Uninstall one.

Local only, on purpose. `install` takes a path already on your disk — never a
URL, a package name, or a version to fetch. There is no index to query and
nothing this command ever reaches out for. If you want a published plugin,
`pip install` it yourself the normal way; CoBirb finds anything registering a
`cobirb.plugins` entry point regardless of how it got there (see 'cobirb help
plugins', plural, for how discovery works generally). This command exists for
the other case: you have a plugin's source on disk and want CoBirb to find it.

WHAT INSTALL ACTUALLY DOES
  `plugins/loader.py` resolves a local plugin through real Python package
  metadata, not by reading its pyproject.toml directly — so it has to be a
  properly installed distribution, editable or not. `install` copies the
  directory into ~/.cobirb/plugins/<name>/ and runs `pip install -e` on it,
  automating a sequence you could do by hand — nothing it does is a new
  capability, only fewer steps to get wrong.

  The source must declare, in pyproject.toml:
    - [project].name equal to cobirb_plugins_<name> — the loader derives the
      distribution name it looks up from the installed directory's own
      basename, not from anything inside the package, so this has to match
      exactly or the plugin will install cleanly and never be found.
    - a [project.entry-points."cobirb.plugins"] table naming the class.

  A failed install (wrong name, no matching entry point, doesn't subclass the
  right SPI interface) is rolled back completely — the copied directory and
  the pip package both — rather than left half-installed and silently broken.

No checksum or signature check: the source you name is already on your own
disk, under your own control, before this command ever touches it. That
verification would only earn its cost once something could be fetched by this
command rather than merely relocated by it — which it deliberately cannot.
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

  "model", "default_model"       Deprecated names for models.default.name,
                                 read only when that is unset.
  "models": {"default": {
      "name": "...", "base_url": "..."}}  Model name/endpoint (local Ollama
                                 by default; any OpenAI-compatible server
                                 works).
  "connect_timeout"              Seconds to wait for the endpoint to accept a
                                 connection (default 10). Short on purpose:
                                 either something is listening on that port or
                                 it is not. This is the timeout behind "Is
                                 Ollama running?".
  "request_timeout"              Seconds to wait for it to say something once
                                 connected (default 600). Long on purpose, and
                                 separate because it measures SILENCE, not
                                 work: a request your endpoint queued behind
                                 another generation sends nothing until it
                                 starts producing tokens. Raise it if you
                                 serve very large models or run several Worker
                                 Birbs at once.
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
  "context_tokens"               Override how much conversation history
                                 CoBirb budgets against, for when the
                                 endpoint can't say what its window is (or to
                                 deliberately trim it). Does not change the
                                 num_ctx CoBirb asks the server for — see
                                 max_num_ctx for that. /context shows what is
                                 in use.
  "max_num_ctx"                  Ceiling on the num_ctx CoBirb requests from
                                 the server on every /api/chat call. CoBirb
                                 normally asks for whatever the model itself
                                 advertises — the Modelfile's num_ctx, or
                                 otherwise the architecture's max context
                                 length, which can be far larger than your
                                 VRAM should be asked to hold as KV cache.
                                 The model still dictates the window whenever
                                 it asks for less than this; only a request
                                 above the ceiling gets clamped down to it.
                                 Written either way round: 65536, or "64k"
                                 (a k is 1024, so "64k" is exactly 65536).
                                 Unset (default) asks for whatever the model
                                 advertises, uncapped.
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
