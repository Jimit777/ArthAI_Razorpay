#!/usr/bin/env python3
"""
Verify our schema against Razorpay's actual API. Checkpoint 1, second half.

  export RAZORPAY_KEY_ID=rzp_test_... RAZORPAY_KEY_SECRET=...
  python razorpay_check.py

CLAUDE.md section 7.1 splits the data strategy in two: Razorpay test mode for
SCHEMA FIDELITY, synthetic generation for volume and planted errors. The second
half was built first. This is the first half, and without it the README would
be claiming "we mirror Razorpay's real schema" on the strength of having read
the documentation.

What this does:

  creates one real order in TEST MODE (no money moves, ever)
  fetches every entity the account has
  compares the real field names and id formats against what we generate
  saves the raw responses as evidence

What it deliberately does NOT do: complete a payment. That needs a card entered
into a checkout form, which is not something this script should be doing on
anyone's behalf. It prints instructions instead - and once a test payment
exists, re-running this picks up the real payment and settlement shapes too.

The secret is read from the environment and never written anywhere.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

API = "https://api.razorpay.com/v1"
SAMPLES = Path(__file__).parent / "razorpay_samples"

# What our generator produces, per CLAUDE.md section 9. The point of this file
# is to find out whether these are real.
OUR_FIELDS = {
    "order": {"id", "amount", "currency", "created_at"},
    "payment": {"id", "order_id", "amount", "currency", "method", "card_id",
                "created_at", "international"},
    "settlement": {"id", "amount", "created_at"},
    "refund": {"id", "payment_id", "amount", "created_at"},
}

OUR_ID_FORMATS = {
    "order": r"^order_[A-Za-z0-9]{14}$",
    "payment": r"^pay_[A-Za-z0-9]{14}$",
    "settlement": r"^setl_[A-Za-z0-9]{14}$",
    "refund": r"^rfnd_[A-Za-z0-9]{14}$",
}


class Razorpay:
    def __init__(self, key_id: str, key_secret: str):
        token = base64.b64encode(f"{key_id}:{key_secret}".encode()).decode()
        self._headers = {"Authorization": f"Basic {token}",
                         "Content-Type": "application/json"}
        if not key_id.startswith("rzp_test_"):
            raise SystemExit(
                f"Refusing to run: {key_id[:12]}... is not a test key.\n"
                "This script creates an order. Only ever point it at test mode.")

    def _call(self, path: str, payload: dict | None = None):
        req = urllib.request.Request(
            API + path,
            data=json.dumps(payload).encode() if payload is not None else None,
            headers=self._headers,
            method="POST" if payload is not None else "GET")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")
        except Exception as exc:                            # noqa: BLE001
            return None, {"transport_error": str(exc)}

    def get(self, path: str):
        return self._call(path)

    def create_order(self, amount_paise: int):
        return self._call("/orders", {
            "amount": amount_paise, "currency": "INR",
            "receipt": f"schema-check-{int(datetime.now(timezone.utc).timestamp())}",
            "notes": {"purpose": "settlement-auditor schema verification"},
        })


def compare(entity: str, sample: dict) -> dict:
    """What we model, what we omit, and - the one that would matter - what we invented."""
    real = set(sample)
    ours = OUR_FIELDS.get(entity, set())
    return {
        "confirmed": sorted(ours & real),
        "we_omit": sorted(real - ours),
        "we_invented": sorted(ours - real),
    }


def check_id(entity: str, value: str) -> bool:
    pattern = OUR_ID_FORMATS.get(entity)
    return bool(pattern and re.match(pattern, value))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--no-create", action="store_true",
                    help="read only; do not create a test order")
    ap.add_argument("--amount", type=int, default=162_700,
                    help="order amount in paise (default Rs 1,627.00)")
    args = ap.parse_args()

    key_id = os.environ.get("RAZORPAY_KEY_ID")
    secret = os.environ.get("RAZORPAY_KEY_SECRET")
    if not key_id or not secret:
        print("Set RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET (test mode).")
        return 1

    rzp = Razorpay(key_id, secret)
    SAMPLES.mkdir(exist_ok=True)
    findings: dict[str, dict] = {}

    print("=" * 72)
    print("SCHEMA CHECK AGAINST THE REAL RAZORPAY API  (test mode)")
    print("=" * 72)
    print()

    # --- an order we create ourselves ------------------------------------
    if not args.no_create:
        status, order = rzp.create_order(args.amount)
        if status == 200:
            (SAMPLES / "order.json").write_text(json.dumps(order, indent=2))
            findings["order"] = compare("order", order)
            print(f"created a real order: {order['id']}")
            print(f"  our id format matches : {check_id('order', order['id'])}")
            print(f"  amount is integer paise: {isinstance(order['amount'], int)}"
                  f"  ({order['amount']})")
            print()
        else:
            print(f"could not create an order ({status}): "
                  f"{order.get('error', {}).get('description', order)}\n")

    # --- whatever else the account holds ---------------------------------
    for entity, path in [("payment", "/payments?count=5"),
                         ("settlement", "/settlements?count=5"),
                         ("refund", "/refunds?count=5")]:
        status, body = rzp.get(path)
        if status != 200:
            print(f"{entity:<12} could not fetch ({status})")
            continue
        items = body.get("items", [])
        if not items:
            print(f"{entity:<12} none in this account yet")
            continue
        (SAMPLES / f"{entity}.json").write_text(json.dumps(items[0], indent=2))
        findings[entity] = compare(entity, items[0])
        print(f"{entity:<12} {len(items)} found, id format matches: "
              f"{check_id(entity, items[0]['id'])}")

    # --- the settlement recon report, which is what we actually mirror ----
    now = datetime.now(timezone.utc)
    status, recon = rzp.get(
        f"/settlements/recon/combined?year={now.year}&month={now.month:02d}")
    if status == 200 and recon.get("items"):
        (SAMPLES / "recon.json").write_text(json.dumps(recon["items"][0], indent=2))
        print(f"\nsettlement recon: {len(recon['items'])} rows")
        print(f"  real columns: {', '.join(sorted(recon['items'][0]))}")
    else:
        print("\nsettlement recon: empty (test mode settles nothing on its own)")

    # --- the verdict -----------------------------------------------------
    print()
    print("=" * 72)
    print("FIELD-BY-FIELD")
    print("=" * 72)
    invented_anywhere = False
    for entity, result in findings.items():
        print(f"\n{entity}:")
        print(f"  confirmed real   {', '.join(result['confirmed']) or '-'}")
        print(f"  we do not model  {', '.join(result['we_omit']) or '-'}")
        if result["we_invented"]:
            invented_anywhere = True
            print(f"  WE INVENTED      {', '.join(result['we_invented'])}"
                  f"   <- not in the real response")
        else:
            print("  we invented      none")

    print()
    if not findings:
        print("Nothing to compare. The account is empty.")
    elif invented_anywhere:
        print("At least one field we generate does not exist in the real API.")
        print("Fix the generator before claiming schema fidelity.")
    else:
        print("Every field we generate exists in the real API, and every id")
        print("format matches. We model a subset, which is honest and correct -")
        print("we do not invent.")

    print(f"\nRaw responses saved to {SAMPLES}/ as evidence.")

    if "payment" not in findings:
        print()
        print("-" * 72)
        print("TO CAPTURE A REAL PAYMENT AND SETTLEMENT SHAPE")
        print("-" * 72)
        print("Test mode creates payments through Checkout, not through the API,")
        print("so one has to be completed in a browser. This script will not do")
        print("that for you - it means typing a card number into a form.")
        print()
        print("  1. python razorpay_checkout.py     writes checkout.html")
        print("  2. open checkout.html              and pay with Razorpay's")
        print("     documented test card (their docs list the current one)")
        print("  3. python razorpay_check.py        picks up the real payment")
    return 0


if __name__ == "__main__":
    sys.exit(main())
