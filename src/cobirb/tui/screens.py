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

from typing import TYPE_CHECKING, Any, Callable, ClassVar, Optional, cast

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, OptionList, Static
from textual.widgets.option_list import Option

from .. import memory
from ..plugins.core import render

if TYPE_CHECKING:
    from .app import CoBirbApp

_SEPARATOR_ID = "__separator__"


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

    def __init__(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        scope: str | None = None,
        preview: str = "",
    ) -> None:
        super().__init__()
        self._tool_name = tool_name
        self._arguments = arguments
        # The change this call would make, where the tool can say in advance
        # (a diff for an edit, the patch for apply_patch). Approving a write
        # you have not seen is approving the tool, not the change — and the
        # change is the thing that matters.
        self._preview = preview
        # What "always" would actually grant (Policy.describe_grant). Shown
        # on the Always button and spelled out in the body, because approving
        # a read widens access to a whole directory tree — agreeing to that
        # from a dialog that only named one file would be agreeing blind.
        self._scope = scope

    def compose(self) -> ComposeResult:
        args = self._arguments
        detail = args.get("command") or args.get("path") or args.get("pattern") or ""
        body = Text()
        body.append("Allow ", style="bold")
        body.append(self._tool_name, style="bold yellow")
        if detail:
            body.append(f"\n{detail}", style="dim")
        body.append("\n\nThis tool is not yet permitted for this session.", style="dim")
        if self._scope:
            body.append("\nAlways will ", style="dim")
            body.append(self._scope, style="bold")
            body.append(" for the rest of this session.", style="dim")
        with VerticalScroll(id="approval-dialog"):
            yield Static(body, id="approval-body")
            if self._preview:
                yield Static(
                    render.build_preview_panel(self._tool_name, self._preview),
                    id="approval-preview",
                )
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


