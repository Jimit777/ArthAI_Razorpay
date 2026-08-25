"""
The supplier-filing simulator.

The gateway simulator answers "what if your payment gateway charged you wrong?"
This answers "what if your supplier did not file?" - the same shape of question
about the other side of the books, and the reason a purchase register is worth
auditing at all.

Like the gateway simulator, this imports NOTHING from engine/. It decides what
suppliers report; the auditor decides whether that is a problem. If the two
shared a module, a demo would only ever be the auditor grading its own
homework.

## Why a simulator at all

In reality GSTR-2B is a JSON file the merchant downloads from the GST portal
once a month. There is no sandbox for it, no test-mode GSTIN, and no way to
make a real supplier deliberately fail to file for a demo. So the honest
approach is the one CLAUDE.md section 7.1 already takes for settlements: real
field shapes, simulated behaviour, and say so plainly.
"""

from __future__ import annotations

import random
from datetime import date, timedelta
from enum import StrEnum
from typing import Optional


class SupplierBehaviour(StrEnum):
    CORRECT = "correct"
    NOT_FILED = "not_filed"
    WRONG_GSTIN = "wrong_gstin"
    SHORT_REPORTED = "short_reported"
    FILED_LATE = "filed_late"


BEHAVIOUR_LABEL = {
    SupplierBehaviour.CORRECT:
        "File every invoice correctly and on time",
    SupplierBehaviour.NOT_FILED:
        "Do not file at all - the invoice never reaches GSTR-2B",
    SupplierBehaviour.WRONG_GSTIN:
        "File against a different registration in another state",
    SupplierBehaviour.SHORT_REPORTED:
        "File the invoice but report less tax than was charged",
    SupplierBehaviour.FILED_LATE:
        "File a period late, so the credit lands next month",
}

BEHAVIOUR_FINDS = {
    SupplierBehaviour.CORRECT: "nothing - the claim is clean",
    SupplierBehaviour.NOT_FILED: "SUPPLIER_NOT_FILED",
    SupplierBehaviour.WRONG_GSTIN: "GSTIN_MISMATCH",
    SupplierBehaviour.SHORT_REPORTED: "AMOUNT_MISMATCH",
    SupplierBehaviour.FILED_LATE: "SUPPLIER_LATE_FILED",
}

BEHAVIOUR_NOTE = {
    SupplierBehaviour.CORRECT:
        "The honest case. A clean purchase register should produce no findings.",
    SupplierBehaviour.NOT_FILED:
        "Since the Supreme Court upheld s.16(2)(c), the credit simply does not "
        "exist until they file - and proving they paid is your burden.",
    SupplierBehaviour.WRONG_GSTIN:
        "The credit exists but sits against the wrong registration. A "
        "correction, not a chase - and only a cross-GSTIN search finds it.",
    SupplierBehaviour.SHORT_REPORTED:
        "The taxable value matches and only the tax is short, which is what "
        "separates a keying error from a credit note.",
    SupplierBehaviour.FILED_LATE:
        "Ordinary and usually harmless. Worth knowing about, not worth alarm - "
        "unless the delay crosses the s.16(4) deadline.",
}

# Which of a merchant's purchases each behaviour actually touches. Same reason
# the gateway simulator carries this: set "wrong GSTIN", book one invoice, see
# nothing happen, and the auditor looks broken unless you knew the fault only
# applies to the supplier it was set for.
BEHAVIOUR_AFFECTS = {
    SupplierBehaviour.CORRECT: [],
    SupplierBehaviour.NOT_FILED: ["every invoice from that supplier"],
    SupplierBehaviour.WRONG_GSTIN: ["every invoice from that supplier"],
    SupplierBehaviour.SHORT_REPORTED: ["every invoice from that supplier"],
    SupplierBehaviour.FILED_LATE: ["every invoice from that supplier"],
}

# How much tax the supplier drops when short-reporting. Deliberately far above
# any sane tolerance band, because a fault the auditor cannot see is not a
# demonstration of anything.
SHORT_REPORT_PAISE = 500_00

STATES = {
    "27": "Maharashtra", "24": "Gujarat", "29": "Karnataka",
    "07": "Delhi", "33": "Tamil Nadu", "06": "Haryana", "09": "Uttar Pradesh",
}


def gstin_for(name: str, state: str = "27") -> str:
    """
    A GSTIN in the real 15-character shape, derived from the supplier name.

    Deterministic, so booking two invoices from "Anand Textiles" gives the same
    registration both times - which is what makes a duplicate or a cross-GSTIN
    search mean anything.
    """
    rng = random.Random(f"{state}:{name.strip().lower()}")
    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    pan = ("".join(rng.choice(letters) for _ in range(5))
           + f"{rng.randint(0, 9999):04d}" + rng.choice(letters))
    return f"{state}{pan}{rng.randint(1, 9)}Z{rng.choice(letters)}"


def split_tax(taxable_paise: int, rate_bps: int, interstate: bool
              ) -> tuple[int, int, int]:
    """
    CGST + SGST within a state, IGST across states. Integers throughout.

    The total is identical either way, which is exactly why a wrong split is
    invisible in a total-only comparison.
    """
    total = (taxable_paise * rate_bps + 5_000) // 10_000
    if interstate:
        return 0, 0, total
    half = total // 2
    return half, total - half, 0


def file_invoice(*, supplier_gstin: str, invoice_number: str,
                 invoice_date: date, taxable_value: int, cgst: int, sgst: int,
                 igst: int, period: str,
                 behaviour: SupplierBehaviour) -> Optional[dict]:
    """
    What the supplier actually reports to the government for one invoice.

    Returns the GSTR-2B line, or None when they file nothing at all. The
    merchant's own books are untouched by this - that gap between what was
    booked and what was reported IS the product.
    """
    if behaviour is SupplierBehaviour.NOT_FILED:
        return None

    gstin = supplier_gstin
    if behaviour is SupplierBehaviour.WRONG_GSTIN:
        # Same supplier, a registration in a different state. Everything else
        # matches, so only a search across GSTINs will find it.
        other = next(s for s in STATES if s != supplier_gstin[:2])
        gstin = gstin_for(f"{invoice_number}-misfiled", other)

    if behaviour is SupplierBehaviour.SHORT_REPORTED:
        # Drop the tax in ONE half only. A short CGST with a correct SGST is
        # the fingerprint of a keying error, and is what lets the agent rule
        # out a credit note or a partial supply.
        if igst:
            igst = max(0, igst - SHORT_REPORT_PAISE)
        else:
            cgst = max(0, cgst - SHORT_REPORT_PAISE)

    filed_period = period
    if behaviour is SupplierBehaviour.FILED_LATE:
        filed_period = _next_period(period)

    return {
        "supplier_gstin": gstin,
        "invoice_number": invoice_number,
        "invoice_date": invoice_date,
        "taxable_value": taxable_value,
        "cgst": cgst, "sgst": sgst, "igst": igst,
        "filed_period": filed_period,
    }


def _next_period(period: str) -> str:
    year, month = (int(p) for p in period.split("-"))
    return f"{year + 1}-01" if month == 12 else f"{year}-{month + 1:02d}"


def current_period(when: Optional[date] = None) -> str:
    when = when or date.today()
    return f"{when.year}-{when.month:02d}"
