from util.logger import get_logger


def add(cart, item, qty=1):
    if qty <= 0:
        get_logger(__name__).warning(f"refused qty {qty} for {item}")
        return cart
    cart[item] = cart.get(item, 0) + qty
    get_logger(__name__).info(f"added {qty} {item}")
    return cart


def remove(cart, item):
    if item not in cart:
        get_logger(__name__).debug(f"no {item} in cart")
        return cart
    del cart[item]
    get_logger(__name__).info(f"removed {item}")
    return cart