class PickerModal(ModalScreen[Optional[str]]):
    """Choose one name from a list, with the current one marked.

    Shared by ``/model`` and ``/persona`` so the two feel like the same
    command with a different noun — picking a persona by arrowing through a
    list is the same job as picking a model, and there is no reason for one
    to be a menu and the other a name you have to already know how to spell.

    Dismisses with the chosen name, or ``None`` on escape/cancel — a cancel
    changes nothing, it never clears what was already in use.
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel", show=True)]
    TITLE_TEXT = "Select"

    def __init__(self, choices: list[str], current: str | None = None) -> None:
        super().__init__()
        self._choices = choices
        self._current = current

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="model-dialog"):
            yield Static(self.TITLE_TEXT, id="model-title")
            options = [
                Option(f"{'> ' if name == self._current else '  '}{name}", id=name)
                for name in self._choices
            ]
            yield OptionList(*options, id="model-options")

    def on_mount(self) -> None:
        self.query_one("#model-options", OptionList).focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self.dismiss(event.option_id)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ModelPickerModal(PickerModal):
    """Choose a model from the list the configured endpoint reported.

    Opened by ``/model`` and by the startup model check (``CoBirbApp``) when
    no configured model turns out to be usable.
    """

    TITLE_TEXT = "Select a model"


class PersonaPickerModal(PickerModal):
    """Choose a persona, including "none" — which is the default.

    Personas are opt-in (see ``plugins.core.persona``), so this list always
    offers the way back out of one as its first entry rather than only
    offering costumes to swap between.
    """

    TITLE_TEXT = "Select a persona"


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


class ConfirmModal(ModalScreen[bool]):
    """A yes/no question, for decisions that are not tool approvals.

    Distinct from ``ApprovalModal`` on purpose. That one answers a
    three-valued permission question and is bound to ``y``/``a``/``n``; this
    one answers "are you sure?" for the Flock — approving a charter, carrying
    on past an overlapping partition, interrupting a run in progress.

    Fails closed like everything else: escape means no. The Flock's charter
    approval is the single place a person sees what the Worker Birbs will be
    allowed to touch, so a dialog that could be dismissed into a *yes* would
    be the one bug worth avoiding above all others here.
    """

    # Two actions rather than one taking a parameter. Textual parses action
    # arguments out of a string, and a yes/no dialog guarding a permission
    # decision is the last place to rely on that round-tripping a bool.
    BINDINGS = [
        Binding("y", "yes", "Yes", show=True),
        Binding("n", "no", "No", show=True),
        Binding("escape", "no", "No", show=False),
    ]

    def __init__(self, question: str, detail: str = "", confirm_label: str = "Yes") -> None:
        super().__init__()
        self._question = question
        self._detail = detail
        self._confirm_label = confirm_label

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="confirm-box"):
            yield Static(Text(self._question, style="bold"), id="confirm-question")
            if self._detail:
                yield Static(Text(self._detail), id="confirm-detail")
            with Horizontal(id="confirm-actions"):
                yield Button(self._confirm_label, id="confirm-yes", variant="primary")
                yield Button("No", id="confirm-no")

    def action_yes(self) -> None:
        self.dismiss(True)

    def action_no(self) -> None:
        self.dismiss(False)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.dismiss(event.button.id == "confirm-yes")


# --------------------------------------------------------------------------- #
# Memory catalogues
# --------------------------------------------------------------------------- #
def _ordered_catalogue_options(
    rows: "list[memory.CatalogueFile]", loaded: "dict[str, memory.MemoryCatalogue]"
) -> list[Option]:
    """Loaded catalogues first, a disabled separator, then the rest —
    alphabetical within each group. Shared by ``/memories`` and the
    ``/remember`` picker so the two can never present a different order for
    the same state.
    """
    loaded_rows = sorted((r for r in rows if r.name in loaded), key=lambda r: r.name)
    other_rows = sorted((r for r in rows if r.name not in loaded), key=lambda r: r.name)
    options = []
    for row in loaded_rows:
        marker = "\U0001f513 " if row.encrypted else "  "  # 🔓 loaded-and-unlocked
        options.append(Option(f"{marker}{row.name}", id=row.name))
    if loaded_rows and other_rows:
        options.append(Option("─" * 24, id=_SEPARATOR_ID, disabled=True))
    for row in other_rows:
        marker = "\U0001f512 " if row.encrypted else "  "  # 🔒 needs a password
        options.append(Option(f"{marker}{row.name}", id=row.name))
    return options


class NewCatalogueModal(ModalScreen[Optional[tuple[str, str]]]):
    """Name + password + confirm-password. Dismisses with ``(name,
    password)`` — ``password`` is ``""`` for an unencrypted catalogue — or
    ``None`` on cancel. Mismatched non-blank passwords are an inline error,
    not a dismiss.
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel", show=True)]

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="new-catalogue-dialog"):
            yield Static("New memory catalogue", id="new-catalogue-title")
            yield Static("Name:", id="new-catalogue-name-label")
            yield Input(id="new-catalogue-name")
            yield Static("Password (optional):", id="new-catalogue-password-label")
            yield Input(password=True, id="new-catalogue-password")
            yield Static("Confirm password:", id="new-catalogue-confirm-label")
            yield Input(password=True, id="new-catalogue-confirm")
            yield Static(
                "Leave both blank to create an unencrypted catalogue.",
                id="new-catalogue-note",
            )
            yield Static("", id="new-catalogue-error")
            with Horizontal(id="new-catalogue-buttons"):
                yield Button("Create", variant="primary", id="new-catalogue-create")
                yield Button("Cancel", id="new-catalogue-cancel")

    def on_mount(self) -> None:
        self.query_one("#new-catalogue-name", Input).focus()

    def _submit(self) -> None:
        name = self.query_one("#new-catalogue-name", Input).value.strip()
        password = self.query_one("#new-catalogue-password", Input).value
        confirm = self.query_one("#new-catalogue-confirm", Input).value
        error = self.query_one("#new-catalogue-error", Static)
        if not name:
            error.update("A name is required.")
            return
        if password != confirm:
            error.update("Passwords do not match.")
            return
        self.dismiss((name, password))

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self._submit()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "new-catalogue-create":
            self._submit()
        else:
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class _CataloguePickerModal(ModalScreen[None]):
    """What ``/memories`` and ``/remember`` both are: a catalogue list that
    can unlock what it lists.

    Subclasses supply the two widget ids and their own layout; everything
    below — reading the catalogues, rendering them loaded-first, showing an
    inline error, and the unlock-then-continue dance — is the same job in
    both, and belongs in one place rather than two.

    Every file and crypto operation is delegated to ``CoBirbApp`` (the same
    ``cast("CoBirbApp", self.app)`` pattern the Plugins pane already uses),
    so these screens stay presentation and nothing else.
    """

    OPTIONS_ID: ClassVar[str] = ""
    ERROR_ID: ClassVar[str] = ""

    def _app(self) -> "CoBirbApp":
        return cast("CoBirbApp", self.app)

    def on_mount(self) -> None:
        self._refresh()
        self.query_one(self.OPTIONS_ID, OptionList).focus()

    def _refresh(self, error: str = "") -> None:
        app = self._app()
        options = self.query_one(self.OPTIONS_ID, OptionList)
        options.clear_options()
        for option in _ordered_catalogue_options(app.memory_catalogue_rows(), app.catalogues.loaded):
            options.add_option(option)
        self.query_one(self.ERROR_ID, Static).update(error)

    def _row_for(self, name: str) -> "memory.CatalogueFile | None":
        return next((r for r in self._app().memory_catalogue_rows() if r.name == name), None)

    def _chosen_name(self, event: OptionList.OptionSelected) -> "str | None":
        """The catalogue a selection names, or ``None`` for the separator."""
        name = event.option_id
        return None if not name or name == _SEPARATOR_ID else name

    def _unlock_then(self, row: "memory.CatalogueFile", then: "Callable[[], None]") -> None:
        """Ask for ``row``'s password, load it, and continue — or re-show
        this picker with the error, which is what keeps a mistyped password
        from costing whatever the user had already typed."""

        def unlock(password: "str | None") -> None:
            if not password:
                return
            error = self._app().memory_load(row, password)
            if error:
                self._refresh(error)
            else:
                then()

        self.app.push_screen(
            TextPromptModal("Unlock catalogue", f"Password for '{row.name}':", password=True), unlock
        )


