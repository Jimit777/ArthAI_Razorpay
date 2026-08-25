"""
Join the books to GSTR-2B, and say what does not line up.

The calculator's job is to find gaps and settle the ones where the law leaves
no room. The agent's job is the rest. Getting that boundary right is the whole
design, so it is stated here rather than left implicit:

## Settled mechanically - no model involved

    CLAIM_CLEAN          both sides agree
    ROUNDING             they differ by less than the tolerance band
    BLOCKED_CREDIT       the category is on the s.17(5) list. Not arguable.
    TIME_BARRED          the deadline is a date and today is after it
    RULE_37_REVERSAL     180 days is a number and the supplier is unpaid
    DUPLICATE_CLAIM      the same GSTIN and invoice number booked twice
    NOT_IN_BOOKS         present in 2B, no purchase invoice anywhere

## Left to the agent - because the evidence genuinely points two ways

    an invoice missing under its own GSTIN while an identical one sits under a
    different GSTIN. Is that the supplier filing against the wrong registration,
    or two suppliers who happened to use the same invoice number? A rule cannot
    tell. Weighing "same number, same amount, same date, different state" is
    judgment.

    a tax amount that differs by a real margin. A credit note, a rate applied
    wrongly, a partial supply, or the supplier under-reporting. Same arithmetic,
    four different actions.

    an invoice that appears in a later filing period than the books expect.
    Late filing is normal and claimable next period; it is only a problem if it
    crosses the year end into the s.16(4) deadline.

## Precedence, and why it is not arbitrary

A blocked or time-barred invoice is not claimable no matter how perfectly the
supplier filed it. So those outrank a clean match rather than competing with
it - checking "did it match" first would report CLAIM_CLEAN on an invoice the
merchant must not touch, which is the one error class that turns a helpful tool
into a notice under Rule 88D.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from engine.gst import rules
from engine.gst.generator import GSTR2BLine, ITCBatch, PurchaseInvoice
from engine.gst.taxonomy import ITCCode


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
class ITCVariance:
    invoice_id: str
    supplier_name: str
    supplier_gstin: str
    invoice_number: str
    invoice_date: date

    claimed_tax: int              # what the books claim, in paise
    available_tax: int            # what GSTR-2B supports
    delta: int                    # claimed - available
    tolerance: int

    in_books: bool
    in_2b: bool
    category: Optional[str] = None
    paid_on: Optional[date] = None
    days_to_deadline: int = 0

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


def _key(gstin: str, number: str) -> tuple[str, str]:
    return (gstin.strip().upper(), number.strip().upper())


def detect_batch(batch: ITCBatch) -> list[ITCVariance]:
    """One ITCVariance per purchase invoice, plus one per unmatched 2B line."""
    today = batch.as_of
    tol = batch.tolerance

    by_key: dict[tuple[str, str], GSTR2BLine] = {}
    for line in batch.gstr2b:
        by_key[_key(line.supplier_gstin, line.invoice_number)] = line

    # An invoice number seen under more than one GSTIN is the fingerprint of a
    # supplier filing against the wrong registration. Indexed separately so the
    # lookup does not depend on which GSTIN we start from.
    by_number: dict[str, list[GSTR2BLine]] = {}
    for line in batch.gstr2b:
        by_number.setdefault(line.invoice_number.strip().upper(), []).append(line)

    seen_books: dict[tuple[str, str], str] = {}
    matched_2b: set[tuple[str, str]] = set()
    out: list[ITCVariance] = []

    for invoice in batch.purchases:
        key = _key(invoice.supplier_gstin, invoice.invoice_number)
        line = by_key.get(key)
        if line is not None:
            matched_2b.add(key)

        variance = _variance_for(invoice, line, today, tol)
        _add_signals(variance, invoice, line, by_number, seen_books, key,
                     today, tol, batch.period)
        _resolve(variance)
        out.append(variance)

        if key not in seen_books:
            seen_books[key] = invoice.invoice_id

    for line in batch.gstr2b:
        key = _key(line.supplier_gstin, line.invoice_number)
        if key in matched_2b or key in seen_books:
            continue
        # A 2B line nothing in the books claims. Either a missing purchase
        # invoice, or somebody else's invoice filed against this GSTIN.
        if _claimed_elsewhere(line, batch):
            continue
        out.append(_orphan_2b(line, today, tol))

    return out


def _claimed_elsewhere(line: GSTR2BLine, batch: ITCBatch) -> bool:
    """True when some purchase invoice already points at this 2B line's number."""
    number = line.invoice_number.strip().upper()
    return any(p.invoice_number.strip().upper() == number for p in batch.purchases)


