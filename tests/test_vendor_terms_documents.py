"""
Tests for the credit note request drafter. Mirrors the shape of
tests/test_gst_filing_documents.py - none of these call the API.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.vendor_terms_documents import credit_note_request  # noqa: E402


@pytest.fixture
def group():
    """
    Built through ClassifiedLineItem.as_dict() - the real shape
    merchant/agents/vendor_terms.py hands this drafter (`[i.as_dict() for i
    in group.overbilled]`), not a hand-rolled dict. A hand-rolled fixture
    here once hid a real bug: as_dict() keys quantity as "quantity" (already
    divided by 100), and the drafter was reading "quantity_x100" off it,
    silently rendering every row's quantity as 0.
    """
    from datetime import date

    from engine.vendor_terms.detector import ClassifiedLineItem

    items = [
        ClassifiedLineItem(
            line_item_id="li1", purchase_id="p1", supplier_name="Anand Steel Traders",
            supplier_gstin="27AABCU9603R1ZM", invoice_number="ANA/1",
            invoice_date=date(2026, 8, 1), description="Steel Rod - 12mm",
            item_key="steel rod 12mm", quantity_x100=10_00,
            unit_price_paise=8_160, line_total_paise=81_600,
            contracted_unit_price_paise=6_800, delta_per_unit_paise=1_360,
            money_at_stake_paise=13_600, code="OVERBILLED",
            action="request_credit_note"),
        ClassifiedLineItem(
            line_item_id="li2", purchase_id="p1", supplier_name="Anand Steel Traders",
            supplier_gstin="27AABCU9603R1ZM", invoice_number="ANA/1",
            invoice_date=date(2026, 8, 1), description="Cement - OPC 53 Grade (bag)",
            item_key="cement opc 53 grade bag", quantity_x100=5_00,
            unit_price_paise=45_320, line_total_paise=226_600,
            contracted_unit_price_paise=41_200, delta_per_unit_paise=4_120,
            money_at_stake_paise=20_600, code="OVERBILLED",
            action="request_credit_note"),
    ]
    return {
        "supplier_name": "Anand Steel Traders",
        "gstin": "27AABCU9603R1ZM",
        "items": [i.as_dict() for i in items],
    }


def test_the_template_document_never_needs_the_model(group):
    doc = credit_note_request(group)
    assert doc.written_by == "template"
    assert doc.error is None
    assert "Anand Steel Traders" in doc.body
    assert "Steel Rod - 12mm" in doc.body


def test_the_amount_is_the_sum_of_the_items_and_is_not_recomputed(group):
    doc = credit_note_request(group)
    assert doc.amount == 13_600 + 20_600
    assert "Rs 342.00" in doc.body        # 13600 + 20600 paise = Rs 342.00


def test_every_item_appears_in_the_table(group):
    doc = credit_note_request(group)
    for item in group["items"]:
        assert item["description"][:24] in doc.body


def test_the_full_item_list_is_preserved_even_when_the_table_truncates(group):
    """The table shortens a long description for column width; the
    underlying item list a merchant might re-render elsewhere keeps it
    whole."""
    doc = credit_note_request(group)
    assert doc.items == group["items"]


def test_a_supplied_case_is_used_verbatim_and_marks_written_by_agent(group):
    doc = credit_note_request(group, case="A short paragraph the model wrote.")
    assert doc.written_by == "agent"
    assert "A short paragraph the model wrote." in doc.body


def test_a_single_item_uses_singular_phrasing():
    group = {"supplier_name": "S", "gstin": "27AABCU9603R1ZM",
             "items": [{"description": "Widget", "quantity": 1,
                       "unit_price_paise": 1_200,
                       "contracted_unit_price_paise": 1_000,
                       "money_at_stake_paise": 200}]}
    doc = credit_note_request(group)
    assert "1 line item was billed" in doc.body


def test_the_quantity_actually_appears_and_is_not_zero(group):
    """Regression: the drafter once read 'quantity_x100' off a dict whose
    real key (from ClassifiedLineItem.as_dict()) is 'quantity', so every
    row silently rendered its quantity as 0."""
    doc = credit_note_request(group)
    assert "    0 " not in doc.body
    assert "   10 " in doc.body       # the Steel Rod line's real quantity
