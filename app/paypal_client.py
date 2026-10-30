"""paypal_client.py — PayPal Orders v2 (sandbox), gated by the rules engine."""
from __future__ import annotations
import os
import uuid
import requests

# Import path assumes rules_engine.py sits next to this file; later you can
# move Decision/Verdict into models.py and import from there instead.
from rules_engine import Decision, Verdict

from dotenv import load_dotenv
load_dotenv()   # finds .env by searching from this file's folder upward

BASE = "https://api-m.sandbox.paypal.com"   # sandbox ONLY, never the live URL
CLIENT_ID = os.environ["PAYPAL_CLIENT_ID"]       # from your .env / Render env vars
CLIENT_SECRET = os.environ["PAYPAL_CLIENT_SECRET"]


def _get_token() -> str:
    """Exchange client id/secret for a short-lived OAuth access token."""
    r = requests.post(
        f"{BASE}/v1/oauth2/token",
        auth=(CLIENT_ID, CLIENT_SECRET),          # HTTP Basic auth
        data={"grant_type": "client_credentials"},
        timeout=10,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def _headers(request_id: str | None = None) -> dict:
    h = {"Authorization": f"Bearer {_get_token()}",
         "Content-Type": "application/json"}
    if request_id:
        # Idempotency key: retrying the same call can never double-charge.
        h["PayPal-Request-Id"] = request_id
    return h


def _require_allowed(decision: Decision, confirmed: bool) -> None:
    """THE GATE. The only way to reach PayPal is through here."""
    if decision.verdict == Verdict.BLOCK:
        raise PermissionError("Blocked by giving rules; PayPal not called.")
    if decision.verdict == Verdict.FLAG and not confirmed:
        raise PermissionError("Flagged: donor must confirm first.")


def create_donation_order(decision: Decision, ein: str, charity_name: str,
                          amount: float, return_url: str, cancel_url: str,
                          confirmed: bool = False) -> dict:
    """Step 1: create the order. Returns {order_id, approve_url}.
    The donor must open approve_url and log in to approve the payment."""
    _require_allowed(decision, confirmed)          # gate runs BEFORE any API call

    body = {
        "intent": "CAPTURE",
        "purchase_units": [{
            "amount": {"currency_code": "USD", "value": f"{amount:.2f}"},
            "description": f"Donation to {charity_name}"[:127],
            "custom_id": ein,                      # lets the ledger tie order -> charity
        }],
        "payment_source": {"paypal": {"experience_context": {
            "return_url": return_url,
            "cancel_url": cancel_url,
            "user_action": "PAY_NOW",
        }}},
    }
    r = requests.post(f"{BASE}/v2/checkout/orders", json=body,
                      headers=_headers(str(uuid.uuid4())), timeout=15)
    r.raise_for_status()
    order = r.json()

    # The redirect link's rel name differs by integration style, so accept both.
    approve = next(l["href"] for l in order["links"]
                   if l["rel"] in ("approve", "payer-action"))
    return {"order_id": order["id"], "approve_url": approve}


def capture_order(order_id: str) -> dict:
    """Step 2: after the donor approved, take the money.
    Returns {status, capture_id, amount} for the ledger."""
    r = requests.post(f"{BASE}/v2/checkout/orders/{order_id}/capture",
                      headers=_headers(str(uuid.uuid4())), timeout=15)
    r.raise_for_status()
    data = r.json()
    cap = data["purchase_units"][0]["payments"]["captures"][0]
    return {"status": data["status"],            # expect "COMPLETED"
            "capture_id": cap["id"],
            "amount": cap["amount"]["value"]}


if __name__ == "__main__":
    # Manual end-to-end test with a fake APPROVE decision.
    ok = Decision(Verdict.APPROVE)
    o = create_donation_order(ok, "530196605", "American Red Cross", 5.00,
                              "http://localhost:8000/return",
                              "http://localhost:8000/cancel")
    print("Open this, log in as your sandbox BUYER, approve:\n", o["approve_url"])
    input("Press Enter after approving...")
    print(capture_order(o["order_id"]))

    # The gate test: this must raise PermissionError and never call PayPal.
    try:
        create_donation_order(Decision(Verdict.BLOCK), "1", "X", 5, "", "")
    except PermissionError as e:
        print("Gate works:", e)