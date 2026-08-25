"""
The agent's tools. Checkpoint 6.

FIVE TOOLS, ALL READ-ONLY. That is not an accident and it is worth saying on
stage: guardrail 1 in CLAUDE.md section 10 says the agent never writes to a
ledger. We do not enforce that with a warning in the prompt, which the model
could ignore. We enforce it by never giving it a tool that can write. There is
no edit, no insert, no file access, no shell. The agent can look at things and
that is the whole of its power.

Every tool returns JSON with the numbers already computed. The agent reads
figures; it never derives them. CLAUDE.md section 2.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from anthropic import beta_tool

from engine.expected_value import compute_expected_fee, rupees

MEMORY_PATH = Path(__file__).parent.parent / "config" / "resolution_memory.json"

TDS_REGIME_CHANGE = datetime(2026, 4, 1, tzinfo=timezone.utc)


def _money(paise: int) -> dict:
    """Money crosses the boundary as both paise and a formatted string.

    The agent quotes the string. It never sees a number it has to format, and
    it never has to divide by 100 - which is arithmetic, and therefore banned.
    """
    return {"paise": paise, "display": rupees(paise)}


def load_memory(path: Path = MEMORY_PATH) -> list[dict]:
    """Resolution memory. CLAUDE.md section 12. Absent file is not an error."""
    if not path.exists():
        return []
    with open(path) as f:
        return json.load(f)


def build_tools(batch, rate_card: dict, memory: list[dict] | None = None) -> list[Callable]:
    """
    Build the tool set bound to one batch.

    The tools close over the batch rather than reading a global, so two audits
    can run side by side without seeing each other's data - and so nothing can
    reach a record that is not in the batch under audit.
    """
    by_payment = {r.record_id: r for r in batch.records}
    cases = memory if memory is not None else load_memory()

    @beta_tool
    def rate_card_lookup(instrument_key: str) -> str:
        """Look up the merchant's contracted rate for one instrument.

        Use this to confirm what a payment SHOULD have cost, and to get the
        citation - the RBI circular or contract clause - that makes a dispute
        arguable rather than merely assertive.

        Args:
            instrument_key: One of upi, rupay_debit, debit_card_low,
                debit_card_high, credit_card, premium_card, international,
                netbanking, wallet.
        """
        spec = rate_card["instruments"].get(instrument_key)
        if spec is None:
            return json.dumps({
                "error": f"no such instrument '{instrument_key}'",
                "available": sorted(rate_card["instruments"]),
            })
        return json.dumps({
            "instrument": instrument_key,
            "label": spec["label"],
            "network_mdr_percent": spec["network_mdr_bps"] / 100,
            "network_mdr_source": spec["network_mdr_source"],
            "regulatory_cap_percent": (None if spec.get("network_mdr_cap_bps") is None
                                       else spec["network_mdr_cap_bps"] / 100),
            "platform_fee_percent": spec["platform_fee_bps"] / 100,
            "platform_fee_source": spec["platform_fee_source"],
            "gst_percent": rate_card["gst_rate_bps"] / 100,
            "gst_source": rate_card["gst_source"],
            "note": ("Platform fee is legal even where network MDR is mandated to "
                     "zero. Zero network MDR does not mean zero fee."),
        })

    @beta_tool
    def payment_detail(payment_id: str) -> str:
        """Fetch the full record for one payment: how it was paid, what was
        deducted, and what the rate card says it should have been.

        Use this when you need to see the raw fields - for instance to check
        whether a payment labelled as a card really looks like a card.

        Args:
            payment_id: The pay_XXXXXXXX identifier.
        """
        record = by_payment.get(payment_id)
        if record is None:
            return json.dumps({"error": f"no payment '{payment_id}' in this batch"})

        p = record.payment
        expected = compute_expected_fee(p, rate_card)
        lines = [{
            "type": ln.type,
            "settlement_id": ln.settlement_id,
            "utr": ln.utr,
            "amount": _money(ln.amount),
            "fee_charged": _money(ln.fee),
            "gst_charged": _money(ln.tax),
            "settled_at": datetime.fromtimestamp(ln.settled_at, timezone.utc).strftime("%Y-%m-%d"),
        } for ln in record.settlement_lines]

        return json.dumps({
            "payment_id": p.payment_id,
            "order_id": record.order_id,
            "amount": _money(p.amount),
            "method": p.method,
            "card_network": p.card_network,
            "card_type": p.card_type,
            "is_international": p.is_international,
            "upi_reference": p.upi_reference,
            "created_at": datetime.fromtimestamp(record.created_at, timezone.utc).strftime("%Y-%m-%d"),
            "priced_as": expected.instrument_label,
            "expected_fee": _money(expected.total_fee_paise),
            "expected_gst": _money(expected.gst_paise),
            "settlement_lines": lines,
            "engine_notes": expected.notes,
        })

    @beta_tool
    def refund_history(payment_id: str) -> str:
        """Check whether a payment was refunded, and what the gateway kept.

        A retained fee on a refunded order is expected behaviour at every
        Indian gateway - it is a cost to book, not money to claim back.

        Args:
            payment_id: The pay_XXXXXXXX identifier.
        """
        record = by_payment.get(payment_id)
        if record is None:
            return json.dumps({"error": f"no payment '{payment_id}' in this batch"})
        if record.refund is None:
            return json.dumps({"payment_id": payment_id, "refunded": False})

        payment_lines = [ln for ln in record.settlement_lines if ln.type == "payment"]
        retained = (payment_lines[0].fee + payment_lines[0].tax) if payment_lines else 0
        return json.dumps({
            "payment_id": payment_id,
            "refunded": True,
            "refund_id": record.refund.refund_id,
            "refund_amount": _money(record.refund.amount),
            "refunded_at": datetime.fromtimestamp(record.refund.created_at, timezone.utc).strftime("%Y-%m-%d"),
            "fee_and_gst_retained": _money(retained),
            "rule": ("Rule 8 - the original fee is not reversed on a refund. "
                     "Expected, not recoverable."),
        })

    @beta_tool
    def tds_code_map(deducted_on: str) -> str:
        """Which TDS section code is correct for a deduction made on a given date.

        India replaced its entire income tax law on 1 April 2026, and the
        identifier changed from a section name to a four-digit code.

        Args:
            deducted_on: The deduction date as YYYY-MM-DD.
        """
        try:
            when = datetime.strptime(deducted_on, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            return json.dumps({"error": f"'{deducted_on}' is not a YYYY-MM-DD date"})

        if when >= TDS_REGIME_CHANGE:
            return json.dumps({
                "date": deducted_on,
                "regime": "Income Tax Act 2025",
                "correct_code": "1035",
                "correct_provision": "s.393(1) Sl. 8(v)",
                "rate_percent": 0.1,
                "reported_in": "Form 168",
                "stale_code_if_used": "194O",
                "consequence_of_stale_code": (
                    "Return validation rejection, Rs 200/day late fee under the "
                    "filing provisions, and the seller's tax credit may not appear."),
            })
        return json.dumps({
            "date": deducted_on,
            "regime": "Income Tax Act 1961",
            "correct_code": "194O",
            "rate_percent": 1.0,
            "reported_in": "Form 26AS",
            "note": "Code 1035 does not exist before 1 April 2026.",
        })

    @beta_tool
    def similar_past_cases(exception_code: str) -> str:
        """Recall how variances like this one were resolved before.

        If the merchant has already confirmed that a recurring deduction is
        legitimate, that decision stands and this record should not be raised
        again. Returns an empty list when there is no history.

        Args:
            exception_code: The taxonomy code you are considering, e.g.
                ZERO_MDR_VIOLATION.
        """
        hits = [c for c in cases if c.get("exception_code") == exception_code][:3]
        return json.dumps({
            "exception_code": exception_code,
            "cases_found": len(hits),
            "cases": hits,
            "note": ("No history yet - judge this record on its own evidence."
                     if not hits else
                     "These are past resolutions confirmed by the merchant."),
        })

    tools = [rate_card_lookup, payment_detail, refund_history, tds_code_map]

    # Only offer the memory tool when there is memory to offer.
    #
    # Measured: with an empty store the agent called similar_past_cases on 100%
    # of records and got "no history" back every time. Each of those was a full
    # extra round trip - the model emits a tool_use block, we answer, it reads
    # the answer - and output tokens are 83% of what a run costs. A tool that
    # can only ever say "nothing here" is not worth a turn of the loop.
    if cases:
        tools.append(similar_past_cases)
    return tools
