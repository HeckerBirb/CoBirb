"""A ranked outline of a codebase, so the agent can orient without grepping.

Without this, every session starts blind: the model greps for a name it is
guessing at, reads three wrong files, and spends a third of a small context
window working out where things live. A map of "these are the files that
matter and this is what is in them" is the cheapest quality improvement
available to a weak model.

**Both injected and offered as a tool.** A map is built into the project
context at session start (see `runtime.wiring._project_context`), because
having it from the first turn is most of why this kind of grounding works —
a few thousand tokens of orientation is cheap against the windows CoBirb's
hardware runs. An earlier version made it tool-only, reasoning that a
permanent map would crowd the window; that reasoning came from an assumed
4,096-token budget the target hardware does not have. The tool remains, for a
subtree or a refresh after the layout changes under the agent's own hands.

**Stdlib only.** Python symbols come from `ast`, which is exact and free.
Other languages get a small set of regexes: crude next to tree-sitter, but
tree-sitter means a compiled dependency and a per-language grammar, and the
job here is orientation rather than analysis. A slightly wrong signature in
the outline costs nothing; the file path being right is what matters.
"""
from __future__ import annotations

import ast
import os
import re
from dataclasses import dataclass, field

from .ignores import IgnoreRules

# Extensions worth outlining. Anything else is listed by name only, since a
# path is still orientation even when the contents can't be summarised.
_PYTHON = {".py", ".pyi"}
_PATTERNS: dict[frozenset[str], list[re.Pattern[str]]] = {
    frozenset({".js", ".jsx", ".ts", ".tsx", ".mjs"}): [
        re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+(\w+)", re.M),
        re.compile(r"^\s*(?:export\s+)?class\s+(\w+)", re.M),
        re.compile(r"^\s*(?:export\s+)?(?:const|let)\s+(\w+)\s*=\s*(?:async\s*)?\(", re.M),
    ],
    frozenset({".go"}): [re.compile(r"^func\s+(?:\([^)]*\)\s*)?(\w+)", re.M),
                         re.compile(r"^type\s+(\w+)", re.M)],
    frozenset({".rs"}): [re.compile(r"^\s*(?:pub\s+)?fn\s+(\w+)", re.M),
                         re.compile(r"^\s*(?:pub\s+)?(?:struct|enum|trait)\s+(\w+)", re.M)],
    frozenset({".rb"}): [re.compile(r"^\s*(?:def|class|module)\s+([\w.]+)", re.M)],
    frozenset({".java", ".kt", ".cs"}): [
        re.compile(r"^\s*(?:public|private|protected)?\s*(?:static\s+)?(?:class|interface)\s+(\w+)", re.M),
    ],
    frozenset({".sh", ".bash"}): [re.compile(r"^\s*(?:function\s+)?(\w+)\s*\(\)\s*\{", re.M)],
}

# Files bigger than this are outlined by name and size only. A vendored
# bundle or a generated parser has thousands of symbols and no orientation
# value, and parsing it is pure cost.
_MAX_PARSE_BYTES = 512 * 1024

# The default output budget. Still bounded, because this becomes a session
# turn and a message in every following request — but sized for the window the
# target hardware actually runs rather than a 4,096-token guess. Roughly
# 4,000 tokens: enough to outline a real project.
DEFAULT_BUDGET_CHARS = 16000

_MAX_SYMBOLS_PER_FILE = 24

# Files a reader opens first whatever the import graph says.
_ENTRY_POINT_NAMES = {"__init__", "__main__", "main", "cli", "app", "index", "server", "setup"}


def _is_entry_point(path: str) -> bool:
    return os.path.splitext(os.path.basename(path))[0] in _ENTRY_POINT_NAMES


def _is_test(path: str) -> bool:
    parts = path.replace(os.sep, "/").split("/")
    name = parts[-1]
    return (
        name.startswith("test_")
        or name.endswith(("_test.py", ".test.ts", ".test.js", ".spec.ts", ".spec.js"))
        or "tests" in parts[:-1]
        or "test" in parts[:-1]
    )


@dataclass
class FileOutline:
    path: str            # relative to the map's root
    lines: int
    symbols: list[str] = field(default_factory=list)
    imported_by: int = 0

    @property
    def score(self) -> float:
        """How much this file is worth showing, highest first.

        Being imported by other files leads, because it is the closest thing
        to an objective measure of what a codebase considers central. Entry
        points are boosted because they are where a reader starts regardless
        of what imports them, and tests are pushed down because "where does
        this behave" is almost never the first question — they are still
        listed, just after the code they test.
        """
        score = self.imported_by * 3.0
        score += min(len(self.symbols), 20) * 0.5
        score += min(self.lines, 1000) / 500.0
        if _is_entry_point(self.path):
            score += 6.0
        if _is_test(self.path):
            score -= 8.0
        return score


