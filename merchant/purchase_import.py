"""
Reading a purchase register out of a file, and grouping it by supplier.

## Phases A and C of the risk pipeline

    A. parse a CSV or Excel export into rows, then reduce those rows to one
       entry per supplier GSTIN with their tax for the period summed
    C. join the risk profiles back on, multiply, and build the payload

Phase B - the agent - is in agent/risk_agent.py, and deliberately knows nothing
about files.

## Why the column names are guessed at rather than demanded

A purchase register comes out of Tally, Zoho, Busy, an accountant's own
spreadsheet, or a template somebody made in 2019. None of them agree on
headers. Demanding an exact format means the first thing a merchant sees is a
rejection, so each field is matched against the names those systems actually
use, and anything unmatched is reported by name rather than swallowed.

## Money

Files carry rupees as text. They are converted once, here, at the boundary, and
everything downstream is integer paise. A float in the middle of a tax
calculation is how figures stop tying out to the paise.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from typing import Iterable, Optional

# What each field is called in the wild. Order matters only in that the first
# match wins, so the most specific names come first.
COLUMNS = {
    "supplier_gstin": ("supplier gstin", "gstin", "gst no", "gstin of supplier",
                       "supplier gst number", "gst number", "gstin/uin",
                       "gstin of the supplier"),
    "supplier_name": ("supplier name", "supplier", "vendor name", "vendor",
                      "party name", "party", "trade name", "name of supplier"),
    "invoice_number": ("invoice number", "invoice no", "invoice", "bill no",
                       "bill number", "document number", "voucher no"),
    "invoice_date": ("invoice date", "date", "bill date", "document date",
                     "voucher date"),
    "taxable_value": ("taxable value", "taxable amount", "assessable value",
                      "net amount", "basic amount", "value"),
    "cgst": ("cgst", "cgst amount", "central tax"),
    "sgst": ("sgst", "sgst amount", "state tax", "utgst", "sgst/utgst"),
    "igst": ("igst", "igst amount", "integrated tax"),
}

GSTIN = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][0-9A-Z]Z[0-9A-Z]$")


@dataclass
class ImportedRow:
    supplier_gstin: str
    supplier_name: str
    invoice_number: str
    invoice_date: str
    taxable_value: int
    cgst: int
    sgst: int
    igst: int

    @property
    def total_tax(self) -> int:
        return self.cgst + self.sgst + self.igst


@dataclass
class SupplierGroup:
    """One supplier, and everything bought from them in this file."""
    supplier_gstin: str
    supplier_name: str
    invoices: list[ImportedRow] = field(default_factory=list)

    @property
    def invoice_count(self) -> int:
        return len(self.invoices)

    @property
    def taxable_value(self) -> int:
        return sum(r.taxable_value for r in self.invoices)

    @property
    def current_month_total_tax_exposure(self) -> int:
        """Their whole tax for the period, in paise. The figure at stake."""
        return sum(r.total_tax for r in self.invoices)


@dataclass
class ImportResult:
    groups: list[SupplierGroup] = field(default_factory=list)
    rows_read: int = 0
    rows_skipped: list[str] = field(default_factory=list)
    missing_columns: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.groups)

    @property
    def total_tax(self) -> int:
        return sum(g.current_month_total_tax_exposure for g in self.groups)


def _normalise(header: str) -> str:
    return re.sub(r"[^a-z0-9 /]", " ", (header or "").lower()).strip()


def map_columns(headers: Iterable[str]) -> dict[str, int]:
    """Which column holds which field. Unmatched fields simply do not appear."""
    seen = [_normalise(h) for h in headers]
    out: dict[str, int] = {}
    for field_name, candidates in COLUMNS.items():
        for candidate in candidates:
            for index, header in enumerate(seen):
                if header == candidate:
                    out[field_name] = index
                    break
            if field_name in out:
                break
        if field_name in out:
            continue
        # Nothing matched exactly, so allow a containment match - "cgst amt
        # (rs)" should still find cgst.
        for candidate in candidates:
            for index, header in enumerate(seen):
                if candidate in header:
                    out[field_name] = index
                    break
            if field_name in out:
                break
    return out


def _paise(value) -> int:
    """
    A rupee figure from a spreadsheet, as integer paise.

    Handles the things people actually put in these cells: blanks, commas,
    currency symbols, and parentheses for negatives.
    """
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        return int(round(float(value) * 100))
    text = str(value).strip()
    if not text:
        return 0
    negative = text.startswith("(") and text.endswith(")")
    text = re.sub(r"[^0-9.\-]", "", text)
    if not text or text in {"-", "."}:
        return 0
    try:
        amount = int(round(float(text) * 100))
    except ValueError:
        return 0
    return -amount if negative else amount


def read_rows(data: bytes, filename: str) -> tuple[list[list], list[str]]:
    """The file as a header row and body rows, whatever format it arrived in."""
    if filename.lower().endswith((".xlsx", ".xlsm")):
        import openpyxl

        book = openpyxl.load_workbook(io.BytesIO(data), read_only=True,
                                      data_only=True)
        sheet = book.active
        rows = [list(r) for r in sheet.iter_rows(values_only=True)]
        book.close()
    else:
        text = data.decode("utf-8-sig", errors="replace")
        # Sniff the delimiter: exports from Indian accounting software are
        # comma, semicolon or tab depending on locale.
        try:
            dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
        rows = [r for r in csv.reader(io.StringIO(text), dialect)]

    rows = [r for r in rows if any(c not in (None, "") for c in r)]
    if not rows:
        return [], []
    return rows[1:], [str(c or "") for c in rows[0]]


def parse(data: bytes, filename: str) -> ImportResult:
    """
    Phase A: parse, then reduce to one entry per supplier.

    A row without a well-formed GSTIN is skipped and named. There is nothing to
    join it to a filing history on, and inventing a supplier out of a blank
    cell would put a row in the register that can never resolve.
    """
    result = ImportResult()
    body, headers = read_rows(data, filename)
    if not headers:
        result.missing_columns = ["the file appears to be empty"]
        return result

    columns = map_columns(headers)
    required = ("supplier_gstin", "cgst", "sgst", "igst")
    result.missing_columns = [
        f for f in required if f not in columns]
    if "supplier_gstin" not in columns:
        return result

    def cell(row, name, default=""):
        index = columns.get(name)
        if index is None or index >= len(row):
            return default
        value = row[index]
        return default if value is None else value

    grouped: dict[str, SupplierGroup] = {}

    for number, row in enumerate(body, start=2):
        result.rows_read += 1
        gstin = str(cell(row, "supplier_gstin")).strip().upper()
        if not GSTIN.match(gstin):
            result.rows_skipped.append(
                f"row {number}: {gstin or '(blank)'} is not a GSTIN")
            continue

        entry = ImportedRow(
            supplier_gstin=gstin,
            supplier_name=str(cell(row, "supplier_name") or gstin).strip(),
            invoice_number=str(cell(row, "invoice_number")).strip(),
            invoice_date=str(cell(row, "invoice_date")).strip()[:10],
            taxable_value=_paise(cell(row, "taxable_value", 0)),
            cgst=_paise(cell(row, "cgst", 0)),
            sgst=_paise(cell(row, "sgst", 0)),
            igst=_paise(cell(row, "igst", 0)))

        if entry.total_tax == 0:
            result.rows_skipped.append(
                f"row {number}: {entry.supplier_name} has no tax on it")
            continue

        group = grouped.get(gstin)
        if group is None:
            group = SupplierGroup(supplier_gstin=gstin,
                                  supplier_name=entry.supplier_name)
            grouped[gstin] = group
        group.invoices.append(entry)

    result.groups = sorted(
        grouped.values(),
        key=lambda g: -g.current_month_total_tax_exposure)
    return result


# --- a register somebody can actually try -----------------------------------
#
# Chosen rather than random. The personas in filing_history are weighted the
# way suppliers really are - about one in ten defaults - so a demo drawing
# fifteen suppliers at random has a one-in-six chance of containing nothing
# worth looking at. These GSTINs are picked because their generated history
# lands on one of each, so the feature can be tried in one click.
SAMPLE_REGISTER = """\
Party Name,GSTIN of Supplier,Invoice No,Invoice Date,Taxable Value,CGST,SGST,IGST
Anand Textiles,24FJAMH3956X5ZJ,ANA/2041,2026-08-04,240000,0,0,43200
Anand Textiles,24FJAMH3956X5ZJ,ANA/2088,2026-08-19,180000,0,0,32400
Kaveri Silk Mills,27GQRIR1135W5ZQ,KAV/774,2026-08-06,150000,13500,13500,0
Deepak Packaging,29NYOZN7564Z9ZV,DEE/1190,2026-08-02,400000,0,0,72000
Deepak Packaging,29NYOZN7564Z9ZV,DEE/1204,2026-08-21,260000,0,0,46800
Bright Print House,24IARVY9763E8ZD,BRI/318,2026-08-11,60000,0,0,10800
Coimbatore Yarns,27XJGQI1052H7ZR,COI/905,2026-08-14,180000,16200,16200,0
Nashik Logistics,27VLBAN4982B2ZX,NAS/66,2026-08-17,45000,4050,4050,0
"""
