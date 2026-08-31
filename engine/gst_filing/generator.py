"""
Synthetic outward sales, with known classifications planted.

Same trick as every generator in this project (CLAUDE.md section 7): plant
the answer, hand back the key, and the demo becomes a measurement.

Built incrementally alongside the layers that consume it - this checkpoint
plants layer 1's invoice-classification scenario only. Layers 2-4 extend
this module with their own planted periods/cycles/ledger snapshots as they
land, rather than front-loading a four-month scenario before anything
downstream exists to test it against.
"""

from __future__ import annotations

import random
from datetime import date, timedelta

from engine.gst_filing.classifier import OutwardInvoice
from engine.gst_filing.taxonomy import CorrectionCode, GSTR1Code
from engine.gst_filing.timing import FilingCycle

HOME_STATE = "27"                      # the demo merchant is in Maharashtra
AS_OF = date(2026, 8, 24)

# A small, honest demo rate card - not a real HSN-to-rate lookup (none
# exists anywhere in this codebase; see rules.py's citation-seam notes),
# just a handful of plausible product categories at real GST rate slabs.
DEMO_RATE_CARD: dict[str, int] = {
    "6109": 500,     # apparel, cotton knit t-shirts - 5%
    "8471": 1_800,   # computers and peripherals - 18%
    "9403": 1_200,   # furniture - 12%
    "3304": 1_800,   # cosmetics - 18%
    # "6403" (footwear) deliberately absent - the HSN_RATE_UNCONFIGURED plant
}
UNCONFIGURED_HSN = "6403"

STATES = {"27": "Maharashtra", "24": "Gujarat", "29": "Karnataka",
         "07": "Delhi", "33": "Tamil Nadu"}

BUYERS = ["Anand Retail", "Kaveri Traders", "Deepak Enterprises",
         "Surat Wholesale", "Nashik Distributors", "Bright Mart",
         "Coimbatore Textiles", "Gurgaon Systems"]

CANONICAL_MIX: dict[str, int] = {
    "b2b_clean": 16,
    "b2b_missing_irn": 6,
    "b2cl": 6,
    "b2cs": 8,
    "hsn_unconfigured": 4,
}


def _gstin(state: str, rng: random.Random) -> str:
    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    pan = ("".join(rng.choice(letters) for _ in range(5))
          + f"{rng.randint(0, 9999):04d}" + rng.choice(letters))
    return f"{state}{pan}{rng.randint(1, 9)}Z{rng.choice(letters)}"


def generate_invoices(n: int = 40, seed: int = 20260905
                      ) -> tuple[list[OutwardInvoice], dict[str, str]]:
    """
    Returns (invoices, ground_truth) where ground_truth maps invoice_id to
    the GSTR1Code it was built to produce.
    """
    rng = random.Random(seed)
    recipes = _recipe_list(n, rng)
    start = date(2026, 8, 1)

    invoices: list[OutwardInvoice] = []
    truth: dict[str, str] = {}

    for i, recipe in enumerate(recipes, start=1):
        invoice_id = f"INV-{i:04d}"
        issued = start + timedelta(days=rng.randint(0, 20))
        hsn = UNCONFIGURED_HSN if recipe == "hsn_unconfigured" else rng.choice(
            list(DEMO_RATE_CARD))
        buyer = BUYERS[i % len(BUYERS)]

        if recipe in ("b2b_clean", "b2b_missing_irn"):
            state = HOME_STATE if rng.random() < 0.5 else rng.choice(
                [s for s in STATES if s != HOME_STATE])
            gstin = _gstin(state, rng)
            taxable = rng.randint(5_000, 80_000) * 100
            irn = (None if recipe == "b2b_missing_irn"
                  else f"IRN{rng.randrange(10 ** 12):012d}")
            truth[invoice_id] = (str(GSTR1Code.IRN_MISSING)
                                if recipe == "b2b_missing_irn"
                                else str(GSTR1Code.CLASSIFIED))
        elif recipe == "b2cl":
            state = rng.choice([s for s in STATES if s != HOME_STATE])
            gstin = None
            # Deliberately above the B2CL threshold - findability, same
            # discipline as every other generator's planted amounts.
            taxable = rng.randint(120_000, 300_000) * 100
            irn = None
            truth[invoice_id] = str(GSTR1Code.CLASSIFIED)
        elif recipe == "hsn_unconfigured":
            state = HOME_STATE
            gstin = None
            taxable = rng.randint(2_000, 20_000) * 100
            irn = None
            truth[invoice_id] = str(GSTR1Code.HSN_RATE_UNCONFIGURED)
        else:                                             # b2cs
            state = HOME_STATE if rng.random() < 0.7 else rng.choice(
                [s for s in STATES if s != HOME_STATE])
            gstin = None
            taxable = rng.randint(500, 15_000) * 100       # under B2CL threshold
            irn = None
            truth[invoice_id] = str(GSTR1Code.CLASSIFIED)

        invoices.append(OutwardInvoice(
            invoice_id=invoice_id, invoice_number=f"INV/{i:04d}",
            invoice_date=issued, buyer_name=buyer, buyer_gstin=gstin,
            place_of_supply=state, hsn_code=hsn, taxable_value=taxable,
            irn=irn))

    return invoices, truth


