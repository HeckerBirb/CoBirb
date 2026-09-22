import os, re, sys
text = open("README.md").read()
original = open(os.path.join(os.environ["BENCH_TASK"], "repo", "README.md")).read()
usage = re.search(r"## Usage(.*?)(\n## |\Z)", text, re.S)
ok = (usage and "--limit" in usage.group(1)
      and "## License" in text and "MIT" in text and "--json" in text)
sys.exit(0 if ok else 1)
