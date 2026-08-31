"""
Tests for the line-item CSV/Excel parser feeding the vendor invoice
auditor. Reuses read_rows()/map_columns()/_paise()/GSTIN from
merchant/purchase_import.py's existing parse() - these tests check the
new grouping and quantity handling parse_line_items() adds on top.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from merchant.purchase_import import (SAMPLE_LINE_ITEM_REGISTER,  # noqa: E402
                                      parse_line_items)


def test_the_sample_register_parses_cleanly():
    result = parse_line_items(SAMPLE_LINE_ITEM_REGISTER.encode(), "sample.csv")
    assert result.ok
    assert not result.missing_columns
    assert result.n_items == result.rows_read


def test_rows_group_by_supplier_and_invoice():
    result = parse_line_items(SAMPLE_LINE_ITEM_REGISTER.encode(), "sample.csv")
    anand = next(i for i in result.invoices if i.invoice_number == "ANA/2201")
    assert anand.supplier_gstin == "27AABCU9603R1ZM"
    assert len(anand.items) == 1
    assert anand.items[0].description == "Steel Rod - 12mm"


def test_quantity_and_price_are_integer_fixed_point():
    result = parse_line_items(SAMPLE_LINE_ITEM_REGISTER.encode(), "sample.csv")
    item = next(i for inv in result.invoices for i in inv.items
               if i.description == "Corrugated Box - Large")
    assert item.quantity_x100 == 100_00        # 100 units
    assert item.unit_price_paise == 34_50       # Rs 34.50
    assert item.line_total_paise == 3_450_00


def test_a_row_without_a_well_formed_gstin_is_skipped_and_named():
    csv_text = ("Supplier Name,Supplier GSTIN,Invoice No,Invoice Date,"
               "Item Description,Qty,Rate,Amount\n"
               "Mystery Vendor,NOTAGSTIN,X/1,2026-08-01,Widget,10,100,1000\n")
    result = parse_line_items(csv_text.encode(), "bad.csv")
    assert not result.ok
    assert any("not a GSTIN" in s for s in result.rows_skipped)


def test_a_row_with_no_usable_quantity_or_price_is_skipped():
    csv_text = ("Supplier Name,Supplier GSTIN,Invoice No,Invoice Date,"
               "Item Description,Qty,Rate,Amount\n"
               "Anand Steel Traders,27AABCU9603R1ZM,X/1,2026-08-01,Widget,0,0,0\n")
    result = parse_line_items(csv_text.encode(), "bad.csv")
    assert not result.ok
    assert any("usable quantity or price" in s for s in result.rows_skipped)


def test_missing_required_columns_are_named():
    csv_text = "Supplier Name,Invoice No\nAnand,X/1\n"
    result = parse_line_items(csv_text.encode(), "bad.csv")
    assert not result.ok
    assert "supplier_gstin" in result.missing_columns


def test_the_existing_tax_focused_parser_is_unaffected():
    """parse() (the ITC-reconciler path) must still work exactly as before
    - a regression here would mean the new parser broke a shared helper."""
    from merchant.purchase_import import SAMPLE_REGISTER, parse

    result = parse(SAMPLE_REGISTER.encode(), "sample.csv")
    assert result.ok
    assert result.groups
