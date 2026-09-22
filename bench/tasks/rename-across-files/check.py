import pathlib, sys
for path in pathlib.Path(".").rglob("*.py"):
    if "calc_total" in path.read_text():
        print("calc_total still in", path); sys.exit(1)
from billing.core import compute_total
from billing.report import invoice_line
import cli  # noqa: F401 - must still import
assert compute_total([(10.0, 2)], tax=0.1) == 22.0
assert invoice_line([(5.0, 1)]) == "TOTAL: 5.00"
