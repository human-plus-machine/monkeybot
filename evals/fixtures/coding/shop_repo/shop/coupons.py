"""Coupon codes."""

COUPONS = {"SAVE10": 10.0, "SAVE25": 25.0, "WELCOME": 5.0}


def coupon_pct(code: str | None) -> float:
    """Percent off for a coupon code; unknown or empty codes give 0."""
    if not code:
        return 0.0
    return COUPONS.get(code.upper(), 0.0)


def is_valid(code: str | None) -> bool:
    """Whether the coupon code exists."""
    return bool(code) and code.upper() in COUPONS
