import sys

from billing.core import calc_total

if __name__ == "__main__":
    print(calc_total([(float(sys.argv[1]), 1)]))
