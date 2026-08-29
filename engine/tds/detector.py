"""
Join Razorpay's own TDS deductions to the merchant's credit statement, and say
what does not line up.

Same boundary as every other detector in this project (CLAUDE.md section 2):
the calculator settles what the law leaves no room to argue about; the rest
goes to the agent.

## Settled mechanically - no model involved

    CREDIT_CLEAN     both sides agree, on amount, code, form and period
    ROUNDING         they differ by less than the tolerance band
    RATE_MISMATCH    the credited amount implies the wrong-era rate - a pure
                     function of the deduction date, so there is nothing to
                     weigh
    CODE_MISMATCH    the credit statement quotes the wrong-era section code
                     or form for that date - same reasoning

## Left to the agent - because the evidence genuinely points two ways

    MISSING_CREDIT   nothing shows up on the statement at all. Ordinary
                     quarterly-refresh lag, or a genuine loss? A rule cannot
                     tell without weighing how long it has been.

    PERIOD_MISMATCH  the credit landed in a later quarter than the deduction.
                     Usually harmless lag; only a problem near a year-end
                     boundary, which needs judgment about materiality, not a
                     lookup.

## Precedence, and why it is not arbitrary

A wrong code on the credit statement outranks a matching amount - a filing
built on a stale 194O after 1 April 2026 gets rejected regardless of whether
the rupee figure happens to be right. Checking amount first would report
CREDIT_CLEAN on a statement the merchant cannot actually file against.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from engine.tds import rules
from engine.tds.generator import CreditEntry, Deduction, TdsBatch
from engine.tds.taxonomy import JUDGMENT_CODES, TdsCode


@dataclass
class Signal:
    """One piece of evidence, with its numbers already worked out."""
    kind: str
    candidate_code: str
    detail: str                   # quotable verbatim
    rule: str
    source: str
    amount_paise: int = 0


@dataclass
class TdsVariance:
    payment_id: str
    deducted_at: date
    deducted_amount: int
    deducted_rate_bps: int
    deducted_code: str

    credited_amount: int
    credited_code: Optional[str]
    credited_form: Optional[str]
    credited_period: Optional[str]

    expected_rate_bps: int
    expected_code: str
    expected_form: str
    expected_period: str

    delta: int                    # credited_amount - deducted_amount, paise
    tolerance: int
    has_credit: bool

    signals: list[Signal] = field(default_factory=list)
    raw: dict = field(default_factory=dict)

    exception_code: Optional[str] = None
    confidence: Optional[float] = None
    reasoning: Optional[str] = None
    rule_cited: Optional[str] = None
    decided_by: str = "pending"

    @property
    def needs_agent(self) -> bool:
        return self.exception_code is None


def detect_batch(batch: TdsBatch) -> list[TdsVariance]:
    """One TdsVariance per deduction, joined to its credit line by payment_id."""
    tol = batch.tolerance
    by_payment: dict[str, CreditEntry] = {
        c.payment_id.strip(): c for c in batch.credits}

    out: list[TdsVariance] = []
    for d in batch.deductions:
        credit = by_payment.get(d.payment_id.strip())
        variance = _variance_for(d, credit, tol)
        _add_signal(variance, d, credit)
        _resolve(variance)
        out.append(variance)
    return out


def _variance_for(d: Deduction, credit: Optional[CreditEntry],
                  tol: rules.Tolerance) -> TdsVariance:
    return TdsVariance(
        payment_id=d.payment_id,
        deducted_at=d.deducted_at,
        deducted_amount=d.amount,
        deducted_rate_bps=d.rate_bps,
        deducted_code=d.section_code,
        credited_amount=credit.amount if credit else 0,
        credited_code=credit.code_shown if credit else None,
        credited_form=credit.form if credit else None,
        credited_period=credit.credited_period if credit else None,
        expected_rate_bps=rules.expected_rate_bps(d.deducted_at),
        expected_code=rules.expected_section_code(d.deducted_at),
        expected_form=rules.expected_form(d.deducted_at),
        expected_period=rules.quarter_of(d.deducted_at),
        delta=(credit.amount if credit else 0) - d.amount,
        tolerance=tol.band(d.amount),
        has_credit=credit is not None,
        raw={
            "gross_amount": d.gross_amount,
            "provision": rules.expected_provision(d.deducted_at),
            "posted_at": str(credit.posted_at) if credit else None,
        })


def _add_signal(variance: TdsVariance, d: Deduction,
                credit: Optional[CreditEntry]) -> None:
    if credit is None:
        variance.signals.append(Signal(
            kind="no_credit_on_record",
            candidate_code=str(TdsCode.MISSING_CREDIT),
            detail=(f"Razorpay deducted {rules.rupees(d.amount)} of TDS on "
                    f"{d.deducted_at} under {d.section_code}, and nothing "
                    f"shows up for this payment on {variance.expected_form}. "
                    f"Either the statement has not refreshed yet, or the "
                    f"credit is genuinely missing."),
            rule="two-source match",
            source=f"{variance.expected_form} - the merchant's own credit "
                   f"statement",
            amount_paise=d.amount))
        return

    code_ok = (credit.code_shown == variance.expected_code
               and credit.form == variance.expected_form)
    if not code_ok:
        variance.signals.append(Signal(
            kind="stale_or_premature_code",
            candidate_code=str(TdsCode.CODE_MISMATCH),
            detail=(f"For a deduction on {d.deducted_at}, the correct label "
                    f"is {variance.expected_code} on {variance.expected_form} "
                    f"({rules.expected_provision(d.deducted_at)}). The credit "
                    f"statement shows {credit.code_shown} on {credit.form}."),
            rule="regime date",
            source=rules.expected_provision(d.deducted_at),
            amount_paise=0))
        return

    gap = credit.amount - d.amount
    if abs(gap) > variance.tolerance:
        variance.signals.append(Signal(
            kind="amount_gap",
            candidate_code=str(TdsCode.RATE_MISMATCH),
            detail=(f"Razorpay deducted {rules.rupees(d.amount)} at "
                    f"{d.rate_bps / 100:.2f}% on {rules.rupees(d.gross_amount)}. "
                    f"The credit statement shows {rules.rupees(credit.amount)} "
                    f"- a {rules.rupees(abs(gap))} difference, consistent "
                    f"with the {rules.OLD_RATE_BPS / 100:.0f}% rate being "
                    f"used where the {rules.NEW_RATE_BPS / 100:.1f}% rate "
                    f"applies, or the reverse, across the 1 April 2026 change."),
            rule="rate table",
            source=rules.expected_provision(d.deducted_at),
            amount_paise=abs(gap)))
        return

    if credit.credited_period != variance.expected_period:
        variance.signals.append(Signal(
            kind="posted_later_quarter",
            candidate_code=str(TdsCode.PERIOD_MISMATCH),
            detail=(f"Deducted in {variance.expected_period}, but credited in "
                    f"{credit.credited_period}. The amount and code both "
                    f"check out - this is a posting-period gap, not a "
                    f"missing-money one."),
            rule="quarterly refresh",
            source="Form 26AS / Form 168 refresh cycle",
            amount_paise=0))
        return

    if gap:
        variance.signals.append(Signal(
            kind="within_tolerance",
            candidate_code=str(TdsCode.ROUNDING),
            detail=(f"Deducted {rules.rupees(d.amount)}, credited "
                    f"{rules.rupees(credit.amount)}. The "
                    f"{rules.rupees(abs(gap))} difference is inside the "
                    f"{rules.rupees(variance.tolerance)} tolerance."),
            rule="tolerance", source="tolerance band, configured",
            amount_paise=abs(gap)))
    else:
        variance.signals.append(Signal(
            kind="matched_exactly",
            candidate_code=str(TdsCode.CREDIT_CLEAN),
            detail=(f"Deducted and credited both agree at "
                    f"{rules.rupees(d.amount)}, under {variance.expected_code} "
                    f"on {variance.expected_form}, in {variance.expected_period}."),
            rule="two-source match", source=variance.expected_form,
            amount_paise=0))


# Order matters: a wrong code outranks a matching amount.
MECHANICAL_PRECEDENCE = (
    TdsCode.CODE_MISMATCH,
    TdsCode.RATE_MISMATCH,
    TdsCode.CREDIT_CLEAN,
    TdsCode.ROUNDING,
)

def _resolve(variance: TdsVariance) -> None:
    codes = {s.candidate_code for s in variance.signals}

    if codes & JUDGMENT_CODES:
        return                                   # the agent decides

    for code in MECHANICAL_PRECEDENCE:
        if str(code) in codes:
            signal = next(s for s in variance.signals
                          if s.candidate_code == str(code))
            variance.exception_code = str(code)
            variance.reasoning = signal.detail
            variance.rule_cited = f"{signal.rule} - {signal.source}"
            variance.confidence = 1.0
            variance.decided_by = "calculator"
            return
