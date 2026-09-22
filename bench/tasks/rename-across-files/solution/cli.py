import sys

from billing.core import compute_total

if __name__ == "__main__":
    print(compute_total([(float(sys.argv[1]), 1)]))
