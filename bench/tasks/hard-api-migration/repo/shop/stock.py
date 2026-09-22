from util import logging_compat


def reserve(stock, item, qty):
    have = stock.get(item, 0)
    if have < qty:
        logging_compat.log(f"short of {item}: {have} < {qty}", "warning")
        return False
    stock[item] = have - qty
    logging_compat.log(f"reserved {qty} {item}", "debug")
    return True