def _period_before(period: str, months: int) -> str:
    year, month = (int(p) for p in period.split("-"))
    total = year * 12 + (month - 1) - months
    y, m = divmod(total, 12)
    return f"{y}-{m + 1:02d}"


def generate_cycles(current_period: str, current_liability_paise: int
                    ) -> tuple[list[FilingCycle], dict[str, str]]:
    """
    Three planted prior periods plus the current one, built from layer 1's
    own just-assembled GSTR-1 total so the two layers tell one consistent
    story rather than two unrelated demos. Deterministic - nothing here
    needs a seed, the same way the ground truth for a planted scenario never
    does.

    Returns (cycles, ground_truth) where ground_truth maps period -> the
    CorrectionCode it was built to produce:
      current_period - 4   LOCKED_NEEDS_DRC03     (filed, gap breaches Rule
                                                    88C - see layer 3)
      current_period - 3   PERIOD_CLEAN           (matches, filed on time)
      current_period - 2   LOCKED_NEEDS_DRC03     (filed, ordinary shortfall)
      current_period - 1   LOCKED_NEEDS_DRC03     (filed, wrongly-claimed ITC)
      current_period       CORRECTABLE_VIA_1A     (not yet filed, real gap)
    """
    from engine.gst.filing_history import due_dates

    p_88c_breach = _period_before(current_period, 4)
    p_clean = _period_before(current_period, 3)
    p_locked_normal = _period_before(current_period, 2)
    p_locked_wrong_itc = _period_before(current_period, 1)

    _, due_88c = due_dates(p_88c_breach)
    _, due_clean = due_dates(p_clean)
    _, due_normal = due_dates(p_locked_normal)
    _, due_wrong = due_dates(p_locked_wrong_itc)

    # Gap of Rs 30,000 against Rs 50,000 paid: Rule 88C's threshold is the
    # LOWER of Rs 1 lakh or 20% of paid (Rs 10,000 here) - this gap clears
    # it, unlike the two ordinary LOCKED periods below whose gaps are built
    # to stay under their own 20% line. See engine/gst_filing/offset.py.
    breach_liability, breach_paid = 80_000_00, 50_000_00
    clean_liability = 45_000_00
    normal_liability, normal_paid = 52_300_00, 48_000_00
    wrong_liability, wrong_paid = 61_750_00, 53_250_00
    current_paid = current_liability_paise - 12_000_00

    cycles = [
        FilingCycle(period=p_88c_breach, gstr1_liability=breach_liability,
                   gstr3b_filed=due_88c + timedelta(days=2),
                   gstr3b_paid=breach_paid),
        FilingCycle(period=p_clean, gstr1_liability=clean_liability,
                   gstr3b_filed=due_clean - timedelta(days=1),
                   gstr3b_paid=clean_liability),
        FilingCycle(period=p_locked_normal, gstr1_liability=normal_liability,
                   gstr3b_filed=due_normal + timedelta(days=2),
                   gstr3b_paid=normal_paid),
        FilingCycle(period=p_locked_wrong_itc, gstr1_liability=wrong_liability,
                   gstr3b_filed=due_wrong + timedelta(days=1),
                   gstr3b_paid=wrong_paid,
                   wrongly_claimed_itc_paise=wrong_liability - wrong_paid),
        FilingCycle(period=current_period,
                   gstr1_liability=current_liability_paise,
                   gstr3b_filed=None, gstr3b_paid=current_paid),
    ]
    truth = {
        p_88c_breach: str(CorrectionCode.LOCKED_NEEDS_DRC03),
        p_clean: str(CorrectionCode.PERIOD_CLEAN),
        p_locked_normal: str(CorrectionCode.LOCKED_NEEDS_DRC03),
        p_locked_wrong_itc: str(CorrectionCode.LOCKED_NEEDS_DRC03),
        current_period: str(CorrectionCode.CORRECTABLE_VIA_1A),
    }
    return cycles, truth


