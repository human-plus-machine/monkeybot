"""Deprecated pricing, kept for reference. Not imported anywhere."""


def apply_discount(amount_cents: int, pct: float) -> int:
    return int(amount_cents * (1 - pct / 100))
