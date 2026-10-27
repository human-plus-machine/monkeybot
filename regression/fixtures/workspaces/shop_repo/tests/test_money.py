from shop.utils.money import format_cents, round_cents


def test_round_cents_half_up():
    assert round_cents(2.5) == 3 and round_cents(1699.15) == 1699


def test_format_cents():
    assert format_cents(123456) == "$1234.56" and format_cents(-5) == "-$0.05"