class MemoryCataloguesModal(_CataloguePickerModal):
    """``/memories``: load, unload, rename, delete, or create catalogues.

    One dialog stays open across several actions rather than reopening per
    operation.
    """

    BINDINGS = [Binding("escape", "close", "Close", show=True)]
    OPTIONS_ID = "#memory-options"
    ERROR_ID = "#memory-error"

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="memory-dialog"):
            yield Static("Memory catalogues", id="memory-title")
            yield Static(
                "Select a catalogue to load it, or select a loaded one to unload it.",
                id="memory-hint",
            )
            yield OptionList(id="memory-options")
            yield Static("", id="memory-error")
            with Horizontal(id="memory-buttons"):
                yield Button("New…", id="memory-new")
                yield Button("Rename", id="memory-rename")
                yield Button("Delete", id="memory-delete", variant="error")
                yield Button("Close", id="memory-close")

    def _highlighted_name(self) -> "str | None":
        options = self.query_one("#memory-options", OptionList)
        if options.highlighted is None:
            return None
        option_id = options.get_option_at_index(options.highlighted).id
        return None if option_id == _SEPARATOR_ID else option_id

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        """Selecting a catalogue toggles it: loaded ones unload, the rest
        load (asking for a password first when they need one)."""
        event.stop()
        name = self._chosen_name(event)
        if name is None:
            return
        app = self._app()
        if name in app.catalogues.loaded:
            app.memory_unload(name)
            self._refresh()
            return
        row = self._row_for(name)
        if row is None:
            return
        if row.encrypted:
            self._unlock_then(row, self._refresh)
        else:
            self._refresh(app.memory_load(row, None))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        button_id = event.button.id
        if button_id == "memory-close":
            self.dismiss(None)
            return
        if button_id == "memory-new":
            self.app.push_screen(NewCatalogueModal(), self._on_new)
            return
        name = self._highlighted_name()
        if name is None:
            self._refresh("Select a catalogue first.")
            return
        if button_id == "memory-delete":
            self.app.push_screen(
                ConfirmModal(f"Delete '{name}'?", "This cannot be undone.", confirm_label="Delete"),
                lambda yes: self._on_delete(name, yes),
            )
        elif button_id == "memory-rename":
            self.app.push_screen(
                TextPromptModal("Rename catalogue", "New name:", name), lambda new: self._on_rename(name, new)
            )

    def _on_new(self, result: "tuple[str, str] | None") -> None:
        if result is None:
            return
        name, password = result
        error = self._app().memory_create(name, password)
        self._refresh(error)

    def _on_delete(self, name: str, confirmed: bool) -> None:
        if confirmed:
            self._refresh(self._app().memory_delete(name))
        else:
            self._refresh()

    def _on_rename(self, name: str, new_name: "str | None") -> None:
        if new_name and new_name != name:
            self._refresh(self._app().memory_rename(name, new_name))
        else:
            self._refresh()

    def action_close(self) -> None:
        self.dismiss(None)


class RememberModal(_CataloguePickerModal):
    """``/remember <fact>``: pick which catalogue to save ``fact`` into.

    Selecting a locked-and-unloaded catalogue prompts for its password right
    there rather than requiring a separate ``/memories`` load first; a wrong
    password re-shows this same picker without losing the fact text.
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel", show=True)]
    OPTIONS_ID = "#remember-options"
    ERROR_ID = "#remember-error"

    def __init__(self, fact: str) -> None:
        super().__init__()
        self._fact = fact

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="remember-dialog"):
            yield Static("Remember this?", id="remember-title")
            yield Static(Text(self._fact), id="remember-fact")
            yield Static("Choose a catalogue to save it in:", id="remember-hint")
            yield OptionList(id="remember-options")
            yield Static("", id="remember-error")

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        """Save into the chosen catalogue, opening it first if it isn't
        already — which for a locked one means asking for its password."""
        event.stop()
        name = self._chosen_name(event)
        if name is None:
            return
        app = self._app()
        if name in app.catalogues.loaded:
            self._save_and_close(name)
            return
        row = self._row_for(name)
        if row is None:
            return
        if row.encrypted:
            self._unlock_then(row, lambda: self._save_and_close(name))
            return
        error = app.memory_load(row, None)
        if error:
            self._refresh(error)
        else:
            self._save_and_close(name)

    def _save_and_close(self, name: str) -> None:
        self._app().memory_remember(name, self._fact)
        self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)
