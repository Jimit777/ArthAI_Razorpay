"""
A settlement batch with known delay planted, and the answer key for it.

Same trick as every generator in this project (CLAUDE.md section 7): plant
the delay, hand back the answer key, and the demo becomes a measurement.

## Why this reuses engine.recon's own dataclasses

`Invoice`/`Settlement` already carry exactly the two dates this agent needs
(`date_issued`, `settlement_date`), and `engine.recon.matcher.reconcile()`
already does the pairing (Pass 1 exact reference, Pass 2 windowed) - so this
generator produces a `ReconBatch` a completely unmodified `reconcile()` can
join, with `bank=[]` since delay is purely an invoice-to-settlement question
and bank-credit posting lag is a different concern the Three-Way Reconciler
already owns.

Delay is planted in whole working days past `engine.payout_timing.rules.due_date`,
deliberately straddling weekends - a flat calendar-day offset would never
exercise the boundary where the promised cycle itself has to skip Saturday
and Sunday.
"""

from __future__ import annotations

import random
from datetime import date, timedelta

from engine.payout_timing.rules import due_date
from engine.payout_timing.taxonomy import PayoutCode
from engine.recon.records import Invoice, ReconBatch, Settlement

AS_OF = date(2026, 8, 24)

# Working days late past the promised due date, per recipe.
DELAY_WORKING_DAYS = {
    "minor_miss": 1,
    "moderate_miss": 3,
    "severe_miss": 7,
}

RECIPE_TRUTH: dict[str, PayoutCode] = {
    "on_time": PayoutCode.ON_TIME,
    "minor_miss": PayoutCode.SLA_MISS,
    "moderate_miss": PayoutCode.SLA_MISS,
    "severe_miss": PayoutCode.SLA_MISS,
    "unmatched_in_transit": PayoutCode.UNMATCHED,
}

# n=60. ~26% of matched records miss the SLA, comfortably over the 20%
# systemic threshold - the canonical batch is built to resolve to
# SYSTEMIC_DELAY, not to a coin toss around the boundary.
CANONICAL_MIX: dict[str, int] = {
    "on_time": 43,
    "minor_miss": 7,
    "moderate_miss": 5,
    "severe_miss": 3,
    "unmatched_in_transit": 2,
}

DECOY_RECIPES = {"on_time"}
MISS_RECIPES = {"minor_miss", "moderate_miss", "severe_miss"}


def _walk_working_days(start: date, working_days: int) -> date:
    """`start` moved forward by `working_days` working days - the same
    weekend-skip rule engine.payout_timing.rules.due_date already uses."""
    from engine.expected_value import add_working_days

    return add_working_days(start, working_days) if working_days else start


def generate_batch(n: int = 60, seed: int = 20260905
                   ) -> tuple[ReconBatch, dict[str, str]]:
    """
    Returns (batch, ground_truth) where ground_truth maps invoice_id to the
    PayoutCode it was built to produce.
    """
    rng = random.Random(seed)
    recipes = _recipe_list(n, rng)

    batch = ReconBatch()
    truth: dict[str, str] = {}
    start = date(2026, 7, 1)

    for i, recipe in enumerate(recipes, start=1):
        invoice_id = f"INV-2026-{i:04d}"
        # Spread across the window so due dates land on every day of the
        # week, not just one - if every invoice were issued on a Monday the
        # weekend-skip boundary would never actually get exercised.
        issued = start + timedelta(days=rng.randint(0, 50))
        amount = rng.randint(800, 15_000) * 100

        batch.invoices.append(Invoice(
            invoice_id=invoice_id, customer_name=f"Customer {i}",
            amount=amount, date_issued=issued, status="paid"))
        truth[invoice_id] = str(RECIPE_TRUTH[recipe])

        if recipe == "unmatched_in_transit":
            continue                                   # billed, not settled

        promised = due_date(issued)
        if recipe in DELAY_WORKING_DAYS:
            settled_on = _walk_working_days(
                promised, DELAY_WORKING_DAYS[recipe])
        else:
            settled_on = promised

        fee = (amount * 200) // 10_000               # a flat 2% stand-in;
        fee += (fee * 1_800) // 10_000                # this agent does not
        net = amount - fee                            # audit the fee itself

        txn_id = f"pay_{rng.randrange(16 ** 12):012x}"
        utr = f"HDFCN{rng.randrange(10 ** 10):010d}"
        batch.settlements.append(Settlement(
            txn_id=txn_id, gross_amount=amount, fee_deducted=fee,
            net_settled=net, settlement_date=settled_on,
            invoice_reference=invoice_id, utr=utr))

    return batch, truth


def _recipe_list(n: int, rng: random.Random) -> list[str]:
    if n == 60:
        recipes = [r for r, count in CANONICAL_MIX.items() for _ in range(count)]
    else:
        recipes = []
        for recipe, count in CANONICAL_MIX.items():
            scaled = max(1, round(count * n / 60)) if recipe != "on_time" else 0
            recipes += [recipe] * scaled
        recipes += ["on_time"] * max(0, n - len(recipes))
        recipes = recipes[:n]
    rng.shuffle(recipes)
    return recipes
