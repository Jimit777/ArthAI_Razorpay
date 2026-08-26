"""
Reading a merchant's own three sources out of the files they actually have.

## Why files, and why this is not a compromise

The three-way reconciliation needs an ERP export, a gateway settlement report
and a bank statement. Two of those have APIs; the third mostly does not.

    invoices     every accounting package exports them. Some have APIs.
    settlements  Razorpay's recon endpoint is real and already wired up in
                 merchant/sources.py, so this path is the fallback rather
                 than the only way.
    bank         there is no free API that hands an Indian merchant their own
                 statement. The Account Aggregator framework is the regulated
                 answer and needs an AA or TSP relationship - the same wall as
                 GSPs for GST filing history. Net-banking CSV export works for
                 every bank today, for everybody, with no commercial
                 dependency.

So the upload path is not a lesser version of the product. For the bank leg it
is currently the only honest one, and it is built to the same standard as the
connected path rather than treated as scaffolding.

## Column names are guessed at, never demanded

Same reasoning as the purchase register importer, and the machinery is shared:
no two banks agree on what to call the credit column, and demanding an exact
layout means the first thing a merchant sees is a rejection. Anything
unmatched is reported by name rather than swallowed.

## The bank statement is the awkward one

Statements come in two shapes and both are common:

    separate columns   Debit | Credit, one of them blank per row
    one column         Amount, with a separate Dr/Cr indicator

Both are handled. Debits are dropped rather than treated as negative credits -
this reconciliation is about money ARRIVING, and a statement full of outgoing
payments would otherwise flood the exception list with rows that were never
supposed to match anything.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

from engine.recon.records import BankCredit, Invoice, ReconBatch, Settlement
from merchant.purchase_import import _paise, map_columns, read_rows

INVOICE_COLUMNS = {
    "invoice_id": ("invoice number", "invoice no", "invoice id", "invoice",
                   "bill number", "bill no", "document number", "voucher no",
                   "reference"),
    "customer_name": ("customer name", "customer", "party name", "party",
                      "buyer name", "client", "account name", "name"),
    "amount": ("invoice amount", "amount", "total", "grand total",
               "invoice total", "value", "gross amount"),
    "date_issued": ("invoice date", "date", "issue date", "document date",
                    "voucher date"),
    "status": ("status", "invoice status", "payment status"),
}

SETTLEMENT_COLUMNS = {
    "txn_id": ("transaction id", "txn id", "payment id", "transaction",
               "entity id", "reference id", "payment"),
    "invoice_reference": ("invoice reference", "invoice number", "invoice no",
                          "order receipt", "receipt", "merchant reference",
                          "order id", "invoice"),
    "gross_amount": ("gross amount", "gross", "amount", "transaction amount",
                     "order amount"),
    "fee_deducted": ("fee deducted", "fee", "commission", "mdr", "charges",
                     "gateway fee"),
    "tax": ("tax", "gst", "tax on fee", "gst on fee"),
    "net_settled": ("net settled", "net amount", "net", "settled amount",
                    "settlement amount", "credit amount"),
    "settlement_date": ("settlement date", "settled at", "settled on", "date",
                        "payout date"),
    "utr": ("utr", "utr number", "settlement utr", "reference number", "rrn"),
}

BANK_COLUMNS = {
    "utr_number": ("utr", "utr number", "reference number", "ref no",
                   "cheque no", "chq/ref no", "transaction id",
                   "transaction reference"),
    "description": ("description", "narration", "particulars", "remarks",
                    "transaction details", "details"),
    "credit_amount": ("credit", "credit amount", "deposit", "deposit amount",
                      "cr amount", "credit(inr)"),
    "amount": ("amount", "transaction amount", "amount(inr)"),
    "kind": ("type", "dr/cr", "drcr", "transaction type", "debit/credit"),
    "debit_amount": ("debit", "debit amount", "withdrawal",
                     "withdrawal amount", "dr amount"),
    "transaction_date": ("transaction date", "date", "value date", "txn date",
                         "posting date", "book date"),
}

CREDIT_WORDS = {"cr", "credit", "c", "deposit", "inward"}

# A UTR as it appears in a statement's own reference column, used when that
# column holds something else entirely and only the narration carries it.
UTR_IN_TEXT = re.compile(r"[A-Z]{4}[A-Z0-9]?\d{6,}")


@dataclass
class SourceImport:
    """What one uploaded file yielded, and what it could not read."""
    rows_read: int = 0
    rows_skipped: list[str] = field(default_factory=list)
    missing_columns: list[str] = field(default_factory=list)
    filename: str = ""

    @property
    def ok(self) -> bool:
        return not self.missing_columns


@dataclass
class InvoiceImport(SourceImport):
    invoices: list = field(default_factory=list)


@dataclass
class SettlementImport(SourceImport):
    settlements: list = field(default_factory=list)


@dataclass
class BankImport(SourceImport):
    credits: list = field(default_factory=list)
    debits_ignored: int = 0


def _date(value) -> Optional[date]:
    """A date from a spreadsheet cell, in the formats these files carry."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    for pattern in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d",
                    "%d-%b-%Y", "%d %b %Y", "%d.%m.%Y", "%m/%d/%Y",
                    # Two-digit years. Banks use them constantly, and their
                    # absence here was worse than a parse failure: the caller
                    # fell back to today, every credit in an HDFC statement
                    # landed outside its three-day window, and the parser
                    # manufactured an exception list of its own.
                    "%d/%m/%y", "%d-%m-%y", "%d-%b-%y", "%d.%m.%y",
                    "%Y-%m-%d %H:%M:%S", "%d/%m/%Y %H:%M:%S"):
        try:
            return datetime.strptime(text[:19], pattern).date()
        except ValueError:
            continue
    return None


