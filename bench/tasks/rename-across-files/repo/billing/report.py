from .core import calc_total


def invoice_line(lines, tax=0.0):
    return f"TOTAL: {calc_total(lines, tax):.2f}"
