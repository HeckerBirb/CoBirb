"""The contributor docs point at things that exist.

`AGENTS.md` holds the rules and `docs/architecture/` how each part works; the
layout table in `AGENTS.md` is how an agent finds the page to read and update.
A link or a path that no longer resolves sends it nowhere, so these fail
instead of rotting quietly.
"""

from __future__ import annotations

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
ARCHITECTURE = ROOT / "docs" / "architecture"
DOCS = [ROOT / "AGENTS.md", *sorted(ARCHITECTURE.glob("*.md"))]


def _links(path: pathlib.Path) -> list[str]:
    return [
        link
        for link in re.findall(r"\]\(([^)#\s]+)(?:#[^)]*)?\)", path.read_text())
        if not link.startswith(("http://", "https://", "mailto:"))
    ]


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.name)
def test_every_relative_link_resolves(doc):
    missing = [link for link in _links(doc) if not (doc.parent / link).exists()]

    assert missing == []


def test_every_architecture_page_is_in_the_index():
    indexed = set(_links(ARCHITECTURE / "README.md"))
    pages = {p.name for p in ARCHITECTURE.glob("*.md") if p.name != "README.md"}

    assert pages <= indexed


def test_every_path_in_the_layout_table_exists():
    text = (ROOT / "AGENTS.md").read_text()
    table = text[text.index("## 4. Layout") : text.index("## 5.")]
    missing = []
    for row in table.splitlines():
        if not row.startswith("| `"):
            continue
        cell = row.split("|")[1]
        for path in re.findall(r"`([^`]+)`", cell):
            if not ((ROOT / "src" / "cobirb" / path).exists() or (ROOT / path).exists()):
                missing.append(path)

    assert missing == []
