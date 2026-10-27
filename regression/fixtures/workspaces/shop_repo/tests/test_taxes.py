from shop.taxes import calc_tax, tax_inclusive


def test_calc_tax_us():
    assert calc_tax(1000, "US") == 70


def test_calc_tax_rounds():
    assert calc_tax(1995, "US") == 140  # 139.65


def test_unknown_country_untaxed():
    assert calc_tax(1000, "ZZ") == 0


def test_tax_inclusive():
    assert tax_inclusive(1000, "GB") == 1200
