from .models import Order


def subtotal(order: Order) -> float:
    return sum(item.price * item.qty for item in order.items)


def order_total(order: Order) -> float:
    return round(subtotal(order) * (1 + order.tax_rate), 2)
