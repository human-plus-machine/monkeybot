"""Stock tracking."""


class OutOfStock(Exception):
    pass


class Inventory:
    def __init__(self, stock: dict[str, int] | None = None) -> None:
        self._stock = dict(stock or {})

    def available(self, sku: str) -> int:
        return self._stock.get(sku, 0)

    def reserve(self, sku: str, qty: int) -> None:
        if self.available(sku) < qty:
            raise OutOfStock(sku)
        self._stock[sku] -= qty

    def release(self, sku: str, qty: int) -> None:
        self._stock[sku] = self.available(sku) + qty