def _cells(row, columns):
    def cell(name, default=""):
        index = columns.get(name)
        if index is None or index >= len(row):
            return default
        value = row[index]
        return default if value is None else value
    return cell


def parse_invoices(data: bytes, filename: str = "") -> InvoiceImport:
    """Source A: what was billed."""
    out = InvoiceImport(filename=filename)
    body, headers = read_rows(data, filename)
    if not headers:
        out.missing_columns = ["the file appears to be empty"]
        return out

    columns = map_columns(headers, INVOICE_COLUMNS)
    out.missing_columns = [f for f in ("invoice_id", "amount")
                           if f not in columns]
    if out.missing_columns:
        return out

    for number, row in enumerate(body, start=2):
        out.rows_read += 1
        cell = _cells(row, columns)
        invoice_id = str(cell("invoice_id")).strip()
        if not invoice_id:
            out.rows_skipped.append(f"row {number}: no invoice number")
            continue
        amount = _paise(cell("amount", 0))
        if amount <= 0:
            out.rows_skipped.append(
                f"row {number}: {invoice_id} has no amount on it")
            continue
        when = _date(cell("date_issued"))
        if when is None:
            # Silently substituting today would put the invoice outside every
            # matching window and produce a finding the parser invented.
            out.rows_skipped.append(
                f"row {number}: {invoice_id} has an unreadable date "
                f"({str(cell('date_issued')).strip() or 'blank'})")
            continue
        out.invoices.append(Invoice(
            invoice_id=invoice_id,
            customer_name=str(cell("customer_name")).strip(),
            amount=amount, date_issued=when,
            status=str(cell("status") or "issued").strip().lower()))
    return out


def parse_settlements(data: bytes, filename: str = "") -> SettlementImport:
    """
    Source B: what the gateway says it processed.

    `net_settled` is taken from the file when it is there and derived from
    gross minus fee minus tax when it is not - because half the exports in
    circulation carry one and not the other, and recomputing a figure the file
    already states would let a rounding difference look like a discrepancy.
    """
    out = SettlementImport(filename=filename)
    body, headers = read_rows(data, filename)
    if not headers:
        out.missing_columns = ["the file appears to be empty"]
        return out

    columns = map_columns(headers, SETTLEMENT_COLUMNS)
    out.missing_columns = [f for f in ("txn_id",) if f not in columns]
    if "net_settled" not in columns and "gross_amount" not in columns:
        out.missing_columns.append("net_settled or gross_amount")
    if out.missing_columns:
        return out

    for number, row in enumerate(body, start=2):
        out.rows_read += 1
        cell = _cells(row, columns)
        txn_id = str(cell("txn_id")).strip()
        if not txn_id:
            out.rows_skipped.append(f"row {number}: no transaction id")
            continue

        gross = _paise(cell("gross_amount", 0))
        fee = _paise(cell("fee_deducted", 0)) + _paise(cell("tax", 0))
        net = _paise(cell("net_settled", 0))
        if not net:
            net = gross - fee
        if not gross:
            gross = net + fee
        if net <= 0:
            out.rows_skipped.append(
                f"row {number}: {txn_id} settles nothing")
            continue

        when = _date(cell("settlement_date"))
        if when is None:
            out.rows_skipped.append(
                f"row {number}: {txn_id} has an unreadable settlement date "
                f"({str(cell('settlement_date')).strip() or 'blank'})")
            continue
        reference = str(cell("invoice_reference")).strip()
        out.settlements.append(Settlement(
            txn_id=txn_id, gross_amount=gross, fee_deducted=fee,
            net_settled=net, settlement_date=when,
            invoice_reference=reference or None,
            utr=str(cell("utr")).strip() or None))
    return out


