# Remote worker: Windows code from a Linux machine

Your CoBirb runs on Linux, and part of the work only builds and runs on Windows. One [Remote
Worker Birb](../../docs/manual/remote-workers.md) on a Windows machine takes those tickets: it writes
the code, compiles it with a Windows compiler and runs its tests natively. It has no model of its
own — its model calls come back to your session and go to your usual model server.

```json
{
  "models": {
    "default": { "name": "qwen3-coder:30b" }
  },
  "remote_workers": [
    { "remote_os": "Windows", "remote_url": "https://192.168.1.67:8443/api" }
  ]
}
```

`run_llms_locally` is left out, so it is `false`: the Windows machine needs no GPU and no model
server, and your model server is never exposed to it.

## On the Windows machine

1. Install Python, then `pip install cobirb`.
2. Install a compiler — Visual Studio Build Tools (`cl`) or MinGW (`gcc`).
3. Allow inbound TCP on port 8443 in Windows Defender Firewall.
4. Start the worker **from a shell where the compiler is on `PATH`** — for `cl`, the "Developer
   Command Prompt for VS" — because the worker's commands inherit that shell's environment:

   ```bat
   cobirb remote-worker --listen 192.168.1.67:8443
   ```

It prints its certificate fingerprint. Leave it running.

## Pairing, once

Start `cobirb` on Linux. After the model is chosen it shows the remote's fingerprint — trust it
only if it matches the one on the Windows screen — and then asks for the 8-digit code the Windows
terminal prints. The pairing lasts 30 days from its last use. `cobirb doctor` shows it as paired.

## A flock that uses it

```text
/flock Produce metric agents for Linux and Windows that expose temperature, CPU and other
low-level readings on a /metrics endpoint for Prometheus to scrape
```

Brainy Birb sees both machines and marks the Windows part with `runs on`. The charter you approve
says where each worker runs and what is sent where:

```text
  [linux_collector] writes agent/linux/collector.py, tests/test_linux_collector.py
      accept python -m pytest tests/test_linux_collector.py -q
  [windows_collector] writes agent/windows/collector.c, tests/windows/test_collector.c
      reads  agent/metrics_format.h
      accept cl /W4 agent\windows\collector.c tests\windows\test_collector.c /Fe:test_collector.exe && .\test_collector.exe
      runs on Windows — a Remote Worker Birb; its files are sent there
```

The Windows worker gets only its own two files and `agent/metrics_format.h`. Its pane on your screen
shows what it does, and a request outside its scope is asked there. When it finishes, its files
land in your project, and its check and the review's stub check run **on Windows**.

## Why this shape

- **Relayed model, no second server.** One model server serves both machines; the Windows side is a
  plain build-and-test box.
- **One remote, one ticket at a time.** It runs on top of your `concurrency`, so the Linux workers
  are not slowed down by it; a second Windows ticket waits until the first is done.
- **The machine is the containment.** Windows has no sandbox like Linux's bubblewrap — use a VM or
  Windows Sandbox if the worker should not act as your Windows user.
