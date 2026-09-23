# MCP servers

The Model Context Protocol is how CoBirb gains tools it knows nothing about: your issue tracker, your
database, your style guide. Each becomes a tool named `mcp__<server>__<tool>`, and goes through the
same permission prompt, redaction and audit log as the built-ins.

Only the **stdio** transport is supported, deliberately: a stdio server is a subprocess on this machine
talking over a pipe. HTTP and SSE point at a URL — a network call CoBirb did not make and cannot see.

## Read this before adding one

A server is a program you are choosing to run and to show your work to. CoBirb's promises — no
telemetry, no outbound network — are about CoBirb, not about a server you configure. A server **can**
open its own network connections, send the arguments of every call it receives anywhere it likes, and
phone home without saying so. Nothing in CoBirb can prevent or detect that. Two defaults reduce the
blast radius:

- **The environment is not inherited.** A server gets `PATH`, `HOME`, `LANG` and whatever you list
  under `env` — not your cloud credentials or API tokens. Set `inherit_env: true` only for a server you
  trust.
- **Nothing is pre-approved.** Each MCP tool is asked about, and "always" grants that one tool for the
  session.

Prefer servers you can read. One that runs entirely offline — a proxy in front of a local database, a
reader for a local archive — gives you the capability without the question.

## Configuring one

In `~/.cobirb/config.json`; a repository cannot name a server (an entry is a command line).

```json
{
  "mcp_servers": {
    "notes": {
      "command": "python3",
      "args": ["/home/you/.cobirb/mcp/notes_server.py"],
      "env": {"NOTES_DIR": "/home/you/notes"},
      "inherit_env": false,
      "timeout": 120,
      "startup_timeout": 30,
      "enabled": true
    }
  }
}
```

A server that fails to start is reported and skipped; the session runs without it. The **Plugins** tab
lists what was found.

## Writing your own: an offline proxy over a database

The case worth building for: the model answers questions about a real dataset, and neither the dataset,
the schema nor the queries leave the machine. CoBirb never sees the database — only the rows your
server chose to return.

A server reads JSON-RPC 2.0 from stdin and writes it to stdout, one message per line. Three methods:

```python
import json, sqlite3, sys

DB = sqlite3.connect("/home/you/data/app.db")
TOOLS = [{
    "name": "query",
    "description": "Run a read-only SQL query against the app database.",
    "inputSchema": {"type": "object", "properties": {"sql": {"type": "string"}}, "required": ["sql"]},
}]

def reply(mid, result):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": mid, "result": result}) + "\n")
    sys.stdout.flush()

def text(body, is_error=False):
    return {"content": [{"type": "text", "text": body}], "isError": is_error}

for line in sys.stdin:
    if not line.strip():
        continue
    msg = json.loads(line)
    method = msg.get("method")
    if method == "initialize":
        reply(msg["id"], {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                          "serverInfo": {"name": "appdb", "version": "1"}})
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
        pass
    elif "id" in msg:
        reply(msg["id"], text(f"unsupported method {method}", True))
```

It appears as `mcp__appdb__query`. What matters in practice:

- **Write nothing but JSON-RPC to stdout.** Banners and `print()` debugging corrupt the stream; use
  stderr, which CoBirb captures and quotes if the server fails.
- **Answer every request that has an `id`**, or the client waits out a timeout.
- **Enforce your limits inside the server.** The `SELECT` check and `fetchmany(100)` are what make this a
  proxy rather than a database handed to a model.
- **Return errors as `isError` results**, not JSON-RPC errors: the model reads the former and adapts; the
  latter reads as a broken server.
- **Descriptions are read by the model.** A precise one earns better calls.
