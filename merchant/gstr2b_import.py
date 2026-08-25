"""
Reading a real GSTR-2B download.

## What this file is

The government's own record of what a merchant's suppliers reported about
them, downloadable as JSON from the portal:

    gst.gov.in -> Services -> Returns -> Return Dashboard -> pick the month
    -> GSTR-2B -> Download JSON

No special access, no API key: every registered business can pull their own.
Which is why this importer exists and the GSP integration does not - a merchant
can use this today, and the API needs a commercial agreement with a
GSTN-authorised provider.

## Why the parser is tolerant

GSTN has shipped the B2B section under three different paths over the years -
`data.docdata.b2b`, `data.b2b`, and inside `data.docsumm` - and the tax figures
appear in two shapes:

    GSTR-2B style   flattened onto the invoice: txval, igst, cgst, sgst
    GSTR-1 style    nested per line item: itms[].itm_det.{txval,iamt,camt,samt}

Both are real, both turn up in files people actually have, and a parser that
insists on one produces "no invoices found" on a file that is perfectly valid.
So every known shape is tried and anything unrecognised is reported with its
supplier and invoice number rather than silently dropped.

## itcavl

GSTN marks each invoice with whether input credit is available on it, and a
reason when it is not. That is the government's own opinion about the credit,
carried through rather than recomputed - if they say no, the merchant needs to
know regardless of what our own rules conclude.

## Money

Rupees as JSON numbers on the way in, integer paise everywhere after. Converted
once, here, at the boundary.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from typing import Iterable, Optional

# Where the B2B section has lived. Tried in order; the first that holds a list
# wins.
B2B_PATHS = (
    ("data", "docdata", "b2b"),
    ("data", "b2b"),
    ("data", "docsumm", "b2b"),
    ("docdata", "b2b"),
    ("b2b",),
)

# Credit notes reduce credit rather than granting it, and amendments supersede
# an earlier invoice. Both belong in a full reconciliation and neither is what
# the current engine joins on, so they are counted and reported rather than
# quietly folded in as though they were invoices.
OTHER_SECTIONS = ("b2ba", "cdnr", "cdnra", "isd", "impg", "impgsez")


@dataclass
class Gstr2bLine:
    supplier_gstin: str
    supplier_name: str
    invoice_number: str
    invoice_date: Optional[date]
    taxable_value: int
    cgst: int
    sgst: int
    igst: int
    cess: int = 0
    filed_period: str = ""              # the period this file is FOR
    supplier_filed_on: Optional[str] = None
    supplier_period: str = ""
    itc_available: bool = True
    itc_unavailable_reason: str = ""

    @property
    def total_tax(self) -> int:
        return self.cgst + self.sgst + self.igst


@dataclass
class Gstr2bImport:
    lines: list[Gstr2bLine] = field(default_factory=list)
    recipient_gstin: str = ""
    period: str = ""                    # "2026-07"
    suppliers: int = 0
    skipped: list[str] = field(default_factory=list)
    other_sections: dict = field(default_factory=dict)
    error: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.lines) and not self.error

    @property
    def total_tax(self) -> int:
        return sum(l.total_tax for l in self.lines)

    @property
    def blocked_by_gstn(self) -> list[Gstr2bLine]:
        return [l for l in self.lines if not l.itc_available]


def _paise(value) -> int:
    """A rupee figure from the file, as integer paise."""
    if value is None or value == "":
        return 0
    try:
        return int(round(float(value) * 100))
    except (TypeError, ValueError):
        return 0


def _date(text) -> Optional[date]:
    """GST writes dates as DD-MM-YYYY. Other shapes are tried, not assumed."""
    if not text:
        return None
    text = str(text).strip()
    for fmt in ("%d-%m-%Y", "%Y-%m-%d", "%d/%m/%Y", "%d-%b-%Y"):
        try:
            from datetime import datetime

            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _period(rtnprd) -> str:
    """
    GSTN writes a return period as MMYYYY. Everything here uses YYYY-MM.

    Both orderings turn up in the wild, so it is read by which half looks like
    a year rather than by position - "072026" and "202607" both mean July 2026
    and guessing wrong shifts every finding by years.
    """
    text = str(rtnprd or "").strip()
    if len(text) != 6 or not text.isdigit():
        return ""
    if text[:4].isdigit() and 2_000 <= int(text[:4]) <= 2_100:
        return f"{text[:4]}-{text[4:]}"
    return f"{text[2:]}-{text[:2]}"


def _dig(payload: dict, path: Iterable[str]):
    node = payload
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node


def _find_b2b(payload: dict) -> Optional[list]:
    for path in B2B_PATHS:
        found = _dig(payload, path)
        if isinstance(found, list):
            return found
    return None


def _tax_from_invoice(invoice: dict) -> tuple[int, int, int, int, int]:
    """
    The tax on one invoice, whichever shape it arrived in.

    GSTR-2B flattens it onto the invoice; GSTR-1 nests it under items with
    different key names. Both are summed the same way.
    """
    flat = ("txval" in invoice or "igst" in invoice or "cgst" in invoice
            or "sgst" in invoice)
    if flat:
        return (_paise(invoice.get("txval")),
                _paise(invoice.get("cgst") or invoice.get("camt")),
                _paise(invoice.get("sgst") or invoice.get("samt")),
                _paise(invoice.get("igst") or invoice.get("iamt")),
                _paise(invoice.get("cess") or invoice.get("csamt")))

    taxable = cgst = sgst = igst = cess = 0
    for item in invoice.get("itms") or []:
        detail = item.get("itm_det") if isinstance(item, dict) else None
        detail = detail if isinstance(detail, dict) else item
        if not isinstance(detail, dict):
            continue
        taxable += _paise(detail.get("txval"))
        cgst += _paise(detail.get("camt") or detail.get("cgst"))
        sgst += _paise(detail.get("samt") or detail.get("sgst"))
        igst += _paise(detail.get("iamt") or detail.get("igst"))
        cess += _paise(detail.get("csamt") or detail.get("cess"))
    return taxable, cgst, sgst, igst, cess


def parse(data: bytes, filename: str = "") -> Gstr2bImport:
    """One GSTR-2B download, as lines this system can reconcile against."""
    result = Gstr2bImport()

    try:
        payload = json.loads(data.decode("utf-8-sig", errors="replace"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        result.error = (f"That is not readable JSON ({exc}). Download the "
                        f"JSON version from the portal rather than the Excel "
                        f"one.")
        return result

    if not isinstance(payload, dict):
        result.error = "That JSON is not a GSTR-2B file."
        return result

    body = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    result.recipient_gstin = str(body.get("gstin") or "").strip().upper()
    result.period = _period(body.get("rtnprd"))

    b2b = _find_b2b(payload)
    if b2b is None:
        result.error = (
            "No B2B section found. This looks like a GST file but not a "
            "GSTR-2B download - check it is the 2B for a month, not a 2A or "
            "a return you filed.")
        return result

    for supplier in b2b:
        if not isinstance(supplier, dict):
            continue
        gstin = str(supplier.get("ctin") or "").strip().upper()
        name = str(supplier.get("trdnm") or supplier.get("nm") or gstin).strip()
        supplier_period = _period(supplier.get("supprd"))
        filed_on = supplier.get("supfildt")

        invoices = supplier.get("inv") or supplier.get("invoices") or []
        if not isinstance(invoices, list):
            continue
        result.suppliers += 1

        for invoice in invoices:
            if not isinstance(invoice, dict):
                continue
            number = str(invoice.get("inum") or invoice.get("nt_num") or "").strip()
            if not gstin or not number:
                result.skipped.append(
                    f"{name or 'a supplier'}: an invoice with no "
                    f"{'GSTIN' if not gstin else 'number'}")
                continue

            taxable, cgst, sgst, igst, cess = _tax_from_invoice(invoice)
            available = str(invoice.get("itcavl") or "Y").strip().upper() != "N"

            result.lines.append(Gstr2bLine(
                supplier_gstin=gstin, supplier_name=name,
                invoice_number=number,
                invoice_date=_date(invoice.get("idt")),
                taxable_value=taxable, cgst=cgst, sgst=sgst, igst=igst,
                cess=cess,
                filed_period=result.period,
                supplier_filed_on=str(filed_on) if filed_on else None,
                supplier_period=supplier_period,
                itc_available=available,
                itc_unavailable_reason=str(invoice.get("rsn") or "").strip()))

    for section in OTHER_SECTIONS:
        found = None
        for path in B2B_PATHS:
            candidate = _dig(payload, tuple(path[:-1]) + (section,))
            if isinstance(candidate, list):
                found = candidate
                break
        if found:
            result.other_sections[section] = len(found)

    if not result.lines and not result.error:
        result.error = ("The B2B section is empty - no supplier reported an "
                        "invoice against you in this period.")
    return result


def parse_many(files: list[tuple[bytes, str]]) -> tuple[list[Gstr2bImport],
                                                        list[str]]:
    """
    Several months at once.

    The supplier watch needs at least three periods to tell a supplier who has
    STOPPED filing from one who simply has not filed yet, so importing one
    month at a time is the slow road to a feature that cannot work. Files are
    returned in period order regardless of what they were named.
    """
    parsed, problems = [], []
    for data, name in files:
        one = parse(data, name)
        if one.error:
            problems.append(f"{name}: {one.error}")
            continue
        parsed.append(one)

    seen: dict[str, Gstr2bImport] = {}
    for one in parsed:
        key = one.period or f"unknown-{len(seen)}"
        if key in seen:
            problems.append(
                f"two files cover {key}; the later one was ignored")
            continue
        seen[key] = one
    return [seen[k] for k in sorted(seen)], problems
