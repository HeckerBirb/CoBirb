"""The tests must pass on the real implementation and fail on each broken one."""
import os, pathlib, shutil, subprocess, sys

def run():
    return subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"],
                          capture_output=True, text=True).returncode

if not pathlib.Path("tests/test_duration.py").is_file():
    print("no tests/test_duration.py"); sys.exit(1)
if run() != 0:
    print("tests fail on the real implementation"); sys.exit(1)
real = pathlib.Path("duration.py").read_text()
mutants = {
    "minutes-wrong": real.replace('"m": 60', '"m": 100'),
    "invalid-accepted": real.replace('raise ValueError(f"not a duration: {text!r}")', 'return 0'),
    "hours-wrong": real.replace('"h": 3600', '"h": 360'),
}
survived = []
for name, source in mutants.items():
    pathlib.Path("duration.py").write_text(source)
    if run() == 0:
        survived.append(name)
pathlib.Path("duration.py").write_text(real)
print("survived:", survived)
sys.exit(1 if survived else 0)
