"""
Paste-ready dispute messages. Checkpoint 8.

The split here is the same one the whole project rests on, applied to prose:

  the agent writes the ARGUMENT, because that is language, not logic
  Python writes the FACTS, because a wrong figure in a message to a payment
      gateway is the most expensive place to put one

The agent's paragraph is checked - every rupee amount and percentage in it must
appear in the evidence it was shown (see classifier.unverified_figures). The
reference block underneath is assembled from the database and cannot drift,
because nothing generated it.

Why that matters more here than anywhere else: a merchant who sends their
gateway a claim with an invented number does not just lose that claim. They
lose the next one too, and the one after that. CLAUDE.md 1.5 notes the dispute
window is 60-180 days; credibility spent early does not come back inside it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from engine.expected_value import rupees
from engine.taxonomy import Action, ExceptionCode

# Only these actions produce something to send. A dismissal has no recipient,
# and an escalation goes to a colleague, not to the gateway.
SENDABLE = {str(Action.DISPUTE), str(Action.FIX_BOOKS)}

SUBJECTS = {
    str(ExceptionCode.ZERO_MDR_VIOLATION):
        "Incorrect MDR applied on a zero-MDR transaction",
    str(ExceptionCode.INSTRUMENT_MISLABEL):
        "Transaction priced on the wrong instrument",
    str(ExceptionCode.RATE_MISMATCH):
        "Deduction above the contracted rate",
    str(ExceptionCode.GST_MISMATCH):
        "GST computed on an incorrect base",
    str(ExceptionCode.MISSING_FROM_SETTLEMENT):
        "Captured payment missing from settlement",
    str(ExceptionCode.TDS_CODE_MISMATCH):
        "TDS certificate quotes a repealed section",
}


def _date(ts: Optional[int]) -> str:
    if not ts:
        return "-"
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%d %b %Y")


def subject_line(exception_code: str, payment_id: str) -> str:
    topic = SUBJECTS.get(exception_code, "Query on a settlement deduction")
    return f"{topic} - {payment_id}"


def reference_block(variance, exception_code: str, money_at_stake: int) -> str:
    """
    The facts, assembled from stored data. No model touches this.

    Everything a gateway support agent needs to find the transaction without a
    reply asking for it: the ids, the settlement it landed in, the UTR that
    joins it to the bank statement, and the arithmetic laid out.
    """
    raw = variance.raw
    lines = [
        "--- Reference details ---",
        f"Payment ID     : {variance.payment_id}",
        f"Order ID       : {variance.order_id}",
        f"Settlement ID  : {raw.get('settlement_id') or 'not settled'}",
        f"UTR            : {raw.get('utr') or 'not settled'}",
        f"Transaction    : {rupees(variance.amount)} on {_date(raw.get('created_at'))}",
        f"Settled        : {_date(raw.get('settled_at'))}",
        f"Instrument     : {variance.instrument_label}"
        f" (method={raw.get('method')}"
        + (f", network={raw.get('card_network')}" if raw.get("card_network") else "")
        + (f", ref={raw.get('upi_reference')}" if raw.get("upi_reference") else "")
        + ")",
    ]

    if variance.settlement_present:
        lines += [
            "",
            f"Fee charged    : {rupees(variance.actual_fee)}",
            f"Fee expected   : {rupees(variance.expected_fee)}",
            f"GST charged    : {rupees(variance.actual_tax)}",
            f"GST expected   : {rupees(variance.expected_tax)}",
            f"Difference     : {rupees(variance.delta)}",
        ]
        if variance.implied_rate_bps is not None:
            lines.append(
                f"Rate applied   : {variance.implied_rate_bps / 100:.2f}%"
                f" against a contracted {variance.contracted_rate_bps / 100:.2f}%")
    else:
        lines += ["", "No settlement line exists for this payment."]

    lines += ["", f"Amount in question : {rupees(money_at_stake)}"]

    citations = sorted({s.source for s in variance.signals
                        if s.candidate_code == exception_code})
    if citations:
        lines.append("")
        for citation in citations:
            lines.append(f"Basis          : {citation}")

    return "\n".join(lines)


def build_dispute(variance, verdict, money_at_stake: int) -> Optional[str]:
    """
    Assemble the full message, or None when there is nothing to send.

    Returns something the merchant can paste into a support ticket without
    editing - which is the actual bar, and the reason the reference block is
    machine-written rather than left for them to fill in.
    """
    if verdict.action not in SENDABLE:
        return None
    if not verdict.dispute_text:
        return None

    return "\n".join([
        f"Subject: {subject_line(verdict.exception_code, variance.payment_id)}",
        "",
        verdict.dispute_text.strip(),
        "",
        reference_block(variance, verdict.exception_code, money_at_stake),
    ])


def attach_disputes(variances, verdicts, decisions) -> dict[str, str]:
    """
    Build every sendable message for a run. Returns payment_id -> message.

    A verdict whose figures failed review is skipped. A message the merchant
    might send is the last place to be relaxed about an unverified number.
    """
    by_variance = {v.payment_id: v for v in variances}
    by_stake = {d.payment_id: d.money_at_stake for d in decisions}

    out: dict[str, str] = {}
    for verdict in verdicts:
        if verdict.invented_figures:
            continue
        variance = by_variance.get(verdict.payment_id)
        if variance is None:
            continue
        message = build_dispute(variance, verdict,
                                by_stake.get(verdict.payment_id, 0))
        if message:
            out[verdict.payment_id] = message
    return out