def _variance_for(invoice: PurchaseInvoice, line: Optional[GSTR2BLine],
                  today: date, tol: rules.Tolerance) -> ITCVariance:
    claimed = invoice.total_tax
    available = line.total_tax if line else 0
    return ITCVariance(
        invoice_id=invoice.invoice_id,
        supplier_name=invoice.supplier_name,
        supplier_gstin=invoice.supplier_gstin,
        invoice_number=invoice.invoice_number,
        invoice_date=invoice.invoice_date,
        claimed_tax=claimed,
        available_tax=available,
        delta=claimed - available,
        tolerance=tol.band(claimed),
        in_books=True,
        in_2b=line is not None,
        category=invoice.category,
        paid_on=invoice.paid_on,
        days_to_deadline=rules.days_to_deadline(invoice.invoice_date, today),
        raw={
            "taxable_value": invoice.taxable_value,
            "cgst": invoice.cgst, "sgst": invoice.sgst, "igst": invoice.igst,
            "filed_period": line.filed_period if line else None,
            "claim_deadline": str(rules.claim_deadline(invoice.invoice_date)),
            "payment_due_by": str(rules.payment_due_by(invoice.invoice_date)),
        })


def _orphan_2b(line: GSTR2BLine, today: date, tol: rules.Tolerance) -> ITCVariance:
    variance = ITCVariance(
        invoice_id=rules.orphan_id(line.supplier_gstin, line.invoice_number),
        supplier_name="(not in your books)",
        supplier_gstin=line.supplier_gstin,
        invoice_number=line.invoice_number,
        invoice_date=line.invoice_date,
        claimed_tax=0,
        available_tax=line.total_tax,
        delta=-line.total_tax,
        tolerance=tol.band(line.total_tax),
        in_books=False,
        in_2b=True,
        days_to_deadline=rules.days_to_deadline(line.invoice_date, today),
        raw={"filed_period": line.filed_period,
             "taxable_value": line.taxable_value})
    variance.signals.append(Signal(
        kind="only_in_2b",
        candidate_code=str(ITCCode.NOT_IN_BOOKS),
        detail=(f"{line.supplier_gstin} reported invoice {line.invoice_number} "
                f"carrying {rules.rupees(line.total_tax)} of tax, and nothing "
                f"in the purchase register claims it. Either the invoice was "
                f"never booked - in which case there is credit going unclaimed "
                f"- or it was filed against this GSTIN by mistake."),
        rule="rule 2",
        source=rules.SOURCE_IN_2B,
        amount_paise=line.total_tax))
    _resolve(variance)
    return variance


