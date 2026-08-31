"""
Classify one supplier's billed line items against the merchant's own rate
card. Fully mechanical - see taxonomy.py's module docstring for why no
per-line judgment code exists here. The judgment this agent has (dispute
this batch or not, and the argument for the credit note) happens once per
supplier, over the OVERBILLED lines only - see agent/vendor_terms_classifier.py.

ALL MONEY IN PAISE, AS INTEGERS. Quantity is stored as an integer hundredth
(`quantity_x100`) for the same reason - a float here would drift by paise
across a batch and the whole point is that the arithmetic is not arguable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from engine.vendor_terms import rules
from engine.vendor_terms.taxonomy import TERMS_ACTION_FOR, TERMS_LABEL, TermsCode


@dataclass
class LineItem:
    """One item, as billed - before classification."""
    line_item_id: str
    purchase_id: str
    supplier_name: str
    supplier_gstin: str
    invoice_number: str
    invoice_date: date
    description: str
    item_key: str
    quantity_x100: int
    unit_price_paise: int
    line_total_paise: int


@dataclass
class ClassifiedLineItem:
    """The same item, with the contracted price it was checked against and
    the code that comparison produced."""
    line_item_id: str
    purchase_id: str
    supplier_name: str
    supplier_gstin: str
    invoice_number: str
    invoice_date: date
    description: str
    item_key: str
    quantity_x100: int
    unit_price_paise: int
    line_total_paise: int
    contracted_unit_price_paise: Optional[int]
    delta_per_unit_paise: int          # billed - contracted; 0 when unconfigured
    money_at_stake_paise: int          # delta_per_unit * quantity; >= 0 always
    code: str                          # TermsCode
    action: str                        # TermsAction
    reasoning: str = ""

    def as_dict(self) -> dict:
        return {
            "line_item_id": self.line_item_id, "purchase_id": self.purchase_id,
            "supplier_name": self.supplier_name,
            "supplier_gstin": self.supplier_gstin,
            "invoice_number": self.invoice_number,
            "invoice_date": str(self.invoice_date),
            "description": self.description,
            "quantity": self.quantity_x100 / 100,
            "unit_price_paise": self.unit_price_paise,
            "unit_price_display": rules.rupees(self.unit_price_paise),
            "line_total_paise": self.line_total_paise,
            "line_total_display": rules.rupees(self.line_total_paise),
            "contracted_unit_price_paise": self.contracted_unit_price_paise,
            "contracted_unit_price_display": (
                rules.rupees(self.contracted_unit_price_paise)
                if self.contracted_unit_price_paise is not None else ""),
            "money_at_stake_paise": self.money_at_stake_paise,
            "money_at_stake_display": rules.rupees(self.money_at_stake_paise),
            "code": self.code,
            "code_label": TERMS_LABEL.get(TermsCode(self.code), self.code),
            "action": self.action, "reasoning": self.reasoning,
        }


def detect(item: LineItem, *, rate_card: dict[tuple[str, str], int],
          tolerance: rules.Tolerance = rules.Tolerance()) -> ClassifiedLineItem:
    """
    One line item, classified. `rate_card` maps (supplier_gstin, item_key)
    -> contracted unit price in paise; a key absent from it is never
    defaulted to a guessed price.
    """
    key = (item.supplier_gstin.strip().upper(), item.item_key)
    contracted = rate_card.get(key)

    if contracted is None:
        code = TermsCode.RATE_UNCONFIGURED
        delta = 0
        stake = 0
        reasoning = (f'"{item.description}" has no contracted price on '
                    f"file for {item.supplier_name} - add one to the "
                    f"vendor rate card before this line can be checked.")
    else:
        delta = item.unit_price_paise - contracted
        band = tolerance.band(contracted)
        if delta <= band:
            code = TermsCode.RATE_CLEAN
            delta = max(0, delta)
            stake = 0
            reasoning = ""
        else:
            code = TermsCode.OVERBILLED
            stake = (delta * item.quantity_x100) // 100
            reasoning = (
                f"Billed at {rules.rupees(item.unit_price_paise)} per unit "
                f"against a contracted price of {rules.rupees(contracted)} - "
                f"{rules.rupees(stake)} over on this line.")

    return ClassifiedLineItem(
        line_item_id=item.line_item_id, purchase_id=item.purchase_id,
        supplier_name=item.supplier_name, supplier_gstin=item.supplier_gstin,
        invoice_number=item.invoice_number, invoice_date=item.invoice_date,
        description=item.description, item_key=item.item_key,
        quantity_x100=item.quantity_x100, unit_price_paise=item.unit_price_paise,
        line_total_paise=item.line_total_paise,
        contracted_unit_price_paise=contracted, delta_per_unit_paise=delta,
        money_at_stake_paise=stake, code=str(code),
        action=str(TERMS_ACTION_FOR[code]), reasoning=reasoning)


def detect_batch(items: list[LineItem], *, rate_card: dict[tuple[str, str], int],
                 tolerance: rules.Tolerance = rules.Tolerance()
                 ) -> list[ClassifiedLineItem]:
    return [detect(i, rate_card=rate_card, tolerance=tolerance) for i in items]


@dataclass
class SupplierGroup:
    """One supplier's classified lines, grouped for the one place judgment
    actually lives: is this batch worth disputing, and what's the letter."""
    supplier_name: str
    supplier_gstin: str
    items: list[ClassifiedLineItem] = field(default_factory=list)

    @property
    def overbilled(self) -> list[ClassifiedLineItem]:
        return [i for i in self.items if i.code == str(TermsCode.OVERBILLED)]

    @property
    def unconfigured(self) -> list[ClassifiedLineItem]:
        return [i for i in self.items
                if i.code == str(TermsCode.RATE_UNCONFIGURED)]

    @property
    def at_stake_paise(self) -> int:
        return sum(i.money_at_stake_paise for i in self.overbilled)


def group_by_supplier(classified: list[ClassifiedLineItem]
                      ) -> list[SupplierGroup]:
    groups: dict[str, SupplierGroup] = {}
    for item in classified:
        key = item.supplier_gstin.strip().upper() or item.supplier_name
        group = groups.setdefault(
            key, SupplierGroup(supplier_name=item.supplier_name,
                               supplier_gstin=item.supplier_gstin))
        group.items.append(item)
    return sorted(groups.values(), key=lambda g: -g.at_stake_paise)
