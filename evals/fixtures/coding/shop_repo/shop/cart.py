"""A mutable shopping cart."""

from shop.models import Item


class Cart:
    def __init__(self) -> None:
        self._items: dict[str, Item] = {}

    def add(self, item: Item) -> None:
        existing = self._items.get(item.sku)
        if existing is None:
            self._items[item.sku] = item
        else:
            self._items[item.sku] = Item(
                item.sku, item.name, item.price_cents, existing.qty + item.qty
            )

    def remove(self, sku: str) -> None:
        self._items.pop(sku, None)

    def items(self) -> list[Item]:
        return list(self._items.values())

    def __len__(self) -> int:
        return sum(i.qty for i in self._items.values())
