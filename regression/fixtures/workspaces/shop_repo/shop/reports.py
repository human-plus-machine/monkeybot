"""Sales reports."""

from shop.models import Order
from shop.orders import order_total
from shop.taxes import calc_tax
from shop.pricing import subtotal


def sales_summary(orders: list[Order]) -> dict[str, int]:
    """Totals across orders: revenue and tax collected, in cents."""
    revenue = sum(order_total(o) for o in orders)
    tax = sum(calc_tax(subtotal(o.items), o.customer.country) for o in orders)
    return {"orders": len(orders), "revenue_cents": revenue, "tax_cents": tax}
