"""
The TDS credit agent's tools. All read-only, same discipline as every other
tool set in this project - guardrail 1 is enforced by never handing the agent
anything that can write, not by asking it nicely in a prompt.

## Why only three, and why these three

The record's OWN date-driven facts - what rate/code/form apply to ITS
deduction date - are already computed and sitting in the evidence block
(agent/tds_prompt.py), the same "hand the model the number" convention used
for settlement and treasury. Tools exist only for what the model has to go
looking for once it starts investigating:

    find_credit_by_payment   every statement line for this payment, plus a
                             fuzzy amount+date match for a MISSING_CREDIT
                             record - the test that separates "genuinely
                             never posted" from "posted under a reference
                             that does not look like this one"
    deduction_detail          the full deduction line as booked
    expected_tds_treatment    the rate/code/form table, for a date OTHER than
                             the record's own - e.g. a credit that turned up
                             posted against an unexpected date
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Callable

from anthropic import beta_tool

from engine.tds import rules


def _money(paise: int) -> dict:
    """Money crosses the boundary as paise AND a formatted string, so the
    agent never has to divide by 100 - which is arithmetic, and banned."""
    return {"paise": paise, "display": rules.rupees(paise)}


def build_tools(batch) -> list[Callable]:
    """Bound to one batch, so two reconciliations never see each other's data."""
    deductions = {d.payment_id: d for d in batch.deductions}
    credits_by_payment: dict[str, list] = {}
    for c in batch.credits:
        credits_by_payment.setdefault(c.payment_id, []).append(c)

    @beta_tool
    def find_credit_by_payment(payment_id: str) -> str:
        """Every credit-statement line recorded against this payment, plus a
        fuzzy amount-and-date search if none is found under the exact id.

        Use this before concluding a credit is missing. A real Form 26AS or
        Form 168 has no per-transaction reference back to a Razorpay payment
        id, so a genuine posting can sit under a slightly different amount or
        date than expected - this is the test that tells "never posted"
        apart from "posted, but not obviously".

        Args:
            payment_id: The Razorpay payment id, e.g. pay_ABC123.
        """
        exact = credits_by_payment.get(payment_id.strip(), [])
        nearby = []
        if not exact:
            deduction = deductions.get(payment_id.strip())
            if deduction is not None:
                window = (deduction.deducted_at - timedelta(days=60),
                          deduction.deducted_at + timedelta(days=60))
                tol = rules.Tolerance().band(deduction.amount)
                for c in batch.credits:
                    if c.payment_id == payment_id.strip():
                        continue
                    if not (window[0] <= c.posted_at <= window[1]):
                        continue
                    if abs(c.amount - deduction.amount) <= tol * 4:
                        nearby.append(c)
        return json.dumps({
            "payment_id": payment_id.strip(),
            "found_under_this_id": len(exact),
            "lines": [{
                "form": c.form, "code_shown": c.code_shown,
                "amount": _money(c.amount),
                "credited_period": c.credited_period,
                "posted_at": str(c.posted_at),
            } for c in exact],
            "similar_but_different_id": [{
                "payment_id": c.payment_id, "form": c.form,
                "code_shown": c.code_shown, "amount": _money(c.amount),
                "credited_period": c.credited_period,
                "posted_at": str(c.posted_at),
            } for c in nearby],
        })

    @beta_tool
    def deduction_detail(payment_id: str) -> str:
        """The full TDS deduction as Razorpay's settlement report booked it.

        Args:
            payment_id: The Razorpay payment id, e.g. pay_ABC123.
        """
        d = deductions.get(payment_id.strip())
        if d is None:
            return json.dumps({"error": f"{payment_id} is not in this batch"})
        return json.dumps({
            "payment_id": d.payment_id,
            "deducted_at": str(d.deducted_at),
            "gross_amount": _money(d.gross_amount),
            "section_code": d.section_code,
            "rate_bps": d.rate_bps,
            "rate_percent": d.rate_bps / 100,
            "amount": _money(d.amount),
        })

    @beta_tool
    def expected_tds_treatment(deducted_on: str) -> str:
        """The rate, section code and form that apply to a deduction made on
        a given date.

        Use this for a date OTHER than the record's own - for example a
        credit that turned up posted against an unexpected date. The
        record's own expected treatment is already in the evidence you were
        given; this is for anything else you find while investigating.

        Args:
            deducted_on: The date to check, as YYYY-MM-DD.
        """
        try:
            year, month, day = (int(p) for p in deducted_on.split("-"))
            when = date(year, month, day)
        except (ValueError, TypeError):
            return json.dumps({"error": f"{deducted_on} is not a YYYY-MM-DD date"})
        return json.dumps({
            "deducted_on": str(when),
            "expected_rate_bps": rules.expected_rate_bps(when),
            "expected_rate_percent": rules.expected_rate_bps(when) / 100,
            "expected_section_code": rules.expected_section_code(when),
            "expected_form": rules.expected_form(when),
            "provision": rules.expected_provision(when),
            "regime_change_date": str(rules.REGIME_CHANGE),
        })

    return [find_credit_by_payment, deduction_detail, expected_tds_treatment]


TOOL_NAMES = frozenset({"find_credit_by_payment", "deduction_detail",
                        "expected_tds_treatment"})
