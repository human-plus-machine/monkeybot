from shop.coupons import coupon_pct, is_valid


def test_known_and_unknown():
    assert coupon_pct("save10") == 10.0
    assert coupon_pct("nope") == 0.0
    assert is_valid("WELCOME") and not is_valid(None)
