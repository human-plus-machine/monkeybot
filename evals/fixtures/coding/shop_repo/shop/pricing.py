"""Price arithmetic."""

from shop.models import Item
from shop.utils.money import round_cents

TIER_DISCOUNT_PCT = {"standard": 0.0, "silver": 5.0, "gold": 10.0}


def line_total(item: Item) -> int:
    """Total price of one order line in cents."""
    return item.price_cents * item.qty


def subtotal(items: list[Item]) -> int:
    """Sum of all line totals in cents."""
    return sum(line_total(i) for i in items)


def apply_discount(amount_cents: int, pct: float) -> int:
    """Return the amount after taking pct percent off, in cents."""
    return round_cents(amount_cents * (1 - pct / 100))


def tier_discount(amount_cents: int, tier: str) -> int:
    """Apply the customer tier discount."""
    return apply_discount(amount_cents, TIER_DISCOUNT_PCT.get(tier, 0.0))
