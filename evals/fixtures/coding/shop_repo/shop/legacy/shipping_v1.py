"""Deprecated shipping rules, kept for reference. Not imported anywhere."""

INTL_BASE_RATE_CENTS = 1500


def international_shipping(order) -> int:
    return INTL_BASE_RATE_CENTS
