import pytest

from shop.inventory import Inventory, OutOfStock


def test_reserve_and_release():
    inv = Inventory({"a": 3})
    inv.reserve("a", 2)
    assert inv.available("a") == 1
    inv.release("a", 5)
    assert inv.available("a") == 6


def test_out_of_stock():
    with pytest.raises(OutOfStock):
        Inventory({"a": 1}).reserve("a", 2)
