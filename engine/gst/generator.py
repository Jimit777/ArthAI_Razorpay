"""
Synthetic purchase register and GSTR-2B, with known errors planted.

Same trick as the settlement generator (CLAUDE.md section 7): plant the errors,
hand back the answer key, and the demo becomes a measurement. Without this
there is no accuracy number, and without an accuracy number the track's bar -
"throughput plus measured accuracy plus an honest exception list" - cannot be
met at all.

## What a real reconciliation actually joins

    purchase register   what the merchant's books say they bought and paid tax on
    GSTR-2B             what the government says suppliers actually reported

The join key is (supplier GSTIN, invoice number). Everything interesting is a
row that appears on one side and not the other, or on both with different
numbers - which is why the planted errors are mostly about breaking that join
in the specific ways it breaks in practice.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

from engine.gst.rules import SUPPLIER_PAYMENT_DAYS, Tolerance
from engine.gst.rules import orphan_id as rules_orphan_id
from engine.gst.taxonomy import ITCCode

# A batch is "as of" a fixed date so deadlines and 180-day windows are
# reproducible. Moving this moves which invoices are time-barred.
AS_OF = date(2026, 8, 24)

STATES = {"27": "Maharashtra", "24": "Gujarat", "29": "Karnataka",
          "07": "Delhi", "33": "Tamil Nadu", "06": "Haryana"}

SUPPLIERS = [
    ("Anand Textiles", "27", "fabric"),
    ("Kaveri Silk Mills", "29", "fabric"),
    ("Deepak Packaging", "24", "packaging"),
    ("Surat Trims & Buttons", "24", "trims"),
    ("Nashik Logistics", "27", "freight"),
    ("Bright Print House", "07", "printing"),
    ("Coimbatore Yarns", "33", "fabric"),
    ("Gurgaon Warehousing", "06", "storage"),
]

BLOCKABLE = [
    ("Le Cafe Catering", "27", "food_beverage"),
    ("Vayu Motors", "27", "motor_vehicle"),
    ("Fitwell Club", "29", "club_membership"),
]


@dataclass
class PurchaseInvoice:
    """One line in the merchant's own books."""
    invoice_id: str
    supplier_name: str
    supplier_gstin: str
    invoice_number: str
    invoice_date: date
    taxable_value: int              # paise
    cgst: int
    sgst: int
    igst: int
    category: Optional[str] = None  # set only when s.17(5) might bite
    paid_on: Optional[date] = None  # None = supplier still unpaid

    @property
    def total_tax(self) -> int:
        return self.cgst + self.sgst + self.igst


@dataclass
class GSTR2BLine:
    """One line the government says a supplier reported."""
    supplier_gstin: str
    invoice_number: str
    invoice_date: date
    taxable_value: int
    cgst: int
    sgst: int
    igst: int
    filed_period: str               # "2026-07"

    @property
    def total_tax(self) -> int:
        return self.cgst + self.sgst + self.igst


@dataclass
class ITCBatch:
    purchases: list[PurchaseInvoice]
    gstr2b: list[GSTR2BLine]
    as_of: date = AS_OF
    tolerance: Tolerance = field(default_factory=Tolerance)
    period: str = "2026-07"


# n=60. Mirrors the settlement generator's composition: mostly clean, a handful
# of each anomaly, and decoys that must NOT be flagged.
CANONICAL_MIX: dict[str, int] = {
    "clean": 38,
    "rounding": 3,                  # decoy - under tolerance, must not flag
    "supplier_not_filed": 4,
    "gstin_mismatch": 2,
    "amount_mismatch": 3,
    "blocked_credit": 2,
    "time_barred": 2,
    "rule_37": 2,
    "duplicate": 1,
    "not_in_books": 2,
    "late_filed": 1,
}

