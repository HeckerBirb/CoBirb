import os, pathlib, subprocess, sys
original = (pathlib.Path(os.environ["BENCH_TASK"]) / "repo" / "tests" / "test_batch.py").read_text()
if pathlib.Path("tests/test_batch.py").read_text() != original:
    print("tests modified"); sys.exit(1)
sys.exit(subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"]).returncode)
