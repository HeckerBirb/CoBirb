from util.logging_compat import log


def total(cart, prices):
    missing = [item for item in cart if item not in prices]
    if missing:
        log(f"no price for {', '.join(missing)}", "error")
        raise KeyError(missing[0])
    amount = sum(prices[item] * qty for item, qty in cart.items())
    log(f"total {amount:.2f}", "info")
    return amount
