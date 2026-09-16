# Commands

Type these in the app. Anything else starting with `/` is tried as one of your own
[custom commands](#your-own-commands), then sent to the model as-is.

| Command | Does |
|---|---|
| `/help` | The help screen. `/help <topic>` for one topic. |
| `/model` | Pick a model from what your endpoint offers. |
| `/persona` | Pick a persona. `/persona <name>` to switch directly. |
| `/plan on` · `/plan off` | Plan → act → validate as three separate phases. |
| `/plan` | Say whether plan mode is on. |
| `/context` | How much of the model's window this session is using. |
| `/diff` | Everything the agent changed this session, as one diff. |
| `/undo` | Put back the files the last turn changed. |
| `/export [PATH]` | Write the session out as markdown (plaintext). |
| `/memories` | Load, create, rename or delete memory catalogues. |
| `/remember <fact>` | Save a fact into a catalogue you pick. |
| `/image <path> [message]` | Attach an image to your next message. |
| `/flock <objective>` | Split work across several agents. |
| `/commands` | List your own prompt files. |

`?` on its own opens help too.

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

## Your own commands

Put a markdown or text file in `<project>/.cobirb/commands/`:

```bash
mkdir -p .cobirb/commands
echo "Review the staged diff for bugs. Be terse." > .cobirb/commands/review.md
```

Now `/review` in the app sends that text. `/commands` lists what's available.

A built-in name always wins, so you can't shadow `/undo`.
