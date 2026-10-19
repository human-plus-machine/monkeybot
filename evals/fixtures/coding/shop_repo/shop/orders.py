"""Order totals."""

from shop.coupons import coupon_pct
from shop.models import Order
from shop.pricing import apply_discount, subtotal, tier_discount
from shop.shipping import shipping_cost
from shop.taxes import calc_tax


def discounted_subtotal(order: Order) -> int:
    """Subtotal after coupon and tier discounts."""
    base = subtotal(order.items)
    after_coupon = apply_discount(base, coupon_pct(order.coupon))
    return tier_discount(after_coupon, order.customer.tier)


def order_total(order: Order) -> int:
    """Grand total in cents: discounted subtotal + tax + shipping."""
    net = discounted_subtotal(order)
    tax = calc_tax(net, order.customer.country)
    return net + tax + shipping_cost(order, net)
