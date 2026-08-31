"""
Real outward invoices, pulled from Razorpay's Invoices API - alongside
Demo Mode, never replacing it. See merchant/agents/gst_filing.py for how
`ctx.source` picks between the two; everything downstream of layer 1
(timing, offset, QRMP) is unchanged either way.

## What Razorpay's Invoices API actually carries

Verified this session against Razorpay's own "Fetch all invoices" API
documentation, including its own example response: `customer_details.gstin`,
`customer_details.billing_address` (line1/line2/zipcode/city/state/country),
and `line_items[].hsn_code`/`.sac_code`/`.tax_rate` are real, documented
fields - but Razorpay's own example response shows every one of them as
`null`. The reason is structural, not a data-quality accident: "You cannot
create GST-compliant invoices using the API" (Razorpay's own docs) - GSTIN,
HSN/SAC and tax rate can only be entered by a person through the Dashboard.
A merchant who only uses Orders or Payment Links, never the Invoices
product's GST fields, will sync real invoices with none of this on them.

That is read the same way an unconfigured HSN rate already is in this
codebase: never guessed, never defaulted, always named. A missing GSTIN
still gets classified - correctly, as B2C, by classify()'s own existing
rule - not skipped. A missing HSN code excludes the invoice from the
draft via the SAME HSN_RATE_UNCONFIGURED path an on-file HSN with no rate
already uses, since an empty HSN code is never a key in any rate card.

## Test mode, honestly

Razorpay test mode does not settle and, by the same token, a fresh test
account has no invoices either unless someone created some by hand through
the test-mode Dashboard. See merchant/sources.py's own docstring for the
identical caveat already stated for settlements - the connector is real,
the data behind it may be empty by design.

## One invoice, one HSN code - so a multi-line invoice may become several

engine.gst_filing.classifier.OutwardInvoice is one invoice, one HSN code,
one taxable value - the shape layer 1 has always taken. A Razorpay invoice
can carry several line items under different HSN codes. Line items that
share an HSN code are summed into one OutwardInvoice; an invoice whose line
items span more than one HSN code becomes one OutwardInvoice per distinct
code, with a `-N` suffix on the invoice number so the document stays
traceable back to the original - never a single row with someone else's
HSN code silently dropped.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from engine.gst_filing.classifier import OutwardInvoice

# CBIC's own state/UT codes (Notification No. 03/2018-19-GST and successors -
# stable government reference data, not a citation seam like the demo's
# rate-slab constants). Keyed by the state name's normalised (lowercased,
# de-hyphenated) form, since Razorpay's own billing_address.state is
# free-text a person typed, not this code.
GST_STATE_CODES: dict[str, str] = {
    "jammu and kashmir": "01", "himachal pradesh": "02", "punjab": "03",
    "chandigarh": "04", "uttarakhand": "05", "haryana": "06", "delhi": "07",
    "nct of delhi": "07", "rajasthan": "08", "uttar pradesh": "09",
    "bihar": "10", "sikkim": "11", "arunachal pradesh": "12", "nagaland": "13",
    "manipur": "14", "mizoram": "15", "tripura": "16", "meghalaya": "17",
    "assam": "18", "west bengal": "19", "jharkhand": "20", "odisha": "21",
    "orissa": "21", "chhattisgarh": "22", "chattisgarh": "22",
    "madhya pradesh": "23", "gujarat": "24",
    "dadra and nagar haveli and daman and diu": "26",
    "daman and diu": "25", "dadra and nagar haveli": "26",
    "maharashtra": "27", "karnataka": "29", "goa": "30", "lakshadweep": "31",
    "kerala": "32", "tamil nadu": "33", "puducherry": "34",
    "pondicherry": "34", "andaman and nicobar islands": "35",
    "telangana": "36", "andhra pradesh": "37", "ladakh": "38",
}


def state_code_for(name: Optional[str]) -> Optional[str]:
    """A best-effort match against CBIC's own state list. None, never a
    guessed code, when the name is absent or matches nothing - a wrong
    place of supply silently mis-splits IGST against CGST+SGST, which is a
    worse failure than leaving it unresolved."""
    if not name:
        return None
    key = " ".join(name.strip().lower().replace("-", " ").split())
    return GST_STATE_CODES.get(key)


def _parse_date(raw: dict) -> Optional[date]:
    for key in ("date", "issued_at", "created_at"):
        value = raw.get(key)
        if value:
            try:
                return datetime.fromtimestamp(int(value)).date()
            except (TypeError, ValueError, OSError):
                continue
    return None


def from_razorpay_invoice(raw: dict) -> tuple[list[OutwardInvoice], Optional[str]]:
    """
    One Razorpay invoice -> zero or more OutwardInvoice rows (see this
    module's docstring for why it can be more than one). Returns
    ([], reason) when the invoice lacks what's structurally needed to
    classify at all - no line items, no date, or nothing taxable.
    """
    invoice_id = raw.get("id", "")
    issued = _parse_date(raw)
    if issued is None:
        return [], "no invoice date on record"

    line_items = raw.get("line_items") or []
    if not line_items:
        return [], "no line items on record"

    customer = raw.get("customer_details") or {}
    buyer_name = (customer.get("name") or customer.get("customer_name")
                 or "Unknown buyer")
    buyer_gstin = customer.get("gstin") or None
    billing = customer.get("billing_address") or {}
    place_of_supply = state_code_for(billing.get("state"))
    if place_of_supply is None:
        # An empty place_of_supply reads to classify() as "not interstate" -
        # a silently WRONG default (intra-state, CGST+SGST) rather than an
        # honest unknown. Skipped, not guessed - the same discipline as a
        # missing taxable amount.
        return [], (f"place of supply not on file (billing state "
                   f"{billing.get('state')!r} did not match a GST state code)")

    by_hsn: dict[str, int] = {}
    for item in line_items:
        hsn = item.get("hsn_code") or item.get("sac_code") or ""
        taxable = item.get("taxable_amount")
        if taxable is None:
            taxable = item.get("amount", 0)
        by_hsn[hsn] = by_hsn.get(hsn, 0) + int(taxable or 0)

    if not any(by_hsn.values()):
        return [], "no taxable amount on record"

    invoice_number = raw.get("invoice_number") or invoice_id
    codes = sorted(by_hsn)
    out = []
    for i, hsn in enumerate(codes, start=1):
        suffix = "" if len(codes) == 1 else f"-{i}"
        out.append(OutwardInvoice(
            invoice_id=f"{invoice_id}{suffix}",
            invoice_number=f"{invoice_number}{suffix}",
            invoice_date=issued, buyer_name=buyer_name,
            buyer_gstin=buyer_gstin, place_of_supply=place_of_supply,
            hsn_code=hsn, taxable_value=by_hsn[hsn], irn=None))
    return out, None


def from_razorpay_batch(raw_items: list[dict]
                        ) -> tuple[list[OutwardInvoice], list[tuple[str, str]]]:
    """Every invoice Razorpay returned, split into what could be classified
    and what could not, with why - never silently dropped."""
    invoices: list[OutwardInvoice] = []
    skipped: list[tuple[str, str]] = []
    for raw in raw_items:
        rows, reason = from_razorpay_invoice(raw)
        if reason is not None:
            skipped.append((raw.get("id", "?"), reason))
        else:
            invoices.extend(rows)
    return invoices, skipped
