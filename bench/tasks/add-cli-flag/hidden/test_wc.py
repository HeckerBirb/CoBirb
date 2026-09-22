import subprocess, sys


def run(tmp_path, *args):
    target = tmp_path / "f.txt"
    target.write_text("one two three\nfour five\n")
    out = subprocess.run([sys.executable, "wc.py", str(target), *args], capture_output=True, text=True)
    return out.stdout.strip()


def test_lines_by_default(tmp_path):
    assert run(tmp_path) == "2"


def test_words_flag(tmp_path):
    assert run(tmp_path, "--words") == "5"
