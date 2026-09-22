import ast, os, pathlib, subprocess, sys
tree = ast.parse(pathlib.Path("app.py").read_text())
defined = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
if any(name.startswith("parse_") for name in defined):
    print("app.py still defines", defined); sys.exit(1)
parsing = ast.parse(pathlib.Path("parsing.py").read_text())
names = {n.name for n in parsing.body if isinstance(n, ast.FunctionDef)}
if not {"parse_duration", "parse_pair", "parse_list"} <= names:
    print("parsing.py has", names); sys.exit(1)
if "from app import parse" in pathlib.Path("jobs.py").read_text():
    print("jobs.py still imports parse_* from app"); sys.exit(1)
original = (pathlib.Path(os.environ["BENCH_TASK"]) / "repo" / "tests" / "test_app.py").read_text()
if pathlib.Path("tests/test_app.py").read_text() != original:
    print("tests modified"); sys.exit(1)
sys.exit(subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"]).returncode)
