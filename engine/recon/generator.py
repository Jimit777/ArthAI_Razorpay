"""
Fifty-five linked records across three sources, with known faults planted.

## Why this returns an answer key

Same trick as generator/synthetic.py, and for the same reason (CLAUDE.md
section 7): a match rate is only worth saying out loud if somebody can check
it. `generate(55)` returns the three sources AND a dict of what each record
was built to be, so the match rate is measured rather than asserted, and a
matcher that quietly stops finding the hard cases fails a test instead of
producing a slightly worse number nobody notices.

## The composition, and why it is not all clean

    ~80%   perfect three-way match
    ~10%   settled correctly but the invoice reference is missing, so the
           only way through is amount and date
    ~10%   genuine exceptions - money that did not arrive, arrived short, or
           arrived with nothing behind it

Eighty per cent clean is deliberate. A demo where half the records are broken
looks like a data-quality problem rather than a reconciliation; the interesting
claim is finding eleven bad records inside fifty-five, not finding twenty-seven.

## The bank descriptions are deliberately unhelpful

Real bank statements carry a single free-text field written by whichever
system pushed the payment, and no two banks agree on its shape. The formats
below are the ones Indian current accounts actually produce, including the one
that truncates the UTR - which is what Pass 3 exists to survive.
"""

from __future__ import annotations

import random
from datetime import date, timedelta

from engine.recon.records import (AMOUNT_MISMATCH, MATCHED, MATCHED_FUZZY,
                                  MISSING_IN_BANK, MISSING_IN_GATEWAY,
                                  ORPHAN_BANK_CREDIT, UNEXPLAINED_FEE,
                                  BankCredit, Invoice, ReconBatch, Settlement)

CUSTOMERS = [
    "Sunrise Retail", "Meridian Traders", "Kavya Enterprises",
    "Bluebird Logistics", "Ashok General Stores", "Nandini Dairy Supply",
    "Vertex Components", "Priya Handlooms", "Orion Electricals",
    "Sagar Marine Foods", "Lotus Stationers", "Ganesh Hardware",
    "Trident Apparel", "Kamala Bakers", "Zenith Instruments",
    "Rathore Motors", "Bela Cosmetics", "Sharma Book Depot",
    "Indus Valley Ceramics", "Padma Jewellers",
]

# The gateway's cut, in basis points, plus GST on it. Not the point of this
# agent - the settlement auditor checks whether the RATE was right - but the
# arithmetic has to tie out or the three-way join is meaningless.
FEE_BPS = 200
GST_BPS = 1_800

# Bank statement narrations, as banks actually write them. The last one drops
# the last four characters of the UTR, which is common and is the case Pass 3
# has to survive.
NARRATIONS = (
    "NEFT-RAZORPAY-SETTLEMENT-{utr}",
    "IMPS/{utr}/RAZORPAY SOFTWARE PVT",
    "NEFT CR-RATN0000088-RAZORPAY-{utr}",
    "ACH-C- RAZORPAYSETTLEMENT-{utr}",
    "MB-NEFT-{short}-RZPY",
)


def _fee_on(gross: int) -> tuple[int, int]:
    """The gateway's fee and the net, as integers. Never floats."""
    fee = (gross * FEE_BPS) // 10_000
    fee += (fee * GST_BPS) // 10_000
    return fee, gross - fee


