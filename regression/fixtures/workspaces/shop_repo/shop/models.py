"""Domain objects."""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Item:
    sku: str
    name: str
    price_cents: int
    qty: int = 1


@dataclass(frozen=True)
class Customer:
    id: str
    name: str
    country: str = "US"
    tier: str = "standard"


@dataclass
class Order:
    id: str
    customer: Customer
    items: list[Item] = field(default_factory=list)
    coupon: str | None = None
