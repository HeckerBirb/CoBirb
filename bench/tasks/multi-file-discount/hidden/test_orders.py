from orders.models import Item, Order
from orders.pricing import order_total


def test_no_discount_unchanged():
    assert order_total(Order([Item("a", 10.0, 2)])) == 24.0


def test_discount_before_tax():
    assert order_total(Order([Item("a", 10.0, 2)], discount_percent=25)) == 18.0


def test_discount_defaults_to_zero():
    assert Order().discount_percent == 0