def generate(n: int = 55, seed: int = 20260905
             ) -> tuple[ReconBatch, dict[str, str]]:
    """
    Build the three sources and the answer key.

    Returns (batch, truth) where truth maps a record id - an invoice id, or a
    txn id or UTR for records with no invoice behind them - to the finding it
    was built to produce.
    """
    rng = random.Random(seed)
    batch = ReconBatch()
    truth: dict[str, str] = {}

    n_clean = round(n * 0.80)
    n_fuzzy = round(n * 0.10)
    n_broken = n - n_clean - n_fuzzy
    # The exception budget, dealt round-robin across the four kinds rather
    # than piled on one, so a matcher cannot score well by handling a single
    # case - and so every kind appears at any batch size, instead of the
    # rarest one vanishing on a small run and taking its test coverage with
    # it.
    kinds = (MISSING_IN_BANK, UNEXPLAINED_FEE, AMOUNT_MISMATCH,
             MISSING_IN_GATEWAY)
    faults = [kinds[i % len(kinds)] for i in range(n_broken)]

    plan = [MATCHED] * n_clean + [MATCHED_FUZZY] * n_fuzzy + faults
    rng.shuffle(plan)

    start = date(2026, 7, 1)
    for i, intended in enumerate(plan, start=1):
        invoice_id = f"INV-2026-{i:04d}"
        customer = CUSTOMERS[i % len(CUSTOMERS)]
        # Varied to the rupee rather than drawn from a handful of round
        # figures. Ten fixed amounts made two unreferenced settlements collide
        # constantly, and Pass 2 correctly refused to guess between them - so
        # the generator, not the matcher, was manufacturing ambiguity that a
        # real ledger does not have. Real invoice values spread; these do too.
        gross = rng.randint(800, 15_000) * 100
        issued = start + timedelta(days=rng.randint(0, 45))
        fee, net = _fee_on(gross)

        batch.invoices.append(Invoice(
            invoice_id=invoice_id, customer_name=customer, amount=gross,
            date_issued=issued, status="paid"))
        truth[invoice_id] = intended

        if intended is MISSING_IN_GATEWAY:
            # Billed, never settled. No B and no C at all.
            continue

        txn_id = f"pay_{rng.randrange(16 ** 12):012x}"
        utr = f"HDFCN{rng.randrange(10 ** 10):010d}"
        settled_on = issued + timedelta(days=2)

        batch.settlements.append(Settlement(
            txn_id=txn_id, gross_amount=gross, fee_deducted=fee,
            net_settled=net, settlement_date=settled_on, utr=utr,
            # The fuzzy case: the reference the join would normally use is
            # simply not there, which is the commonest real defect in this
            # data.
            invoice_reference=None if intended is MATCHED_FUZZY else invoice_id))

        if intended is MISSING_IN_BANK:
            # Settled and never credited. The urgent one.
            continue

        credited = net
        credited_on = settled_on
        if intended is UNEXPLAINED_FEE:
            # A bank charge nobody agreed to. Small, so it survives a
            # tolerance band and still has to be explained.
            credited = net - rng.choice([1180, 2360, 5900])
        elif intended is AMOUNT_MISMATCH:
            credited = net - rng.choice([25000, 47500, 88000])
            credited_on = settled_on + timedelta(days=rng.randint(0, 2))
        elif intended is MATCHED_FUZZY:
            # No reference anywhere, and the credit lands a day or two later -
            # so amount alone is not enough and the date window matters.
            credited_on = settled_on + timedelta(days=rng.randint(0, 2))

        shape = NARRATIONS[i % len(NARRATIONS)]
        # Every fifth statement line carries the bank's OWN reference in the
        # UTR column and the gateway's only in the narration. Extremely common
        # - plenty of current accounts have no clean UTR field at all - and it
        # is the case Pass 3 exists for. Without it the narration parser was
        # dead code that every test passed.
        on_statement = (f"REF{rng.randrange(10 ** 9):09d}"
                        if i % 5 == 0 else utr)
        batch.bank.append(BankCredit(
            utr_number=on_statement,
            description=shape.format(utr=utr, short=utr[:-4]),
            credit_amount=credited, transaction_date=credited_on))

    # One credit that belongs to nobody. Every real statement has one, and a
    # matcher that never looks for them silently drops money.
    orphan_utr = f"HDFCN{rng.randrange(10 ** 10):010d}"
    batch.bank.append(BankCredit(
        utr_number=orphan_utr,
        description=f"NEFT-INWARD-MISC-{orphan_utr}",
        credit_amount=rng.choice([64000, 91500]),
        transaction_date=start + timedelta(days=rng.randint(5, 40))))
    truth[orphan_utr] = ORPHAN_BANK_CREDIT

    return batch, truth
