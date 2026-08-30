"""
Layer 2: is a period's GSTR-1/GSTR-3B mismatch still correctable via
GSTR-1A, or is it locked and needs a DRC-03 voluntary payment?

## The state machine

Since July 2025, GSTR-3B's Table 3 liability figures are hard-locked against
manual editing at the time of filing. Before GSTR-3B is filed, a mismatch
between what GSTR-1 actually supports (built from real invoices - layer 1's
own output) and what GSTR-3B is about to auto-populate can still be fixed by
amending GSTR-1 itself through GSTR-1A, for the same period. Once GSTR-3B is
filed, that door closes; the only route left is a DRC-03 voluntary payment,
plus s.50 interest for the days the shortfall sat unpaid. Window state is
DERIVED from `gstr3b_filed`, never stored redundantly - see
merchant/ledger.py's gst_filing_cycles table comment.

## What is mechanical here, and what needs an agent

Comparing gstr1_liability to gstr3b_paid against a tolerance band, deriving
window_state and exception_code, and computing s.50 interest are all
arithmetic (CLAUDE.md section 2) - the same discipline as every other engine
in this project. The `category` argument to `rules.interest_on()` is named
by THIS module from the cycle's own `wrongly_claimed_itc_paise` field, never
inferred from the bare size of the delta - a wrongly-claimed-ITC shortfall
and an ordinary understatement can be the same rupee amount and carry
different interest rates, and only the cycle's own record can say which one
happened.

What genuinely needs judgment is choosing, across a whole run's OPEN
periods, which is worth filing a GSTR-1A for first - see
agent/gst_correction_classifier.py. The mechanical action for every
CORRECTABLE_VIA_1A period is already FILE_1A regardless of what the agent
adds; there is no "softer" action for it to relax into, so its only lever is
ordering and narrative, never whether to file at all.

## Overpayment is not treated as an exception

A LOCKED period where gstr3b_paid exceeds gstr1_liability by more than
tolerance means the merchant paid MORE tax than their own invoices support -
no cash is owed, no interest accrues (rules.interest_on already returns 0
for a non-positive amount), and there is no DRC-03 to file over an
overpayment. Flagging that as an exception would be the same mistake
CLAUDE.md section 5 already warns against with REFUND_MDR_RETAINED: a
correct, harmless outcome dressed up as a finding. It is reported as
PERIOD_CLEAN, with the overpayment named in the reasoning rather than hidden.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional

from engine.gst_filing import rules
from engine.gst_filing.taxonomy import (CORRECTION_LABEL, CorrectionAction,
                                        CorrectionCode, WindowState)


@dataclass
class FilingCycle:
    """One tax period's GSTR-1/GSTR-3B pairing, as recorded."""
    period: str                            # 'YYYY-MM'
    gstr1_liability: int                   # paise - from the assembled GSTR-1
    gstr3b_filed: Optional[date]           # None = window still open
    gstr3b_paid: int                       # paise - what GSTR-3B declared
    wrongly_claimed_itc_paise: int = 0     # >0 names the s.50(3) 24% category


@dataclass
class CorrectionFinding:
    period: str
    gstr1_liability: int
    gstr3b_paid: int
    delta: int                             # gstr1_liability - gstr3b_paid
    tolerance: int
    window_state: str                      # WindowState
    exception_code: str                    # CorrectionCode
    action: str                            # CorrectionAction
    days_overdue: int = 0
    interest_paise: int = 0
    interest_rate_bps: int = 0
    rule_cited: str = ""
    reasoning: str = ""

    def as_dict(self) -> dict:
        return {
            "period": self.period,
            "gstr1_liability": self.gstr1_liability,
            "gstr1_liability_display": rules.rupees(self.gstr1_liability),
            "gstr3b_paid": self.gstr3b_paid,
            "gstr3b_paid_display": rules.rupees(self.gstr3b_paid),
            "delta": self.delta, "delta_display": rules.rupees(self.delta),
            "tolerance": self.tolerance,
            "window_state": self.window_state,
            "exception_code": self.exception_code,
            "exception_label": CORRECTION_LABEL.get(
                CorrectionCode(self.exception_code), self.exception_code),
            "action": self.action,
            "days_overdue": self.days_overdue,
            "interest_paise": self.interest_paise,
            "interest_display": rules.rupees(self.interest_paise),
            "interest_rate_bps": self.interest_rate_bps,
            "rule_cited": self.rule_cited,
            "reasoning": self.reasoning,
        }


def window_state(gstr3b_filed: Optional[date]) -> WindowState:
    return WindowState.LOCKED if gstr3b_filed is not None else WindowState.OPEN


