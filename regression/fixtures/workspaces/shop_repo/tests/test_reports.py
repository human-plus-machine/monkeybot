from shop.models import Customer, Item, Order
from shop.reports import sales_summary


def test_sales_summary():
    orders = [Order("o1", Customer("c", "C", "US"), [Item("a", "A", 1000)])]
    summary = sales_summary(orders)
    assert summary == {"orders": 1, "revenue_cents": 1000 + 70 + 500, "tax_cents": 70}
