#!/usr/bin/env python3
"""
Writes a checkout page so a real test payment can be completed by hand.

  export RAZORPAY_KEY_ID=rzp_test_... RAZORPAY_KEY_SECRET=...
  python razorpay_checkout.py
  open checkout.html

Why this is a separate script you run yourself: completing a payment means
typing a card number into a form. That is not something the auditor should be
doing on anyone's behalf, even in test mode with a documented dummy card. So
the tooling creates the order and hands you the page; you do the part that
involves a card.

Once a test payment exists, `python razorpay_check.py` picks up the real
payment object and compares its field names against ours.

ONLY THE KEY ID GOES IN THE PAGE. The key id is public by design - it ships in
every merchant's checkout. The secret is used here to create the order and is
never written to the file.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.request
from pathlib import Path

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Razorpay test payment</title>
<style>
  body {{ margin:0; min-height:100vh; display:grid; place-items:center;
    background:#fbfaf9; color:#1a1a19;
    font:15px/1.6 ui-sans-serif,-apple-system,"Segoe UI",Roboto,sans-serif }}
  .card {{ background:#fff; border:1px solid #e5e3e0; border-radius:12px;
    padding:32px; max-width:460px }}
  h1 {{ font-size:19px; margin:0 0 6px }}
  p  {{ color:#6b6b68; font-size:13.5px; margin:0 0 18px }}
  dl {{ display:grid; grid-template-columns:auto 1fr; gap:6px 14px;
    font-size:13px; margin:0 0 22px }}
  dt {{ color:#6b6b68 }}
  dd {{ margin:0; font-family:ui-monospace,Menlo,monospace }}
  button {{ width:100%; padding:12px; font-size:15px; border:0; border-radius:8px;
    background:#1a1a19; color:#fff; cursor:pointer }}
  .note {{ margin-top:18px; padding:12px; background:#fbfaf9; border-radius:8px;
    font-size:12.5px; color:#6b6b68 }}
  code {{ font-family:ui-monospace,Menlo,monospace }}
</style></head><body>
<div class="card">
  <h1>Razorpay test payment</h1>
  <p>Test mode. No money moves. This exists so the auditor can compare its
     schema against a real payment object.</p>
  <dl>
    <dt>Order</dt><dd>{order_id}</dd>
    <dt>Amount</dt><dd>Rs {rupees}</dd>
    <dt>Key</dt><dd>{key_id}</dd>
  </dl>
  <button id="pay">Pay in test mode</button>
  <div class="note">
    Use a Razorpay <b>test card</b> from their documentation &mdash; never a real
    one. Any future expiry and any CVV. Afterwards run
    <code>python razorpay_check.py</code> to capture the real payment shape.
  </div>
</div>
<script src="https://checkout.razorpay.com/v1/checkout.js"></script>
<script>
document.getElementById('pay').onclick = function () {{
  new Razorpay({{
    key: "{key_id}",
    order_id: "{order_id}",
    amount: {amount},
    currency: "INR",
    name: "Settlement Auditor",
    description: "Schema verification - test mode",
    handler: function (r) {{
      document.querySelector('.card').innerHTML =
        '<h1>Done</h1><p>Payment ' + r.razorpay_payment_id +
        '</p><p>Now run <code>python razorpay_check.py</code>.</p>';
    }}
  }}).open();
}};
</script></body></html>
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--amount", type=int, default=162_700, help="paise")
    ap.add_argument("--out", default="checkout.html")
    args = ap.parse_args()

    key_id = os.environ.get("RAZORPAY_KEY_ID")
    secret = os.environ.get("RAZORPAY_KEY_SECRET")
    if not key_id or not secret:
        print("Set RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET (test mode).")
        return 1
    if not key_id.startswith("rzp_test_"):
        print(f"Refusing to run: {key_id[:12]}... is not a test key.")
        return 1

    token = base64.b64encode(f"{key_id}:{secret}".encode()).decode()
    req = urllib.request.Request(
        "https://api.razorpay.com/v1/orders",
        data=json.dumps({"amount": args.amount, "currency": "INR",
                         "receipt": "schema-check-checkout"}).encode(),
        headers={"Authorization": f"Basic {token}",
                 "Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        order = json.loads(r.read())

    Path(args.out).write_text(PAGE.format(
        order_id=order["id"], key_id=key_id, amount=args.amount,
        rupees=f"{args.amount // 100:,}.{args.amount % 100:02d}"))

    print(f"created order {order['id']}")
    print(f"wrote {args.out}  (contains the key id only - never the secret)")
    print(f"\n  open {args.out}\n")
    print("Pay with a Razorpay test card from their docs, then run:")
    print("  python razorpay_check.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
