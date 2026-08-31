"""
Synthetic disputes, with known evidence completeness planted. Same trick as
every generator in this project (CLAUDE.md section 7): plant the answer,
hand back the key, and the demo becomes a measurement.

Reason codes are drawn from rules.REASON_CODE_EVIDENCE's own real keys (a
mix of UPI, RuPay and Razorpay-native codes), plus one code deliberately
absent from that table - the REASON_CODE_UNMAPPED plant, same role
UNCONFIGURED_ITEM plays in engine/vendor_terms/generator.py.
"""

from __future__ import annotations

import random
from datetime import datetime, timezone

from engine.chargeback.detector import Dispute
from engine.chargeback.rules import REASON_CODE_EVIDENCE
from engine.chargeback.taxonomy import DisputeCode

AS_OF = datetime(2026, 8, 24, tzinfo=timezone.utc)

# A representative spread across the three networks this build covers.
UPI_CODES = ["1061", "1062", "1064", "128", "1085"]
RUPAY_CODES = ["1101", "1104", "1141", "1121", "1082"]
RZP_CODES = ["RZP00", "RZP01", "RZP04", "RZP06"]
KNOWN_CODES = UPI_CODES + RUPAY_CODES + RZP_CODES
UNMAPPED_CODE = "V99"       # deliberately not in REASON_CODE_EVIDENCE

REASON_TEXT = {
    "1061": "Credit Not Processed", "1062": "Goods/Services Not As Described",
    "1064": "Goods/Services Not Received", "128": "Fraudulent Transaction",
    "1085": "Charge Amount Exceeds Authorisation Amount",
    "1101": "Illegible Fulfilment", "1104": "Cardholder Does Not Recognise the Transaction",
    "1141": "Fraudulent Card-Present Transaction",
    "1121": "Transaction Received Declined Authorisation Response",
    "1082": "Credit Posted as Debit", "RZP00": "Not Available",
    "RZP01": "Goods/Services not Provided", "RZP04": "Refund not Processed",
    "RZP06": "Business Not Responding", "V99": "Unrecognised network code",
}

PHASES = ["chargeback", "chargeback", "chargeback", "pre_arbitration"]

CANONICAL_MIX: dict[str, int] = {
    "complete": 12,
    "partial": 10,
    "missing": 6,
    "unmapped": 2,
}


def _days_to_seconds(days: int) -> int:
    return days * 86_400


def generate_disputes(n: int = 30, seed: int = 20260824
                      ) -> tuple[list[Dispute], dict[str, set[str]], dict[str, str]]:
    """
    Returns (disputes, evidence_by_dispute, ground_truth) where
    ground_truth maps dispute_id -> the DisputeCode it was built to
    produce. One dispute (the first "complete" one) is deliberately given a
    near-deadline respond_by, so the gate's deadline trigger has something
    real to fire on in a demo run - see engine/chargeback/gate.py's own
    docstring for why a closing deadline queues a dispute regardless of
    confidence.
    """
    rng = random.Random(seed)
    recipes = _recipe_list(n, rng)
    now_ts = int(AS_OF.timestamp())
    near_deadline_index = (recipes.index("complete")
                          if "complete" in recipes else -1)

    disputes: list[Dispute] = []
    evidence_by_dispute: dict[str, set[str]] = {}
    truth: dict[str, str] = {}

    for i, recipe in enumerate(recipes, start=1):
        dispute_id = f"disp_demo_{i:04d}"
        payment_id = f"pay_demo_{i:04d}"
        amount = rng.randint(500, 15_000) * 100
        phase = rng.choice(PHASES)
        days_left = rng.randint(4, 14)

        if recipe == "unmapped":
            code = UNMAPPED_CODE
            evidence_by_dispute[dispute_id] = set()
            truth[dispute_id] = str(DisputeCode.REASON_CODE_UNMAPPED)
        else:
            code = rng.choice(KNOWN_CODES)
            required = REASON_CODE_EVIDENCE[code]
            if recipe == "complete":
                evidence_by_dispute[dispute_id] = set(required)
                truth[dispute_id] = str(DisputeCode.EVIDENCE_COMPLETE)
            elif recipe == "partial":
                # At least one, never all - a genuine partial plant.
                k = rng.randint(1, len(required) - 1) if len(required) > 1 else 1
                evidence_by_dispute[dispute_id] = set(rng.sample(required, k))
                truth[dispute_id] = str(DisputeCode.EVIDENCE_PARTIAL)
            else:                                          # missing
                evidence_by_dispute[dispute_id] = set()
                truth[dispute_id] = str(DisputeCode.EVIDENCE_MISSING)

        if i - 1 == near_deadline_index:
            days_left = 1               # the deliberate near-deadline plant

        disputes.append(Dispute(
            dispute_id=dispute_id, payment_id=payment_id, amount_paise=amount,
            reason_code=code, reason_description=REASON_TEXT.get(code, ""),
            phase=phase, status="open",
            respond_by=now_ts + _days_to_seconds(days_left)))

    return disputes, evidence_by_dispute, truth


def _recipe_list(n: int, rng: random.Random) -> list[str]:
    if n == 30:
        recipes = [r for r, count in CANONICAL_MIX.items() for _ in range(count)]
    else:
        recipes = []
        for recipe, count in CANONICAL_MIX.items():
            recipes += [recipe] * max(1, round(count * n / 30))
        recipes = recipes[:n]
    rng.shuffle(recipes)
    return recipes