def _python_symbols(source: str) -> tuple[list[str], set[str]]:
    """Top-level definitions and imported module names, via ``ast``.

    Returns ``([], set())`` for a file that does not parse: a syntax error in
    somebody's work-in-progress is not a reason to fail the whole map.
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return [], set()

    symbols: list[str] = []
    imports: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            methods = [
                child.name
                for child in node.body
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                and not child.name.startswith("__")
            ]
            shown = ", ".join(methods[:6])
            symbols.append(f"class {node.name}" + (f": {shown}" if shown else ""))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            symbols.append(f"def {node.name}")
        elif isinstance(node, ast.Import):
            imports.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module.split(".")[0])
    return symbols, imports


def _regex_symbols(source: str, extension: str) -> list[str]:
    for extensions, patterns in _PATTERNS.items():
        if extension in extensions:
            found: list[str] = []
            for pattern in patterns:
                found.extend(pattern.findall(source))
            # Ordered by appearance, de-duplicated — a reader scanning an
            # outline expects it to follow the file.
            seen: set[str] = set()
            return [s for s in found if not (s in seen or seen.add(s))]
    return []


def build_outlines(root: str, max_files: int = 400) -> list[FileOutline]:
    """Outline every source file under ``root``, ranked.

    ``max_files`` bounds the walk rather than the output: a repository with
    fifty thousand files should not be fully parsed to produce four thousand
    characters of summary.
    """
    root = os.path.abspath(root)
    rules = IgnoreRules.for_directory(root)
    outlines: list[FileOutline] = []
    # Module name -> how many other files import it, for the ranking.
    import_counts: dict[str, int] = {}
    # A list per name, not one outline: `__init__.py` alone appears once per
    # package, and a dict silently kept whichever came last — so every
    # package but one scored zero no matter how widely it was imported.
    by_module_name: dict[str, list[FileOutline]] = {}

    for directory, subdirectories, filenames in os.walk(root):
        subdirectories[:] = [
            name for name in sorted(subdirectories)
            if not rules.is_ignored(os.path.join(directory, name), is_dir=True)
        ]
        for filename in sorted(filenames):
            if len(outlines) >= max_files:
                break
            path = os.path.join(directory, filename)
            if rules.is_ignored(path, is_dir=False):
                continue
            extension = os.path.splitext(filename)[1]
            if extension not in _PYTHON and not any(extension in group for group in _PATTERNS):
                continue
            try:
                size = os.path.getsize(path)
                if size > _MAX_PARSE_BYTES:
                    outlines.append(FileOutline(os.path.relpath(path, root), 0))
                    continue
                with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                    source = fh.read()
            except OSError:
                continue

            relative = os.path.relpath(path, root)
            outline = FileOutline(relative, source.count("\n") + 1)
            if extension in _PYTHON:
                outline.symbols, imports = _python_symbols(source)
                for name in imports:
                    import_counts[name] = import_counts.get(name, 0) + 1
                stem = os.path.splitext(os.path.basename(relative))[0]
                # A package is imported by its directory name, not by
                # "__init__" — which nothing ever imports by that name.
                if stem == "__init__":
                    stem = os.path.basename(os.path.dirname(relative)) or stem
                by_module_name.setdefault(stem, []).append(outline)
            else:
                outline.symbols = _regex_symbols(source, extension)
            outlines.append(outline)

    for name, sharing_the_name in by_module_name.items():
        count = import_counts.get(name, 0)
        for outline in sharing_the_name:
            outline.imported_by = count

    outlines.sort(key=lambda o: (o.score, o.path), reverse=True)
    return outlines


def render_map(root: str, budget_chars: int = DEFAULT_BUDGET_CHARS) -> str:
    """A ranked outline of ``root``, within ``budget_chars``.

    Files that don't fit are named in a trailing line rather than dropped
    silently: knowing a file exists is most of what orientation is, even
    without its contents.
    """
    outlines = build_outlines(root)
    if not outlines:
        return f"No source files found under {root}."

    lines: list[str] = [f"Repository map of {root}, most-referenced first:", ""]
    used = sum(len(line) + 1 for line in lines)
    shown = 0

    for outline in outlines:
        header = f"{outline.path}" + (f"  ({outline.lines} lines)" if outline.lines else "")
        block = [header]
        for symbol in outline.symbols[:_MAX_SYMBOLS_PER_FILE]:
            block.append(f"    {symbol}")
        if len(outline.symbols) > _MAX_SYMBOLS_PER_FILE:
            block.append(f"    … and {len(outline.symbols) - _MAX_SYMBOLS_PER_FILE} more")
        cost = sum(len(line) + 1 for line in block)
        if used + cost > budget_chars and shown:
            break
        lines.extend(block)
        used += cost
        shown += 1

    remaining = outlines[shown:]
    if remaining:
        names = ", ".join(o.path for o in remaining[:40])
        lines.append("")
        lines.append(f"{len(remaining)} more file(s) not outlined: {names}")
    return "\n".join(lines)
