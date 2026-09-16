# First run

## 1. Tell CoBirb which model

```bash
mkdir -p ~/.cobirb
cat > ~/.cobirb/config.json <<'JSON'
{
  "models": {
    "default": { "name": "qwen2.5-coder:14b", "base_url": "http://localhost:11434" }
  }
}
JSON
```

Or skip the file and pass it per run: `cobirb --model qwen2.5-coder:14b`.

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
- **a** — that directory, for the rest of the session.
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
| `ctrl+c` | Copy selection, or cancel the running turn |
| `ctrl+q` | Quit |

## 5. Ask for something real

```
read src/main.py and tell me what the entry point does
```

Then try `/diff` to see what it changed, and `/undo` to put it back.
