from shop.models import Customer, Item, Order
from shop.shipping import domestic_shipping, international_shipping, item_count, shipping_cost


def _order(country):
    return Order("o", Customer("c", "C", country), [Item("a", "A", 100, 3)])


def test_item_count():
    assert item_count(_order("US")) == 3


def test_domestic():
    assert domestic_shipping(_order("US")) == 500


def test_international():
    assert international_shipping(_order("DE")) == 1850 + 75 * 3


def test_shipping_cost_routes_by_country():
    assert shipping_cost(_order("US"), 300) == 500
    assert shipping_cost(_order("CA"), 300) == 1850 + 225
    assert shipping_cost(_order("US"), 10_000) == 0
