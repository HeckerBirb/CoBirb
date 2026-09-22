"""Command-line interface for CoBirb.

Three modes:

- **Interactive** (default): a full-screen Textual app (see ``cobirb.tui``)
  with a tab bar, a live status footer and a boxed input.
- **One-shot** (``--prompt``/``-p``): run a single task then exit. Stays a
  plain-stdout, pipeable CLI — a full-screen app can't be scripted.
- **Session** (``--session`` with ``--password``/``-w``): resume/continue an
  encrypted session on disk.

This module parses arguments, prints help, and runs the one-shot mode. The
wiring — plugin slots, building an orchestrator, resolving
a session — lives in ``cobirb.runtime``, because interactive mode needs
exactly the same wiring and neither front-end should reach into the other's
private functions to get it.

"""
from __future__ import annotations

import argparse
import os
import sys
from typing import Any

from .config import Config
from .help_text import HELP_TEXT, HELP_TOPICS
from .orchestrator import REPLY_LABEL, RunStop, render_through
from .policy import PermissionError
from .plugins.core import render
from .runtime import plugins, sessions, wiring
from .runtime.system_prompt import build_system_prompt
from .plugins.core import TerminalIO
from .runtime.custom_commands import describe_commands, discover_commands, expand_custom_command
from .runtime.plugin_install import PluginInstallError, install_plugin, list_installed, remove_plugin
from .runtime import doctor
from .runtime.upgrade import UpgradeError, upgrade
from .runtime.export import write_export
from .flock.run import Asker, run_flock_session
from .runtime.bootstrap import ensure_home
from .runtime.models import describe_roles
from .session import SessionManager, fork_session
from .runtime.headless import EXIT_DENIED, EXIT_ERROR, EXIT_OK, HeadlessIO, HeadlessResult, describe_context
from .runtime.plugins import describe_plugins
# Re-exported, not used here: tui.panes annotates with PluginsSummary and
# test_render imports both through this module.
from .runtime.plugins import PluginsSummary, ToolInfo  # noqa: F401


def _resolve_harness_prompt(cli_value: str | None, config: Config) -> bool:
    """Resolve whether CoBirb sends its own harness block:
    ``--system-prompt {off,harness}`` overrides the ``"system_prompt"`` config
    key, and with neither given it is **off** — CoBirb sends no system message
    of its own and the model's Modelfile ``SYSTEM`` applies untouched.
    """
    value = cli_value if cli_value is not None else config.get("system_prompt", default="off")
    return str(value).strip().lower() == "harness"


def _resolve_plan_mode(cli_value: str | None, config: Config) -> bool:
    """Resolve whether plan mode starts on: ``--plan-mode {on,off}`` (CLI)
    overrides the ``"plan_mode"`` boolean in config; with neither given, it
    defaults off. Either way it can still be flipped mid-conversation with
    ``/plan on``/``/plan off`` (see ``_apply_plan_toggle``).
    """
    if cli_value is not None:
        return cli_value == "on"
    return bool(config.get("plan_mode", default=False))


def _render(text: str) -> None:
    print(text)


def _render_user_prompt(prompt: str) -> None:
    """Echo what the user asked, marked the way interactive mode marks it.

    Goes through a Rich ``Console`` rather than ``_render``'s bare ``print``
    so the ``>`` is coloured and a multi-line prompt is indented under it.
    The two renderers are meant to look identical — see
    ``plugins.core.render``.
    """
    from rich.console import Console

    Console().print(render.build_user_message(prompt))
    print()


def _render_final_answer(orchestrator: Any, label: str, text: str) -> None:
    """Show the model's finished reply.

    Prefers the orchestrator's ``io`` adapter's ``render_answer`` hook when
    it has one (marked, markdown-rendered — see
    ``TerminalIO.render_answer``), falling back to a plain "name: text"
    line for an adapter without it (including test doubles and a bare
    stub orchestrator with no ``io`` attribute at all).
    """
    answer = text or "Completed."

    def plain() -> None:
        _render(f"{label}: {answer}")

    io_adapter = getattr(orchestrator, "io", None)
    if not render_through(io_adapter, "render_answer", label, answer, fallback=plain):
        plain()  # no adapter at all (a bare stub orchestrator)


