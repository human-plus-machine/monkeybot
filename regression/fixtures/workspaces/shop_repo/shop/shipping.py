"""Shipping costs."""

from shop.models import Order

DOMESTIC_RATE_CENTS = 500
INTL_BASE_RATE_CENTS = 1850
PER_ITEM_CENTS = 75
FREE_SHIPPING_OVER_CENTS = 10_000


def item_count(order: Order) -> int:
    """Total number of units in the order."""
    return sum(i.qty for i in order.items)


def domestic_shipping(order: Order) -> int:
    """Flat domestic rate, free over the threshold."""
    return DOMESTIC_RATE_CENTS


def international_shipping(order: Order) -> int:
    """International shipping: base rate plus a per-unit surcharge."""
    return INTL_BASE_RATE_CENTS + PER_ITEM_CENTS * item_count(order)


def shipping_cost(order: Order, subtotal_cents: int) -> int:
    """Shipping charge for an order given its subtotal."""
    if subtotal_cents >= FREE_SHIPPING_OVER_CENTS and order.customer.country == "US":
        return 0
    if order.customer.country == "US":
        return domestic_shipping(order)
    return international_shipping(order)
