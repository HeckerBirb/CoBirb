from util.logger import get_logger


def reserve(stock, item, qty):
    have = stock.get(item, 0)
    if have < qty:
        get_logger(__name__).warning(f"short of {item}: {have} < {qty}")
        return False
    stock[item] = have - qty
    get_logger(__name__).debug(f"reserved {qty} {item}")
    return True
