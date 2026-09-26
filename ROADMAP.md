# Ideas and future features

Things that would be good for CoBirb but are not being worked on. **Nothing here is scheduled** —
this is a list of ideas with the reason each one waits, not a timeline or a promise. An idea moves
out of this file when someone decides to build it, at which point it becomes a change like any other.

For things that were considered and deliberately *rejected*, see §17 of [AGENTS.md](./AGENTS.md).
The difference matters: those were costed and decided against; these are waiting for a reason to
start.

## Self-hosted remote endpoints

TLS, optional authentication headers, and a loud warning for plain HTTP to anything other than
loopback — for people who run their models on a rented GPU or a machine elsewhere on their network.

**Why it waits:** CoBirb is deliberately local-only. This is the one feature that would put its
traffic on a network, and that is a line to cross on purpose, if at all, rather than by accident.
Today `base_url` is whatever the config says, and nothing is done to make a remote address safer.

## A sandbox without bubblewrap (Landlock)

A second sandbox backend built from what a modern Linux kernel already provides: Landlock to confine
where a command may write, plus unprivileged user and network namespaces for the no-network rule —
all through the standard library, with no binary to install. Machines without bubblewrap would then
get contained commands too, instead of falling back to asking about every one.

**Why it waits:** it is security code, and it has to be exactly right; bubblewrap already covers the
machines that have it. Landlock also only grants access rather than denying it, so hiding credential
directories while leaving the rest of the filesystem readable needs a careful design of its own —
without that, this backend would contain writes and network but hide less than bubblewrap does.

## Worker Birbs on other machines

Brainy Birb on one machine, Worker Birbs spawned on others — macOS, Debian, Windows — so a ticket that
can only be built or tested on a particular OS runs where it can. Today every build and check runs on
the machine CoBirb runs on, and work for another OS ends up as a build-only check plus a note in the
flock's report of what is left to do elsewhere.

**Why it waits:** to be discussed. It puts a worker's brief, files and results on a network, which
crosses invariant 3 on purpose; each remote machine needs its own sandbox, checkpoints and way to
return a worker's changes; and the permission model has to say who may grant what on which machine.

## Editor integration over the Agent Client Protocol

A `cobirb acp` mode speaking [ACP](https://zed.dev/acp) — JSON-RPC over stdio — so
the same agent, permission policy and encrypted sessions run inside Zed, JetBrains IDEs, Neovim and
Emacs, without CoBirb writing a plugin per editor. No network is involved.

**Why it waits:** the terminal loop has to be excellent first. An editor integration multiplies
whatever quality the agent already has, including its failures.

## A multi-language repo map with tree-sitter

Symbol outlines and syntax checks for every language at the quality `ast` gives Python today,
replacing the regular expressions used for everything else.

**Why it waits:** it is a native dependency, against the preference for the standard library, and
it is only worth that if measurement shows the repo map matters for non-Python projects.

## Personas

A voice and tone layered over a working agent — if they are ever wanted back, as an optional
plugin that cannot interfere with the instructions that make the agent work.

**Why it waits:** they are irrelevant to what CoBirb is for, and in testing they were seen to
degrade how models behaved.

## Language-server diagnostics

Type and lint errors from a language server fed back to the model after each edit.

**Why it waits:** a syntax check plus the user's own lint command covers most of the benefit with
none of the machinery of running and managing language servers.

## A public benchmark score

Running CoBirb against a recognised public benchmark such as Terminal-Bench, so its reliability can
be compared with other tools on a number people already know.

**Why it waits:** those benchmarks run tasks in containers their own harness builds and manages,
which needs its own privacy review first, and an offline benchmark of CoBirb's own should come before it.
