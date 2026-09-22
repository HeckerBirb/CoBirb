import os, sys
answer = open(os.environ["BENCH_ANSWER"]).read()
sys.exit(0 if "validate_session" in answer else 1)
