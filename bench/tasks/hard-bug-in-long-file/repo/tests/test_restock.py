from inventory import Item, restock_orders


def test_an_item_exactly_at_its_reorder_point_is_restocked():
    assert restock_orders([Item("a", 5, 5, 20)]) == {"a": 20}


def test_an_item_above_it_is_not():
    assert restock_orders([Item("b", 6, 5, 20)]) == {}
