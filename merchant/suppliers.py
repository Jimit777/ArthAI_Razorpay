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


# --- picking a behaviour when several are switched on ----------------------
#
# The switch used to be one behaviour for the whole book, which produces a
# register where every supplier has the same fault. That demonstrates one
# finding well and the thing the product is actually for - a mixed register
# where several kinds of problem sit side by side and have to be told apart -
# not at all.
#
# So several can be selected at once. The question is then what a mix MEANS,
# and the answer is not "pick one at random per invoice":
#
# Filing behaviour is a property of the SUPPLIER, not of an invoice. A supplier
# who misfiles to a Karnataka registration does it on every invoice, which is
# exactly why a cross-GSTIN search finds it. Re-rolling per invoice would give
# one supplier three different faults across three months, and the findings
# would stop meaning anything - GSTIN_MISMATCH in particular becomes
# unfindable, because there is no consistent wrong registration to search for.
#
# So each SUPPLIER is assigned one behaviour from the selected set, chosen
# deterministically from their name. The same supplier always behaves the same
# way, different suppliers exhibit different faults, and the register is
# reproducible - open it twice and it says the same thing.

SEPARATOR = ","


def parse_behaviours(text) -> list["SupplierBehaviour"]:
    """
    The stored setting as a list.

    Accepts a bare value as well as a list, because that is what every row
    written before this feature existed contains, and a migration that
    rewrites live rows to add a feature is a worse trade than four lines here.
    Unknown values are dropped rather than raising: a setting a merchant
    cannot correct from the UI must not be able to break their register.
    """
    if not text:
        return [SupplierBehaviour.CORRECT]
    if isinstance(text, SupplierBehaviour):
        return [text]
    if isinstance(text, str):
        parts = [p.strip() for p in text.split(SEPARATOR)]
    else:
        parts = [str(p).strip() for p in text]

    out = []
    for part in parts:
        try:
            found = SupplierBehaviour(part)
        except ValueError:
            continue
        if found not in out:
            out.append(found)
    return out or [SupplierBehaviour.CORRECT]


def join_behaviours(chosen) -> str:
    """The list as it is stored. Order follows the enum so it is stable."""
    kept = [b for b in SupplierBehaviour if b in set(parse_behaviours(chosen))]
    return SEPARATOR.join(str(b) for b in kept)


def next_behaviour(chosen, already_assigned: int) -> "SupplierBehaviour":
    """
    Which behaviour the NEXT new supplier gets, given how many already have one.

    A rotation rather than a hash of the name, and the reason is small numbers.
    Hashing distributes well over hundreds of suppliers and badly over the six
    in a demo register - selecting all five faults and watching four suppliers
    land on the same one is a worse demonstration than the single choice this
    replaces. A rotation guarantees that every selected behaviour appears as
    soon as there are enough suppliers to go round.

    Stability comes from the caller, not from here: a supplier who already has
    a behaviour keeps it, and this is only asked about ones that do not. See
    Ledger._supplier_behaviour, where that lookup lives, and note why it
    matters - a supplier whose fault changed between two invoices would make
    GSTIN_MISMATCH unfindable, because there would be no consistent wrong
    registration to search for.
    """
    options = parse_behaviours(chosen)
    return options[already_assigned % len(options)]


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
