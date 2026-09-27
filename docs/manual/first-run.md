# First run

## 1. Tell CoBirb which model

```bash
cobirb setup
```

It asks where your model server is (Ollama's `http://localhost:11434` is suggested), whether it
speaks Ollama's API or the OpenAI one (llama.cpp, LM Studio, vLLM), lists the models that server has,
and saves your choice to `~/.cobirb/config.json`. Only the address you give is contacted.

Or pass a model per run: `cobirb --model qwen2.5-coder:14b`. The interactive app also offers the
model list at startup when none is configured, and asks whether to remember your pick.

## 2. Start it

```bash
cd ~/your-project
cobirb
```

You get the full-screen app. Type a message, press **enter**.

## 3. The agent asks before it acts

The first time it wants to read a file, you get a prompt:

```
Allow read_file
  src/main.py

  Once (y)   Always (a)   Deny (n)
```

- **y** — this one call.
- **a** — for a read, the whole project, for the rest of the session; for a write, that directory.
- **n** or **escape** — no.

Nothing is pre-approved. See [Permissions](permissions.md).

## 4. Keys worth knowing

| Key | Does |
|---|---|
| `enter` | Send |
| `shift+enter` | New line (`alt+enter` if your terminal eats it) |
| `up` / `down` | Previous prompts |
| `f1` | Help |
| `f2` | Next tab |
| `f3` | Auto-pilot on or off — works mid-turn and mid-flock; the footer shows which |
| `ctrl+c` | Copy selection, or cancel the running turn |
| `ctrl+q` | Quit |

## 5. Ask for something real

```
read src/main.py and tell me what the entry point does
```

While it works, the reply being written appears just above the prompt, under a rule naming who is
writing, and moves into the conversation when it's done.

**You can steer it while it works.** Type into the prompt while a turn is running — the box is
titled `Steering conversation:` — and press enter: your message joins the same conversation at the
next step, and CoBirb carries on with it in mind, instead of starting a new turn. A reply being
written stops, and your message appears once CoBirb has taken it in — after the reply it cut off,
followed by "redirected by a new message"; while a tool is running, after that tool's result. Use it
to nudge work that is on the right track but drifting. (Not during a flock.)

Then try `/diff` to see what it changed, and `/undo` to put it back.
