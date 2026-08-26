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


def map_columns(headers: Iterable[str],
                columns: Optional[dict] = None) -> dict[str, int]:
    """Which column holds which field. Unmatched fields simply do not appear."""
    seen = [_normalise(h) for h in headers]
    out: dict[str, int] = {}
    for field_name, candidates in (columns or COLUMNS).items():
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
# Composed rather than drawn at random, and larger than it needs to be to
# prove the arithmetic works.
#
# The personas in filing_history are weighted the way suppliers really are -
# about one in ten defaults - so a demo of six suppliers can easily contain
# nothing worth looking at, and did: an earlier version showed four clean
# suppliers and one late filer, which demonstrates a working table and not a
# working product. Twenty-two suppliers with a deliberate spread means every
# pattern the engine can find is on the first screen:
#
#     9  file everything on time            CLEAN_HISTORY
#     5  file late but always file          HABITUAL_LATE_FILER
#     3  report sales and never pay         GSTR3B_DEFAULTER
#     3  no pattern at all                  ERRATIC
#     2  registered too recently to judge   TOO_LITTLE_HISTORY
#
# The GSTINs are real in shape and their generated history lands on the
# persona named above, because history_for seeds on the GSTIN. Changing one
# character changes that supplier's entire record, so they are fixed here
# rather than generated per run.
#
# The names are ordinary Indian trade names from the sectors a textile
# merchant in Maharashtra would actually buy from, and the state codes are
# the real ones - so the CGST/SGST versus IGST split is genuine rather than
# arbitrary, and an inter-state supply looks like one.
SAMPLE_REGISTER = """\
Party Name,GSTIN of Supplier,Invoice No,Invoice Date,Taxable Value,CGST,SGST,IGST
Deepak Packaging,24RWIZN6453L6ZT,DEE/1470,2026-08-10,400000,0,0,72000
Deepak Packaging,24RWIZN6453L6ZT,DEE/5254,2026-08-18,400000,0,0,72000
Bright Print House,27OENNZ1701S7ZP,BRI/3836,2026-08-05,260000,23400,23400,0
Surat Fabrics,24TRQGP7249B1ZD,SUR/3492,2026-08-11,45000,0,0,8100
Pune Threads,27BBHQB9848A8ZC,PUN/9338,2026-08-03,84000,7560,7560,0
Pune Threads,27BBHQB9848A8ZC,PUN/196,2026-08-03,120000,10800,10800,0
Ludhiana Wool,06MTMYP0271S9ZJ,LUD/9628,2026-08-20,120000,0,0,21600
Erode Dyeing,33EKEQC2642X7ZO,ERO/5527,2026-08-08,84000,0,0,15120
Salem Weaves,33TOSGH7225K8ZC,SAL/1695,2026-08-03,400000,0,0,72000
Salem Weaves,33TOSGH7225K8ZC,SAL/896,2026-08-22,240000,0,0,43200
Karur Textiles,33OBCMX5225H5ZB,KAR/7016,2026-08-05,150000,0,0,27000
Noida Electronics,09PADDP4378A9ZX,NOI/9366,2026-08-02,400000,0,0,72000
Kaveri Silk Mills,29PEFVE7643H2ZZ,KAV/8780,2026-08-03,45000,0,0,8100
Kaveri Silk Mills,29PEFVE7643H2ZZ,KAV/975,2026-08-05,240000,0,0,43200
Nashik Logistics,27WXGGN9582G3ZM,NAS/3409,2026-08-18,84000,7560,7560,0
Tirupur Knits,33OEUMR8101E9ZB,TIR/1632,2026-08-19,400000,0,0,72000
Panipat Home Furnishing,06ISEQT9952A3ZH,PAN/9166,2026-08-15,150000,0,0,27000
Panipat Home Furnishing,06ISEQT9952A3ZH,PAN/1177,2026-08-18,120000,0,0,21600
Rajkot Machine Tools,24GSTKO3920D3ZE,RAJ/6751,2026-08-09,45000,0,0,8100
Anand Textiles,27GQRIR1135W5ZQ,ANA/5280,2026-08-26,320000,28800,28800,0
Bhilwara Suiting,08LBMTG4381T4ZM,BHI/8486,2026-08-25,180000,0,0,32400
Bhilwara Suiting,08LBMTG4381T4ZM,BHI/2955,2026-08-03,240000,0,0,43200
Kanpur Leather,09IFEBG8410K5ZS,KAN/8640,2026-08-10,60000,0,0,10800
Jaipur Blocks,08SHPUA3119N2ZY,JAI/8321,2026-08-18,120000,0,0,21600
Aligarh Locks,09NVMIU3344T3ZY,ALI/2733,2026-08-17,150000,0,0,27000
Aligarh Locks,09NVMIU3344T3ZY,ALI/9875,2026-08-16,260000,0,0,46800
Kochi Marine Exports,32QUTXK8026L6ZQ,KOC/6352,2026-08-14,260000,0,0,46800
Coimbatore Yarns,33QJAEU7258T1ZT,COI/439,2026-08-04,45000,0,0,8100
Bhadohi Carpets,09GSLOD6294R2ZF,BHA/6388,2026-08-20,320000,0,0,57600
Bhadohi Carpets,09GSLOD6294R2ZF,BHA/1600,2026-08-26,240000,0,0,43200
"""


# --- mode B: a filing history somebody assembled ---------------------------
#
# The purchase register (above) says what a merchant BOUGHT. This says what
# their suppliers FILED, which is the other half of the same question and the
# half that decides whether the credit exists.
#
# A merchant without a GSP contract can still get this: their own accountant
# keeps it, or a supplier sends filing acknowledgements, or somebody works
# through the portal's public search once a quarter. Tedious, but real - and
# real beats simulated every time, which is why this path exists at all.