def _run_one_shot(
    prompt: str,
    system: str,
    allow_overrides: dict[str, str],
    session_path: str | None,
    password: str | None,
    cwd: str,
    model_name: str | None = None,
    plan_mode: bool = False,
    headless: bool = False,
    output: str = "text",
    autopilot: bool = False,
) -> int:
    """Run one prompt and exit.

    ``headless`` refuses every unpermitted call instead of prompting — in a
    pipeline the terminal prompt would block on a question nobody will answer.
    ``output="json"`` replaces the rendered transcript with one machine-
    readable object on stdout, and nothing else goes there.
    """
    as_json = output == "json"
    io_factory = HeadlessIO if headless else TerminalIO
    report = HeadlessResult(ok=False, summary="")

    # Wiring first, echo second: a prompt echoed before the session failed to
    # open reads as though the task was attempted, when nothing ran at all.
    try:
        orchestrator = wiring.build_orchestrator(
            cwd, allow_overrides, session_path, password, model_name,
            io_factory=io_factory,
        )
    except Exception as exc:  # noqa: BLE001 - a bad password must not traceback
        # Chiefly a session that wouldn't decrypt. An unhandled traceback is
        # a terrible way to be told about the commonest cause of that — a
        # mistyped password.
        message = f"could not open {session_path} — {sessions.session_open_error(exc)}"
        if as_json:
            report.error = message
            print(report.to_json())
            return report.exit_code(unattended=headless)
        print(f"cobirb: {message}", file=sys.stderr)
        return EXIT_ERROR

    if autopilot:
        problem = orchestrator.enable_autopilot()
        if problem:
            orchestrator.close()
            message = f"auto-pilot cannot start: {problem}"
            if as_json:
                report.error = message
                print(report.to_json())
            else:
                print(f"cobirb: {message}", file=sys.stderr)
            return EXIT_ERROR
    try:
        return _drive_one_shot(
            orchestrator, prompt, system, session_path, password, cwd,
            plan_mode, headless, as_json,
        )
    finally:
        # Whatever happened, don't leave MCP servers running behind a process
        # that has finished. `finally` rather than a line at the end: the error
        # paths above are exactly when a leaked child is least likely to be
        # noticed.
        orchestrator.close()


def _drive_one_shot(
    orchestrator: Any,
    prompt: str,
    system: str,
    session_path: str | None,
    password: str | None,
    cwd: str,
    plan_mode: bool,
    headless: bool,
    as_json: bool,
) -> int:
    """Run the turn against an already-wired orchestrator and report on it.

    Split from ``_run_one_shot`` only so that wiring's own failure path and
    this one can be told apart: everything here has an orchestrator to shut
    down afterwards, and nothing above it does.
    """
    report = HeadlessResult(ok=False, summary="")
    if not as_json:
        _render_user_prompt(prompt)
    try:
        session = orchestrator.run(
            prompt, system, cwd=cwd, session_path=session_path, plan_mode=plan_mode
        )
    except PermissionError as exc:
        report.error = f"blocked — {exc}"
        if as_json:
            print(report.to_json())
            return report.exit_code(unattended=headless)
        _render(f"{REPLY_LABEL}: blocked — {exc}\n")
        return EXIT_ERROR
    except Exception as exc:  # noqa: BLE001 - surface provider/tool errors cleanly
        report.error = f"could not complete — {exc}"
        if as_json:
            print(report.to_json())
            return report.exit_code(unattended=headless)
        _render(f"{REPLY_LABEL}: could not complete — {exc}\n")
        return EXIT_ERROR

    if session_path is not None and orchestrator.session is not None:
        orchestrator.session.save(password)

    calls = orchestrator.last_run_tool_calls
    stop = getattr(orchestrator, "last_stop", None) or RunStop()
    report = HeadlessResult(
        # A run that stopped short did not do what it was asked. Exit 0 would
        # tell a pipeline it had, which is what the made-up "Stopped after N
        # turns" answer used to do.
        ok=stop.finished,
        error=None if stop.finished else stop.describe(),
        stop_reason=stop.reason,
        summary=session.summary or "",
        validation=session.validation,
        turns=len(session.turns),
        tool_calls=calls,
        denied=[call["name"] for call in calls if call["denied"]],
        session_path=session_path,
        context=describe_context(orchestrator),
    )
    if as_json:
        print(report.to_json())
        return report.exit_code(unattended=headless)

    # If the final answer already streamed live via the I/O adapter, printing
    # session.summary again here would just show it a second time.
    # A stop short of an answer has already been reported by the orchestrator;
    # "Completed." underneath it would contradict it.
    if not orchestrator.last_turn_streamed and stop.finished:
        _render_final_answer(orchestrator, REPLY_LABEL, session.summary)
    return report.exit_code(unattended=headless)


