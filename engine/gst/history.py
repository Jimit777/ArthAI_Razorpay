"""
Several months of purchases from the same suppliers, for the watch.

## Why this is a separate generator

engine/gst/generator.py builds ONE period and gives every invoice its own
random GSTIN, because reconciliation is a per-invoice job and never asks
whether two invoices came from the same supplier. That is correct for what it
does, and useless for watching: 59 invoices produced 58 "suppliers", none of
whom had any history at all.

The watch needs the opposite - a handful of suppliers, each appearing month
after month, each behaving in a way that CHANGES partway through. So it gets
its own generator rather than a flag on the other one, and the validated
reconciliation batch is left exactly as it was measured.

## The scenarios, and why each is here

    reliable      files everything, every month. The control - a watch that
                  raises anything about this supplier is crying wolf.
    always_late   files six weeks late, every month, forever. The decoy. It
                  never changes, so it must never be raised twice.
    stops         files perfectly, then stops. The one real event.
    dies          registration gets cancelled partway through.
    newcomer      appears only in the final month.

The point of the decoy is the same as REFUND_MDR_RETAINED in the settlement
taxonomy: it prevents an entire class of false alarm, which matters more than
catching one extra problem.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

from engine.gst.generator import GSTR2BLine, ITCBatch, PurchaseInvoice
from engine.gst.watch import (STATUS_ACTIVE, STATUS_CANCELLED)
from merchant.suppliers import gstin_for, split_tax

RELIABLE = "reliable"
ALWAYS_LATE = "always_late"
STOPS = "stops"
DIES = "dies"
NEWCOMER = "newcomer"


@dataclass
class SupplierScript:
    name: str
    state: str
    behaviour: str
    monthly_taxable: int
    rate_bps: int = 1800
    changes_in_month: Optional[int] = None      # 1-indexed


DEFAULT_CAST = [
    SupplierScript("Anand Textiles", "27", RELIABLE, 240_000_00, 500),
    SupplierScript("Coimbatore Yarns", "33", RELIABLE, 180_000_00, 500),
    SupplierScript("Kaveri Silk Mills", "29", ALWAYS_LATE, 150_000_00, 500),
    SupplierScript("Deepak Packaging", "24", STOPS, 210_000_00, 1800,
                   changes_in_month=4),
    SupplierScript("Vayu Motors", "27", DIES, 90_000_00, 1800,
                   changes_in_month=5),
    SupplierScript("Bright Print House", "07", RELIABLE, 60_000_00, 1800),
    SupplierScript("Gurgaon Warehousing", "06", NEWCOMER, 130_000_00, 1800),
]

HOME_STATE = "27"


def _period(start: date, offset: int) -> str:
    month = start.month + offset
    year = start.year + (month - 1) // 12
    return f"{year}-{(month - 1) % 12 + 1:02d}"


def _shift(period: str, months: int) -> str:
    year, month = (int(p) for p in period.split("-"))
    total = (year * 12 + month - 1) + months
    return f"{total // 12}-{total % 12 + 1:02d}"


def generate_history(months: int = 6, start: Optional[date] = None,
                     cast: Optional[list[SupplierScript]] = None,
                     seed: int = 20260905
                     ) -> tuple[list[ITCBatch], dict[str, dict]]:
    """
    One cumulative ITCBatch per month, plus the GSTIN statuses at the end.

    Cumulative on purpose: a merchant's purchase register is not wiped every
    month, and a supplier's filing rate only means anything across the whole
    relationship.
    """
    rng = random.Random(seed)
    cast = cast or DEFAULT_CAST
    start = start or date(2026, 3, 1)

    purchases: list[PurchaseInvoice] = []
    filed: list[GSTR2BLine] = []
    statuses: dict[str, dict] = {}
    batches: list[ITCBatch] = []
    counter = 0

    for month in range(1, months + 1):
        period = _period(start, month - 1)
        invoice_day = date(int(period.split("-")[0]), int(period.split("-")[1]),
                           rng.randint(3, 25))

        for script in cast:
            if script.behaviour == NEWCOMER and month < months:
                continue
            if script.behaviour == DIES and script.changes_in_month \
                    and month > script.changes_in_month:
                continue          # a cancelled supplier stops invoicing

            counter += 1
            gstin = gstin_for(script.name, script.state)
            statuses.setdefault(gstin.upper(), {"status": STATUS_ACTIVE})
            interstate = script.state != HOME_STATE
            number = f"{script.name.split()[0][:3].upper()}/{1000 + counter}"
            taxable = script.monthly_taxable
            cgst, sgst, igst = split_tax(taxable, script.rate_bps, interstate)

            purchases.append(PurchaseInvoice(
                invoice_id=f"inv_{counter:04d}", supplier_name=script.name,
                supplier_gstin=gstin, invoice_number=number,
                invoice_date=invoice_day, taxable_value=taxable,
                cgst=cgst, sgst=sgst, igst=igst,
                paid_on=invoice_day + timedelta(days=20)))

            filed_period = _filing_period(script, month, period)
            if filed_period is not None:
                filed.append(GSTR2BLine(
                    supplier_gstin=gstin, invoice_number=number,
                    invoice_date=invoice_day, taxable_value=taxable,
                    cgst=cgst, sgst=sgst, igst=igst,
                    filed_period=filed_period))

            if (script.behaviour == DIES and script.changes_in_month
                    and month == script.changes_in_month):
                statuses[gstin.upper()] = {
                    "status": STATUS_CANCELLED,
                    "changed_on": str(invoice_day + timedelta(days=10))}

        batches.append(ITCBatch(
            purchases=list(purchases), gstr2b=list(filed),
            as_of=date(int(period.split("-")[0]), int(period.split("-")[1]), 28),
            period=period))

    return batches, statuses


def _filing_period(script: SupplierScript, month: int,
                   period: str) -> Optional[str]:
    """Which period this supplier reports the invoice in, or None if never."""
    if script.behaviour == ALWAYS_LATE:
        return _shift(period, 1)
    if script.behaviour == STOPS and script.changes_in_month \
            and month >= script.changes_in_month:
        return None
    return period
