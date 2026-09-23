"""Build hook: ship the user manual inside the package.

`cobirb help <topic>` renders `docs/manual/<topic>.md`, so the wheel has to
carry those pages. They stay in `docs/manual/` — where the repository's own
links and readers expect them — and are copied to `cobirb/manual/` at build
time. Everything else about the build is in pyproject.toml.
"""
import shutil
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py


class BuildWithManual(build_py):
    def run(self):
        super().run()
        source = Path(__file__).resolve().parent / "docs" / "manual"
        target = Path(self.build_lib) / "cobirb" / "manual"
        if source.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            for page in source.glob("*.md"):
                shutil.copy2(page, target / page.name)


setup(cmdclass={"build_py": BuildWithManual})
