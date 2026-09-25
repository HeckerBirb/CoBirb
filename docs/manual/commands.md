# Commands

Type these in the app. Anything else starting with `/` is tried as one of your own
[custom commands](#your-own-commands), then sent to the model as-is.

| Command | Does |
|---|---|
| `/help` | The help screen. `/help <topic>` for one topic. |
| `/model` | Pick a model from what your endpoint offers. |
| `/plan on` · `/plan off` | Look and plan first — the model can read but not change anything — then act. |
| `/plan` | Say whether plan mode is on. |
| `/autopilot on` · `/autopilot off` (or `f3`) | Work unattended: read and change files in the project and run commands in the sandbox without asking; refuse anything else. Needs the sandbox and git. |
| `/context` | How much of the model's window this session is using. |
| `/clear` | Start over from here — clears the screen and the model's context, deletes nothing. |
| `/diff` | Everything the agent changed this session, as one diff. |
| `/undo` | Put back the files the last turn changed — shell changes included when git is installed. Files you edited since are left alone. |
| `/export [PATH]` | Write the session out as markdown (plaintext). |
| `/memories` | Load, create, rename or delete memory catalogues. |
| `/remember <fact>` | Save a fact into a catalogue you pick. |
| `/image <path> [message]` | Attach an image to your next message. |
| `/flock <objective>` | Split work across several agents. |
| `/charter` | Review the last proposed charter and run it if you approve. |
| `/commands` | List your own prompt files. |

`?` on its own opens help too.

## Finding them

Type `/` at the start of a message and a list appears under the prompt box — five at a time, with
a count of how many more match. `↑`/`↓` to move, `tab` or `enter` to pick, `escape` to dismiss.
Keep typing to narrow it; `/cle` gets you to `/clear`.

Your own [custom commands](#your-own-commands) are in the list too, tagged `user` or `project`, so
they are findable without remembering what you called them.

A slash only means a command as the **first word** of a message. "remind me to /clear later" is
prose, no list appears, and it goes to the model as written — as it always has.

## Mentioning a file

Type `@` and start typing a filename. A list of up to five matches appears; **↑/↓** to move,
**tab** or **enter** to pick, **escape** to dismiss.

```
summarise @src/main.py
```

The file is sent with your message. The transcript shows `@src/main.py`, not the whole file.

Matching is fuzzy: letters must appear in order but needn't be adjacent, so `gba` finds
`global.py`, `general_batch.py` and `gba.py` — with the exact name first. Ignored files
(`.gitignore`, dotfiles) never appear.

## Plan mode

`/plan on` makes each message start with a planning pass: the model can read, search and outline the
project, and keep a checklist, but cannot change anything — a call that tries is refused. It ends with a
short numbered plan, shown to you at once, and then does the work. It costs a few extra model calls per
message. `--plan-mode on|off` and `"plan_mode"` in config set the starting state.

## Your own commands

A prompt you have written down, invoked by name. Its filename is the command:

| File | Available |
|---|---|
| `~/.cobirb/commands/review.md` | everywhere |
| `<project>/.cobirb/commands/review.md` | in that project |

```bash
mkdir -p .cobirb/commands
echo 'Review $1 for bugs. Be terse.' > .cobirb/commands/review.md
```

`/review src/parser.py` in the app sends that text — and so does `cobirb -p "/review src/parser.py"`.
`/commands` (or `cobirb commands`) lists what's available.

- `$ARGUMENTS` is everything after the command; `$1` … `$9` are single words. A placeholder with nothing
  to fill it is empty. With no placeholder at all, what you typed is appended.
- An optional frontmatter block describes it in the listing, and is not sent:

  ```
  ---
  description: Review a diff the way this team does
  ---
  Read the staged diff and check it against $ARGUMENTS.
  ```

A project command is data, not code: it expands to a prompt and nothing else, which is why commands are
read from a project when hooks and MCP servers are not. Every tool call it leads to is still gated, and a
built-in name always wins, so a custom `/undo` cannot change what `/undo` does.