RECIPE_TRUTH: dict[str, ITCCode] = {
    "clean": ITCCode.CLAIM_CLEAN,
    "rounding": ITCCode.ROUNDING,
    "supplier_not_filed": ITCCode.SUPPLIER_NOT_FILED,
    "gstin_mismatch": ITCCode.GSTIN_MISMATCH,
    "amount_mismatch": ITCCode.AMOUNT_MISMATCH,
    "blocked_credit": ITCCode.BLOCKED_CREDIT,
    "time_barred": ITCCode.TIME_BARRED,
    "rule_37": ITCCode.RULE_37_REVERSAL,
    "duplicate": ITCCode.DUPLICATE_CLAIM,
    "not_in_books": ITCCode.NOT_IN_BOOKS,
    "late_filed": ITCCode.SUPPLIER_LATE_FILED,
}

# Anomalies must be found; decoys must be left alone.
DECOY_RECIPES = {"rounding"}
CLEAN_RECIPES = {"clean"}


def _gstin(state: str, rng: random.Random) -> str:
    """15 characters: state code, a PAN-shaped middle, entity digit, Z, check."""
    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    pan = ("".join(rng.choice(letters) for _ in range(5))
           + f"{rng.randint(0, 9999):04d}"
           + rng.choice(letters))
    return f"{state}{pan}{rng.randint(1, 9)}Z{rng.choice(letters)}"


def _split_tax(taxable: int, rate_bps: int, interstate: bool) -> tuple[int, int, int]:
    """
    GST splits by where the supplier and buyer are.

    Same state -> CGST + SGST, half each. Different states -> IGST, all of it.
    The total is identical either way, which is exactly why a wrong split is
    invisible in a total-only comparison and shows up here.
    """
    total = (taxable * rate_bps + 5_000) // 10_000
    if interstate:
        return 0, 0, total
    half = total // 2
    return half, total - half, 0


def generate_batch(n: int = 60, seed: int = 20260905
                   ) -> tuple[ITCBatch, dict[str, str]]:
    """
    Returns (batch, ground_truth) where ground_truth maps invoice_id to the
    ITCCode that should be concluded about it.
    """
    rng = random.Random(seed)
    recipes = _recipe_list(n, rng)

    purchases: list[PurchaseInvoice] = []
    gstr2b: list[GSTR2BLine] = []
    truth: dict[str, str] = {}

    home_state = "27"               # the merchant is in Maharashtra
    counter = 0

    for recipe in recipes:
        counter += 1
        invoice_id = f"inv_{counter:04d}"
        _build(recipe, invoice_id, home_state, rng, purchases, gstr2b, truth)

    rng.shuffle(gstr2b)
    return ITCBatch(purchases=purchases, gstr2b=gstr2b), truth


def _recipe_list(n: int, rng: random.Random) -> list[str]:
    if n == 60:
        recipes = [r for r, count in CANONICAL_MIX.items() for _ in range(count)]
    else:
        # Scale the canonical mix, keeping at least one of every anomaly so a
        # smaller batch still exercises every rule.
        recipes = []
        for recipe, count in CANONICAL_MIX.items():
            scaled = max(1, round(count * n / 60)) if recipe != "clean" else 0
            recipes += [recipe] * scaled
        recipes += ["clean"] * max(0, n - len(recipes))
        recipes = recipes[:n]
    rng.shuffle(recipes)
    return recipes


