"""Modal screens: tool approval, help, model selection, and text/password
entry (used by the Sessions tab).

``ApprovalModal`` is the TUI's replacement for ``TerminalIO``'s
``input("Allow 'shell'? [y]es / [a]lways / [N]o: ")`` prompt. It dismisses
with the literal string ``I_OAdapter.confirm()`` is contracted to return
(``"once"``/``"always"``/``"deny"``), so nothing between here and
``Orchestrator._execute_tool`` has to translate anything — and, like the
terminal prompt, it fails closed: escape means deny.
"""
from __future__ import annotations

from typing import Any, Optional

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, OptionList, Static
from textual.widgets.option_list import Option


class ApprovalModal(ModalScreen[str]):
    """Ask whether to allow a tool call the policy hasn't already permitted.

    Permission is default-deny, so this exists to make "denied" mean "asks
    first" rather than "the model silently never finds out it could have
    worked". Every exit path resolves to one of the three contract strings.
    """

    BINDINGS = [
        Binding("y", "decide('once')", "Once", show=True),
        Binding("a", "decide('always')", "Always", show=True),
        Binding("n", "decide('deny')", "Deny", show=True),
        Binding("escape", "decide('deny')", "Deny", show=False),
    ]

    def __init__(self, tool_name: str, arguments: dict[str, Any]) -> None:
        super().__init__()
        self._tool_name = tool_name
        self._arguments = arguments

    def compose(self) -> ComposeResult:
        detail = self._arguments.get("command") or self._arguments.get("path") or ""
        body = Text()
        body.append("Allow ", style="bold")
        body.append(self._tool_name, style="bold yellow")
        if detail:
            body.append(f"\n{detail}", style="dim")
        body.append("\n\nThis tool is not yet permitted for this session.", style="dim")
        with VerticalScroll(id="approval-dialog"):
            yield Static(body, id="approval-body")
            with Horizontal(id="approval-buttons"):
                yield Button("Once (y)", variant="primary", id="approve-once")
                yield Button("Always (a)", variant="warning", id="approve-always")
                yield Button("Deny (n)", variant="error", id="approve-deny")

    def action_decide(self, decision: str) -> None:
        self.dismiss(decision)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        # Stopped so it can never bubble past this screen — the App defines
        # no on_button_pressed of its own today, but an unstopped message
        # keeps climbing the DOM regardless, and a future App-level handler
        # (or one added to a widget higher up) would otherwise silently also
        # receive every modal button press. See TextPromptModal's handler
        # below for what happens when a bubbled message *does* land on a
        # same-named handler that wasn't expecting it.
        event.stop()
        self.dismiss(
            {"approve-once": "once", "approve-always": "always"}.get(event.button.id or "", "deny")
        )


class HelpModal(ModalScreen[None]):
    """CoBirb's own ``help`` text, scrollable, dismissed with escape.

    Opened by submitting ``?`` or ``/help`` rather than by a global ``?``
    key binding: the input widget consumes printable keys first, which is
    correct — you must still be able to type "what's a bird?" as a prompt.
    """

    BINDINGS = [
        Binding("escape", "dismiss_help", "Close", show=True),
        Binding("q", "dismiss_help", "Close", show=False),
    ]

    def __init__(self, text: str) -> None:
        super().__init__()
        self._text = text

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="help-dialog"):
            yield Static(Text(self._text), id="help-body")

    def action_dismiss_help(self) -> None:
        self.dismiss(None)


class ModelPickerModal(ModalScreen[Optional[str]]):
    """Choose a model from the list the configured endpoint reported.

    Opened by ``/model`` and by the startup model check (``CoBirbApp``) when
    no configured model turns out to be usable. Dismisses with the chosen
    model name, or ``None`` on escape/cancel — a cancel changes nothing, it
    doesn't clear whatever model was already in use.
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel", show=True)]

    def __init__(self, models: list[str], current: str | None = None) -> None:
        super().__init__()
        self._models = models
        self._current = current

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="model-dialog"):
            yield Static("Select a model", id="model-title")
            options = [
                Option(f"{'> ' if name == self._current else '  '}{name}", id=name)
                for name in self._models
            ]
            yield OptionList(*options, id="model-options")

    def on_mount(self) -> None:
        self.query_one("#model-options", OptionList).focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self.dismiss(event.option_id)

    def action_cancel(self) -> None:
        self.dismiss(None)


class TextPromptModal(ModalScreen[Optional[str]]):
    """A single-line text (or masked password) prompt.

    Used by the Sessions tab wherever it needs to ask something a plain
    ``getpass.getpass()``/``input()`` can't reach — a full-screen Textual app
    owns the terminal, so this is interactive mode's only way to collect a
    session password or a new session's name. Dismisses with the entered
    text, or ``None`` on escape/cancel (never ``""`` — an empty submission
    also cancels, since neither an empty name nor an empty password is ever
    what's wanted here).
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel", show=True)]

    def __init__(self, title: str, label: str, default: str = "", *, password: bool = False) -> None:
        super().__init__()
        self._title = title
        self._label = label
        self._default = default
        self._password = password

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="prompt-dialog"):
            yield Static(self._title, id="prompt-title")
            yield Static(self._label, id="prompt-label")
            yield Input(value=self._default, password=self._password, id="prompt-value")
            with Horizontal(id="prompt-buttons"):
                yield Button("OK", variant="primary", id="prompt-ok")
                yield Button("Cancel", id="prompt-cancel")

    def on_mount(self) -> None:
        self.query_one("#prompt-value", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        # Critical: an unstopped Input.Submitted keeps bubbling past this
        # screen once dismissed — including all the way up to the App,
        # which has its *own* on_input_submitted for the chat box and
        # cannot tell this one apart from a real message. Without this
        # stop(), pressing enter here after typing a session name or
        # password also submits that same text as a chat prompt.
        event.stop()
        self.dismiss(event.value or None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "prompt-ok":
            self.dismiss(self.query_one("#prompt-value", Input).value or None)
        else:
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)
