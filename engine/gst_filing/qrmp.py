"""
Layer 4: the QRMP method choice and the IFF plan.

Fully mechanical - zero agent calls, ever. See taxonomy.py's module
docstring: eligibility is a turnover comparison, the fixed-sum vs
self-assessment choice is a >= comparison between two already-computed
rupee figures, and whether an invoice clears the bar for an optional IFF
filing is a materiality threshold the merchant sets, not a judgment call
this system makes on their behalf.

## What each piece answers

  eligible()             is this business under the QRMP turnover ceiling?

  fixed_sum_amount()     35% of the previous quarter's cash liability - the
                         "safe harbour" monthly amount that avoids interest
                         even if it turns out short of the real liability.

  recommend_method()     fixed-sum or self-assessment for months 1 and 2 of
                         the quarter - whichever asks for LESS cash across
                         those two months, since QRMP's whole point is
                         deferring cash and a merchant who can self-assess
                         lower has no reason to overpay a safe harbour.

  iff_worth_filing()     does this invoice clear the merchant's own
                         materiality bar for filing early? Only months 1
                         and 2 of the quarter have an IFF window - month 3
                         is covered by the quarter's own regular GSTR-1, so
                         this always returns False for month 3.

## The demo's month 1 is estimated, not invented

This codebase's demo data has one real month of classified invoices
(layer 1's own current period). A QRMP quarter needs two months of
self-assessment to compare against the fixed-sum safe harbour, and there is
no invoice data for the quarter's other demo month - see
engine/gst_filing/generator.py's `plant_qrmp_quarter()` for exactly how
that gap is estimated and labelled, never presented as a second real month
of invoices.
"""

from __future__ import annotations

from dataclasses import dataclass

from engine.gst_filing import rules
from engine.gst_filing.taxonomy import QRMPMethod


def eligible(turnover_paise: int) -> bool:
    return turnover_paise <= rules.QRMP_TURNOVER_THRESHOLD_PAISE


def fixed_sum_amount(previous_quarter_cash_paise: int) -> int:
    """The safe-harbour amount, per month - 35% of the whole PRIOR
    quarter's cash liability, not per month of it."""
    return (previous_quarter_cash_paise * rules.QUARTERLY_FIXED_SUM_PCT_BPS
           + 5_000) // 10_000


def recommend_method(fixed_sum_paise: int, month1_self_assessed_paise: int,
                     month2_self_assessed_paise: int) -> QRMPMethod:
    """Compares the TWO-MONTH totals - the two months actually paid under
    QRMP before the quarter's own GSTR-3B settles the balance in month 3 -
    not a single month against a single month."""
    self_assessed_total = month1_self_assessed_paise + month2_self_assessed_paise
    fixed_sum_total = fixed_sum_paise * 2
    if self_assessed_total < fixed_sum_total:
        return QRMPMethod.SELF_ASSESSMENT
    return QRMPMethod.FIXED_SUM


def iff_worth_filing(invoice_value_paise: int, month_in_quarter: int,
                     materiality_paise: int) -> bool:
    """month_in_quarter is 1, 2 or 3 - IFF only exists for 1 and 2."""
    if month_in_quarter not in (1, 2):
        return False
    return invoice_value_paise >= materiality_paise


@dataclass
class QRMPFinding:
    quarter: str
    turnover_paise: int
    is_eligible: bool
    method: str
    fixed_sum_paise: int
    self_assessed_paise: int         # the two-month total compared against
    month1_pmt06: int
    month2_pmt06: int
    iff_used_month1: int
    iff_used_month2: int
    reasoning: str

    def as_dict(self) -> dict:
        return {
            "quarter": self.quarter, "turnover_paise": self.turnover_paise,
            "turnover_display": rules.rupees(self.turnover_paise),
            "eligible": self.is_eligible, "method": self.method,
            "fixed_sum_paise": self.fixed_sum_paise,
            "fixed_sum_display": rules.rupees(self.fixed_sum_paise),
            "self_assessed_paise": self.self_assessed_paise,
            "self_assessed_display": rules.rupees(self.self_assessed_paise),
            "month1_pmt06": self.month1_pmt06,
            "month1_pmt06_display": rules.rupees(self.month1_pmt06),
            "month2_pmt06": self.month2_pmt06,
            "month2_pmt06_display": rules.rupees(self.month2_pmt06),
            "iff_used_month1": self.iff_used_month1,
            "iff_used_month2": self.iff_used_month2,
            "reasoning": self.reasoning,
        }


def build_qrmp_plan(quarter: str, turnover_paise: int,
                    previous_quarter_cash_paise: int,
                    month1_self_assessed_paise: int,
                    month2_self_assessed_paise: int,
                    month1_iff_invoices: list[int],
                    month2_iff_invoices: list[int],
                    materiality_paise: int) -> QRMPFinding:
    """
    `month1_iff_invoices`/`month2_iff_invoices` are per-invoice values in
    paise for that month's B2B invoices - iff_worth_filing() is applied to
    each to count how many clear the merchant's own materiality bar.
    """
    is_eligible = eligible(turnover_paise)
    fixed_sum = fixed_sum_amount(previous_quarter_cash_paise)

    if not is_eligible:
        return QRMPFinding(
            quarter=quarter, turnover_paise=turnover_paise,
            is_eligible=False, method="", fixed_sum_paise=fixed_sum,
            self_assessed_paise=0, month1_pmt06=0, month2_pmt06=0,
            iff_used_month1=0, iff_used_month2=0,
            reasoning=(f"Turnover of {rules.rupees(turnover_paise)} is above "
                      f"the QRMP ceiling of "
                      f"{rules.rupees(rules.QRMP_TURNOVER_THRESHOLD_PAISE)} "
                      f"- not eligible for quarterly filing."))

    method = recommend_method(fixed_sum, month1_self_assessed_paise,
                              month2_self_assessed_paise)
    if method == QRMPMethod.FIXED_SUM:
        month1_pmt06 = month2_pmt06 = fixed_sum
    else:
        month1_pmt06, month2_pmt06 = (month1_self_assessed_paise,
                                      month2_self_assessed_paise)

    iff_1 = sum(1 for v in month1_iff_invoices
               if iff_worth_filing(v, 1, materiality_paise))
    iff_2 = sum(1 for v in month2_iff_invoices
               if iff_worth_filing(v, 2, materiality_paise))

    self_assessed_total = month1_self_assessed_paise + month2_self_assessed_paise
    fixed_sum_total = fixed_sum * 2
    reasoning = (
        f"Self-assessing both months would cost "
        f"{rules.rupees(self_assessed_total)} against a "
        f"{rules.rupees(fixed_sum_total)} fixed-sum safe harbour - "
        f"{'self-assessment' if method == QRMPMethod.SELF_ASSESSMENT else 'the fixed sum'} "
        f"ties up less cash. {iff_1 + iff_2} B2B invoice(s) across the "
        f"quarter's first two months clear the "
        f"{rules.rupees(materiality_paise)} bar for an early IFF filing.")

    return QRMPFinding(
        quarter=quarter, turnover_paise=turnover_paise, is_eligible=True,
        method=str(method), fixed_sum_paise=fixed_sum,
        self_assessed_paise=self_assessed_total, month1_pmt06=month1_pmt06,
        month2_pmt06=month2_pmt06, iff_used_month1=iff_1,
        iff_used_month2=iff_2, reasoning=reasoning)
