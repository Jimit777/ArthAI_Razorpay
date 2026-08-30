"""
The outward-tax rules. Pure Python, no model, never wrong.

## What's reused, and why

`rupees`, `Tolerance`, `gstin_state`, `gstin_well_formed`, `financial_year_of`,
`INTEREST_PCT_BPS` come from `engine.gst.rules` - the same statutory facts
already coded once for the input-credit side, not reinvented here.
`GSTR1_DUE_DAY`/`GSTR3B_DUE_DAY`/`due_dates()` come from
`engine.gst.filing_history` - same reasoning.

`split_tax` is the one exception, kept as its own four-line copy rather than
imported. `engine/` code must never import from `merchant/` (stated
explicitly in `merchant/agents/gst.py`: "the engine knows nothing about
businesses, sessions or the web"), and `engine/gst/generator.py` already
keeps its own private copy alongside `merchant/suppliers.py`'s public one for
exactly that reason - a third copy here is consistent with what this
codebase already tolerates, not a new problem.

## Citation seams - read before touching any of these

Three numbers below are NOT verified against a current, dated source and
must not be treated as settled fact (CLAUDE.md section 16: "a wrong rule is
worse than a missing rule"). Each is a named constant with a comment saying
so, exactly like `engine/payout_timing/rules.py`'s `HOLIDAY_DATES` seam -
fixing the citation later is a one-line edit, not a redesign.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from engine.gst.filing_history import GSTR1_DUE_DAY, GSTR3B_DUE_DAY, due_dates
from engine.gst.rules import (INTEREST_PCT_BPS, Tolerance, financial_year_of,
                              gstin_state, gstin_well_formed, rupees)

__all__ = ["INTEREST_PCT_BPS", "Tolerance", "financial_year_of", "gstin_state",
          "gstin_well_formed", "rupees", "split_tax", "B2CL_THRESHOLD_PAISE",
          "E_INVOICING_TURNOVER_THRESHOLD_PAISE", "WRONG_ITC_INTEREST_PCT_BPS",
          "interest_on", "rule_88c_threshold", "IGST_UTILISATION_SOURCE",
          "QUARTERLY_FIXED_SUM_PCT_BPS", "GSTR1_DUE_DAY", "GSTR3B_DUE_DAY",
          "due_dates", "QRMP_TURNOVER_THRESHOLD_PAISE"]

# --- citation seams ---------------------------------------------------------

# The interstate-invoice-value line that separates B2CL from B2CS. Commonly
# quoted as Rs 1,00,000 - but this figure has moved by notification before
# and is NOT verified against a current, dated CBIC source. Confirm before
# relying on it for anything beyond a demo.
B2CL_THRESHOLD_PAISE = 100_000_00

# The AATO above which e-invoicing is mandatory. Has moved down by
# notification repeatedly (was Rs 500cr, then 100cr, 50cr, 20cr, 10cr, 5cr).
# NOT verified against a current, dated source - treat as a demo default,
# not a citation.
E_INVOICING_TURNOVER_THRESHOLD_PAISE = 5_00_00_000_00

# The QRMP fixed-sum method's percentage of the previous quarter's cash
# liability. Commonly quoted as 35% - not independently verified this
# session against a current, dated source.
QUARTERLY_FIXED_SUM_PCT_BPS = 3_500

# The aggregate annual turnover ceiling under which a registered person may
# opt into QRMP. Commonly quoted as Rs 5 crore - not independently verified
# this session against a current, dated CBIC source.
QRMP_TURNOVER_THRESHOLD_PAISE = 5_00_00_000_00

# The IGST-first ITC utilisation hierarchy is real and was described this
# session in general terms (IGST credit clears IGST then spills to CGST/SGST;
# CGST credit never offsets SGST and vice versa) but the exact rule number
# was not pinned down to a specific, dated citation - stated here as a fact
# to implement, not as a citable rule number to quote to a merchant.
IGST_UTILISATION_SOURCE = ("the CGST Act's ITC utilisation order - IGST "
                           "credit first, then CGST/SGST, never cross-major-"
                           "head - exact rule number not yet sourced")

# --- confirmed this session, safe to cite -----------------------------------

# CGST Act s.50: 18% p.a. on ordinary late payment (imported as
# INTEREST_PCT_BPS above), 24% p.a. where the shortfall is wrongly-claimed
# ITC rather than an ordinary understatement.
WRONG_ITC_INTEREST_PCT_BPS = 2_400
SOURCE_INTEREST_NORMAL = "CGST Act s.50(1) - 18% a year on late payment"
SOURCE_INTEREST_WRONG_ITC = "CGST Act s.50(3) - 24% a year on wrongly-availed ITC"

# Rule 88C: DRC-01B auto-issues when GSTR-1's declared liability exceeds
# GSTR-3B's paid tax by more than whichever is LOWER of Rs 1 lakh or 20% -
# same shape already coded in engine/gst/rules.py's notice_threshold for
# Rule 88D, confirmed by reading it this session.
RULE_88C_ABSOLUTE_PAISE = 100_000_00
RULE_88C_PCT_BPS = 2_000
SOURCE_RULE_88C = "CGST Rule 88C - DRC-01B issues above Rs 1 lakh or 20% of the paid tax, whichever is lower"


def rule_88c_threshold(paid_paise: int) -> int:
    """How far declared liability may exceed paid tax before Rule 88C fires.
    Mirrors engine.gst.rules.notice_threshold's exact shape."""
    pct = (abs(paid_paise) * RULE_88C_PCT_BPS) // 10_000
    return min(RULE_88C_ABSOLUTE_PAISE, pct)


def interest_on(amount_paise: int, days: int, category: str = "normal") -> int:
    """
    Interest under s.50 on an unpaid liability, in paise. `category` must be
    named by the caller ("normal" or "wrong_itc") - never inferred from the
    amount, since the same rupee shortfall means a different rate depending
    on WHY it's owed, a fact this function cannot see.
    """
    if amount_paise <= 0 or days <= 0:
        return 0
    rate = WRONG_ITC_INTEREST_PCT_BPS if category == "wrong_itc" else INTEREST_PCT_BPS
    return (abs(amount_paise) * rate * days + 365 * 10_000 // 2) // (365 * 10_000)


def split_tax(taxable_paise: int, rate_bps: int, interstate: bool
             ) -> tuple[int, int, int]:
    """CGST + SGST within a state, IGST across states. Integers throughout."""
    total = (taxable_paise * rate_bps + 5_000) // 10_000
    if interstate:
        return 0, 0, total
    half = total // 2
    return half, total - half, 0
