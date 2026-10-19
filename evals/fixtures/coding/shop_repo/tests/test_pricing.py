from shop.models import Item
from shop.pricing import apply_discount, line_total, subtotal, tier_discount


def test_line_total():
    assert line_total(Item("a", "A", 250, 3)) == 750


def test_subtotal():
    assert subtotal([Item("a", "A", 100, 2), Item("b", "B", 50)]) == 250


def test_apply_discount_exact():
    assert apply_discount(1000, 10) == 900


def test_apply_discount_rounds_half_up():
    assert apply_discount(1999, 15) == 1699  # 1699.15 rounds to 1699
    assert apply_discount(995, 50) == 498  # 497.5 rounds up


def test_tier_discount():
    assert tier_discount(2000, "gold") == 1800
    assert tier_discount(2000, "standard") == 2000
