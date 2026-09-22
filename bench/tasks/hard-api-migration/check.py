import os, pathlib, subprocess, sys
if pathlib.Path("util/logging_compat.py").exists():
    print("compat module still there"); sys.exit(1)
for path in pathlib.Path(".").rglob("*.py"):
    if "logging_compat" in path.read_text():
        print("still referenced in", path); sys.exit(1)
for module in ("shop/cart.py", "shop/checkout.py", "shop/stock.py"):
    if "get_logger(__name__)" not in pathlib.Path(module).read_text():
        print("not migrated:", module); sys.exit(1)
original = (pathlib.Path(os.environ["BENCH_TASK"]) / "repo" / "tests" / "test_shop.py").read_text()
if pathlib.Path("tests/test_shop.py").read_text() != original:
    print("tests modified"); sys.exit(1)
sys.exit(subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"]).returncode)
