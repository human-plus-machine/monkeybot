"""Sales tax."""

from shop.utils.money import round_cents

TAX_RATES = {"US": 0.07, "CA": 0.05, "GB": 0.20, "DE": 0.19}


def calc_tax(amount_cents: int, country: str) -> int:
    """Tax owed on an amount, in cents, for the given country code."""
    return int(amount_cents * TAX_RATES.get(country, 0.0))


def tax_inclusive(amount_cents: int, country: str) -> int:
    """Amount plus tax."""
    return amount_cents + calc_tax(amount_cents, country)
