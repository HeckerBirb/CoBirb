import os, re, sys
answer = open(os.environ["BENCH_ANSWER"]).read()
sys.exit(0 if re.search(r"\b8081\b", answer) else 1)
