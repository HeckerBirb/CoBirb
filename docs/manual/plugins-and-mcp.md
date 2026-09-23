# Plugins & MCP

Two ways to give CoBirb more tools.

## MCP servers

A configured server's tools appear alongside the built-ins and go through the same permission
prompt.

```json
{
  "mcp_servers": {
    "sqlite": {
      "command": "uvx",
      "args": ["mcp-server-sqlite", "--db-path", "/home/you/data.db"]
    }
  }
}
```

The **Plugins** tab lists what was found. [MCP](mcp.md) has the details, including a worked example
of writing your own offline server.

A server does **not** inherit your environment — no cloud credentials, no unrelated API
tokens — and none of its tools are pre-approved. It is still a program you chose to run: it can
open its own network connections and send the arguments of every call it receives anywhere it
likes.

## Plugins

A plugin is a Python package that fills one of CoBirb's slots.

```bash
cobirb plugin install ./my-plugin
cobirb plugin list
cobirb plugin remove my-plugin
```

`install` takes a directory already on your disk — never a URL, a package name or a version — and
fetches nothing. It copies the directory to `~/.cobirb/plugins/<name>/` and runs `pip install -e` on it.
The plugin's `pyproject.toml` must name the distribution `cobirb_plugins_<name>` (exactly — that is how
the loader finds it) and declare a `[project.entry-points."cobirb.plugins"]` table. A failed install is
rolled back completely. A plugin installed some other way is found too, as long as it registers a
`cobirb.plugins` entry point.

| Slot | Replaces |
|---|---|
| `tool` | Adds tools (any number) |
| `model` | The model provider |
| `io` | How CoBirb renders and asks |
| `crypto` | The session cipher |

Tool plugins are merged in. The other slots replace the core one only when you name it:

```json
{ "plugins": { "model": "my-provider", "io": "core-io", "crypto": "core-crypto" } }
```

Declare the contract version your plugin was written against:

```python
class MyTool(Tool):
    COBIRB_SPI = 1
```

A plugin that fails to load, or whose tool name collides with an existing one, is reported and
skipped, never fatal. See [`AGENTS.md`](../../AGENTS.md) for the full SPI.

**Installing runs the plugin's code.** `pip` executes the package's own build backend before
CoBirb has looked at a single class. No prompt can cover that — choosing to install *is* the
security decision.
