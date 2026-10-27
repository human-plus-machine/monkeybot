from shop.cart import Cart
from shop.models import Item


def test_add_merges_quantities():
    cart = Cart()
    cart.add(Item("a", "A", 100, 1))
    cart.add(Item("a", "A", 100, 2))
    assert len(cart) == 3 and len(cart.items()) == 1


def test_remove():
    cart = Cart()
    cart.add(Item("a", "A", 100))
    cart.remove("a")
    assert len(cart) == 0