def _build(recipe, invoice_id, home_state, rng, purchases, gstr2b, truth) -> None:
    from engine.gst.rules import claim_deadline

    name, state, _kind = rng.choice(SUPPLIERS)
    category = None

    if recipe == "blocked_credit":
        name, state, category = rng.choice(BLOCKABLE)

    gstin = _gstin(state, rng)
    interstate = state != home_state
    taxable = rng.randrange(5_000_00, 250_000_00, 100)
    rate_bps = rng.choice([500, 1_200, 1_800])

    # Dates: most invoices are recent; the ones that need to be old are made old
    # deliberately rather than by luck.
    if recipe == "time_barred":
        # Must be past 30 Nov of the year after its FY. An FY2024-25 invoice
        # was due by 30 Nov 2025, which is behind us.
        invoice_date = date(2024, rng.randint(5, 12), rng.randint(1, 28))
        assert claim_deadline(invoice_date) < AS_OF
    elif recipe == "rule_37":
        # Old enough that 180 days have passed, but not so old it is also
        # time-barred - otherwise the record has two truths and the answer key
        # is ambiguous.
        invoice_date = AS_OF - timedelta(days=rng.randint(200, 300))
        assert claim_deadline(invoice_date) > AS_OF
    else:
        invoice_date = AS_OF - timedelta(days=rng.randint(5, 90))

    cgst, sgst, igst = _split_tax(taxable, rate_bps, interstate)
    number = f"{name.split()[0][:3].upper()}/{rng.randint(1000, 9999)}"

    paid_on = None if recipe == "rule_37" else invoice_date + timedelta(
        days=rng.randint(1, 40))

    invoice = PurchaseInvoice(
        invoice_id=invoice_id, supplier_name=name, supplier_gstin=gstin,
        invoice_number=number, invoice_date=invoice_date,
        taxable_value=taxable, cgst=cgst, sgst=sgst, igst=igst,
        category=category, paid_on=paid_on)

    def filed(**overrides) -> GSTR2BLine:
        base = dict(supplier_gstin=gstin, invoice_number=number,
                    invoice_date=invoice_date, taxable_value=taxable,
                    cgst=cgst, sgst=sgst, igst=igst, filed_period="2026-07")
        base.update(overrides)
        return GSTR2BLine(**base)

    if recipe == "not_in_books":
        # Only the government has it. No purchase invoice at all - the answer
        # key is keyed on the 2B line instead.
        line = filed()
        gstr2b.append(line)
        truth[rules_orphan_id(line.supplier_gstin, line.invoice_number)] = \
            str(ITCCode.NOT_IN_BOOKS)
        return

    purchases.append(invoice)
    truth[invoice_id] = str(RECIPE_TRUTH[recipe])

    if recipe == "supplier_not_filed":
        return                                   # deliberately absent from 2B

    if recipe == "blocked_credit":
        gstr2b.append(filed())                   # filed correctly, still blocked
        return

    if recipe == "gstin_mismatch":
        # Filed under a GSTIN in a different state. Everything else matches,
        # which is why a total-only comparison finds nothing.
        other = rng.choice([s for s in STATES if s != gstin[:2]])
        gstr2b.append(filed(supplier_gstin=_gstin(other, rng)))
        return

    if recipe == "amount_mismatch":
        # A real difference, deliberately above the tolerance band so it is
        # findable. Under it, the correct answer is ROUNDING and the answer key
        # would be wrong.
        band = Tolerance().band(invoice.total_tax)
        shortfall = max(band * 3, 500_00)
        if igst:
            gstr2b.append(filed(igst=max(0, igst - shortfall)))
        else:
            gstr2b.append(filed(cgst=max(0, cgst - shortfall)))
        return

    if recipe == "rounding":
        # Under the band. Must be dismissed, not flagged.
        nudge = max(1, Tolerance().band(invoice.total_tax) // 2)
        if igst:
            gstr2b.append(filed(igst=igst - nudge))
        else:
            gstr2b.append(filed(cgst=cgst - nudge))
        return

    if recipe == "duplicate":
        # The same invoice booked twice under two ids, filed once.
        #
        # Only the SECOND booking is the bad claim. The first is a perfectly
        # good invoice that GSTR-2B supports, and calling both duplicates would
        # tell the merchant to drop credit they are entitled to. The convention
        # matters: a reconciliation reports the later of a pair, not the pair.
        gstr2b.append(filed())
        truth[invoice_id] = str(ITCCode.CLAIM_CLEAN)
        twin = PurchaseInvoice(**{**invoice.__dict__,
                                  "invoice_id": f"{invoice_id}b"})
        purchases.append(twin)
        truth[twin.invoice_id] = str(ITCCode.DUPLICATE_CLAIM)
        return

    if recipe == "late_filed":
        gstr2b.append(filed(filed_period="2026-08"))
        return

    gstr2b.append(filed())                       # clean, time_barred, rule_37
