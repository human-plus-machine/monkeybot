"""Integer-cent helpers."""

from decimal import ROUND_HALF_UP, Decimal


def round_cents(value: float) -> int:
    """Round a fractional cent amount half up to whole cents."""
    return int(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def format_cents(cents: int) -> str:
    """Render integer cents as a dollar string."""
    sign = "-" if cents < 0 else ""
    dollars, rest = divmod(abs(cents), 100)
    return f"{sign}${dollars}.{rest:02d}"
