"""Tests for the codebase outline the agent uses to orient itself."""
from __future__ import annotations

from cobirb.plugins.core.repomap import build_outlines, render_map
from cobirb.plugins.core.tools import RepoMapTool


def _project(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "core.py").write_text(
        "class Engine:\n"
        "    def start(self): pass\n"
        "    def stop(self): pass\n"
        "    def __repr__(self): pass\n"
        "\n"
        "def helper():\n"
        "    pass\n"
    )
    (tmp_path / "src" / "cli.py").write_text("import core\n\ndef main():\n    pass\n")
    (tmp_path / "src" / "other.py").write_text("import core\n\ndef thing():\n    pass\n")
    return tmp_path


def test_python_symbols_come_out_of_the_ast(tmp_path):
    outlines = {o.path: o for o in build_outlines(str(_project(tmp_path)))}

    symbols = outlines["src/core.py"].symbols
    assert any("class Engine" in s and "start" in s and "stop" in s for s in symbols)
    assert "def helper" in symbols
    # Dunders are noise in an outline.
    assert not any("__repr__" in s for s in symbols)


def test_a_widely_imported_file_ranks_above_one_nothing_imports(tmp_path):
    """The closest thing to an objective measure of what a codebase considers
    central."""
    outlines = {o.path: o for o in build_outlines(str(_project(tmp_path)))}

    assert outlines["src/core.py"].imported_by == 2
    assert outlines["src/core.py"].score > outlines["src/other.py"].score


def test_entry_points_are_boosted(tmp_path):
    """Where a reader starts, whatever the import graph says. `cli.py` here
    is imported by nothing at all."""
    outlines = {o.path: o for o in build_outlines(str(_project(tmp_path)))}

    assert outlines["src/cli.py"].imported_by == 0
    assert outlines["src/cli.py"].score > outlines["src/other.py"].score


def test_tests_rank_below_the_code_they_test(tmp_path):
    """"Where does this behave" is almost never the first question."""
    _project(tmp_path)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_core.py").write_text(
        "import core\n" + "\n".join(f"def test_{n}(): pass" for n in range(30))
    )

    ranked = [o.path for o in build_outlines(str(tmp_path))]

    assert ranked.index("src/core.py") < ranked.index("tests/test_core.py")


def test_every_package_gets_its_import_count(tmp_path):
    """Regression: the counts were kept in a dict keyed on basename, so every
    `__init__.py` but the last silently scored zero however widely its package
    was imported."""
    for name in ("alpha", "beta"):
        package = tmp_path / name
        package.mkdir()
        (package / "__init__.py").write_text("VALUE = 1\n")
    (tmp_path / "uses_alpha.py").write_text("import alpha\n")
    (tmp_path / "uses_both.py").write_text("import alpha\nimport beta\n")

    outlines = {o.path: o for o in build_outlines(str(tmp_path))}

    assert outlines["alpha/__init__.py"].imported_by == 2
    assert outlines["beta/__init__.py"].imported_by == 1


def test_a_file_that_does_not_parse_does_not_break_the_map(tmp_path):
    """Somebody's work in progress is not a reason to fail everything."""
    _project(tmp_path)
    (tmp_path / "src" / "broken.py").write_text("def oops(:\n")

    paths = [o.path for o in build_outlines(str(tmp_path))]

    assert "src/broken.py" in paths
    assert "src/core.py" in paths


def test_other_languages_get_a_rough_outline(tmp_path):
    (tmp_path / "app.ts").write_text(
        "export function handler() {}\nexport class Router {}\nconst make = (x) => x\n"
    )

    symbols = {o.path: o.symbols for o in build_outlines(str(tmp_path))}["app.ts"]

    assert {"handler", "Router", "make"} <= set(symbols)


def test_the_map_respects_gitignore(tmp_path):
    _project(tmp_path)
    (tmp_path / ".gitignore").write_text("generated/\n")
    (tmp_path / "generated").mkdir()
    (tmp_path / "generated" / "big.py").write_text("def generated_thing(): pass\n")

    assert "generated/big.py" not in render_map(str(tmp_path))


def test_the_map_stays_inside_its_budget(tmp_path):
    """It becomes a session turn and a message in the next request, so an
    unbounded map would undo the compaction it is meant to complement."""
    (tmp_path / "src").mkdir()
    for n in range(60):
        (tmp_path / "src" / f"mod{n}.py").write_text(
            "\n".join(f"def function_with_a_longish_name_{i}(): pass" for i in range(40))
        )

    rendered = render_map(str(tmp_path), budget_chars=1500)

    assert len(rendered) < 4000  # budget plus the trailing "more files" line


def test_files_that_do_not_fit_are_still_named(tmp_path):
    """Knowing a file exists is most of what orientation is, even without
    its contents."""
    (tmp_path / "src").mkdir()
    for n in range(30):
        (tmp_path / "src" / f"mod{n}.py").write_text("def a(): pass\ndef b(): pass\n")

    rendered = render_map(str(tmp_path), budget_chars=600)

    assert "more file(s) not outlined" in rendered


def test_the_tool_reports_a_bad_path_rather_than_raising(tmp_path):
    result = RepoMapTool(str(tmp_path)).execute({"path": "no-such-directory"})

    assert not result.ok
    assert result.error == "not_a_directory"


def test_the_tool_maps_the_working_directory_by_default(tmp_path):
    _project(tmp_path)

    result = RepoMapTool(str(tmp_path)).execute({})

    assert result.ok
    assert "src/core.py" in result.content


def test_an_empty_directory_says_so_rather_than_returning_nothing(tmp_path):
    assert "No source files" in render_map(str(tmp_path))
