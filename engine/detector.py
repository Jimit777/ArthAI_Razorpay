"""
Variance detector. Checkpoint 5.

Every record in, one Variance out. Pure Python - there is no LLM anywhere in
this file, and that is the whole architecture (CLAUDE.md section 2).

Two jobs, and keeping them apart is the point:

  FINDING the gap is arithmetic. Actual minus expected. Never wrong.

  EXPLAINING the gap is judgment. A Rs 198 gap could be a rate breach, a
  mislabelled instrument, or an unclaimed refund, and telling those apart means
  weighing evidence. That is the agent's job, in checkpoint 6.

So the detector does not guess at causes. It emits SIGNALS - pieces of evidence,
each already carrying its rupee figure and the rule it comes from - and hands
them to the agent to weigh. Two signals on one record is not a bug; it is the
detector being honest that two explanations fit.

WHY THE SIGNALS CARRY PRE-COMPUTED NUMBERS: because the agent must never do
arithmetic. Every rupee figure the agent will quote in its explanation is
computed here, in Python, and handed to it as a finished string. The agent
chooses which explanation is right; it never works out how much.

WHAT THE DETECTOR DOES DECIDE: the handful of cases where exactly one rule can
apply and there is nothing to weigh - a gap of zero, a gap inside the tolerance
band, an order with no settlement line at all. Those are resolved here and never
reach the LLM. Roughly three records in four, which is a feature: it is cheaper,
it is faster, and a deterministic answer cannot hallucinate.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Optional

from engine.expected_value import (
    FeeBreakdown,
    Payment,
    SETTLEMENT_WORKING_DAYS,
    add_working_days,
    compute_expected_fee,
    reprice_as,
    rupees,
    tolerance_paise,
)
from engine.expected_value import _bps as apply_bps
from engine.taxonomy import ACTION_FOR, Action, ExceptionCode

# Rails where network MDR is mandated to zero. Rules 1 and 2.
ZERO_MDR_INSTRUMENTS = {"upi", "rupay_debit"}

# Rule 10. The Income Tax Act 2025 replaced the 1961 Act on this date; over
# fifty TDS sections collapsed into s.393 and the identifier became a 4-digit
# code instead of a section name.
TDS_REGIME_CHANGE = datetime(2026, 4, 1, tzinfo=timezone.utc)
TDS_OLD_CODE = "194O"
TDS_NEW_CODE = "1035"

# How long after the expected settlement date we wait before calling a record
# missing rather than merely late. Without this, every payment made in the last
# two days of a batch looks like a disappearance.
MISSING_GRACE_DAYS = 1

# Until August 2026 this read "PSS Act s.10A read with IT Act s.269SU".
# The Taxation and Other Laws (Amendment) Act 2026 (assent 17 Aug 2026)
# cut the s.269SU link: the modes that carry zero MDR are now whichever
# ones the Central Government notifies. UPI and RuPay debit are still
# among them, so the RATE below is unchanged - but the authority for it
# is a notification that can be varied, not a fixed statutory reference.
SOURCE_ZERO_MDR = ("PSS Act s.10A (as amended, assent 17 Aug 2026): "
                   "zero MDR for Centre-notified modes; "
                   "UPI/RuPay unchanged since 2020-01-01")
SOURCE_GST = "GST law - 18% on payment aggregator service fees"
SOURCE_REFUND = "Industry standard across Indian gateways - the original fee is not reversed"
SOURCE_TDS = "Income Tax Act 2025 s.393(1) Sl. 8(v), code 1035, effective 2026-04-01"
SOURCE_MISLABEL = "Cross-field consistency check - method vs UPI reference"


@dataclass
class Signal:
    """
    One piece of evidence about one record.

    `detail` is written to be quotable verbatim: the numbers are already in it,
    already formatted, already correct.
    """
    kind: str                     # machine-readable, stable
    candidate_code: str           # the taxonomy code this evidence points at
    detail: str                   # plain English, numbers pre-computed
    rule: str                     # "rule 1", "rule 7" - traceable to CLAUDE.md section 6
    source: str                   # the citation that makes it arguable
    amount_paise: int = 0         # money this signal accounts for


@dataclass
class Variance:
    """
    Mirrors the `variances` table in CLAUDE.md section 9, plus the evidence.

    The agent fills in exception_code / confidence / reasoning / dispute_text
    later, EXCEPT where the detector already resolved it deterministically.
    """
    payment_id: str
    order_id: str
    amount: int
    instrument_key: str
    instrument_label: str

    expected_fee: int
    actual_fee: int
    expected_tax: int
    actual_tax: int
    fee_delta: int                # actual - expected
    tax_delta: int
    delta: int                    # total over-deduction: fee_delta + tax_delta
    fee_tolerance: int
    tax_tolerance: int

    contracted_rate_bps: int
    implied_rate_bps: Optional[int]   # what they actually charged, in bps
    settlement_present: bool

    signals: list[Signal] = field(default_factory=list)

    # The raw Razorpay fields, carried alongside so the evidence can state them
    # outright. Measured: the agent called payment_detail on 100% of records
    # purely to read these. A tool round trip costs output tokens, and output is
    # 83% of the bill; the same facts inline cost input tokens, which are a
    # tenth the price and mostly cached. Answer the obvious question before it
    # is asked.
    raw: dict = field(default_factory=dict)

    exception_code: Optional[str] = None    # None => the agent must decide
    action: Optional[str] = None
    rule_cited: Optional[str] = None
    confidence: Optional[float] = None
    reasoning: Optional[str] = None
    decided_by: str = "pending"             # "calculator" | "agent" | "pending"
    created_at: int = field(default_factory=lambda: int(time.time()))

    @property
    def needs_agent(self) -> bool:
        return self.exception_code is None

    @property
    def is_over_deduction(self) -> bool:
        return self.delta > 0

    def to_dict(self) -> dict:
        return asdict(self)

    def headline(self) -> str:
        """
        One line, for terminal output.

        Shows whichever leg actually moved. A GST-on-transaction-value error
        leaves the fee identical and moves the tax by hundreds of rupees -
        printing that under a "fee" heading makes a correct number look like a
        bug, which is the last thing to be explaining on stage.
        """
        code = self.exception_code or "-> agent"
        if self.tax_delta and not self.fee_delta:
            leg = (f"GST {rupees(self.actual_tax):>10} vs {rupees(self.expected_tax):>10}")
        else:
            leg = (f"fee {rupees(self.actual_fee):>10} vs {rupees(self.expected_fee):>10}")
        return (f"{self.payment_id}  {self.instrument_label[:22]:<24}"
                f"{rupees(self.amount):>13}  {leg}  "
                f"total gap {rupees(self.delta):>9}  {code}")


# --- helpers -------------------------------------------------------------

def _implied_bps(fee: int, amount: int) -> Optional[int]:
    """What rate did they actually charge? Integer bps, rounded half-up."""
    if amount <= 0:
        return None
    return (fee * 10_000 + amount // 2) // amount


def _contracted_bps(instrument_key: str, rate_card: dict) -> int:
    spec = rate_card["instruments"][instrument_key]
    network = spec["network_mdr_bps"]
    cap = spec.get("network_mdr_cap_bps")
    if cap is not None and network > cap:
        network = cap
    return network + spec["platform_fee_bps"]


def _month(ts: int) -> tuple[int, int]:
    dt = datetime.fromtimestamp(ts, timezone.utc)
    return dt.year, dt.month


# --- the signal detectors ------------------------------------------------
#
# One function per kind of evidence. Each returns a Signal or None. Small and
# separate so that adding an eleventh rule is not a refactor, and so that a
# wrong rule can be deleted without touching the others.

def _signal_zero_mdr(expected: FeeBreakdown, actual_fee: int, fee_delta: int,
                     tolerance: int) -> Optional[Signal]:
    """Rules 1 and 2: network MDR is mandated to zero on UPI and RuPay debit."""
    if expected.instrument_key not in ZERO_MDR_INSTRUMENTS:
        return None
    if fee_delta <= tolerance:
        return None
    return Signal(
        kind="ZERO_MDR_RAIL_OVERCHARGED",
        candidate_code=ExceptionCode.ZERO_MDR_VIOLATION,
        detail=(
            f"{expected.instrument_label} carries zero network MDR by mandate. "
            f"The only chargeable component is the platform fee of "
            f"{rupees(expected.platform_fee_paise)}, but "
            f"{rupees(actual_fee)} was deducted - "
            f"{rupees(fee_delta)} more than the rate card allows."
        ),
        rule="rule 1/2",
        source=SOURCE_ZERO_MDR,
        amount_paise=fee_delta,
    )


def _signal_rate_above_contract(expected: FeeBreakdown, actual_fee: int,
                                fee_delta: int, tolerance: int, amount: int,
                                rate_card: dict) -> Optional[Signal]:
    """
    Rules 3-6: charged above the contracted or regulated slab.

    Where the slab is an RBI cap rather than a negotiated rate, the citation is
    a circular rather than a contract - a much stronger thing to put in a
    dispute, so it is worth saying which one applies.
    """
    if fee_delta <= tolerance:
        return None
    if expected.instrument_key in ZERO_MDR_INSTRUMENTS:
        return None   # that is rule 1/2's territory, not this one

    spec = rate_card["instruments"][expected.instrument_key]
    contracted = _contracted_bps(expected.instrument_key, rate_card)
    implied = _implied_bps(actual_fee, amount)
    capped = spec.get("network_mdr_cap_bps") is not None

    return Signal(
        kind="RATE_ABOVE_CONTRACT",
        candidate_code=ExceptionCode.RATE_MISMATCH,
        detail=(
            f"{expected.instrument_label} is contracted at "
            f"{contracted / 100:.2f}%. The deduction of {rupees(actual_fee)} on "
            f"{rupees(amount)} works out to {implied / 100:.2f}% - "
            f"{rupees(fee_delta)} above the {'regulatory cap' if capped else 'contracted slab'}."
        ),
        rule="rule 3/4/5/6",
        source=spec["network_mdr_source"],
        amount_paise=fee_delta,
    )


def _signal_mislabel(payment: Payment, expected: FeeBreakdown, actual_fee: int,
                     rate_card: dict) -> Optional[Signal]:
    """
    Rule 9. The one that arithmetic alone cannot find.

    A UPI payment tagged as a card is charged the CORRECT card rate. The fee
    matches expectation to the paise and every fee comparison in the world says
    it is clean. The only thing wrong is the label - and the label is worth the
    difference between two rate cards.
    """
    if not any("mislabel" in note for note in expected.notes):
        return None

    as_upi = reprice_as(payment, "upi", rate_card)
    recoverable = actual_fee - as_upi.total_fee_paise
    return Signal(
        kind="INSTRUMENT_MISLABEL_SIGNATURE",
        candidate_code=ExceptionCode.INSTRUMENT_MISLABEL,
        detail=(
            f"This payment is recorded as method='card' but carries a UPI "
            f"reference ({payment.upi_reference}), which a card payment does not "
            f"have. It was charged {rupees(actual_fee)} at the "
            f"{expected.instrument_label} rate. Priced as UPI it would have cost "
            f"{rupees(as_upi.total_fee_paise)}. The difference is "
            f"{rupees(recoverable)}. Note that the fee is correct FOR A CARD - "
            f"nothing in the arithmetic is wrong, only the instrument it was "
            f"applied to."
        ),
        rule="rule 9",
        source=SOURCE_MISLABEL,
        amount_paise=recoverable,
    )


def _signal_gst(expected: FeeBreakdown, actual_fee: int, actual_tax: int,
                tax_delta: int, tolerance: int, amount: int,
                rate_card: dict) -> Optional[Signal]:
    """
    Rule 7: GST is 18% OF THE FEE, never of the transaction value.

    The important subtlety is the early return. If the fee was overcharged, the
    GST on it will be "wrong" too - but correctly wrong, being exactly 18% of
    what was charged. That is a CONSEQUENCE of the fee error, not a second,
    separate error. Reporting both would double-count the money and hand the
    agent two signals where there is one problem.
    """
    gst_bps = rate_card["gst_rate_bps"]

    if actual_tax == apply_bps(actual_fee, gst_bps):
        return None      # GST is a faithful 18% of whatever the fee was

    if abs(tax_delta) <= tolerance:
        return None

    # Which flavour? Naming it makes the dispute concrete.
    if actual_tax == apply_bps(amount, gst_bps):
        detail = (
            f"GST of {rupees(actual_tax)} is exactly 18% of the transaction value "
            f"({rupees(amount)}), not 18% of the fee. GST on a payment "
            f"aggregator's service is charged on the service fee of "
            f"{rupees(expected.total_fee_paise)}, which comes to "
            f"{rupees(expected.gst_paise)}. Overcharged by {rupees(tax_delta)}."
        )
    else:
        implied = _implied_bps(actual_tax, actual_fee) if actual_fee else None
        rate = f"{implied / 100:.2f}%" if implied is not None else "an unexpected rate"
        detail = (
            f"GST of {rupees(actual_tax)} on a fee of {rupees(actual_fee)} is "
            f"{rate}, not 18%. Expected {rupees(expected.gst_paise)}, a "
            f"difference of {rupees(tax_delta)}. Input tax credit can only be "
            f"claimed on GST that was correctly charged."
        )

    return Signal(
        kind="GST_NOT_EIGHTEEN_PERCENT_OF_FEE",
        candidate_code=ExceptionCode.GST_MISMATCH,
        detail=detail,
        rule="rule 7",
        source=SOURCE_GST,
        amount_paise=tax_delta,
    )


def _signal_refund(record, actual_fee: int, actual_tax: int) -> Optional[Signal]:
    """
    Rule 8, the sleeper. Prevents a whole class of false alarms.

    The merchant sees a fee charged on an order that produced no revenue and
    reasonably assumes they have been robbed. They have not. Every Indian
    gateway keeps the original fee on a refund - the transaction was processed,
    the processing was the service. It is a cost to book, not a claim to file.
    """
    if record.refund is None:
        return None
    retained = actual_fee + actual_tax
    return Signal(
        kind="FEE_RETAINED_ON_REFUND",
        candidate_code=ExceptionCode.REFUND_MDR_RETAINED,
        detail=(
            f"This order was refunded in full ({rupees(record.refund.amount)}) "
            f"and the gateway retained {rupees(retained)} in fee and GST. This is "
            f"expected: the original fee is not reversed on a refund at any "
            f"Indian gateway. Book it as a cost. It is not recoverable and it is "
            f"not an overcharge."
        ),
        rule="rule 8",
        source=SOURCE_REFUND,
        amount_paise=retained,
    )


def _signal_unrecognised_adjustment(record) -> Optional[Signal]:
    """
    A deduction in the settlement that is not a payment fee and not a refund.

    We have ten rules and this matches none of them, which is exactly the point:
    CLAUDE.md section 6.1 says you cannot write a rule for the unknown. The
    detector's honest contribution is to say a deduction exists and that nothing
    accounts for it. What it must NOT do is guess.

    Note that until now the detector ignored non-payment, non-refund lines
    entirely - meaning a real settlement file's adjustments would have passed
    through silently. That was a hole in the product, not just in the tests.
    """
    adjustments = [ln for ln in record.settlement_lines
                   if ln.type not in ("payment", "refund")]
    if not adjustments:
        return None

    total = sum(abs(ln.amount) for ln in adjustments)
    ids = ", ".join(ln.entity_id for ln in adjustments)
    return Signal(
        kind="UNRECOGNISED_ADJUSTMENT",
        candidate_code=ExceptionCode.UNEXPLAINED,
        detail=(
            f"A deduction of {rupees(total)} appears in this settlement as "
            f"'{adjustments[0].type}' ({ids}) with no fee, no tax and no rule "
            f"that accounts for it. It is not a payment fee and it is not a "
            f"refund. Nothing in the rate card, the RBI caps or the GST rules "
            f"explains it, and no amount here is a multiple or fraction of any "
            f"other. What it is for cannot be determined from the settlement "
            f"file alone - the merchant has to ask."
        ),
        rule="none - no rule matches",
        source="Merchant settlement file; unmatched against every rule in the rate card",
        amount_paise=total,
    )


def _signal_period_boundary(record) -> Optional[Signal]:
    """
    Deliberately NOT auto-resolved. CLAUDE.md section 6.1 puts this in the
    agent's column, because whether a June order settling in July matters at all
    depends on the merchant's accounting period - which is not in any rate card.
    """
    payment_lines = [ln for ln in record.settlement_lines if ln.type == "payment"]
    if not payment_lines:
        return None
    settled_at = payment_lines[0].settled_at
    if not settled_at:
        # No settlement date on this source at all - the Payments API carries
        # none, and test mode never settles. Zero is not a date: read as one
        # it becomes 1 Jan 1970, so every payment "crossed an accounting
        # period" and the agent had to argue that away on all twelve records
        # of a real run. A question we cannot ask is not a finding.
        return None
    if _month(record.created_at) == _month(settled_at):
        return None

    ordered = datetime.fromtimestamp(record.created_at, timezone.utc)
    settled = datetime.fromtimestamp(settled_at, timezone.utc)
    return Signal(
        kind="CROSSES_ACCOUNTING_PERIOD",
        candidate_code=ExceptionCode.PERIOD_BOUNDARY,
        detail=(
            f"A sale of {rupees(record.payment.amount)} ordered {ordered:%d %b %Y} "
            f"settled {settled:%d %b %Y} - the revenue and the cash landed in "
            f"different months. The deduction itself is correct. Whether this needs "
            f"reclassifying depends on the accounting period being closed."
        ),
        rule="timing",
        source="T+2 working-day settlement cycle",
        amount_paise=record.payment.amount,
    )


def _signal_tds(record) -> Optional[Signal]:
    """
    Rule 10. Not a money error - a filing error, with a different deadline and
    a worse consequence: the credit may simply not appear.
    """
    tds = record.tds
    if tds is None:
        return None

    deducted = datetime.fromtimestamp(tds.deducted_at, timezone.utc)
    post_change = deducted >= TDS_REGIME_CHANGE

    if post_change and tds.section_code == TDS_OLD_CODE:
        return Signal(
            kind="STALE_TDS_SECTION_CODE",
            candidate_code=ExceptionCode.TDS_CODE_MISMATCH,
            detail=(
                f"TDS of {rupees(tds.amount)} was deducted on {deducted:%d %b %Y} "
                f"under section 194O at {tds.rate_bps / 100:.2f}%. Section 194O "
                f"ceased to exist on 1 April 2026. Deductions from that date fall "
                f"under s.393(1) Sl. 8(v), reported as code 1035 at 0.10%, and "
                f"appear in Form 168 rather than Form 26AS. Quoting the old "
                f"section on a return triggers validation rejection and a "
                f"Rs 200/day late fee, and the credit may not appear at all."
            ),
            rule="rule 10",
            source=SOURCE_TDS,
            amount_paise=tds.amount,
        )

    if not post_change and tds.section_code == TDS_NEW_CODE:
        return Signal(
            kind="PREMATURE_TDS_SECTION_CODE",
            candidate_code=ExceptionCode.TDS_CODE_MISMATCH,
            detail=(
                f"TDS deducted {deducted:%d %b %Y} is reported under code 1035, "
                f"which only exists from 1 April 2026. Deductions before that date "
                f"belong to section 194O and Form 26AS."
            ),
            rule="rule 10",
            source=SOURCE_TDS,
            amount_paise=tds.amount,
        )

    return None


def _signal_missing(record, expected: FeeBreakdown, as_of: int) -> Optional[Signal]:
    """
    The one anomaly a net-level reconciliation can never surface: there is no
    line, so there is nothing to reconcile. It is only visible by comparing the
    merchant's own books against the settlement file.

    Guarded by the settlement calendar. A payment taken yesterday is not missing,
    it is in transit, and calling it missing would flag the tail of every batch.
    """
    if record.settlement_lines:
        return None

    created = datetime.fromtimestamp(record.created_at, timezone.utc)
    due = add_working_days(created, SETTLEMENT_WORKING_DAYS + MISSING_GRACE_DAYS)
    if as_of < int(due.timestamp()):
        return Signal(
            kind="SETTLEMENT_NOT_YET_DUE",
            candidate_code=ExceptionCode.CLEAN,
            detail=(
                f"No settlement line yet, but this payment was taken "
                f"{created:%d %b %Y} and is not due until {due:%d %b %Y}. In "
                f"transit, not missing."
            ),
            rule="timing",
            source="T+2 working-day settlement cycle",
        )

    net_expected = record.payment.amount - expected.total_deduction_paise
    return Signal(
        kind="NO_SETTLEMENT_LINE",
        candidate_code=ExceptionCode.MISSING_FROM_SETTLEMENT,
        detail=(
            f"A payment of {rupees(record.payment.amount)} was captured on "
            f"{created:%d %b %Y} and settlement was due by {due:%d %b %Y}, but no "
            f"settlement line exists for it in any batch. Net of expected "
            f"deductions, {rupees(net_expected)} has not arrived. The money at "
            f"stake is the whole sale, not a fee gap."
        ),
        rule="completeness",
        source="Merchant books vs settlement recon report",
        amount_paise=net_expected,
    )


# --- the detector --------------------------------------------------------

def detect(record, rate_card: dict, as_of: Optional[int] = None) -> Variance:
    """One record in, one Variance out. No LLM, no guessing at causes."""
    if as_of is None:
        as_of = int(time.time())

    expected = compute_expected_fee(record.payment, rate_card)

    payment_lines = [ln for ln in record.settlement_lines if ln.type == "payment"]
    settlement_present = bool(payment_lines)
    actual_fee = payment_lines[0].fee if settlement_present else 0
    actual_tax = payment_lines[0].tax if settlement_present else 0

    if settlement_present:
        fee_delta = actual_fee - expected.total_fee_paise
        tax_delta = actual_tax - expected.gst_paise
    else:
        # Nothing to compare. The deltas are zero not because the record is
        # clean but because there is no actual to subtract an expected from.
        # The money at stake travels on the signal instead.
        fee_delta = tax_delta = 0

    fee_tol = tolerance_paise(expected.total_fee_paise, rate_card)
    tax_tol = tolerance_paise(expected.gst_paise, rate_card)

    variance = Variance(
        payment_id=record.record_id,
        order_id=record.order_id,
        amount=record.payment.amount,
        instrument_key=expected.instrument_key,
        instrument_label=expected.instrument_label,
        expected_fee=expected.total_fee_paise,
        actual_fee=actual_fee,
        expected_tax=expected.gst_paise,
        actual_tax=actual_tax,
        fee_delta=fee_delta,
        tax_delta=tax_delta,
        delta=fee_delta + tax_delta,
        fee_tolerance=fee_tol,
        tax_tolerance=tax_tol,
        contracted_rate_bps=_contracted_bps(expected.instrument_key, rate_card),
        implied_rate_bps=_implied_bps(actual_fee, record.payment.amount)
                          if settlement_present else None,
        settlement_present=settlement_present,
        raw={
            "method": record.payment.method,
            "card_network": record.payment.card_network,
            "card_type": record.payment.card_type,
            "is_international": record.payment.is_international,
            "upi_reference": record.payment.upi_reference,
            "created_at": record.created_at,
            "settled_at": payment_lines[0].settled_at if settlement_present else None,
            "settlement_id": payment_lines[0].settlement_id if settlement_present else None,
            "utr": payment_lines[0].utr if settlement_present else None,
            "refunded": record.refund is not None,
            "refund_amount": record.refund.amount if record.refund else None,
            "tds_code": record.tds.section_code if record.tds else None,
            "tds_amount": record.tds.amount if record.tds else None,
        },
    )

    # --- gather the evidence ---------------------------------------------
    candidates = [
        _signal_missing(record, expected, as_of),
        _signal_zero_mdr(expected, actual_fee, fee_delta, fee_tol)
            if settlement_present else None,
        _signal_rate_above_contract(expected, actual_fee, fee_delta, fee_tol,
                                    record.payment.amount, rate_card)
            if settlement_present else None,
        _signal_mislabel(record.payment, expected, actual_fee, rate_card)
            if settlement_present else None,
        _signal_gst(expected, actual_fee, actual_tax, tax_delta, tax_tol,
                    record.payment.amount, rate_card)
            if settlement_present else None,
        _signal_refund(record, actual_fee, actual_tax),
        _signal_unrecognised_adjustment(record),
        _signal_period_boundary(record),
        _signal_tds(record),
    ]
    variance.signals = [s for s in candidates if s is not None]

    _resolve_if_mechanical(variance)
    return variance


def _resolve_if_mechanical(v: Variance) -> None:
    """
    Settle the cases where exactly one rule can apply and there is nothing to
    weigh. Everything else keeps exception_code=None and goes to the agent.

    The discipline here is deliberately conservative. It resolves a record only
    when NO alternative explanation is on the table - which in practice means
    zero or one signal. The moment two pieces of evidence disagree, a human-like
    judgment is required and this function steps back.
    """
    kinds = {s.kind for s in v.signals}

    # A settlement in transit is not a finding at all.
    if kinds == {"SETTLEMENT_NOT_YET_DUE"}:
        _decide(v, ExceptionCode.CLEAN, "timing",
                "Not yet due for settlement. Nothing to reconcile.")
        return

    # No settlement line, past due, and nothing else muddying it.
    if kinds == {"NO_SETTLEMENT_LINE"}:
        _decide(v, ExceptionCode.MISSING_FROM_SETTLEMENT, "completeness",
                v.signals[0].detail)
        return

    if not v.signals:
        within = abs(v.fee_delta) <= v.fee_tolerance and abs(v.tax_delta) <= v.tax_tolerance
        if not within:
            # A gap outside tolerance that no rule explains. This is exactly
            # what UNEXPLAINED is for - but it is a judgment call whether it is
            # truly unexplained or a rule we have not written, so the agent
            # gets it rather than the calculator asserting.
            return
        if v.fee_delta == 0 and v.tax_delta == 0:
            _decide(v, ExceptionCode.CLEAN, "rate card",
                    "Fee and GST match the rate card to the paise.")
        else:
            _decide(v, ExceptionCode.ROUNDING, "tolerance band",
                    f"Gap of {rupees(v.delta)} sits inside the tolerance band of "
                    f"{rupees(v.fee_tolerance)}. Rounding noise, not a finding.")
        return

    # A refunded order whose fee is otherwise correct. Rule 8 is unambiguous
    # and there is no competing evidence, so the calculator can close it.
    if kinds == {"FEE_RETAINED_ON_REFUND"} and abs(v.fee_delta) <= v.fee_tolerance:
        _decide(v, ExceptionCode.REFUND_MDR_RETAINED, "rule 8", v.signals[0].detail)
        return

    # Anything else - a fee gap, a mislabel, a period boundary, a stale tax
    # code, or two signals disagreeing - needs judgment. Leave it for the agent.


def _decide(v: Variance, code: ExceptionCode, rule: str, reasoning: str) -> None:
    v.exception_code = str(code)
    v.action = str(ACTION_FOR[code])
    v.rule_cited = rule
    v.reasoning = reasoning
    v.confidence = 1.0          # arithmetic, not opinion
    v.decided_by = "calculator"


def detect_batch(batch, as_of: Optional[int] = None) -> list[Variance]:
    """
    Run the detector over a whole batch.

    `as_of` defaults to the latest settlement date in the batch, which is when
    a merchant would realistically be looking at it. Passing it explicitly keeps
    the "is this missing or merely late?" question deterministic rather than
    depending on the wall clock.
    """
    if as_of is None:
        settled = [ln.settled_at for r in batch.records for ln in r.settlement_lines]
        as_of = max(settled) if settled else int(time.time())
    return [detect(record, batch.rate_card, as_of) for record in batch.records]


# --- terminal view -------------------------------------------------------
#
# Not the final report (checkpoint 9) - but if the React UI never happens, this
# is what the demo runs on, and a working system in a terminal beats a pretty
# UI around a broken one.

def print_audit(variances: list[Variance]) -> None:
    from collections import Counter

    resolved = [v for v in variances if not v.needs_agent]
    pending = [v for v in variances if v.needs_agent]

    print(f"\n{len(variances)} records audited\n")
    print(f"  {len(resolved):>3} resolved by the rate card - never reach the LLM")
    for code, n in Counter(v.exception_code for v in resolved).most_common():
        print(f"      {n:>3} x {code}")
    print(f"  {len(pending):>3} need judgment - routed to the agent")
    for kind, n in Counter(s.candidate_code for v in pending
                           for s in v.signals).most_common():
        print(f"      {n:>3} x evidence pointing at {kind}")

    if pending:
        print("\nrouted to the agent:\n")
        for v in pending:
            print(f"  {v.headline()}")
            for signal in v.signals:
                print(f"        [{signal.rule}] {signal.detail}")
                print(f"        source: {signal.source}")
            print()


if __name__ == "__main__":
    import argparse

    from generator.synthetic import generate_batch

    ap = argparse.ArgumentParser(description="Run the variance detector over a batch.")
    ap.add_argument("--n", type=int, default=60)
    ap.add_argument("--seed", type=int, default=20260905)
    args = ap.parse_args()

    batch, _ = generate_batch(args.n, args.seed)
    print_audit(detect_batch(batch))
