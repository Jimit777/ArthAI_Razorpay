"""
Layer 1: classify outward sales into B2B/B2CL/B2CS and assemble a GSTR-1
draft. Fully mechanical - see taxonomy.py's module docstring for why no
judgment code exists here.

## What this produces, and what it deliberately does not

A draft laid out like the real GSTR-1 return - the same table names
(B2B/B2CL/B2CS, an HSN-wise summary) a merchant already recognises. Layer
1's own findings are the input to engine/gst_filing/gstn_export.py, which
turns this draft into the real GSTN offline-utility JSON shape (cross-
verified against a certified GSP's API docs and a production GST-filing
tool - see that module's docstring for exactly what "verified" means and
what still is not). See merchant/agents/gst_filing.py and the Overview tab
for exactly how the export gets labelled to the merchant.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from engine.gst_filing import rules
from engine.gst_filing.taxonomy import GSTR1_LABEL, GSTR1Code, InvoiceType


@dataclass
class OutwardInvoice:
    """One sale, as recorded - before classification."""
    invoice_id: str
    invoice_number: str
    invoice_date: date
    buyer_name: str
    buyer_gstin: Optional[str]
    place_of_supply: str          # 2-digit state code
    hsn_code: str
    taxable_value: int            # paise
    irn: Optional[str] = None


@dataclass
class ClassifiedInvoice:
    """The same sale, with the tax split and its GSTR-1 table decided."""
    invoice_id: str
    invoice_number: str
    invoice_date: date
    buyer_name: str
    buyer_gstin: Optional[str]
    place_of_supply: str
    hsn_code: str
    taxable_value: int
    cgst: int
    sgst: int
    igst: int
    invoice_type: str             # InvoiceType
    irn: Optional[str]
    code: str                     # GSTR1Code
    reasoning: str = ""

    @property
    def total_tax(self) -> int:
        return self.cgst + self.sgst + self.igst

    def as_dict(self) -> dict:
        return {
            "invoice_id": self.invoice_id, "invoice_number": self.invoice_number,
            "invoice_date": str(self.invoice_date), "buyer_name": self.buyer_name,
            "buyer_gstin": self.buyer_gstin or "",
            "place_of_supply": self.place_of_supply, "hsn_code": self.hsn_code,
            "taxable_value": self.taxable_value,
            "taxable_value_display": rules.rupees(self.taxable_value),
            "cgst": self.cgst, "sgst": self.sgst, "igst": self.igst,
            "total_tax": self.total_tax,
            "total_tax_display": rules.rupees(self.total_tax),
            "invoice_type": self.invoice_type, "irn": self.irn or "",
            "code": self.code, "code_label": GSTR1_LABEL.get(GSTR1Code(self.code), self.code),
            "reasoning": self.reasoning,
        }


def classify(invoice: OutwardInvoice, *, home_state: str,
            rate_card: dict[str, int], e_invoicing_applicable: bool
            ) -> ClassifiedInvoice:
    """
    One invoice, classified. `rate_card` maps HSN code -> rate in basis
    points; a code absent from it is never defaulted to a guessed rate.
    """
    rate_bps = rate_card.get(invoice.hsn_code)
    interstate = bool(invoice.place_of_supply) and invoice.place_of_supply != home_state

    if rate_bps is None:
        cgst = sgst = igst = 0
    else:
        cgst, sgst, igst = rules.split_tax(invoice.taxable_value, rate_bps,
                                           interstate)

    if rules.gstin_well_formed(invoice.buyer_gstin or ""):
        invoice_type = InvoiceType.B2B
    elif interstate and invoice.taxable_value > rules.B2CL_THRESHOLD_PAISE:
        invoice_type = InvoiceType.B2CL
    else:
        invoice_type = InvoiceType.B2CS

    irn_required = invoice_type == InvoiceType.B2B and e_invoicing_applicable
    missing_irn = irn_required and not invoice.irn

    if rate_bps is None:
        code = GSTR1Code.HSN_RATE_UNCONFIGURED
        reasoning = (f"HSN {invoice.hsn_code} has no rate on file - add it to "
                    f"your HSN rate card before this invoice can be assembled.")
    elif missing_irn:
        code = GSTR1Code.IRN_MISSING
        reasoning = (f"B2B invoice above the e-invoicing threshold, no IRN "
                    f"recorded - raise it through the e-invoice portal.")
    else:
        code = GSTR1Code.CLASSIFIED
        reasoning = ""

    return ClassifiedInvoice(
        invoice_id=invoice.invoice_id, invoice_number=invoice.invoice_number,
        invoice_date=invoice.invoice_date, buyer_name=invoice.buyer_name,
        buyer_gstin=invoice.buyer_gstin, place_of_supply=invoice.place_of_supply,
        hsn_code=invoice.hsn_code, taxable_value=invoice.taxable_value,
        cgst=cgst, sgst=sgst, igst=igst, invoice_type=str(invoice_type),
        irn=invoice.irn, code=str(code), reasoning=reasoning)


def classify_batch(invoices: list[OutwardInvoice], *, home_state: str,
                   rate_card: dict[str, int], e_invoicing_applicable: bool
                   ) -> list[ClassifiedInvoice]:
    return [classify(i, home_state=home_state, rate_card=rate_card,
                     e_invoicing_applicable=e_invoicing_applicable)
            for i in invoices]


@dataclass
class GSTR1Draft:
    period: str
    b2b: list[ClassifiedInvoice] = field(default_factory=list)
    b2cl: list[ClassifiedInvoice] = field(default_factory=list)
    b2cs: list[ClassifiedInvoice] = field(default_factory=list)
    unconfigured: list[ClassifiedInvoice] = field(default_factory=list)
    missing_irn: list[ClassifiedInvoice] = field(default_factory=list)
    hsn_summary: list[dict] = field(default_factory=list)
    total_taxable: int = 0
    total_tax: int = 0

    def as_dict(self) -> dict:
        return {
            "period": self.period,
            "b2b": [i.as_dict() for i in self.b2b],
            "b2cl": [i.as_dict() for i in self.b2cl],
            "b2cs": [i.as_dict() for i in self.b2cs],
            "unconfigured": [i.as_dict() for i in self.unconfigured],
            "missing_irn": [i.as_dict() for i in self.missing_irn],
            "hsn_summary": self.hsn_summary,
            "total_taxable": self.total_taxable,
            "total_taxable_display": rules.rupees(self.total_taxable),
            "total_tax": self.total_tax,
            "total_tax_display": rules.rupees(self.total_tax),
        }


def assemble_gstr1(classified: list[ClassifiedInvoice], period: str) -> GSTR1Draft:
    """
    Lay classified invoices out like the real GSTR-1 return: one table per
    invoice type, an HSN-wise summary, and the two lists that need action
    (unconfigured HSN, missing IRN) kept visible rather than silently
    dropped from the totals they're excluded from.
    """
    out = GSTR1Draft(period=period)
    hsn_totals: dict[str, dict] = {}

    for inv in classified:
        if inv.code == str(GSTR1Code.HSN_RATE_UNCONFIGURED):
            out.unconfigured.append(inv)
            continue                      # no rate, no tax - not in any table

        bucket = {InvoiceType.B2B: out.b2b, InvoiceType.B2CL: out.b2cl,
                 InvoiceType.B2CS: out.b2cs}[InvoiceType(inv.invoice_type)]
        bucket.append(inv)
        out.total_taxable += inv.taxable_value
        out.total_tax += inv.total_tax

        if inv.code == str(GSTR1Code.IRN_MISSING):
            out.missing_irn.append(inv)

        h = hsn_totals.setdefault(inv.hsn_code, {
            "hsn_code": inv.hsn_code, "taxable_value": 0,
            "cgst": 0, "sgst": 0, "igst": 0})
        h["taxable_value"] += inv.taxable_value
        h["cgst"] += inv.cgst
        h["sgst"] += inv.sgst
        h["igst"] += inv.igst

    out.hsn_summary = [
        {**h, "taxable_value_display": rules.rupees(h["taxable_value"]),
         "total_tax": h["cgst"] + h["sgst"] + h["igst"],
         "total_tax_display": rules.rupees(h["cgst"] + h["sgst"] + h["igst"])}
        for h in sorted(hsn_totals.values(), key=lambda h: -h["taxable_value"])]

    return out