def detect_period(cycle: FilingCycle, *, tol: rules.Tolerance = rules.Tolerance(),
                  today: date) -> CorrectionFinding:
    """
    One period, judged mechanically. `today` must be supplied by the caller
    (never date.today()) so a demo run stays deterministic and reproducible.
    """
    delta = cycle.gstr1_liability - cycle.gstr3b_paid
    band = tol.band(cycle.gstr1_liability)
    state = window_state(cycle.gstr3b_filed)

    if delta <= band:
        # Either within tolerance, or an overpayment (delta negative) - see
        # this module's docstring for why overpayment is never an exception.
        note = ""
        if delta < -band:
            note = (f" GSTR-3B paid {rules.rupees(-delta)} more than GSTR-1 "
                    f"supports - an overpayment, not a shortfall; nothing "
                    f"owed, safe to leave for the next period's books.")
        return CorrectionFinding(
            period=cycle.period, gstr1_liability=cycle.gstr1_liability,
            gstr3b_paid=cycle.gstr3b_paid, delta=delta, tolerance=band,
            window_state=str(state),
            exception_code=str(CorrectionCode.PERIOD_CLEAN),
            action=str(CorrectionAction.NONE),
            reasoning=(f"GSTR-1 supports {rules.rupees(cycle.gstr1_liability)}, "
                      f"GSTR-3B paid {rules.rupees(cycle.gstr3b_paid)} - "
                      f"within tolerance.{note}"))

    if state == WindowState.OPEN:
        return CorrectionFinding(
            period=cycle.period, gstr1_liability=cycle.gstr1_liability,
            gstr3b_paid=cycle.gstr3b_paid, delta=delta, tolerance=band,
            window_state=str(state),
            exception_code=str(CorrectionCode.CORRECTABLE_VIA_1A),
            action=str(CorrectionAction.FILE_1A),
            rule_cited="GSTR-1A amendment window (pre-GSTR-3B filing)",
            reasoning=(f"GSTR-3B for {cycle.period} isn't filed yet. GSTR-1 "
                      f"supports {rules.rupees(cycle.gstr1_liability)} but "
                      f"GSTR-3B is about to auto-populate "
                      f"{rules.rupees(cycle.gstr3b_paid)} - a "
                      f"{rules.rupees(abs(delta))} gap that can still go "
                      f"through GSTR-1A before it locks."))

    # LOCKED and delta > band: a real shortfall, already filed, needs a
    # DRC-03. Interest accrues from the statutory GSTR-3B due date to
    # `today` - the day this draft is being computed, standing in for "if
    # paid now".
    _, due3b = rules.due_dates(cycle.period)
    days_overdue = max(0, (today - due3b).days)
    category = "wrong_itc" if cycle.wrongly_claimed_itc_paise > 0 else "normal"
    rate_bps = (rules.WRONG_ITC_INTEREST_PCT_BPS if category == "wrong_itc"
               else rules.INTEREST_PCT_BPS)
    interest = rules.interest_on(delta, days_overdue, category=category)
    source = (rules.SOURCE_INTEREST_WRONG_ITC if category == "wrong_itc"
             else rules.SOURCE_INTEREST_NORMAL)

    return CorrectionFinding(
        period=cycle.period, gstr1_liability=cycle.gstr1_liability,
        gstr3b_paid=cycle.gstr3b_paid, delta=delta, tolerance=band,
        window_state=str(state),
        exception_code=str(CorrectionCode.LOCKED_NEEDS_DRC03),
        action=str(CorrectionAction.PAY_DRC03),
        days_overdue=days_overdue, interest_paise=interest,
        interest_rate_bps=rate_bps, rule_cited=source,
        reasoning=(f"GSTR-3B for {cycle.period} is already filed - Table 3 "
                  f"has been locked against manual editing since Jul 2025. "
                  f"The {rules.rupees(delta)} gap needs a DRC-03 voluntary "
                  f"payment, plus {rules.rupees(interest)} interest "
                  f"({days_overdue} days at {rate_bps / 100:.0f}% p.a., "
                  f"{source})."))


def detect_cycles(cycles: list[FilingCycle], *,
                  tol: rules.Tolerance = rules.Tolerance(),
                  today: date) -> list[CorrectionFinding]:
    return [detect_period(c, tol=tol, today=today) for c in cycles]


# --- document drafts: pure data, rendered as HTML by the caller ------------

def gstr1a_draft(finding: CorrectionFinding) -> dict:
    """
    What a GSTR-1A amendment would say for this period - not a claim that
    filing one is worth it (that's the agent's call, see
    agent/gst_correction_classifier.py), just the arithmetic of what would
    change if you did.
    """
    return {
        "period": finding.period,
        "currently_reflected": finding.gstr3b_paid,
        "currently_reflected_display": rules.rupees(finding.gstr3b_paid),
        "corrected_to": finding.gstr1_liability,
        "corrected_to_display": rules.rupees(finding.gstr1_liability),
        "amendment_paise": finding.delta,
        "amendment_display": rules.rupees(finding.delta),
    }


# Cause-of-payment options DRC-03 actually offers include Voluntary, SCN,
# Annual Return and Others; a self-detected shortfall with no notice and no
# suggestion of fraud is Voluntary under s.73(5) - s.74 (wilful
# misstatement/suppression) is never assumed by default, only s.73's
# no-fault reading.
DRC03_CAUSE = "Voluntary payment - CGST Act s.73(5), self-assessed shortfall"


def _financial_year_label(period: str) -> str:
    year_text, _, month_text = period.strip().partition("-")
    fy_start = rules.financial_year_of(date(int(year_text), int(month_text), 1))
    return f"{fy_start}-{str(fy_start + 1)[-2:]}"


def drc03_draft(finding: CorrectionFinding, *, gstin: str = "") -> dict:
    """The form's own fields, values only - filed through the portal's own
    web form, never a submission this tool makes itself."""
    tax = max(0, finding.delta)
    total = tax + finding.interest_paise
    return {
        "period": finding.period,
        "gstin": gstin,
        "financial_year": _financial_year_label(finding.period),
        "tax_period": finding.period,
        "cause_of_payment": DRC03_CAUSE,
        "tax_paise": tax, "tax_display": rules.rupees(tax),
        "interest_paise": finding.interest_paise,
        "interest_display": rules.rupees(finding.interest_paise),
        "interest_rate_bps": finding.interest_rate_bps,
        "days_overdue": finding.days_overdue,
        "penalty_paise": 0, "penalty_display": rules.rupees(0),
        "fee_paise": 0, "fee_display": rules.rupees(0),
        "others_paise": 0, "others_display": rules.rupees(0),
        "total_paise": total, "total_display": rules.rupees(total),
    }
