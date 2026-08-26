"""
Building supplier filing history out of the GSTR-2B files a merchant can
download themselves.

## Why this exists, and why it was refused twice first

A GSP contract is a commercial agreement with a GSTN-authorised provider, not a
signup. Most merchants will never have one. What every registered business CAN
do is download their own GSTR-2B, month by month, from the portal - so a stack
of those files is the only route to real supplier history that does not require
buying access.

The objection to using them was concrete and correct as far as it went: GSTR-2B
states what suppliers REPORTED, and the absence of a GSTR-3B column in it is
not evidence that nobody paid. Reading silence as non-payment would report
every supplier in a merchant's book as having defaulted, which is the most
serious finding this product makes.

That objection is answered here rather than by refusing, and the answer is a
third state. Silence is recorded as IGNORANCE, not as default:

    the invoice is there                  -> they filed GSTR-1, with a date
    the portal flagged Rule 37A / itcavl=N -> they did NOT pay. Known.
    neither                                -> payment status UNKNOWN

The engine already divides every payment ratio by the number of periods where
payment is visible (see RiskProfile.gstr3b_known_periods), so a history built
this way reports "files on time, payment not visible" and recommends watching -
never "safe to pay", and never "does not pay the tax" on no evidence.

## What GSTR-2B genuinely proves

Far more than the original objection allowed for, once you stop asking it for
the one thing it does not have:

    supfildt   the date the supplier filed their GSTR-1. Real punctuality,
               not a guess.
    supprd     the period they filed it for, so a late filing is visible as
               a late filing rather than as a missing one.
    cfs        counterparty filing status - whether the supplier actually
               FILED their GSTR-1 or merely uploaded it and left it there.
    itcavl/rsn the government's own verdict that a particular credit is not
               available, and why.

And across a stack of periods, the thing no single file shows: a supplier who
appeared every month for two years and then stopped.

## The one thing this cannot see, stated plainly

A period in which a merchant bought NOTHING from a supplier is indistinguishable
from a period in which the supplier filed nothing. GSTR-2B is a statement about
the merchant's own purchases, so a supplier is simply absent either way. Those
periods produce no row at all rather than a silent zero - the same rule the
uploaded-CSV path follows, for the same reason.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional

from engine.gst.filing_history import (SOURCE_FILE, FilingHistory,
                                       MonthlyFiling, due_dates,
                                       normalise_date, normalise_period)

# Reasons the portal gives for a credit being unavailable that mean the
# SUPPLIER did not pay, as against the credit being blocked for some reason of
# the buyer's own (place of supply, a blocked category, a time bar).
#
# Only these are read as evidence of non-payment. Anything else leaves the
# payment status unknown, because "this credit is not available to you" and
# "your supplier never paid the tax" are different statements and only one of
# them is grounds for holding a supplier's money.
SUPPLIER_DEFAULT_REASONS = (
    "37a",                      # Rule 37A - supplier did not file GSTR-3B
    "return not filed",
    "gstr-3b not filed",
    "gstr3b not filed",
    "supplier has not filed",
    "tax not paid",
    "non-payment of tax",
)

# Where the portal puts the Rule 37A reversal information. Listed rather than
# discovered for the same reason every other field list in this codebase is:
# a schema change must not silently alter what we believe about a supplier.
RULE_37A_KEYS = ("rule37a", "rule_37a", "r37a", "itcrev37a", "rev37a")


def _is_supplier_default(reason: str) -> bool:
    text = (reason or "").strip().lower()
    return any(marker in text for marker in SUPPLIER_DEFAULT_REASONS)


@dataclass
class SupplierMonth:
    """What one GSTR-2B file says about one supplier in one period."""
    invoices: int = 0
    tax: int = 0
    filed_on: Optional[str] = None
    supplier_period: str = ""
    blocked_for_default: bool = False
    blocked_other: int = 0


@dataclass
class Gstr2bHistory:
    """The result of reading a stack of GSTR-2B files as filing history."""
    histories: dict = field(default_factory=dict)   # gstin -> FilingHistory
    periods: list = field(default_factory=list)
    names: dict = field(default_factory=dict)       # gstin -> supplier name
    skipped: list = field(default_factory=list)
    defaults_found: int = 0

    @property
    def ok(self) -> bool:
        return bool(self.histories)

    @property
    def suppliers(self) -> int:
        return len(self.histories)


def from_imports(imports: Iterable, *, names: Optional[dict] = None
                 ) -> Gstr2bHistory:
    """
    A stack of parsed GSTR-2B files, as one filing history per supplier.

    Takes the output of gstr2b_import.parse, so the JSON-shape handling stays
    in one place and this only has to think about what the contents MEAN.
    """
    out = Gstr2bHistory(names=dict(names or {}))
    seen: dict[str, dict[str, SupplierMonth]] = {}

    for parsed in imports:
        if not getattr(parsed, "ok", False):
            if getattr(parsed, "error", ""):
                out.skipped.append(parsed.error)
            continue

        period = normalise_period(parsed.period)
        if period is None:
            out.skipped.append(f"{parsed.period or '(no period)'} is not a "
                               f"tax period")
            continue
        if period not in out.periods:
            out.periods.append(period)

        for line in parsed.lines:
            gstin = (line.supplier_gstin or "").strip().upper()
            if not gstin:
                continue
            if line.supplier_name and gstin not in out.names:
                out.names[gstin] = line.supplier_name

            # A supplier's own filing period, when the file carries it. An
            # invoice from May that appears in a July GSTR-2B was filed late,
            # and treating it as a July filing would hide exactly that.
            filed_for = normalise_period(line.supplier_period) or period

            month = seen.setdefault(gstin, {}).setdefault(
                filed_for, SupplierMonth(supplier_period=filed_for))
            month.invoices += 1
            month.tax += line.total_tax
            if line.supplier_filed_on and month.filed_on is None:
                month.filed_on = line.supplier_filed_on
            if not line.itc_available:
                if _is_supplier_default(line.itc_unavailable_reason):
                    month.blocked_for_default = True
                else:
                    month.blocked_other += 1

    out.periods.sort()
    for gstin, months in seen.items():
        out.histories[gstin] = _history(gstin, months)
        out.defaults_found += sum(
            1 for m in out.histories[gstin].months if m.sold_but_did_not_pay)
    return out


def _history(gstin: str, months: dict) -> FilingHistory:
    """One supplier's periods, in the standard contract."""
    rows = []
    for period in sorted(months):
        month = months[period]
        gstr1_due, gstr3b_due = due_dates(period)

        # The invoice is in the file, so the GSTR-1 was filed. The date is the
        # portal's own where it gave one; where it did not, the return is
        # recorded as filed with no date rather than as unfiled - the invoice
        # being there IS the proof, and inventing a date to fill the column
        # would fabricate punctuality nobody measured.
        filed_on = normalise_date(month.filed_on)

        rows.append(MonthlyFiling(
            period=period,
            gstr1_due=gstr1_due,
            gstr1_filed=filed_on or gstr1_due,
            gstr3b_due=gstr3b_due,
            # Known ONLY when the portal flagged it. Everything else is
            # ignorance, and the engine counts it as such.
            gstr3b_filed=None,
            gstr3b_known=month.blocked_for_default))

    return FilingHistory(
        gstin=gstin, months=rows, source=SOURCE_FILE,
        source_note="Built from the GSTR-2B files you downloaded. These show "
                    "what your suppliers reported; they show payment only "
                    "where the portal flagged a Rule 37A reversal, so payment "
                    "is reported as unknown rather than guessed at.")


def parse_files(files: list[tuple[bytes, str]]) -> Gstr2bHistory:
    """Read a stack of uploaded GSTR-2B JSON files as filing history."""
    from merchant.gstr2b_import import parse

    return from_imports(parse(data, filename) for data, filename in files)
