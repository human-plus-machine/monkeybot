"""Card payments through the external payment provider."""

import json
import urllib.request

GATEWAY_URL = "https://payments.internal.example/v1/charge"


class PaymentError(Exception):
    pass


def charge(card_token: str, amount_cents: int) -> str:
    """Charge a card and return the provider's transaction id."""
    body = json.dumps({"token": card_token, "amount": amount_cents}).encode()
    req = urllib.request.Request(
        GATEWAY_URL, data=body, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            return json.loads(resp.read())["transaction_id"]
    except OSError as exc:
        raise PaymentError(str(exc)) from exc
