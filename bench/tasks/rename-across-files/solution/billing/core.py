def compute_total(lines, tax=0.0):
    subtotal = sum(price * qty for price, qty in lines)
    return round(subtotal * (1 + tax), 2)
