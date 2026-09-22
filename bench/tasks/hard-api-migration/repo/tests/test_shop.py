import pytest

from shop import cart, checkout, stock
from util.logger import RECORDS


@pytest.fixture(autouse=True)
def clear():
    RECORDS.clear()


def test_add_logs_info():
    cart.add({}, "tea", 2)
    assert ("shop.cart", "info", "added 2 tea") in RECORDS


def test_refused_add_warns():
    cart.add({}, "tea", 0)
    assert ("shop.cart", "warning", "refused qty 0 for tea") in RECORDS


def test_remove_missing_is_debug():
    cart.remove({}, "tea")
    assert ("shop.cart", "debug", "no tea in cart") in RECORDS


def test_total_and_its_log():
    assert checkout.total({"tea": 2}, {"tea": 1.5}) == 3.0
    assert ("shop.checkout", "info", "total 3.00") in RECORDS


def test_missing_price_is_an_error():
    with pytest.raises(KeyError):
        checkout.total({"cake": 1}, {})
    assert ("shop.checkout", "error", "no price for cake") in RECORDS


def test_reserve_short_warns():
    assert not stock.reserve({"tea": 1}, "tea", 5)
    assert ("shop.stock", "warning", "short of tea: 1 < 5") in RECORDS
