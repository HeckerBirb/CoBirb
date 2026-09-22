from util.logger import get_logger


def total(cart, prices):
    missing = [item for item in cart if item not in prices]
    if missing:
        get_logger(__name__).error(f"no price for {', '.join(missing)}")
        raise KeyError(missing[0])
    amount = sum(prices[item] * qty for item, qty in cart.items())
    get_logger(__name__).info(f"total {amount:.2f}")
    return amount