def _add_signals(variance, invoice, line, by_number, seen_books, key, today,
                 tol, period: str):
    # --- things that stop a claim regardless of how well it matched ---------
    blocked = rules.blocked_reason(invoice.category)
    if blocked:
        variance.signals.append(Signal(
            kind="blocked_category",
            candidate_code=str(ITCCode.BLOCKED_CREDIT),
            detail=(f"{invoice.supplier_name} supplied "
                    f"{invoice.category.replace('_', ' ')}, which is a blocked "
                    f"category. The {rules.rupees(invoice.total_tax)} of tax on "
                    f"this invoice is not claimable however correctly it was "
                    f"filed."),
            rule="rule 5", source=blocked,
            amount_paise=invoice.total_tax))

    if rules.is_time_barred(invoice.invoice_date, today):
        deadline = rules.claim_deadline(invoice.invoice_date)
        variance.signals.append(Signal(
            kind="past_claim_deadline",
            candidate_code=str(ITCCode.TIME_BARRED),
            detail=(f"Invoice dated {invoice.invoice_date}. The deadline to "
                    f"claim credit on it was {deadline}, "
                    f"{(today - deadline).days} days ago. "
                    f"{rules.rupees(invoice.total_tax)} is no longer claimable."),
            rule="rule 3", source=rules.SOURCE_DEADLINE,
            amount_paise=invoice.total_tax))

    if rules.needs_rule_37_reversal(invoice.invoice_date, invoice.paid_on, today):
        due = rules.payment_due_by(invoice.invoice_date)
        variance.signals.append(Signal(
            kind="supplier_unpaid_180",
            candidate_code=str(ITCCode.RULE_37_REVERSAL),
            detail=(f"{invoice.supplier_name} has not been paid. The 180 days "
                    f"ran out on {due}, {(today - due).days} days ago, so "
                    f"{rules.rupees(invoice.total_tax)} of credit already taken "
                    f"has to be reversed - with interest until it is."),
            rule="rule 4", source=rules.SOURCE_RULE_37,
            amount_paise=invoice.total_tax))

    if key in seen_books:
        variance.signals.append(Signal(
            kind="duplicate_pair",
            candidate_code=str(ITCCode.DUPLICATE_CLAIM),
            detail=(f"Invoice {invoice.invoice_number} from "
                    f"{invoice.supplier_gstin} is already booked as "
                    f"{seen_books[key]}. The same "
                    f"{rules.rupees(invoice.total_tax)} is claimed twice; "
                    f"GSTR-2B supports it once."),
            rule="rule 2", source=rules.SOURCE_IN_2B,
            amount_paise=invoice.total_tax))

    # --- how it matched -----------------------------------------------------
    if line is None:
        elsewhere = [c for c in by_number.get(
            invoice.invoice_number.strip().upper(), [])
            if c.supplier_gstin.upper() != invoice.supplier_gstin.upper()]
        if elsewhere:
            other = elsewhere[0]
            variance.signals.append(Signal(
                kind="absent_but_similar_elsewhere",
                candidate_code=str(ITCCode.GSTIN_MISMATCH),
                detail=(f"Nothing was filed against {invoice.supplier_gstin} "
                        f"for invoice {invoice.invoice_number}. An invoice with "
                        f"the same number, dated {other.invoice_date}, carrying "
                        f"{rules.rupees(other.total_tax)}, was filed against "
                        f"{other.supplier_gstin} - a "
                        f"{'different' if other.supplier_gstin[:2] != invoice.supplier_gstin[:2] else 'same'}"
                        f"-state registration."),
                rule="rule 8", source=rules.SOURCE_IN_2B,
                amount_paise=invoice.total_tax))
        else:
            variance.signals.append(Signal(
                kind="absent_from_2b",
                candidate_code=str(ITCCode.SUPPLIER_NOT_FILED),
                detail=(f"{invoice.supplier_name} has not reported invoice "
                        f"{invoice.invoice_number}. "
                        f"{rules.rupees(invoice.total_tax)} of credit is at "
                        f"risk: since the Supreme Court upheld s.16(2)(c), the "
                        f"credit does not exist until they file, and proving "
                        f"they paid is the buyer's burden."),
                rule="rule 1", source=rules.SOURCE_SUPPLIER_PAID,
                amount_paise=invoice.total_tax))
        return

    gap = invoice.total_tax - line.total_tax
    if abs(gap) <= variance.tolerance:
        # The period the books expect comes from the batch. It was hardcoded
        # to the synthetic generator's month, which meant every correctly filed
        # invoice on LIVE data looked late - the fault only appears once real
        # purchases exist, which is exactly why the deployment layer needed
        # building rather than assuming the engine was already portable.
        if period and line.filed_period > period:
            variance.signals.append(Signal(
                kind="filed_in_later_period",
                candidate_code=str(ITCCode.SUPPLIER_LATE_FILED),
                detail=(f"{invoice.supplier_name} filed invoice "
                        f"{invoice.invoice_number} in {line.filed_period} "
                        f"rather than the period the books expect. The credit "
                        f"is intact but lands one period later."),
                rule="rule 2", source=rules.SOURCE_IN_2B,
                amount_paise=0))
        elif gap:
            variance.signals.append(Signal(
                kind="within_tolerance",
                candidate_code=str(ITCCode.ROUNDING),
                detail=(f"Books claim {rules.rupees(invoice.total_tax)}, "
                        f"GSTR-2B shows {rules.rupees(line.total_tax)}. The "
                        f"{rules.rupees(abs(gap))} difference is inside the "
                        f"{rules.rupees(variance.tolerance)} tolerance."),
                rule="tolerance", source="tolerance band, configured",
                amount_paise=abs(gap)))
        else:
            variance.signals.append(Signal(
                kind="matched_exactly",
                candidate_code=str(ITCCode.CLAIM_CLEAN),
                detail=(f"Books and GSTR-2B agree at "
                        f"{rules.rupees(invoice.total_tax)}."),
                rule="rule 2", source=rules.SOURCE_IN_2B, amount_paise=0))
        return

    variance.signals.append(Signal(
        kind="tax_short_in_2b" if gap > 0 else "tax_over_in_2b",
        candidate_code=str(ITCCode.AMOUNT_MISMATCH),
        detail=(f"Books claim {rules.rupees(invoice.total_tax)} on invoice "
                f"{invoice.invoice_number}; GSTR-2B supports "
                f"{rules.rupees(line.total_tax)}. "
                f"{rules.rupees(abs(gap))} "
                f"{'more than was reported' if gap > 0 else 'less than was reported'}, "
                f"against a tolerance of {rules.rupees(variance.tolerance)}."),
        rule="rule 2", source=rules.SOURCE_IN_2B, amount_paise=abs(gap)))


# Order matters: an invoice that must not be claimed outranks one that matched.
MECHANICAL_PRECEDENCE = (
    ITCCode.DUPLICATE_CLAIM,
    ITCCode.BLOCKED_CREDIT,
    ITCCode.TIME_BARRED,
    ITCCode.RULE_37_REVERSAL,
    ITCCode.NOT_IN_BOOKS,
    ITCCode.SUPPLIER_NOT_FILED,
    ITCCode.CLAIM_CLEAN,
    ITCCode.ROUNDING,
)

# These need weighing, so they go to the agent even when they are the only
# signal on the record.
JUDGMENT_CODES = frozenset({
    str(ITCCode.GSTIN_MISMATCH),
    str(ITCCode.AMOUNT_MISMATCH),
    str(ITCCode.SUPPLIER_LATE_FILED),
})


def _resolve(variance: ITCVariance) -> None:
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
