"""
The input tax credit agent's tools. All read-only, same as the settlement set.

Guardrail 1 says the agent never writes to a ledger, and that is enforced by
never handing it a tool that can write - not by asking it nicely in a prompt.
There is no edit, no insert, no file access, no shell.

Every tool returns figures already computed. The agent reads numbers; it never
derives them (CLAUDE.md section 2).

## Why these four

Each one answers a question that actually comes up on the records the detector
could not settle:

    supplier_filing_history   "has this supplier filed reliably before?" -
                              the difference between a supplier who missed one
                              return and one who never files
    find_invoice_number       "does this invoice number appear anywhere else in
                              GSTR-2B?" - the test that separates a wrong-GSTIN
                              filing from a genuine non-filing
    invoice_detail            the full purchase line, including the tax split
    claim_window              how long is left to claim, and what happens after
"""

from __future__ import annotations

import json
from typing import Callable

from anthropic import beta_tool

from engine.gst import rules


def _money(paise: int) -> dict:
    """Money crosses the boundary as paise AND a formatted string, so the agent
    never has to divide by 100 - which is arithmetic, and therefore banned."""
    return {"paise": paise, "display": rules.rupees(paise)}


def build_tools(batch) -> list[Callable]:
    """
    Bound to one batch, so two reconciliations can run side by side and neither
    can reach a record that is not in the batch under audit.
    """
    purchases = {p.invoice_id: p for p in batch.purchases}

    by_gstin: dict[str, list] = {}
    for line in batch.gstr2b:
        by_gstin.setdefault(line.supplier_gstin.upper(), []).append(line)

    claimed_by_gstin: dict[str, list] = {}
    for invoice in batch.purchases:
        claimed_by_gstin.setdefault(invoice.supplier_gstin.upper(), []).append(invoice)

    @beta_tool
    def supplier_filing_history(gstin: str) -> str:
        """How reliably this supplier has reported the invoices you booked.

        Use this to tell a supplier who missed one return apart from one who
        does not file at all. The first is chased with an email; the second is
        a supplier to stop buying from, and the credit may never arrive.

        Args:
            gstin: The supplier's 15-character GSTIN, as it appears on the invoice.
        """
        key = gstin.strip().upper()
        booked = claimed_by_gstin.get(key, [])
        filed = by_gstin.get(key, [])

        # What the supplier REPORTED comes off the GSTR-2B lines, not off the
        # merchant's own invoices. Summing the booked amounts and labelling
        # them "reported" made the tool assert that supplier and books agreed
        # on invoices where they demonstrably did not - the agent caught the
        # contradiction on its first live run and correctly lowered its
        # confidence, which is the behaviour you want but not the tool you want.
        filed_by_number = {line.invoice_number.strip().upper(): line
                           for line in filed}
        pairs = [(i, filed_by_number[i.invoice_number.strip().upper()])
                 for i in booked
                 if i.invoice_number.strip().upper() in filed_by_number]
        matched = [i for i, _line in pairs]
        return json.dumps({
            "gstin": key,
            "state_code": rules.gstin_state(key),
            "well_formed": rules.gstin_well_formed(key),
            "invoices_you_booked": len(booked),
            "invoices_they_reported": len(matched),
            "invoices_missing": len(booked) - len(matched),
            "tax_you_booked": _money(sum(i.total_tax for i in booked)),
            "tax_they_reported": _money(sum(
                line.total_tax for _i, line in pairs)),
            "supplier_name": booked[0].supplier_name if booked else None,
        })

    @beta_tool
    def find_invoice_number(invoice_number: str) -> str:
        """Every GSTR-2B line carrying this invoice number, under any GSTIN.

        This is the test that separates a supplier filing against the wrong
        registration from a supplier who simply has not filed. If the same
        number, date and amount appear under a different GSTIN, the credit
        exists and is sitting in the wrong place. If nothing appears anywhere,
        it was never reported.

        Args:
            invoice_number: The invoice number exactly as booked.
        """
        wanted = invoice_number.strip().upper()
        hits = [line for line in batch.gstr2b
                if line.invoice_number.strip().upper() == wanted]
        return json.dumps({
            "invoice_number": wanted,
            "found_in_gstr2b": len(hits),
            "lines": [{
                "gstin": line.supplier_gstin,
                "state_code": rules.gstin_state(line.supplier_gstin),
                "invoice_date": str(line.invoice_date),
                "taxable_value": _money(line.taxable_value),
                "cgst": _money(line.cgst),
                "sgst": _money(line.sgst),
                "igst": _money(line.igst),
                "total_tax": _money(line.total_tax),
                "filed_period": line.filed_period,
            } for line in hits],
        })

    @beta_tool
    def invoice_detail(invoice_id: str) -> str:
        """The full purchase invoice as your books hold it.

        Use it to check the tax split. CGST plus SGST means the supplier
        treated the supply as within your state; IGST means across states. The
        total is identical either way, so a wrong split is invisible in a
        total-only comparison and is a common cause of a 2B line that will
        never match.

        Args:
            invoice_id: The record id, for example inv_0042.
        """
        invoice = purchases.get(invoice_id)
        if invoice is None:
            return json.dumps({"error": f"{invoice_id} is not in this batch"})
        return json.dumps({
            "invoice_id": invoice.invoice_id,
            "supplier_name": invoice.supplier_name,
            "supplier_gstin": invoice.supplier_gstin,
            "supplier_state_code": rules.gstin_state(invoice.supplier_gstin),
            "invoice_number": invoice.invoice_number,
            "invoice_date": str(invoice.invoice_date),
            "taxable_value": _money(invoice.taxable_value),
            "cgst": _money(invoice.cgst),
            "sgst": _money(invoice.sgst),
            "igst": _money(invoice.igst),
            "total_tax": _money(invoice.total_tax),
            "split": "intra-state (CGST+SGST)" if invoice.igst == 0
                     else "inter-state (IGST)",
            "category": invoice.category,
            "blocked_reason": rules.blocked_reason(invoice.category),
            "supplier_paid_on": str(invoice.paid_on) if invoice.paid_on else None,
        })

    @beta_tool
    def claim_window(invoice_date: str) -> str:
        """How long is left to claim credit on an invoice of this date.

        The deadline is 30 November following the invoice's financial year, so
        two invoices a few days apart can be nearly a year apart in urgency.
        Use this before telling a merchant a mismatch can wait.

        Args:
            invoice_date: The invoice date as YYYY-MM-DD.
        """
        from datetime import date

        try:
            year, month, day = (int(p) for p in invoice_date.split("-"))
            when = date(year, month, day)
        except (ValueError, TypeError):
            return json.dumps({"error": f"{invoice_date} is not a YYYY-MM-DD date"})

        today = batch.as_of
        deadline = rules.claim_deadline(when)
        left = rules.days_to_deadline(when, today)
        return json.dumps({
            "invoice_date": str(when),
            "financial_year": f"FY{rules.financial_year_of(when)}-"
                              f"{str(rules.financial_year_of(when) + 1)[2:]}",
            "claim_deadline": str(deadline),
            "days_remaining": left,
            "time_barred": left < 0,
            "supplier_payment_due_by": str(rules.payment_due_by(when)),
            "source": rules.SOURCE_DEADLINE,
        })

    return [supplier_filing_history, find_invoice_number, invoice_detail,
            claim_window]


TOOL_NAMES = frozenset({"supplier_filing_history", "find_invoice_number",
                        "invoice_detail", "claim_window"})