def parse_bank(data: bytes, filename: str = "") -> BankImport:
    """
    Source C: what actually arrived.

    Debits are counted and dropped. This reconciliation is about money coming
    IN, and a current account statement is mostly money going out - keeping
    those rows would flood the exception list with lines that were never
    supposed to match anything, which is the fastest way to make an exception
    list nobody reads.
    """
    out = BankImport(filename=filename)
    body, headers = read_rows(data, filename)
    if not headers:
        out.missing_columns = ["the file appears to be empty"]
        return out

    columns = map_columns(headers, BANK_COLUMNS)
    has_amount = ("credit_amount" in columns or "amount" in columns)
    if not has_amount:
        out.missing_columns.append("credit or amount")
    if "transaction_date" not in columns:
        out.missing_columns.append("transaction_date")
    if out.missing_columns:
        return out

    for number, row in enumerate(body, start=2):
        out.rows_read += 1
        cell = _cells(row, columns)
        credit = _credit_on(cell, columns)
        if credit is None:
            out.debits_ignored += 1
            continue
        if credit <= 0:
            out.rows_skipped.append(f"row {number}: no amount")
            continue

        description = str(cell("description")).strip()
        reference = str(cell("utr_number")).strip()
        if not reference:
            # Plenty of statements have no reference column at all and put the
            # UTR in the narration. Pulling it out here means the matcher's
            # narration pass is a fallback rather than the only hope.
            found = UTR_IN_TEXT.search(description.upper())
            reference = found.group(0) if found else f"row-{number}"

        when = _date(cell("transaction_date"))
        if when is None:
            out.rows_skipped.append(
                f"row {number}: unreadable date "
                f"({str(cell('transaction_date')).strip() or 'blank'})")
            continue
        out.credits.append(BankCredit(
            utr_number=reference, description=description,
            credit_amount=credit, transaction_date=when))
    return out


def _credit_on(cell, columns) -> Optional[int]:
    """
    The credit on this row, or None if it is a debit.

    Two statement shapes, both common: separate Debit and Credit columns, or
    one Amount column with a Dr/Cr indicator beside it.
    """
    if "credit_amount" in columns:
        credit = _paise(cell("credit_amount", 0))
        if credit:
            return credit
        # A blank credit column on a row with a debit is a debit, not a
        # zero-value credit.
        if "debit_amount" in columns and _paise(cell("debit_amount", 0)):
            return None
        return credit or None

    amount = _paise(cell("amount", 0))
    if not amount:
        return None
    if "kind" in columns:
        word = str(cell("kind")).strip().lower()
        return amount if word in CREDIT_WORDS else None
    if "debit_amount" in columns and _paise(cell("debit_amount", 0)):
        return None
    # No indicator anywhere: a negative amount is the only remaining signal.
    return amount if amount > 0 else None


def batch_from(invoices, settlements, credits) -> ReconBatch:
    """The three imports, in the shape the matcher already takes."""
    return ReconBatch(invoices=list(invoices), settlements=list(settlements),
                      bank=list(credits))


# --- the connected path ---------------------------------------------------

def settlements_from_razorpay(rows) -> list:
    """
    Razorpay's settlement recon report, as Settlements.

    The one source that does not need a file. `order_receipt` is the
    merchant's OWN reference for the order, which is what makes Pass 1 exact
    rather than a search - it is the field that ties the gateway's line back
    to the invoice number in their books.
    """
    out = []
    for row in rows:
        if row.get("type") != "payment":
            # Refunds, transfers and adjustments belong in a settlement audit,
            # not in a three-way match against sales invoices. Counted by the
            # caller rather than silently folded in.
            continue
        amount = int(row.get("amount") or 0)
        fee = int(row.get("fee") or 0) + int(row.get("tax") or 0)
        settled_at = row.get("settled_at")
        out.append(Settlement(
            txn_id=str(row.get("payment_id") or row.get("entity_id") or ""),
            gross_amount=amount, fee_deducted=fee, net_settled=amount - fee,
            settlement_date=(date.fromtimestamp(settled_at) if settled_at
                             else date.today()),
            invoice_reference=(str(row.get("order_receipt")).strip()
                               if row.get("order_receipt") else None),
            utr=str(row.get("settlement_utr")).strip() or None
            if row.get("settlement_utr") else None))
    return out
