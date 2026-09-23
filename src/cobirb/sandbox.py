"""Where an approved shell command runs: contained, or not at all differently.

The permission policy decides *whether* a command runs; it never governed what
the command could reach once it did — an approved ``npm test`` could read
``~/.ssh`` and open a socket like any program. The sandbox is the other half:

- the whole filesystem is **read-only**, except the project and a private
  ``/tmp``;
- there is **no network** — its own network namespace, with nothing in it;
- its own **process and IPC namespaces**, so it cannot see or signal anything
  outside;
- **credential directories are hidden** (``~/.ssh``, ``~/.gnupg``, cloud CLI
  configs, CoBirb's own home), because a command that runs without asking must
  not be able to read a secret into the model's context either.

A network-less sandbox is what turns "an approved command cannot exfiltrate"
from something hoped for into something enforced, which is the whole point for
a privacy tool. It is also what makes it reasonable to run a command *without*
asking (``mode = "auto"``): nothing inside can reach further than the project.

Backed by bubblewrap when it is installed. Without it, commands run as they
always have — unsandboxed and asked about — and ``cobirb doctor`` says so.
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field

MODE_ASK = "ask"      # sandboxed, and still asked about
MODE_AUTO = "auto"    # sandboxed, and not asked about
MODE_OFF = "off"      # no sandbox: the pre-0.34 behaviour
MODES = (MODE_ASK, MODE_AUTO, MODE_OFF)
# Auto by default — but a *default* auto only takes effect where every turn is
# checkpointed whole, so what a command changes can be undone (see
# wiring.attach_sandbox). A mode the user set is honoured as written.
DEFAULT_MODE = MODE_AUTO

# Relative to the home directory. Hidden only when they exist; directories are
# covered by an empty tmpfs, files by /dev/null.
DEFAULT_HIDDEN = (
    ".ssh", ".gnupg", ".aws", ".azure", ".config/gcloud", ".kube", ".docker",
    ".netrc", ".git-credentials", ".password-store", ".local/share/keyrings",
    ".pypirc", ".npmrc", ".cobirb",
)


@dataclass
class Sandbox:
    """How shell commands are contained for one session."""

    mode: str = DEFAULT_MODE
    project: str = "."
    hidden: list[str] = field(default_factory=list)
    bwrap: str | None = None
    explicit: bool = False  # whether the mode came from the user's config

    @property
    def active(self) -> bool:
        """Whether commands are actually contained."""
        return self.mode != MODE_OFF and self.bwrap is not None

    @property
    def auto_approve(self) -> bool:
        """Whether a contained command may run without asking."""
        return self.active and self.mode == MODE_AUTO

    def describe(self) -> str:
        if self.mode == MODE_OFF:
            return "off"
        if not self.active:
            return "unavailable (install bubblewrap)"
        return f"bubblewrap, {'runs without asking' if self.auto_approve else 'asks first'}"

    def argv(self, command: str, cwd: str) -> list[str]:
        """The command line that runs ``command`` inside the sandbox."""
        assert self.bwrap is not None
        project = os.path.realpath(self.project)
        args = [
            self.bwrap,
            "--ro-bind", "/", "/",
            "--dev", "/dev",
            "--proc", "/proc",
            "--tmpfs", "/tmp",
            "--bind", project, project,
        ]
        # By real path: a hidden entry is often a symlink (on WSL, ~/.aws points
        # into the Windows drive), and bubblewrap resolves it inside the new
        # root, where masking the link itself fails.
        for path in dict.fromkeys(os.path.realpath(p) for p in self.hidden):
            if project == path or project.startswith(path + os.sep):
                continue  # masking it would hide the project itself
            if os.path.isdir(path):
                args += ["--tmpfs", path]
            elif os.path.exists(path):
                args += ["--ro-bind", "/dev/null", path]
        args += [
            "--unshare-net", "--unshare-pid", "--unshare-ipc", "--unshare-uts",
            "--die-with-parent", "--new-session",
            "--chdir", os.path.realpath(cwd),
            "--", "/bin/sh", "-c", command,
        ]
        return args

    def note(self) -> str:
        """What a sandboxed command's result says about where it ran, so a
        model whose command failed for lack of network knows why."""
        return "(sandboxed: no network; writable only inside the project and /tmp)"


def from_config(value: object, project: str, extra_hidden: object = None) -> Sandbox:
    """Build the session's sandbox from the ``sandbox`` config value.

    ``value`` is a mode string, or ``{"mode": ..., "hide": [...]}``. Anything
    unreadable is the default mode rather than an error.
    """
    mode, hide, explicit = DEFAULT_MODE, extra_hidden, False
    if isinstance(value, str):
        mode, explicit = value, True
    elif isinstance(value, dict):
        explicit = "mode" in value
        mode = str(value.get("mode", DEFAULT_MODE))
        hide = value.get("hide", hide)
    mode = mode.strip().lower() if isinstance(mode, str) else DEFAULT_MODE
    if mode not in MODES:
        mode = DEFAULT_MODE
    home = os.path.expanduser("~")
    hidden = [os.path.join(home, rel) for rel in DEFAULT_HIDDEN]
    # CoBirb's own home wherever it actually is — COBIRB_HOME can move it
    # away from ~/.cobirb, and sessions, memory and the audit log live there.
    from . import paths

    hidden.append(paths.cobirb_dir())
    for path in hide if isinstance(hide, list) else []:
        hidden.append(os.path.abspath(os.path.expanduser(str(path))))
    return Sandbox(mode=mode, project=project, hidden=hidden, bwrap=find_bwrap(), explicit=explicit)


_PROBED: dict[str, bool] = {}


def find_bwrap() -> str | None:
    """The bubblewrap binary, if one is installed *and works here*.

    Installed is not enough: without unprivileged user namespaces (some
    distributions and containers disable them) every command would fail
    inside it. So it is tried once, with the same isolation the real commands
    get, and remembered for the process.
    """
    path = shutil.which("bwrap")
    if path is None:
        return None
    if path not in _PROBED:
        import subprocess

        try:
            probe = subprocess.run(
                [path, "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
                 "--unshare-net", "--unshare-pid", "--die-with-parent", "true"],
                capture_output=True, timeout=10,
            )
            _PROBED[path] = probe.returncode == 0
        except (OSError, subprocess.SubprocessError):
            _PROBED[path] = False
    return path if _PROBED[path] else None
