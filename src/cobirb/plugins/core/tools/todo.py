"""``todo``: the checklist a model keeps for a multi-step task."""

from __future__ import annotations

from typing import Any

from ....typing.spi import ToolResult
from .base import CobirbTool


class TodoTool(CobirbTool):
    """A checklist the model keeps for a multi-step task.

    The whole list is sent on every call rather than edited item by item: one
    shape to get right, which matters for small models, and no way for the
    list to drift out of step with what the model thinks it says. It changes
    nothing on disk and reaches nothing, which is why it is permitted outright
    (``wiring``) — the same considered exception the charter tools are.
    """

    NAME = "todo"
    _STATUSES = ("pending", "in_progress", "done")
    _MARKS = {"pending": "[ ]", "in_progress": "[>]", "done": "[x]"}

    def __init__(self, cwd: str | None = None) -> None:
        super().__init__(cwd)
        self.items: list[dict[str, str]] = []

    def description(self) -> str:
        return (
            "Keep a checklist for a task with several steps, and keep it current: send the whole "
            "list every time, each item marked pending, in_progress or done. The user sees it. "
            "Changes nothing on disk."
        )

    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "description": "The full checklist, in order.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "text": {"type": "string"},
                            "status": {"type": "string", "enum": list(self._STATUSES)},
                        },
                        "required": ["text"],
                    },
                },
            },
            "required": ["items"],
        }

    def progress(self) -> tuple[int, int]:
        """``(done, total)``."""
        return sum(1 for item in self.items if item["status"] == "done"), len(self.items)

    def execute(self, arguments: dict[str, Any]) -> ToolResult:
        raw = arguments.get("items")
        if isinstance(raw, str):
            raw = [line for line in raw.splitlines() if line.strip()]
        if not isinstance(raw, list):
            return ToolResult(
                ok=False, error="bad_items", content="items must be a list of {text, status} entries."
            )
        items = []
        for entry in raw:
            if isinstance(entry, str):
                entry = {"text": entry}
            if not isinstance(entry, dict) or not str(entry.get("text", "")).strip():
                continue
            status = str(entry.get("status", "pending")).strip().lower().replace(" ", "_")
            items.append(
                {
                    "text": str(entry["text"]).strip(),
                    "status": status if status in self._STATUSES else "pending",
                }
            )
        self.items = items[:50]
        done, total = self.progress()
        lines = [f"{self._MARKS[item['status']]} {item['text']}" for item in self.items]
        return ToolResult(ok=True, content=f"Checklist ({done}/{total} done):\n" + "\n".join(lines))
