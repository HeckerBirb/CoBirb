# Security Policy

CoBirb makes strong privacy and safety claims — no telemetry, no outbound network by default,
encrypted sessions, credential redaction — and takes reports that one of those claims doesn't
hold seriously.

## Supported versions

CoBirb is pre-1.0 and moves quickly. Only the **latest tagged release** is supported with
security fixes; please upgrade (`cobirb --upgrade`) and confirm the issue still reproduces before
reporting.

## Reporting a vulnerability

**Please don't open a public issue for a security report.** Use GitHub's private reporting
instead: go to the [Security tab](https://github.com/HeckerBirb/CoBirb/security) → **Report a
vulnerability**. This opens a private advisory only you and the maintainer can see, so the
issue isn't public before there's a fix.

Include what you'd include in any bug report — the output of `cobirb doctor`, the version, and
clear reproduction steps — plus what the impact is (what an attacker gains, and what they need
in order to get there).

This is a small, largely solo-maintained project — there's no formal SLA, but security reports
get priority over everything else in the queue, and you'll get an acknowledgement as soon as it's
seen.

## Scope

In scope, roughly in order of how much it matters:

- Credential/secret redaction (`redaction.py`) failing to strip a private key, token or similar
  from tool output before it reaches the model, the session file, or the audit log.
- Session encryption (`AesGcmScryptSessionCrypto`) — a flaw in the scheme itself, key handling, or
  a way to read an encrypted session without the password.
- The permission system (`policy.py`) letting a tool run, or a shell command execute, without the
  approval it should have needed — or a way to escape an approved command's scope.
- Path handling under `~/.cobirb` (`paths.py`) that lets a session, config or plugin escape that
  directory.
- A plugin or MCP boundary that grants more trust than documented (e.g. an MCP server unexpectedly
  inheriting your environment or credentials).

## Out of scope

CoBirb is explicit in the README's ["what CoBirb does not protect you
from"](./README.md#disclaimer-what-cobirb-does-not-protect-you-from) section about where its
guarantees end. These are not CoBirb vulnerabilities:

- Anything your model server (Ollama, `llama-server`, LM Studio, vLLM, …) does on its own — CoBirb
  is a client of an OpenAI-compatible endpoint and doesn't run models itself.
- A shell command you approved doing what a command running at your privileges can do. CoBirb
  decides *whether* a command runs, not what it can reach once it does.
- An MCP server or plugin you deliberately installed opening its own network connections or
  sending data elsewhere — installing one is choosing to run someone else's code, and that choice
  is the security decision, documented as such in the README and `cobirb help mcp`.
- Findings from an automated scanner with no demonstrated, concrete impact.

If you're not sure whether something is in scope, report it privately anyway and let's work it
out there rather than in a public issue.
