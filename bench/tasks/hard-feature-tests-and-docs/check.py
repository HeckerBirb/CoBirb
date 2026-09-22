import os, pathlib, re, shutil, subprocess, sys
readme = pathlib.Path("README.md").read_text()
usage = re.search(r"## Usage(.*?)(\n## |\Z)", readme, re.S)
if not usage or "--priority" not in usage.group(1):
    print("README Usage does not mention --priority"); sys.exit(1)
if not pathlib.Path("tests/test_priority.py").is_file():
    print("no tests/test_priority.py"); sys.exit(1)
if subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"]).returncode:
    print("the project's own tests fail"); sys.exit(1)
hidden = pathlib.Path(os.environ["BENCH_HIDDEN"])
sys.exit(subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                         "-c", str(hidden / "pytest.ini"), "--rootdir", str(hidden), str(hidden)]).returncode)
