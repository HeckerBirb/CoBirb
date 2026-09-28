# Remote Worker Birbs

`remote/` — a Worker Birb on another machine and OS, used by the Flock as if it were local. The
user-facing page is [`docs/manual/remote-workers.md`](../manual/remote-workers.md).

## Shape

- **Two ends, one connection.** `remote/server.py` (`cobirb remote-worker`) serves a TLS WebSocket
  with a self-signed certificate (`certs.py`); `remote/client.py` on the main machine opens it. The
  main machine never listens (invariant 3 names configured remotes as an explicit exception).
  `protocol.py` lists every message; the server sends no TLS session tickets (`num_tickets = 0`,
  see `serve_forever`); `websockets`' synchronous API keeps it thread-based like the
  rest of CoBirb.
- **A pure worker.** The remote reads nothing from its own config. Each job runs in its own process
  (`python -m cobirb.remote.job <workspace>`) with a temporary `COBIRB_HOME` holding the main
  machine's settings (`runner._SETTINGS`; hooks are not sent — they name commands on the main
  machine). The job runs the ordinary `run_worker()` with a model handed in (`build_subagent(model=)`).
- **Config** (`settings.py`): `remote_workers` entries — `remote_os` (`osnames.canonical_os`:
  `platform.system()` or `sys.platform` names, any case, reduced to one family), `remote_url`
  (https), `run_llms_locally` (default false), `openai_endpoint`, `model_name`. Same-OS remotes are
  dropped by `configured()`, with a line `doctor` shows.

## Trust (`trust.py`)

- The main machine pins the certificate fingerprint the user accepted and keeps the token the remote
  issued for the 8-digit pairing code (`paths.remotes_path()`); the remote keeps only token hashes
  (`paths.remote_worker_dir()`). Both 0600, written atomically; a pairing lapses after
  `PAIRING_DAYS = 30` without use, and every connection renews it.
- `cobirb remote-worker --verbose` prints one timestamped line per message and job event
  (`server.summarise`, bounded; a relayed model's streamed chunks are left out).
- A changed certificate is refused (`Untrusted`) until trusted again, and trusting a new one drops
  the old token. Pairing prompts only in the TUI, after model selection (`CoBirbApp._pair_remotes`);
  `RemoteClient` without `ask_trust`/`ask_code` fails closed (`NotPaired`, `Untrusted`).

## Staying alive

- Heartbeats every `HEARTBEAT_SECONDS = 30`; the reply carries the remote's state (`idle`, `busy`
  with another main machine, `yours`).
- A remote without a heartbeat from the job's owner for `HALT_AFTER_SECONDS = 300` halts: the job's
  process tree is killed (`_kill_tree`: process group, or `taskkill /T`) and its workspace deleted.
- Job events carry a `seq`; after a drop the client reconnects (`_reconnect`, until the remote
  would have halted anyway) and sends `resume` with the last `seq` it saw.

## A job (`runner.RemoteRun`)

- **Out:** a work order (the brief and scope, settings, session grants, auto-pilot, the model) and
  only the ticket's `writes` and `reads` files.
- **The brief** says the machine's OS and shell and that only the ticket's files are there
  (`compose_brief`, from the `runs_on` the job sets), not that the whole project may be read.
- **During:** tool calls and notices render into the worker's pane; approvals are asked there (or
  refused under auto-pilot); relayed model calls are answered by a per-run worker-role provider
  (`relay.answer_model_request`; `RelayProvider` is the remote end); grants flow both ways
  (`_GrantRelay` registered with the session's grants, `ReportingGrants` on the remote); `f3`
  reaches it live. A colleague's finished `reads` file is pushed mid-round (`RemoteRun.push`).
- **Back:** the report and the worker's own files, applied locally — only files it owns.
- **After the round:** `check()` pushes the final `writes` + `reads`, then runs the command in the
  workspace, with `overrides` swapped in and put back — `supervisor.recheck(run_check_for=)` and
  `review_worker(run_check=)` → `expect_red(run_check=)` use it for remote tickets.

## In the Flock

- Staged planning (`run._drive` → `run._StagedRounds`) opens a `pool.RemotePool` (existing pairings
  only, never prompting) for the flock's length; a charter from the one-prompt planner or the chat
  (`run._drive_single`) opens one only when a worker has `runs_on`, and puts an OS no remote offers
  to the user the same way (`_charter_static_or_stop`). `add_worker` and the TOML take `runs_on`
  (`charter.runs_on_os`: a family, empty for this machine's OS).
- `Stager.machine()` adds each remote's facts to every planning prompt; tickets may carry
  `- runs on: <OS>`, kept through the restatement (`_keep_requirements`).
- `tickets.check_tickets(which_for=)`: a remote ticket's `accept` and `requires` programs are looked
  up on its remote in one `which` request that also asks for every `TOOLCHAINS` name (for the refusal),
  the command read Windows' way for a Windows remote (`policy.command_segments(windows=)`); for
  an OS with no remote, `UNCHECKABLE` skips the lookup. `check_requirements(remotes=)` runs a remote
  ticket's requirement checks there.
- `run._static_or_stop`: tickets for an OS no remote offers are put to the user; going on makes them
  `static` (no `accept`, no review stub pass, `WorkerReport.complete` when written, described
  "written, not verified"); declining stops with `stopped_at="remote"`.
- `supervisor.run_flock(remotes=)`: a `runs_on` ticket takes the first idle remote of its OS instead
  of a slot (`RemotePool.acquire`, polling on the heartbeat, event `waiting_remote` → pane "Waiting
  for available worker"); one task per remote, outside `concurrency`. Remote runs end and are
  released after review.

## Windows

The worker side runs on Windows. Every command CoBirb starts gets `stdin=DEVNULL`: the job reads
its orders from a stdin pipe, and a child holding it blocks at start-up on Windows.
`policy._tokenize` reads commands with non-POSIX `shlex` there (backslash paths, `.\test.exe`,
quotes taken off), the shell tool and `run_verification` start
commands in their own process group and end the whole tree with `taskkill /T`, and the job's shell is
`cmd.exe`. There is no sandbox on Windows; the machine itself is the containment.
