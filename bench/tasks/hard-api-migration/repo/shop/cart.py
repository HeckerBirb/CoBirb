from util.logging_compat import log


def add(cart, item, qty=1):
    if qty <= 0:
        log(f"refused qty {qty} for {item}", "warning")
        return cart
    cart[item] = cart.get(item, 0) + qty
    log(f"added {qty} {item}", "info")
    return cart


def remove(cart, item):
    if item not in cart:
        log(f"no {item} in cart", "debug")
        return cart
    del cart[item]
    log(f"removed {item}", "info")
    return cart
