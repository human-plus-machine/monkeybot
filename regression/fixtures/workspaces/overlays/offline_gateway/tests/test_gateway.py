from shop.gateway import charge


def test_charge_returns_transaction_id():
    # Talks to the real payment provider.
    txn = charge("tok_test_4242", 1999)
    assert txn.startswith("txn_")
