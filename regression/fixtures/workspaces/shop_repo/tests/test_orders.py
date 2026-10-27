from shop.models import Customer, Item, Order
from shop.orders import discounted_subtotal, order_total


def _order(country="US", tier="standard", coupon=None, price=1999, qty=1):
    return Order("o1", Customer("c1", "Ann", country, tier), [Item("a", "A", price, qty)], coupon)


def test_plain_us_order():
    # 1999 + 7% tax (140) + 500 shipping
    assert order_total(_order()) == 1999 + 140 + 500


def test_coupon_discount_rounds_half_up():
    assert discounted_subtotal(_order(coupon="SAVE10", price=1995)) == 1796  # 1795.5 -> 1796


def test_free_shipping_over_threshold():
    assert order_total(_order(price=20000)) == 20000 + 1400


def test_international_order():
    total = order_total(_order(country="GB", price=1000, qty=2))
    assert total == 2000 + 400 + 1850 + 75 * 2


def test_gold_member_with_coupon():
    assert discounted_subtotal(_order(tier="gold", coupon="SAVE25", price=1999)) == 1349
