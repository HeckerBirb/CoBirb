# Remote workers: several machines, some with their own model

Three [Remote Worker Birbs](../../docs/manual/remote-workers.md): two Windows machines, one of them
with a GPU and a model server of its own, and a Mac that borrows your session's model. Tickets that
must be built and tested on Windows go to whichever Windows machine is idle first; macOS tickets go
to the Mac.

```json
{
  "models": {
    "default": { "name": "qwen3-coder:30b" },
    "worker":  { "name": "qwen3-coder:30b" }
  },
  "remote_workers": [
    {
      "remote_os": "win32",
      "remote_url": "https://10.10.10.10:8443/api",
      "run_llms_locally": true,
      "openai_endpoint": "http://localhost:11434",
      "model_name": "devstral:24b"
    },
    {
      "remote_os": "Windows",
      "remote_url": "https://10.10.10.11:8443/api"
    },
    {
      "remote_os": "Darwin",
      "remote_url": "https://mac-mini.lan:8443/api"
    }
  ]
}
```

## What each entry says

- **`10.10.10.10`** thinks with its own model. `openai_endpoint` is an address *as that machine
  sees it* — here the Ollama server running on it, `localhost:11434` — and your CoBirb never
  contacts it. `model_name` picks the model there; without it, the remote would ask for your
  `models.worker` name.
- **`10.10.10.11`** has no model of its own (`run_llms_locally` defaults to `false`): its model calls
  are relayed to your session.
- **The Mac** is the same, named by `sys.platform`'s spelling. `remote_os` takes either Python name,
  in any case — `win32`, `Windows` and `wINdOwS` are all Windows; `darwin` and `Darwin` are macOS.

## How tickets are shared out

- Each remote runs **one ticket at a time**, on top of your local `concurrency`. With a limit of 2,
  two local workers plus these three remotes can all be working at once.
- A Windows ticket goes to the **first idle** Windows remote. With both busy, its pane shows
  **Waiting for available worker** until one frees up.
- A ticket for an OS none of these offers — say FreeBSD-only work — is put to you: go on, and it
  is written without being built or tested, and reported as **written, not verified**.

This config assumes your own CoBirb runs on Linux. A remote with your own machine's OS is ignored,
so on a Mac the `Darwin` entry would do nothing.

## Checking it

```bash
cobirb doctor
```

lists each remote, whether it is paired and for how many more days, and which model it will think
with. A remote configured with your own machine's OS is listed as ignored, and an OS name CoBirb
does not know is a failure. Pairing happens when `cobirb` starts in interactive mode; a flock run
headless uses the pairings you already have and skips any remote that is not paired.