HISTORY_COLUMNS = {
    "supplier_gstin": ("supplier gstin", "gstin", "gstin of supplier",
                       "gst no", "gst number", "gstin/uin", "supplier gst number"),
    "period": ("period", "tax period", "return period", "period yyyy mm",
               "month", "ret prd", "filing period"),
    "gstr1_filed": ("gstr 1 filed date", "gstr1 filed date", "gstr 1 filed",
                    "gstr1 filed", "gstr 1 date", "gstr1 date", "gstr 1",
                    "gstr1", "r1 filed", "gstr 1 filing date"),
    "gstr3b_filed": ("gstr 3b filed date", "gstr3b filed date", "gstr 3b filed",
                     "gstr3b filed", "gstr 3b date", "gstr3b date", "gstr 3b",
                     "gstr3b", "3b filed", "gstr 3b filing date"),
    "registration_status": ("registration status", "gstin status", "status",
                            "taxpayer status"),
}


@dataclass
class FilingHistoryImport:
    """What a filing-history upload yielded, and what it could not read."""
    histories: dict = field(default_factory=dict)   # gstin -> FilingHistory
    rows_read: int = 0
    rows_skipped: list[str] = field(default_factory=list)
    missing_columns: list[str] = field(default_factory=list)
    filename: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.histories)

    @property
    def suppliers(self) -> int:
        return len(self.histories)

    @property
    def periods(self) -> int:
        """Total period rows understood, across every supplier."""
        return sum(len(h.months) for h in self.histories.values())


def parse_filing_history(data: bytes, filename: str = "") -> FilingHistoryImport:
    """
    A filing-history export, normalised into the standard contract.

    Wants a GSTIN, a period, and the two filing dates. A blank filing-date cell
    is meaningful and is kept: the row asserts that the period was looked at
    and the return was not filed, which is what makes a default countable. A
    period with no row at all is never counted - see the module docstring in
    engine/gst/filing_history.py, where that distinction is the load-bearing
    one.

    An optional registration-status column is honoured because a cancelled
    registration outranks every other signal, and a merchant who knows one is
    dead should not have to enter it a second time somewhere else.
    """
    from engine.gst.filing_history import (SOURCE_FILE, from_filing_rows,
                                           normalise_period)

    result = FilingHistoryImport(filename=filename)
    body, headers = read_rows(data, filename)
    if not headers:
        result.missing_columns = ["the file appears to be empty"]
        return result

    columns = map_columns(headers, HISTORY_COLUMNS)
    required = ("supplier_gstin", "period", "gstr1_filed", "gstr3b_filed")
    result.missing_columns = [f for f in required if f not in columns]
    # Both filing columns are required rather than merely wanted. A file with
    # GSTR-1 dates and no GSTR-3B column would score every supplier as having
    # never paid a rupee of tax - the most serious finding this product makes -
    # about a merchant's entire supplier book. Refusing is the only safe read.
    if [f for f in required if f not in columns]:
        return result

    def cell(row, name, default=""):
        index = columns.get(name)
        if index is None or index >= len(row):
            return default
        value = row[index]
        return default if value is None else value

    grouped: dict[str, list[dict]] = {}
    statuses: dict[str, str] = {}

    for number, row in enumerate(body, start=2):
        result.rows_read += 1
        gstin = str(cell(row, "supplier_gstin")).strip().upper()
        if not GSTIN.match(gstin):
            result.rows_skipped.append(
                f"row {number}: {gstin or '(blank)'} is not a GSTIN")
            continue

        raw_period = cell(row, "period")
        period = normalise_period(raw_period)
        if period is None:
            result.rows_skipped.append(
                f"row {number}: {str(raw_period).strip() or '(blank)'} is not "
                f"a tax period")
            continue

        grouped.setdefault(gstin, []).append({
            "period": period,
            "gstr1_filed": cell(row, "gstr1_filed", None),
            "gstr3b_filed": cell(row, "gstr3b_filed", None)})

        status = str(cell(row, "registration_status")).strip().lower()
        if status in {"active", "suspended", "cancelled", "canceled"}:
            statuses[gstin] = "cancelled" if status == "canceled" else status

    result.histories = {
        gstin: from_filing_rows(
            gstin, rows, source=SOURCE_FILE,
            registration_status=statuses.get(gstin, "active"))
        for gstin, rows in grouped.items()}
    return result


def filing_history_csv(histories) -> str:
    """
    A set of histories written back out in the upload format.

    Exists so the two modes can be shown to agree: export the simulator's
    history for the sample register, upload it as a file, and every score,
    pattern and recommendation lands identically because the same arithmetic
    ran over the same contract. That is the claim this refactor makes, and this
    is how a person checks it rather than taking it on trust.
    """
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["Supplier GSTIN", "Period", "GSTR-1 Filed Date",
                     "GSTR-3B Filed Date", "Registration Status"])
    for history in histories:
        for month in history.months:
            writer.writerow([
                history.gstin, month.period,
                month.gstr1_filed.isoformat() if month.gstr1_filed else "",
                month.gstr3b_filed.isoformat() if month.gstr3b_filed else "",
                history.registration_status])
    return out.getvalue()


def sample_filing_history(months: int = 36) -> str:
    """
    A filing-history file for the sample register's eight suppliers.

    Generated from the simulator on purpose. It means the "download a sample"
    path produces a file whose upload reproduces the simulated run exactly -
    a one-click demonstration that the mode genuinely does not change the
    answer, rather than an assertion in a README.
    """
    from engine.gst.filing_history import history_for

    parsed = parse(SAMPLE_REGISTER.encode(), "sample.csv")
    return filing_history_csv(
        history_for(g.supplier_gstin, months=months) for g in parsed.groups)
