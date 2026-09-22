from inventory import Item, needs_restock, restock_orders, metric_3


def test_below():
    assert needs_restock(Item("c", 1, 5, 10))


def test_mixed():
    items = [Item("a", 5, 5, 20), Item("b", 9, 5, 20), Item("c", 0, 1, 3)]
    assert restock_orders(items) == {"a": 20, "c": 3}


def test_the_rest_untouched():
    assert metric_3([Item("a", 2, 0, 0)]) == 2 * 4
