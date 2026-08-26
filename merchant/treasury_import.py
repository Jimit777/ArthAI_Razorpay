"""
Reading a merchant's own balances and obligations out of files.

Three uploads, matching the three things a forecast is built from that are not
already on this platform:

    balances    a one-line export, or typed. Every bank shows it; almost none
                give an API a small merchant can use.
    payouts     an AP ageing report or a payment run - what is due and when.
                Every accounting package exports one.
    recurring   OPTIONAL, and the interesting one. If it is not supplied it is
                INFERRED from the payout history rather than assumed to be
                zero, because a forecast that silently omits rent is worse
                than no forecast.

Gateway receipts are not uploaded. This platform already pulls settlements -
see merchant/sources.py - and asking a merchant to export a file this product
can fetch would be inventing work.

## Column names are guessed at, never demanded

Shared machinery with the other importers here. The one addition is the payout
KIND, which decides whether an outflow can be moved at all - and where a file
does not say, it is inferred from the payee, because "Payroll July" and "Salary
transfer" are not vendor invoices and treating them as movable would have the
agent suggest delaying somebody's salary.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from engine.treasury.records import (KIND_LOAN, KIND_PAYROLL, KIND_RECURRING,
                                     KIND_STATUTORY, KIND_VENDOR, BankAccount,
                                     RecurringExpense, ScheduledPayout)
from merchant.purchase_import import _paise, map_columns, read_rows
from merchant.recon_import import _date

BALANCE_COLUMNS = {
    "account_id": ("account number", "account no", "account id", "account",
                   "a/c no", "a/c number"),
    "nickname": ("nickname", "account name", "bank", "bank name", "name",
                 "description"),
    "balance": ("balance", "closing balance", "available balance",
                "current balance", "amount"),
    "as_of": ("as of", "as on", "date", "statement date", "balance date"),
    "overdraft_limit": ("overdraft", "overdraft limit", "od limit",
                        "credit limit", "sanctioned limit"),
}

PAYOUT_COLUMNS = {
    "payout_id": ("payout id", "reference", "invoice number", "invoice no",
                  "bill number", "bill no", "document number", "voucher no",
                  "id"),
    "payee": ("payee", "vendor", "vendor name", "supplier", "supplier name",
              "party", "party name", "beneficiary", "description"),
    "amount": ("amount", "amount due", "outstanding", "balance due", "total",
               "value", "payable"),
    "due_on": ("due date", "due on", "payment date", "scheduled date", "date",
               "maturity date"),
    "kind": ("type", "category", "kind", "payout type", "expense type"),
}

RECURRING_COLUMNS = {
    "name": ("name", "description", "expense", "particulars", "vendor",
             "payee"),
    "amount": ("amount", "monthly amount", "value", "typical amount"),
    "day_of_month": ("day", "day of month", "due day", "charge day"),
    "kind": ("type", "category", "kind"),
}

# What a payee is called, and what that makes it. Order matters: the first
# match wins, and payroll is checked before everything because getting it
# wrong means advising somebody to delay a salary.
KIND_WORDS = (
    (KIND_PAYROLL, ("payroll", "salary", "salaries", "wages", "staff cost",
                    "epf", "provident fund", "esic", "gratuity")),
    (KIND_STATUTORY, ("tds", "tcs", "gst", "advance tax", "income tax",
                      "statutory", "challan", "pf ", "professional tax")),
    (KIND_LOAN, ("emi", "loan", "term loan", "repayment", "instalment to bank",
                 "installment to bank")),
    (KIND_RECURRING, ("rent", "aws", "azure", "google cloud", "subscription",
                      "saas", "licence", "license", "internet", "electricity",
                      "insurance")),
)


def infer_kind(text: str, stated: str = "") -> str:
    """
    What kind of outflow this is, from what it is called.

    A stated kind wins when it is one we recognise. Otherwise the payee is
    read, and anything unrecognised is a vendor invoice - which is the
    MOVABLE default, so the failure mode is the agent suggesting a delay that
    turns out to be awkward, rather than one that is illegal.
    """
    said = (stated or "").strip().lower().replace(" ", "_")
    if said in {KIND_PAYROLL, KIND_STATUTORY, KIND_VENDOR, KIND_RECURRING,
                KIND_LOAN}:
        return said
    haystack = f" {(text or '').lower()} "
    for kind, words in KIND_WORDS:
        if any(word in haystack for word in words):
            return kind
    return KIND_VENDOR


@dataclass
class TreasuryImport:
    rows_read: int = 0
    rows_skipped: list = field(default_factory=list)
    missing_columns: list = field(default_factory=list)
    filename: str = ""
    accounts: list = field(default_factory=list)
    payouts: list = field(default_factory=list)
    recurring: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.missing_columns


def _cells(row, columns):
    def cell(name, default=""):
        index = columns.get(name)
        if index is None or index >= len(row):
            return default
        value = row[index]
        return default if value is None else value
    return cell


def parse_balances(data: bytes, filename: str = "") -> TreasuryImport:
    """What is in the accounts today."""
    out = TreasuryImport(filename=filename)
    body, headers = read_rows(data, filename)
    if not headers:
        out.missing_columns = ["the file appears to be empty"]
        return out

    columns = map_columns(headers, BALANCE_COLUMNS)
    if "balance" not in columns:
        out.missing_columns = ["balance"]
        return out

    for number, row in enumerate(body, start=2):
        out.rows_read += 1
        cell = _cells(row, columns)
        balance = _paise(cell("balance", 0))
        nickname = str(cell("nickname")).strip()
        account_id = str(cell("account_id")).strip() or nickname or f"acc-{number}"
        if not balance and not nickname:
            out.rows_skipped.append(f"row {number}: nothing on it")
            continue
        out.accounts.append(BankAccount(
            account_id=account_id, nickname=nickname or account_id,
            balance=balance, as_of=_date(cell("as_of")) or date.today(),
            overdraft_limit=_paise(cell("overdraft_limit", 0))))
    return out


def parse_payouts(data: bytes, filename: str = "") -> TreasuryImport:
    """
    What is due, when, and whether it can move.

    A payout with no due date is dropped and named. A forecast is a statement
    about DATES, and an obligation with no date on it cannot be placed on the
    curve - guessing today would put it in the wrong week and invent a trough.
    """
    out = TreasuryImport(filename=filename)
    body, headers = read_rows(data, filename)
    if not headers:
        out.missing_columns = ["the file appears to be empty"]
        return out

    columns = map_columns(headers, PAYOUT_COLUMNS)
    out.missing_columns = [f for f in ("amount", "due_on") if f not in columns]
    if out.missing_columns:
        return out

    for number, row in enumerate(body, start=2):
        out.rows_read += 1
        cell = _cells(row, columns)
        amount = _paise(cell("amount", 0))
        if amount <= 0:
            out.rows_skipped.append(f"row {number}: no amount")
            continue
        due = _date(cell("due_on"))
        if due is None:
            out.rows_skipped.append(
                f"row {number}: no readable due date "
                f"({str(cell('due_on')).strip() or 'blank'})")
            continue
        payee = str(cell("payee")).strip()
        out.payouts.append(ScheduledPayout(
            payout_id=str(cell("payout_id")).strip() or f"P-{number}",
            payee=payee or "(unnamed)", amount=amount, due_on=due,
            kind=infer_kind(payee, str(cell("kind")))))
    return out


def parse_recurring(data: bytes, filename: str = "") -> TreasuryImport:
    """Monthly outflows a merchant already knows about."""
    out = TreasuryImport(filename=filename)
    body, headers = read_rows(data, filename)
    if not headers:
        out.missing_columns = ["the file appears to be empty"]
        return out

    columns = map_columns(headers, RECURRING_COLUMNS)
    out.missing_columns = [f for f in ("amount", "day_of_month")
                           if f not in columns]
    if out.missing_columns:
        return out

    for number, row in enumerate(body, start=2):
        out.rows_read += 1
        cell = _cells(row, columns)
        amount = _paise(cell("amount", 0))
        try:
            day = int(str(cell("day_of_month", 1)).strip() or 1)
        except ValueError:
            day = 0
        if amount <= 0 or not 1 <= day <= 31:
            out.rows_skipped.append(
                f"row {number}: needs an amount and a day between 1 and 31")
            continue
        name = str(cell("name")).strip() or f"recurring-{number}"
        out.recurring.append(RecurringExpense(
            name=name, amount=amount, day_of_month=day,
            kind=infer_kind(name, str(cell("kind"))),
            seen_in_months=0, confidence=1.0))
    return out


# --- inferring what recurs, when nobody uploaded it ------------------------

# How many months a charge has to appear in before it counts as recurring.
# Two is a coincidence; three is a pattern.
MIN_MONTHS = 3

# How much a charge may vary month to month and still be the same charge. A
# cloud bill is never the same twice and a rent is; both are recurring.
TOLERANCE_BPS = 2_000


def infer_recurring(payouts, *, today: Optional[date] = None) -> list:
    """
    Find the monthly outflows nobody listed, from what has already been paid.

    The alternative is to assume a merchant has none, which produces a forecast
    that omits rent and every subscription - cheerful, wrong, and wrong in the
    direction that hurts. So when the recurring file is absent this reads the
    payout history instead and says how confident it is.

    Deliberately conservative: three appearances on a similar day of the
    month, and the amount is the median rather than the latest, because one
    unusual month should not set the expectation for the next.
    """
    today = today or date.today()
    groups: dict = defaultdict(list)
    for payout in payouts:
        if payout.due_on >= today:
            # Only history. A future payout is already on the schedule and
            # counting it twice would double it.
            continue
        key = re.sub(r"[^a-z]", "", (payout.payee or "").lower())[:18]
        if key:
            groups[key].append(payout)

    out = []
    for _key, seen in groups.items():
        months = {(p.due_on.year, p.due_on.month) for p in seen}
        if len(months) < MIN_MONTHS:
            continue
        amounts = sorted(p.amount for p in seen)
        median = amounts[len(amounts) // 2]
        spread = max(abs(a - median) for a in amounts)
        if median and (spread * 10_000) // median > TOLERANCE_BPS:
            continue
        days = sorted(p.due_on.day for p in seen)
        out.append(RecurringExpense(
            name=seen[-1].payee, amount=median,
            day_of_month=days[len(days) // 2],
            kind=infer_kind(seen[-1].payee),
            seen_in_months=len(months),
            # Stated rather than assumed. An inference from four months is a
            # weaker claim than an invoice, and the page says so.
            confidence=min(0.95, 0.6 + 0.1 * len(months))))
    return sorted(out, key=lambda r: -r.amount)
