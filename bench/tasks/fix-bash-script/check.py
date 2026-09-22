import pathlib, subprocess, sys, tempfile
with tempfile.TemporaryDirectory() as d:
    p = pathlib.Path(d)
    for name in ("a.txt", "b.txt", "notes.md", "c.TXT.bak", "d.txt"):
        (p / name).write_text("x")
    (p / "sub").mkdir(); (p / "sub" / "e.txt").write_text("x")
    out = subprocess.run(["bash", "count_txt.sh", d], capture_output=True, text=True).stdout.strip()
sys.exit(0 if out == "3" else 1)
