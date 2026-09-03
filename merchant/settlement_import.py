"""
Razorpay's settlement recon report, as an auditable Batch.

This is the ingestion the connector was missing. Until now the Data &
integrations page fetched the recon report, recorded that the call had
succeeded, and dropped the rows - so the settlement auditor, the agent the
whole product is named after, could only ever run on the simulator.

What makes this possible at all is that the report already carries the three
fields the expected-value engine needs to price a transaction: `method`,
`card_network` and `card_type`. The engine can therefore compute what the fee
SHOULD have been from the gateway's own description of the instrument, and
compare it against the `fee` on the same row. That comparison is the product.

WHAT IS NOT AVAILABLE HERE, AND IS NOT INVENTED
-----------------------------------------------
`upi_reference` (the RRN/UMN) is not a field on the recon report. Rule 9 -
the instrument mislabel check, which fires when a row calls itself a card
payment while carrying a UPI reference - therefore cannot fire on imported
data. It is left as None rather than derived from `description` or `notes`,
which are free text a merchant controls: guessing a payment rail from a memo
field and then accusing a gateway of mispricing it is exactly the kind of
confident wrongness this product exists to avoid.

Everything else the auditor checks - the rate against the instrument, GST
against the fee, refunds retaining their fee, the period boundary - works on
imported rows exactly as it does on generated ones.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from engine.expected_value import Payment
from generator.synthetic import Batch, BankCredit, Record, Refund, SettlementLine

# The row types the recon report uses. Payments and refunds are auditable
# here; the other two are real and deliberately left alone - a transfer is
# money moving between the merchant's own accounts and an adjustment is a
# manual correction, and neither is a priced transaction with a rate to check.
AUDITABLE = ("payment", "refund")


@dataclass
class ImportResult:
    batch: Optional[Batch] = None
    payments: int = 0
    refunds: int = 0
    skipped: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.batch is not None


def _int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _text(value) -> Optional[str]:
    text = str(value).strip() if value is not None else ""
    return text or None


def _split_fee(row, rate_card: dict) -> tuple:
    """
    Razorpay's two endpoints disagree about what `fee` means, and reading one
    with the other's convention is a real error, not a nicety.

    On the SETTLEMENT RECON REPORT, `fee` and `tax` are separate columns: the
    total deducted is fee + tax, which is why recon_import adds them.

    On the PAYMENTS API, `fee` is documented as the fee INCLUDING GST, and
    `tax` is the GST portion within it. Storing that `fee` as this engine's
    `fee` - which is exclusive of GST - overstates the fee by the GST on it
    and leaves the tax reading zero, so every record raises a GST mismatch
    against a tax that was actually charged and simply not separated.

    Where `tax` is reported, it is used and nothing is assumed. Where it comes
    back zero - which test mode does - the split is computed from the
    documented inclusive relationship rather than left as "no GST was
    charged", because a fee with no GST is not a thing that happens in India
    and reporting twelve GST violations on that basis would be a false
    accusation twelve times over.
    """
    fee_inclusive = _int(row.get("fee"))
    reported_tax = _int(row.get("tax"))
    if reported_tax:
        return fee_inclusive - reported_tax, reported_tax

    gst_bps = _int(rate_card.get("gst_rate_bps"), 1_800)
    if not fee_inclusive or gst_bps <= 0:
        return fee_inclusive, 0
    # fee_inclusive = base * (1 + gst); recover base, then the remainder is
    # the GST actually inside it, so the two always add back to what was paid.
    base = round(fee_inclusive * 10_000 / (10_000 + gst_bps))
    return base, fee_inclusive - base


def batch_from_payments(rows, rate_card: dict) -> ImportResult:
    """
    Captured payments from the Payments API, as an auditable Batch.

    The fallback for accounts whose settlement recon report is empty - which
    on test mode is all of them, permanently. A captured payment carries the
    amount, the instrument and Razorpay's own `fee` and `tax`, so the rate
    check works in full.

    It carries no settlement date and no UTR, and this does not invent
    either. The settlement line is written with settled_at=0 and no UTR, and
    the batch has no bank credits, because nothing here says when the money
    arrived or whether it did. Agents that measure timing must not read this;
    the rate and GST checks are unaffected, and those are what the auditor is
    for.
    """
    result = ImportResult()
    records = []

    for row in rows or []:
        payment_id = _text(row.get("id"))
        if not payment_id:
            result.skipped.append("a payment with no id")
            continue
        if not row.get("captured"):
            result.skipped.append(f"{payment_id} (not captured)")
            continue

        amount = abs(_int(row.get("amount")))
        order_id = _text(row.get("order_id")) or payment_id
        record = Record(
            record_id=payment_id, order_id=order_id,
            created_at=_int(row.get("created_at")),
            payment=Payment(
                payment_id=payment_id, amount=amount,
                method=(_text(row.get("method")) or "unknown").lower(),
                # The Payments API nests card details rather than flattening
                # them the way the recon report does.
                card_network=(_text((row.get("card") or {}).get("network"))
                              or "").lower() or None,
                card_type=(_text((row.get("card") or {}).get("type"))
                           or "").lower() or None,
                is_international=bool(row.get("international")),
                # `vpa` is the payer's UPI address, not an RRN. It says the
                # rail was UPI, which `method` already says - it is not the
                # cross-field evidence rule 9 needs, so it is not used as it.
                upi_reference=None))

        fee_ex_gst, gst = _split_fee(row, rate_card)
        record.settlement_lines.append(SettlementLine(
            entity_id=payment_id, settlement_id="", type="payment",
            payment_id=payment_id, order_id=order_id, amount=amount,
            fee=fee_ex_gst, tax=gst, utr="", settled_at=0))

        refunded = abs(_int(row.get("amount_refunded")))
        if refunded:
            result.refunds += 1
            record.refund = Refund(f"rfnd_{payment_id}", payment_id, refunded,
                                   record.created_at)
            record.settlement_lines.append(SettlementLine(
                entity_id=f"rfnd_{payment_id}", settlement_id="",
                type="refund", payment_id=payment_id, order_id=order_id,
                amount=-refunded, fee=0, tax=0, utr="", settled_at=0))

        result.payments += 1
        records.append(record)

    if not records:
        return result

    # No bank credits: this source cannot say what reached the bank, and an
    # empty list is the honest way to say so. Inventing one from the fees
    # would make Layer 1 ("did the money arrive?") answer itself.
    result.batch = Batch(records=records, bank_credits=[], seed=0,
                         rate_card=rate_card)
    return result


def batch_from_recon(rows, rate_card: dict) -> ImportResult:
    """
    Turn recon rows into the same Batch the simulator produces, so nothing
    downstream of ingestion can tell the two apart - which is the point:
    the auditor must behave identically whichever produced the data.

    Rows it cannot audit are counted and named rather than dropped silently.
    """
    result = ImportResult()
    by_payment: dict[str, Record] = {}
    credits: dict[str, list] = {}

    for row in rows or []:
        kind = str(row.get("type") or "").lower()
        if kind not in AUDITABLE:
            result.skipped.append(
                f"{row.get('entity_id') or 'a row'} ({kind or 'no type'})")
            continue

        payment_id = _text(row.get("payment_id")) or _text(row.get("entity_id"))
        if not payment_id:
            result.skipped.append("a row with no payment id")
            continue

        settlement_id = _text(row.get("settlement_id")) or "setl_unknown"
        utr = _text(row.get("settlement_utr")) or settlement_id
        settled_at = _int(row.get("settled_at"))
        amount = _int(row.get("amount"))
        order_id = _text(row.get("order_id")) or payment_id

        record = by_payment.get(payment_id)
        if record is None:
            record = Record(
                record_id=payment_id, order_id=order_id,
                created_at=_int(row.get("created_at")) or settled_at,
                payment=Payment(
                    payment_id=payment_id,
                    amount=abs(amount),
                    method=(_text(row.get("method")) or "unknown").lower(),
                    card_network=(_text(row.get("card_network")) or "").lower()
                                 or None,
                    card_type=(_text(row.get("card_type")) or "").lower()
                              or None,
                    is_international=False,
                    # Not on this report. See the module docstring - guessing
                    # it from a free-text memo would make rule 9 accuse the
                    # gateway on the strength of a merchant's own note.
                    upi_reference=None))
            by_payment[payment_id] = record

        # The signed amount this row contributes to the bank credit. A refund
        # leaves the merchant, so it must reduce the credit: the report states
        # refund amounts as positive magnitudes and marks them as debits, and
        # adding one would overstate what arrived by twice the refund.
        signed = abs(amount) if kind == "payment" else -abs(amount)

        if kind == "payment":
            result.payments += 1
            record.payment.amount = abs(amount)
            record.settlement_lines.append(SettlementLine(
                entity_id=_text(row.get("entity_id")) or payment_id,
                settlement_id=settlement_id, type="payment",
                payment_id=payment_id, order_id=order_id, amount=amount,
                fee=_int(row.get("fee")), tax=_int(row.get("tax")),
                utr=utr, settled_at=settled_at))
        else:
            result.refunds += 1
            refund_id = _text(row.get("entity_id")) or f"rfnd_{payment_id}"
            record.refund = Refund(refund_id, payment_id, abs(amount),
                                   record.created_at)
            record.settlement_lines.append(SettlementLine(
                entity_id=refund_id, settlement_id=settlement_id,
                type="refund", payment_id=payment_id, order_id=order_id,
                amount=-abs(amount), fee=_int(row.get("fee")),
                tax=_int(row.get("tax")), utr=utr, settled_at=settled_at))

        credits.setdefault(utr, []).append(
            (signed - _int(row.get("fee")) - _int(row.get("tax")), settled_at))

    records = list(by_payment.values())
    if not records:
        return result

    result.batch = Batch(
        records=records,
        bank_credits=[
            BankCredit(utr, sum(a for a, _t in lines),
                       max((t for _a, t in lines), default=0))
            for utr, lines in credits.items()],
        # Imported, not generated: there is no seed behind these rows, and a
        # number here would suggest they could be reproduced from one.
        seed=0,
        rate_card=rate_card)
    return result