def _recipe_list(n: int, rng: random.Random) -> list[str]:
    if n == 40:
        recipes = [r for r, count in CANONICAL_MIX.items() for _ in range(count)]
    else:
        recipes = []
        for recipe, count in CANONICAL_MIX.items():
            recipes += [recipe] * max(1, round(count * n / 40))
        recipes = recipes[:n]
    rng.shuffle(recipes)
    return recipes


def quarter_of(period: str) -> tuple[str, int]:
    """
    The Indian FY quarter a 'YYYY-MM' period falls in, and that period's
    position within it (1, 2 or 3). Apr-Jun is Q1, Jul-Sep is Q2, and so on
    - the financial year, not the calendar year.
    """
    year, month = (int(p) for p in period.split("-"))
    fy_month = (month - 4) % 12               # 0 = April
    q_index = fy_month // 3 + 1
    month_in_quarter = fy_month % 3 + 1
    fy_start_year = year if month >= 4 else year - 1
    label = f"Q{q_index} FY{fy_start_year}-{str(fy_start_year + 1)[-2:]}"
    return label, month_in_quarter


def plant_qrmp_quarter(current_period: str, current_month_taxable_paise: int,
                       current_month_self_assessed_paise: int,
                       current_month_b2b_tax_paise: list[int]
                       ) -> dict:
    """
    Layer 1 only ever gives this demo ONE real month of invoices - the
    current period. A QRMP quarter needs two months of self-assessment to
    compare against the fixed-sum safe harbour, so month 1 here is an
    ESTIMATE built from the one real month (85% of it, for a plausible
    month-to-month dip), never a second month of fabricated invoices.
    Month 3 (returned as "month3_liability_paise", for
    qrmp.build_quarterly_gstr3b - not consumed by build_qrmp_plan) is a
    second, independent estimate (110% of the real month) for the same
    reason. Turnover is likewise a naive x12 annualisation of the one real
    month's taxable value, clearly a demo stand-in - no real API for a
    business's own annual turnover exists anywhere in this codebase (same
    honest-gap shape as live_gst_ledger_balances).

    Returns (kwargs, month3_liability_paise): `kwargs` are exactly what
    engine.gst_filing.qrmp.build_qrmp_plan() takes (spread it with **,
    nothing extra to pop off first); `month3_liability_paise` is a second,
    independent estimate for qrmp.build_quarterly_gstr3b, which
    build_qrmp_plan() itself never touches.
    """
    quarter, month_in_quarter = quarter_of(current_period)
    month1_estimate = (current_month_self_assessed_paise * 85) // 100
    month3_estimate = (current_month_self_assessed_paise * 110) // 100

    kwargs = {
        "quarter": quarter,
        "turnover_paise": current_month_taxable_paise * 12,
        "previous_quarter_cash_paise": current_month_self_assessed_paise * 3,
        "month1_self_assessed_paise": month1_estimate,
        "month2_self_assessed_paise": current_month_self_assessed_paise,
        "month1_iff_invoices": [],       # no invoice-level data for the
                                          # estimated month - see docstring
        "month2_iff_invoices": list(current_month_b2b_tax_paise),
    }
    return kwargs, month3_estimate