def _run_export(destination: str, session_path: str | None, password: str | None, cwd: str) -> int:
    """Decrypt a session and write it out as markdown."""
    if session_path is None:
        print("cobirb: --export needs --session to say which one.", file=sys.stderr)
        return EXIT_ERROR
    try:
        config = Config()
        _, discovered, _ = plugins.discover_plugins(cwd, config)
        crypto, _ = plugins.build_crypto(config, discovered)
        manager = SessionManager.load(session_path, crypto, password, cwd)
    except Exception as exc:  # noqa: BLE001 - a wrong password is routine
        print(
            f"cobirb: could not open {session_path} — {sessions.session_open_error(exc)}",
            file=sys.stderr,
        )
        return EXIT_ERROR

    written = write_export(manager.session, destination, title=os.path.basename(session_path))
    print(f"Exported {len(manager.session.turns)} turn(s) to {written}")
    print("This file is plaintext — the session it came from stays encrypted.", file=sys.stderr)
    return EXIT_OK


def _run_branch(
    destination: str,
    up_to_turn: int | None,
    session_path: str | None,
    password: str | None,
    cwd: str,
) -> int:
    """Fork the session named by --session into a new file at ``destination``."""
    if session_path is None:
        print("cobirb: --branch needs --session to say which one to fork.", file=sys.stderr)
        return EXIT_ERROR
    try:
        config = Config()
        _, discovered, _ = plugins.discover_plugins(cwd, config)
        crypto, _ = plugins.build_crypto(config, discovered)
        branch = fork_session(
            session_path, crypto, password, up_to_turn=up_to_turn, out_path=destination
        )
    except ValueError as exc:
        print(f"cobirb: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except Exception as exc:  # noqa: BLE001 - a wrong password is routine
        print(
            f"cobirb: could not open {session_path} — {sessions.session_open_error(exc)}",
            file=sys.stderr,
        )
        return EXIT_ERROR

    print(
        f"Branched {len(branch.session.turns)} turn(s) from {session_path} to {branch.path}.\n"
        "The original is untouched. Resume the branch with:\n"
        f"  cobirb --session {sessions.abbreviate_home(branch.path)} -w"
    )
    return EXIT_OK


def _run_tui(
    system: str,
    allow_overrides: dict[str, str],
    session_path: str | None,
    password: str | None,
    cwd: str,
    model_name: str | None = None,
    plan_mode: bool = False,
) -> int:
    """Interactive mode: hand off to the full-screen Textual app.

    ``cobirb.tui`` is imported lazily, here, rather than at module scope.
    Textual is only needed for this one mode, so a missing or broken install
    must not take down ``cobirb -p`` or ``cobirb help`` — modes that never
    touch it — with an import error at the top of this file.
    """
    try:
        from .tui.app import CoBirbApp
    except Exception as exc:  # noqa: BLE001 - a missing optional dep is not a crash
        print(
            f"cobirb: interactive mode needs textual, which could not be loaded ({exc}).\n"
            "Install it with 'pip install textual', or use one-shot mode: cobirb -p \"...\"",
            file=sys.stderr,
        )
        return 1

    app = CoBirbApp(
        system=system,
        allow_overrides=allow_overrides,
        session_path=session_path,
        password=password,
        cwd=cwd,
        model_name=model_name,
        plan_mode=plan_mode,
    )

    # Resuming an existing session file: open it *before* the app starts.
    #
    # Doing this inside the running app meant a wrong password took you all
    # the way in — full-screen UI, a model to pick — only to report the
    # failure into a transcript belonging to a session that had never
    # opened. If a session was asked for and can't be unlocked, there is
    # nothing to interact with, so nothing should start: the error belongs
    # on the terminal you typed the command into, with a non-zero exit.
    #
    # The app object exists by now but hasn't run, so its I/O bridge can
    # already be handed to the orchestrator — which is what keeps this the
    # same single code path that opens a session everywhere else, and means
    # the file is decrypted exactly once.
    if session_path is not None and os.path.isfile(session_path):
        try:
            app.orchestrator = wiring.build_orchestrator(
                cwd,
                allow_overrides,
                session_path,
                password,
                model_name,
                io_factory=lambda: app.io_bridge,
            )
        except Exception as exc:  # noqa: BLE001 - a bad password is routine, not a crash
            print(
                f"cobirb: could not open {session_path} — {sessions.session_open_error(exc)}",
                file=sys.stderr,
            )
            return 1

    app.run()

    # After app.run() returns, never from inside the app: Textual owns the
    # whole screen until then, so anything printed earlier would be painted
    # over and lost. Read back off the app rather than using the argument —
    # the Sessions tab can start or resume a different session mid-run, and
    # the hint has to name the file that was actually written.
    if app.session_path is not None and os.path.isfile(app.session_path):
        print(sessions.resume_hint(app.session_path))
    return 0


def _run_flock(objective: str, cwd: str, model_name: str | None, *, headless: bool) -> int:
    """``cobirb flock -p "..."`` — divide a piece of work between several agents.

    Brainy Birb plans, designs the seams and builds the skeleton, then proposes
    a charter. You approve it once, and the Worker Birbs run unattended inside
    exactly the scopes you saw. One round, then it reports back.

    Refused outright in headless mode. The charter approval is the *only* place
    a person sees what a flock is about to be allowed to do, and a flock that
    approved its own charter would be an agent granting itself permissions —
    precisely what the permission layer exists to prevent.
    """
    if headless:
        print(
            "cobirb: flock mode needs someone to approve the charter, so it cannot run "
            "headless. The charter is the only place you see what the workers will be "
            "allowed to touch.",
            file=sys.stderr,
        )
        return EXIT_ERROR

    try:
        orchestrator = wiring.build_orchestrator(cwd, {}, model_name=model_name)
    except Exception as exc:  # noqa: BLE001
        print(f"cobirb: could not start — {exc}", file=sys.stderr)
        return EXIT_ERROR

    def confirm(question: str, detail: str = "") -> bool:
        if detail:
            print(f"\n{detail}")
        print(f"\n{question} [y/N] ", end="", flush=True)
        try:
            return input().strip().lower() in ("y", "yes")
        except (EOFError, KeyboardInterrupt):
            print()
            return False

    try:
        run = run_flock_session(
            orchestrator, objective, cwd,
            ask=Asker(confirm=confirm, show=lambda text: print(f"\n{text}")),
        )
    except KeyboardInterrupt:
        print("\ncobirb: interrupted.", file=sys.stderr)
        return EXIT_ERROR
    finally:
        orchestrator.close()

    print(f"\n{run.report}")
    if run.stopped_at:
        return EXIT_ERROR if run.stopped_at == "partition" else EXIT_OK
    return EXIT_OK if run.outcome is not None and run.outcome.all_done else EXIT_DENIED


def _run_models(config: Config, override: str | None) -> int:
    """``cobirb models`` — which model plays which part, and why.

    Worth its own subcommand rather than a line in ``help``: role resolution
    has three layers (the role's own key, the default's, and ``--model``), and
    the question people actually have is "so which one runs?" — which is a
    fact about *their* config, not something documentation can answer.
    """
    print("Model roles:\n")
    for spec in describe_roles(config, override):
        print(f"  - {spec.describe()}")
    print()
    return EXIT_OK


def _run_plugin(verb: str | None, target: str | None, *, replace: bool, cwd: str) -> int:
    """``cobirb plugin install|list|remove`` — see 'cobirb help plugin'.

    Deliberately thin: all the actual work (validation, the pip round trip,
    rollback on a failed install) lives in ``runtime.plugin_install``, tested
    on its own. This only turns three verbs and an optional path/name into
    calls on it and reports what happened.

    ``cwd`` matters to ``list`` alone, and only because discovery also looks
    at a project's own ``cobirb/plugins/`` directory — listing what is active
    from the process's directory while the rest of the run works somewhere
    else would quietly answer a different question than the one asked.
    """
    if verb not in ("install", "list", "remove"):
        print(
            "cobirb: usage: cobirb plugin install <path> | list | remove <name>",
            file=sys.stderr,
        )
        return EXIT_ERROR

    if verb == "list":
        from rich.console import Console

        installed = list_installed()
        if not installed:
            print("No plugins installed via 'cobirb plugin install'.")
        else:
            print("Installed via 'cobirb plugin install':")
            for name in installed:
                print(f"  - {name}")
        print()
        print("Everything currently active, including plain pip-installed plugins:")
        Console().print(render.build_plugins_view(describe_plugins(cwd)))
        return EXIT_OK

    if not target:
        noun = "source directory" if verb == "install" else "name"
        print(f"cobirb: 'plugin {verb}' needs a {noun}.", file=sys.stderr)
        return EXIT_ERROR

    try:
        if verb == "install":
            result = install_plugin(target, replace=replace)
        else:
            result = remove_plugin(target)
    except PluginInstallError as exc:
        print(f"cobirb: {exc}", file=sys.stderr)
        return EXIT_ERROR
    print(result.describe())
    return EXIT_OK


def _run_doctor() -> int:
    """``cobirb doctor`` — see 'runtime/doctor.py'.

    Thin, the same way ``_run_upgrade`` is: every check lives in
    ``runtime.doctor``, tested on its own. Exits non-zero only on an actual
    failure — a warning is something worth knowing, not a broken install, and
    a check command that fails the build over "you are one release behind"
    would quickly be a check command nobody runs.
    """
    report = doctor.run()
    print(report.describe())
    return EXIT_OK if report.ok else EXIT_ERROR


def _run_upgrade(tag: str | None, *, force: bool) -> int:
    """``cobirb --upgrade [tag] [--force]`` — see 'runtime/upgrade.py'.

    Thin on purpose, the same way ``_run_plugin`` is: the git/pip round trip,
    the downgrade guard and the dirty-tree refusal all live in
    ``runtime.upgrade``, tested on its own. This only reports what happened.
    """
    try:
        result = upgrade(tag, force=force)
    except UpgradeError as exc:
        print(f"cobirb: {exc}", file=sys.stderr)
        return EXIT_ERROR
    # A managed upgrade describes itself as nothing: the installer it delegated
    # to has already streamed the whole story to this terminal.
    summary = result.describe()
    if summary:
        print(summary)
    return EXIT_OK


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cobirb",
        description="CoBirb — a privacy-first, local agentic CLI. No telemetry, no "
        "outbound network by default, sessions encrypted at rest.",
    )
    parser.add_argument(
        "subcommand",
        nargs="?",
        choices=["help", "models", "commands", "flock", "plugin", "doctor", "setup"],
        help="'setup' to choose your model server and model, "
        "'help' for the overview, 'models' for how each role resolves, "
        "'commands' for the custom commands available here, 'doctor' to check "
        "a piece of work between several agents, 'plugin' to install/list/remove "
        "a local plugin (see 'cobirb help flock'/'cobirb help plugin').",
    )
    parser.add_argument(
        "topic",
        nargs="?",
        # Not constrained to HELP_TOPICS here: with subcommand="plugin" this
        # slot holds the verb (install/list/remove) instead of a help topic,
        # since argparse's `choices` can't depend on another argument's value.
        # main() validates it against the right set once it knows which.
        help=f"With 'help': a specific topic ({', '.join(sorted(HELP_TOPICS))}). "
        "With 'plugin': install, list, or remove.",
    )
    parser.add_argument(
        "target",
        nargs="?",
        help="With 'plugin install': the local plugin source directory. "
        "With 'plugin remove': the plugin's name.",
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help="With 'plugin install': overwrite an already-installed plugin of the same name.",
    )
    parser.add_argument(
        "--doctor",
        action="store_true",
        help=(
            "Check that CoBirb is ready to go — config, endpoint, models, install — then "
            "exit. Same as 'cobirb doctor'. Exits non-zero if something is actually broken."
        ),
    )
    parser.add_argument(
        "--upgrade",
        nargs="?",
        const="",
        default=None,
        metavar="TAG",
        help="Move this install to another release, then exit: the latest by default, "
        "or a specific one ('cobirb --upgrade v0.8.0'). An install.sh install re-runs "
        "that installer; a git clone installed with 'pip install -e .' or 'pipx install "
        "--editable .' moves the checkout. Refuses a downgrade unless --force says so, "
        "and a checkout with uncommitted changes outright.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="With --upgrade: allow moving to a release older than the one currently "
        "running (a downgrade). Meaningless without --upgrade.",
    )

    mode = parser.add_argument_group("modes")
    mode.add_argument("-p", "--prompt", help="One-shot prompt: run once then exit.")
    mode.add_argument("--session", metavar="PATH", help="Resume/continue an encrypted session at PATH.")
    mode.add_argument(
        "--continue",
        dest="continue_last",   # "continue" is a keyword, so it cannot be the attribute name
        action="store_true",
        help=(
            "Reopen the session you were last in, under ~/.cobirb/sessions. Implies "
            "--password: continuing a session means unlocking one, so you are prompted "
            "for it unless '-w <password>' already gave it."
        ),
    )
    mode.add_argument(
        "--password",
        "-w",
        nargs="?",
        const=True,
        metavar="PASSWORD",
        help="Run in an encrypted session, starting a new one under "
        f"{sessions.sessions_dir_display()} if --session names none. Give the password "
        "here ('-w hunter2') or leave it off ('-w') to be prompted without "
        "echo — a password on the command line is visible in shell history "
        "and process listings.",
    )

    opts = parser.add_argument_group("options")
    opts.add_argument(
        "--model", help="Ollama model name, e.g. 'llama3.1' (or use config/COBIRB_MODEL_NAME)."
    )
    opts.add_argument(
        "--allow-tool",
        action="append",
        default=[],
        metavar="SPEC",
        help="Permit a tool up front: 'name' or 'name(arg)' (e.g. "
        "'shell(git),read_file'). Repeatable, and equivalent to the "
        "'allow_tools' config key. Without one, every tool call asks.",
    )
    opts.add_argument(
        "--autopilot",
        action="store_true",
        help="With -p: work unattended — read and change files in the project and run commands "
        "in the sandbox without asking, and refuse anything else. Needs the sandbox and git "
        "(see 'cobirb doctor'). In the interactive app, use /autopilot.",
    )
    opts.add_argument(
        "--plan-mode",
        choices=["on", "off"],
        default=None,
        help="Plan, then act, then validate with references, as 3 separate model "
        "phases (default: off, or the 'plan_mode' config key). Toggle mid-session "
        "with /plan on|off.",
    )
    opts.add_argument(
        "--system-prompt",
        choices=["off", "harness"],
        default=None,
        metavar="{off,harness}",
        help="Whether CoBirb sends a system prompt of its own (default: off, "
        "or the 'system_prompt' config key). Off means no system message is "
        "sent at all, so the SYSTEM directive your model was built with "
        "applies exactly as it does in Ollama. 'harness' adds a short block "
        "on how to work as a coding agent (look first, check your work, keep "
        "going until done, don't retry a denied call). It is added after your "
        "model's own prompt, never instead of it.",
    )
    opts.add_argument(
        "--export",
        metavar="PATH",
        help="Decrypt the session named by --session and write it to PATH as "
        "markdown, then exit. The output is plaintext — that is the point of "
        "exporting — so CoBirb never picks the destination for you.",
    )
    opts.add_argument(
        "--branch",
        metavar="PATH",
        help="Fork the session named by --session into a new, independent "
        "encrypted session at PATH, then exit. The original is left exactly "
        "as it was and stays resumable at its current length; the branch "
        "picks up from there in whatever direction you take it next. "
        "Combine with --branch-at to fork from an earlier point instead of "
        "the session's current end.",
    )
    opts.add_argument(
        "--branch-at",
        type=int,
        metavar="N",
        help="With --branch: keep only turns 0..N (0-indexed, inclusive) "
        "rather than the whole session — branching from an earlier point in "
        "the conversation. Without it, the branch keeps every turn.",
    )
    opts.add_argument(
        "--headless",
        action="store_true",
        help="Never prompt. Anything not already permitted by --allow-tool or "
        "the 'allow_tools' config key is refused outright, so a run can go "
        "unattended in CI. There is deliberately no flag that approves "
        "everything: say what is allowed, in a file someone can review.",
    )
    opts.add_argument(
        "--output",
        choices=["text", "json"],
        default="text",
        help="'json' prints one machine-readable object on stdout and nothing "
        "else — summary, tool calls, what was refused, context usage. Exit "
        "code 0 completed, 1 failed, 2 completed with something refused.",
    )
    opts.add_argument("--cwd", help="Working directory.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    # Resolved once, to an absolute path, rather than passing the literal
    # string "." to every call site below. A bare "." reaches the status bar
    # verbatim (`CoBirb · model · .`) — technically correct, since every path
    # is joined against the real process cwd regardless, but it reads as a
    # bug rather than as "here".
    cwd = os.path.abspath(args.cwd) if args.cwd else os.getcwd()

    if args.subcommand == "help":
        if args.topic and args.topic not in HELP_TOPICS:
            print(
                f"cobirb: no help topic {args.topic!r}. Topics: {', '.join(sorted(HELP_TOPICS))}.",
                file=sys.stderr,
            )
            return EXIT_ERROR
        print(HELP_TOPICS[args.topic] if args.topic else HELP_TEXT)
        return 0

    if args.upgrade is not None:
        return _run_upgrade(args.upgrade or None, force=args.force)
    if args.doctor or args.subcommand == "doctor":
        return _run_doctor()
    if args.subcommand == "setup":
        from .runtime import setup

        return setup.run()
    if args.force:
        # Said rather than ignored — see --branch-at's own check below for
        # why a flag that would otherwise silently do nothing gets a line.
        print("cobirb: --force only means something with --upgrade.", file=sys.stderr)
        return EXIT_ERROR

    # Before the first Config() read, so a new user's very first run leaves a
    # file where the docs say one lives. Reported on stderr rather than
    # silently, since something appearing in your home directory should be
    # something you were told about.
    seeded = ensure_home()
    if seeded:
        print(f"cobirb: created a starter config at {seeded}", file=sys.stderr)

    config = Config()

    if args.subcommand == "models":
        return _run_models(config, args.model)
    if args.subcommand == "commands":
        print(describe_commands(discover_commands(cwd)))
        return EXIT_OK
    if args.subcommand == "flock":
        if not args.prompt:
            print(
                "cobirb flock needs an objective: cobirb flock -p \"add CSV export\"",
                file=sys.stderr,
            )
            return EXIT_ERROR
        return _run_flock(args.prompt, cwd, args.model, headless=args.headless)
    if args.subcommand == "plugin":
        # args.topic holds the verb here, not a help topic — the two share a
        # positional slot; see _build_parser.
        return _run_plugin(
            args.topic, args.target, replace=args.replace, cwd=cwd
        )

    allow_overrides = wiring.parse_allow_tools(args.allow_tool)
    harness = _resolve_harness_prompt(args.system_prompt, config)
    system = build_system_prompt(harness=harness)
    plan_mode = _resolve_plan_mode(args.plan_mode, config)

    # Either flag alone is enough to mean "this is a session": a path always
    # needs a password to encrypt to, and a password with no path gets a new
    # session under ~/.cobirb/sessions rather than being silently ignored.
    try:
        session_path, password = sessions.resolve_session(
            args.session, args.password, continue_last=args.continue_last
        )
    except sessions.NoSessionToContinue as exc:
        print(f"cobirb: {exc}", file=sys.stderr)
        return EXIT_ERROR

    if args.export:
        return _run_export(args.export, session_path, password, cwd)

    if args.branch:
        return _run_branch(args.branch, args.branch_at, session_path, password, cwd)

    # Said rather than ignored: a flag someone typed that quietly does nothing
    # is the same failure as `-w` once being inert without `--session`, which
    # this project has already fixed once (see sessions.resolve_session).
    if args.branch_at is not None:
        print("cobirb: --branch-at only means something with --branch.", file=sys.stderr)
        return EXIT_ERROR
    if args.autopilot and args.prompt is None:
        print("cobirb: --autopilot goes with -p. In the interactive app, use /autopilot.", file=sys.stderr)
        return EXIT_ERROR

    if args.prompt is not None:
        status = _run_one_shot(
            expand_custom_command(args.prompt, cwd),
            system,
            allow_overrides,
            session_path,
            password,
            cwd,
            args.model,
            plan_mode,
            headless=args.headless,
            output=args.output,
            autopilot=args.autopilot,
        )
        # Only on success: the hint is about a session this run actually
        # wrote to. Printing it after a failure ("could not open …" followed
        # by "Session saved") claims something that didn't happen.
        if (
            status == 0
            and args.output != "json"
            and session_path is not None
            and os.path.isfile(session_path)
        ):
            # stderr, not stdout: one-shot mode is meant to pipe, and this
            # hint is for the human, not for whatever is reading the output.
            print(sessions.resume_hint(session_path), file=sys.stderr)
        return status

    return _run_tui(
        system,
        allow_overrides,
        session_path,
        password,
        cwd,
        args.model,
        plan_mode,
    )


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\ncobirb: interrupted.")
        sys.exit(130)
