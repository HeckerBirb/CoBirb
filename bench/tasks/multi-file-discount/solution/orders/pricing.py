from .models import Order


def subtotal(order: Order) -> float:
    return sum(item.price * item.qty for item in order.items)


def order_total(order: Order) -> float:
    discounted = subtotal(order) * (1 - order.discount_percent / 100)
    return round(discounted * (1 + order.tax_rate), 2)
