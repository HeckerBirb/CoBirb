from .core import compute_total


def invoice_line(lines, tax=0.0):
    return f"TOTAL: {compute_total(lines, tax):.2f}"
