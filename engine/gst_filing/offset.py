"""
Layer 3: the ITC utilisation hierarchy and the Rule 88C shield.

## Two independent checks, bundled under one taxonomy code

`allocate()` and `rule_88c_check()` answer two different questions and share
nothing but the fact that both are pure arithmetic (CLAUDE.md section 2):

  allocate()        given THIS period's liability by head and what's sitting
                     in the electronic credit and cash ledgers, how much NEW
                     cash (a PMT-06 deposit) does the merchant actually need
                     - after the IGST-first credit hierarchy and any cash
                     already on hand are both applied? Needs the period's own
                     invoices, so it only ever runs for the current period -
                     see merchant/agents/gst_filing.py.

  rule_88c_check()   does the gap between what GSTR-1 declared and what
                     GSTR-3B paid exceed the threshold that auto-issues a
                     DRC-01B notice? This is the SAME two figures layer 2
                     already computed a finding from (see
                     engine.gst_filing.timing.CorrectionFinding) - Rule 88C
                     is an escalation on top of a LOCKED finding, not a fresh
                     comparison, so this takes the same two numbers layer 2
                     already has rather than re-deriving them from invoices.
                     It only applies once GSTR-3B is actually filed - GSTN
                     has nothing to compare against before that, so an OPEN
                     period is never checked against it.

Only when rule_88c_check() reports a breach does anything get drafted for a
model to touch, and even then the model only writes 2-4 connecting
sentences - see agent/gst_filing_documents.py::drc01b_response(), which
mirrors agent/vendor_documents.py::write_case() exactly.

## The utilisation hierarchy

IGST credit clears IGST liability first; whatever is left over spills into
CGST, then SGST. CGST credit never touches SGST liability and SGST credit
never touches CGST liability - the two state-level heads are never fungible
with each other, only through IGST. The exact rule number for this hierarchy
is a citation seam - see rules.py's IGST_UTILISATION_SOURCE.

Cash already sitting in the electronic cash ledger is applied next, per
head, with no spillover between heads - unlike credit, cash ledger balances
are never fungible even via IGST; each head's cash pays only that head's own
liability. Whatever is left after both steps is the new PMT-06 deposit
actually required - the number the pitch means by "the minimum cash you
actually owe."

A clean allocation is never itself an exception; every period needs SOME
cash unless credit and cash-on-hand happen to cover it exactly. Only a Rule
88C breach is a taxonomy exception, because only that carries a clock and a
notice - see taxonomy.py's OffsetCode.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from engine.gst_filing import rules
from engine.gst_filing.classifier import ClassifiedInvoice
from engine.gst_filing.taxonomy import OFFSET_LABEL, OffsetCode


@dataclass
class HeadAmounts:
    igst: int = 0
    cgst: int = 0
    sgst: int = 0


def liability_from_invoices(invoices: list[ClassifiedInvoice]) -> HeadAmounts:
    """The current period's own liability, by head - summed from the
    invoices layer 1 actually classified, never guessed."""
    return HeadAmounts(
        igst=sum(i.igst for i in invoices),
        cgst=sum(i.cgst for i in invoices),
        sgst=sum(i.sgst for i in invoices))


@dataclass
class OffsetPlan:
    """The numbers alone - no judgment, see allocate()."""
    liability: HeadAmounts
    credit: HeadAmounts
    cash_on_hand: HeadAmounts
    offset_igst_to_igst: int = 0
    offset_igst_to_cgst: int = 0
    offset_igst_to_sgst: int = 0
    offset_cgst_to_cgst: int = 0
    offset_sgst_to_sgst: int = 0
    cash_applied_igst: int = 0
    cash_applied_cgst: int = 0
    cash_applied_sgst: int = 0
    cash_igst_needed: int = 0
    cash_cgst_needed: int = 0
    cash_sgst_needed: int = 0

    @property
    def total_cash_needed(self) -> int:
        return (self.cash_igst_needed + self.cash_cgst_needed
               + self.cash_sgst_needed)


def allocate(liability: HeadAmounts, credit: HeadAmounts, *,
            cash_on_hand: Optional[HeadAmounts] = None) -> OffsetPlan:
    """
    IGST credit first, spilling CGST then SGST; then each state head's own
    credit; then cash already on hand, per head with no spillover; whatever
    remains is the new deposit required.
    """
    cash_on_hand = cash_on_hand or HeadAmounts()
    plan = OffsetPlan(liability=liability, credit=credit,
                      cash_on_hand=cash_on_hand)

    remaining_igst, remaining_cgst, remaining_sgst = (
        liability.igst, liability.cgst, liability.sgst)

    igst_pool = credit.igst
    plan.offset_igst_to_igst = min(igst_pool, remaining_igst)
    remaining_igst -= plan.offset_igst_to_igst
    igst_pool -= plan.offset_igst_to_igst

    plan.offset_igst_to_cgst = min(igst_pool, remaining_cgst)
    remaining_cgst -= plan.offset_igst_to_cgst
    igst_pool -= plan.offset_igst_to_cgst

    plan.offset_igst_to_sgst = min(igst_pool, remaining_sgst)
    remaining_sgst -= plan.offset_igst_to_sgst

    plan.offset_cgst_to_cgst = min(credit.cgst, remaining_cgst)
    remaining_cgst -= plan.offset_cgst_to_cgst

    plan.offset_sgst_to_sgst = min(credit.sgst, remaining_sgst)
    remaining_sgst -= plan.offset_sgst_to_sgst

    plan.cash_applied_igst = min(cash_on_hand.igst, remaining_igst)
    remaining_igst -= plan.cash_applied_igst
    plan.cash_applied_cgst = min(cash_on_hand.cgst, remaining_cgst)
    remaining_cgst -= plan.cash_applied_cgst
    plan.cash_applied_sgst = min(cash_on_hand.sgst, remaining_sgst)
    remaining_sgst -= plan.cash_applied_sgst

    plan.cash_igst_needed = remaining_igst
    plan.cash_cgst_needed = remaining_cgst
    plan.cash_sgst_needed = remaining_sgst
    return plan


def rule_88c_check(gstr1_liability_paise: int, gstr3b_paid_paise: int
                   ) -> tuple[bool, int]:
    """
    Mirrors engine.gst.rules.notice_threshold's exact shape. Returns
    (breach, excess_over_threshold_paise) - excess is 0 when there is no
    breach, never a negative number standing in for "clean."
    """
    delta = gstr1_liability_paise - gstr3b_paid_paise
    if delta <= 0:
        return False, 0
    threshold = rules.rule_88c_threshold(gstr3b_paid_paise)
    if delta <= threshold:
        return False, 0
    return True, delta - threshold


@dataclass
class OffsetFinding:
    """One period's row in gst_offset_findings - either the current
    period's cash-needed allocation, or a Rule 88C breach on an
    already-locked period, never both computed for the same row unless the
    caller genuinely has both kinds of data for it."""
    period: str
    plan: Optional[OffsetPlan]
    rule_88c_breach: bool
    breach_amount: int
    exception_code: str
    reasoning: str
    rule_cited: str = ""

    def as_dict(self) -> dict:
        p = self.plan
        out = {
            "period": self.period,
            "rule_88c_breach": self.rule_88c_breach,
            "breach_amount": self.breach_amount,
            "breach_amount_display": rules.rupees(self.breach_amount),
            "exception_code": self.exception_code,
            "exception_label": OFFSET_LABEL.get(
                OffsetCode(self.exception_code), self.exception_code),
            "reasoning": self.reasoning, "rule_cited": self.rule_cited,
            "has_allocation": p is not None,
        }
        if p is not None:
            out.update({
                "liability_igst": p.liability.igst, "liability_cgst": p.liability.cgst,
                "liability_sgst": p.liability.sgst,
                "credit_igst": p.credit.igst, "credit_cgst": p.credit.cgst,
                "credit_sgst": p.credit.sgst,
                "offset_igst_to_igst": p.offset_igst_to_igst,
                "offset_igst_to_cgst": p.offset_igst_to_cgst,
                "offset_igst_to_sgst": p.offset_igst_to_sgst,
                "offset_cgst_to_cgst": p.offset_cgst_to_cgst,
                "offset_sgst_to_sgst": p.offset_sgst_to_sgst,
                "cash_applied_igst": p.cash_applied_igst,
                "cash_applied_cgst": p.cash_applied_cgst,
                "cash_applied_sgst": p.cash_applied_sgst,
                "cash_igst_needed": p.cash_igst_needed,
                "cash_cgst_needed": p.cash_cgst_needed,
                "cash_sgst_needed": p.cash_sgst_needed,
                "total_cash_needed": p.total_cash_needed,
                "total_cash_needed_display": rules.rupees(p.total_cash_needed),
            })
        return out


def finding_from_allocation(period: str, liability: HeadAmounts,
                            credit: HeadAmounts, *,
                            cash_on_hand: Optional[HeadAmounts] = None
                            ) -> OffsetFinding:
    """The current period's cash-needed finding. Never a Rule 88C check -
    GSTR-3B for an open period hasn't been filed yet, so GSTN has nothing to
    compare it against (see this module's docstring)."""
    plan = allocate(liability, credit, cash_on_hand=cash_on_hand)
    return OffsetFinding(
        period=period, plan=plan, rule_88c_breach=False, breach_amount=0,
        exception_code=str(OffsetCode.OFFSET_CLEAN), rule_cited="",
        reasoning=(
            f"After the IGST-first credit hierarchy"
            f"{' and cash already on hand' if (cash_on_hand and (cash_on_hand.igst or cash_on_hand.cgst or cash_on_hand.sgst)) else ''}, "
            f"a new PMT-06 deposit of {rules.rupees(plan.total_cash_needed)} "
            f"is what {period} actually needs."))


def pmt06_draft(finding: OffsetFinding, *, gstin: str = "") -> dict:
    """The challan's own head-wise fields, values only - CPIN, its expiry
    and the CIN are assigned by the portal when a challan is generated
    there; this computes the amount only."""
    p = finding.plan
    if p is None:
        return {}
    return {
        "period": finding.period, "gstin": gstin,
        "igst_paise": p.cash_igst_needed,
        "igst_display": rules.rupees(p.cash_igst_needed),
        "cgst_paise": p.cash_cgst_needed,
        "cgst_display": rules.rupees(p.cash_cgst_needed),
        "sgst_paise": p.cash_sgst_needed,
        "sgst_display": rules.rupees(p.cash_sgst_needed),
        "cess_paise": 0, "cess_display": rules.rupees(0),
        "total_paise": p.total_cash_needed,
        "total_display": rules.rupees(p.total_cash_needed),
    }


def finding_from_88c_check(period: str, gstr1_liability_paise: int,
                           gstr3b_paid_paise: int) -> Optional[OffsetFinding]:
    """A LOCKED period, already filed, with no per-head invoice data behind
    it - only worth a finding when Rule 88C actually breaches; a clean
    check here is nothing to show (CLAUDE.md section 5: three exception
    codes exist precisely so a tool does not flag everything)."""
    breach, excess = rule_88c_check(gstr1_liability_paise, gstr3b_paid_paise)
    if not breach:
        return None
    threshold = rules.rule_88c_threshold(gstr3b_paid_paise)
    delta = gstr1_liability_paise - gstr3b_paid_paise
    return OffsetFinding(
        period=period, plan=None, rule_88c_breach=True, breach_amount=excess,
        exception_code=str(OffsetCode.RULE_88C_BREACH),
        rule_cited=rules.SOURCE_RULE_88C,
        reasoning=(
            f"GSTR-1 declared {rules.rupees(gstr1_liability_paise)}, GSTR-3B "
            f"paid {rules.rupees(gstr3b_paid_paise)} - a "
            f"{rules.rupees(delta)} gap against a Rule 88C threshold of "
            f"{rules.rupees(threshold)} (whichever is lower of Rs 1 lakh or "
            f"20% of paid tax). {rules.rupees(excess)} over that threshold "
            f"auto-issues a DRC-01B intimation."))
