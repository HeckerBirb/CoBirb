# Remote Worker Birbs

A Worker Birb can run on another machine with another operating system — your CoBirb on Linux
hands the Windows-specific part of a flock to a Worker Birb on Windows, which writes it, builds it
and tests it natively. Everything else about the flock stays the same: the worker's pane is on your
screen, its questions are asked there, and its work comes back into your project.

## Setting one up

**On the other machine** — Python, CoBirb (`pip install cobirb`), and whatever the work needs to
build (a compiler, a test runner). Then:

```bash
cobirb remote-worker                      # serves on 0.0.0.0:8443
cobirb remote-worker --listen 10.0.0.5:9000
cobirb remote-worker --verbose            # a line for every message and everything the job does
```

It prints its certificate fingerprint, and a pairing code when your CoBirb first connects. Without
`--verbose` it says little more than when a ticket starts and finishes; with it, each heartbeat,
program lookup, check and tool call gets a timestamped line, so you can see it is working. It reads
nothing from its own config — every setting comes from your machine.

**On your machine** — add it to `~/.cobirb/config.json`:

```json
"remote_workers": [
  {"remote_os": "Windows", "remote_url": "https://10.0.0.5:8443/api"}
]
```

| Key | Default | What it is |
|---|---|---|
| `remote_os` | — | The remote's OS, as its Python names it (see below) |
| `remote_url` | — | Its address: `https://host:port/path` |
| `run_llms_locally` | `false` | `false`: its model calls come back to your session and go to your model server. `true`: it uses its own |
| `openai_endpoint` | — | With `run_llms_locally`: its model server, as *the remote* reaches it (OpenAI-compatible; an Ollama server works) |
| `model_name` | your `models.worker` | With `run_llms_locally`: which model to ask for |

**`remote_os` values**, in any case: `Windows` or `win32` · `Linux` or `linux` · `Darwin` or
`darwin` (macOS) · `FreeBSD`, `OpenBSD`, `NetBSD` (or `freebsd14` and so on) · `SunOS`, `AIX`.
A remote with the same OS as your machine is ignored. `cobirb doctor` checks every entry and says
whether each is paired.

## Pairing

The first time you start `cobirb` after adding a remote, right after the model is chosen:

1. CoBirb shows the remote's certificate fingerprint. Compare it with the one the remote printed,
   and trust it only if they match.
2. The remote prints an 8-digit code. Type it into CoBirb.

That pairing lasts 30 days from its last use, so a remote you use regularly never asks again. A
remote with a different certificate is refused until you trust it again. Only interactive mode
pairs; a flock uses the pairings you already have.

## In a flock

- Brainy Birb is told which machines exist and gives a ticket that must be built and tested on
  another OS a `runs on` line. The ticket's check, and anything it `requires`, is checked **on that
  machine**.
- The charter shows where each worker runs, and that its files are sent there.
- A remote gets **only what its ticket needs**: its own files and the files it reads, as the
  skeleton left them. A ticket with `needs` waits for those workers and starts with their finished
  files; a file it reads that another worker finishes meanwhile is sent to it.
- Each remote takes one ticket at a time, on top of your `concurrency` — two local workers and one
  remote worker run at once under a limit of two. With every remote of that OS busy, the ticket
  shows **Waiting for available worker** until one is free.
- Its tests run **natively**, while it works and again after the round, and the review's
  put-the-stub-back check runs there too — a Windows test is judged on Windows.
- If a ticket needs an OS no remote offers, you're asked whether to go on. If you do, a local
  worker writes it without building or testing it, and it's reported as **written, not verified**.

## The connection

CoBirb connects to the remote — over TLS, a WebSocket — and never listens itself. It sends a
heartbeat every 30 seconds. A dropped connection costs nothing: CoBirb reconnects and picks up
where it left off. But a remote that hears nothing from CoBirb for **five minutes stops its work
hard** — mid-reply, mid-test — and deletes it.

## What to know

- **On Windows, the machine itself is the containment.** There's no sandbox there as there is on
  Linux: a worker on your own Windows computer acts as your Windows user. Use a VM or Windows
  Sandbox when that matters.
- The remote sees only its ticket's files, never your credentials, your config or your sessions.
- Configured remotes are the one exception, besides `--upgrade` and MCP servers, to CoBirb opening
  no network connection except to your model server.
