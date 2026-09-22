import os, pathlib, subprocess, sys
repo = pathlib.Path(os.environ["BENCH_TASK"]) / "repo" / "tests"
for original in repo.glob("*.py"):
    if pathlib.Path("tests", original.name).read_text() != original.read_text():
        print("modified", original.name); sys.exit(1)
sys.exit(subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"]).returncode)
