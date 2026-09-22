import os, subprocess, sys
original = open(os.path.join(os.environ["BENCH_TASK"], "repo", "tests", "test_stats.py")).read()
if open("tests/test_stats.py").read() != original:
    print("tests were modified"); sys.exit(1)
sys.exit(subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"]).returncode)
