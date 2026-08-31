"""
Tests for engine/gst_filing/razorpay_import.py - real Razorpay invoices,
alongside Demo Mode, never replacing it.
"""

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from engine.gst_filing.razorpay_import import (from_razorpay_batch,  # noqa: E402
                                                from_razorpay_invoice,
                                                state_code_for)

NOW = int(time.time())


def _raw(**overrides) -> dict:
    base = {
        "id": "inv_ABC123", "invoice_number": "INV-001", "date": NOW,
        "customer_details": {
            "name": "Kaveri Traders", "gstin": "33BREHG1077D1ZX",
            "billing_address": {"state": "Tamil Nadu"},
        },
        "line_items": [{"hsn_code": "9403", "taxable_amount": 500000}],
    }
    base.update(overrides)
    return base


# --- state code lookup ---------------------------------------------------

def test_known_states_resolve_case_and_hyphen_insensitively():
    assert state_code_for("Tamil Nadu") == "33"
    assert state_code_for("TAMIL NADU") == "33"
    assert state_code_for("tamil-nadu") == "33"


def test_common_aliases_resolve():
    assert state_code_for("Pondicherry") == "34"
    assert state_code_for("Orissa") == "21"


def test_an_unrecognised_name_resolves_to_none_not_a_guess():
    assert state_code_for("Neverland") is None
    assert state_code_for("") is None
    assert state_code_for(None) is None


# --- from_razorpay_invoice --------------------------------------------

def test_a_complete_invoice_classifies_cleanly():
    rows, reason = from_razorpay_invoice(_raw())
    assert reason is None
    assert len(rows) == 1
    row = rows[0]
    assert row.buyer_gstin == "33BREHG1077D1ZX"
    assert row.place_of_supply == "33"
    assert row.hsn_code == "9403"
    assert row.taxable_value == 500000


def test_no_line_items_is_skipped_with_a_reason():
    rows, reason = from_razorpay_invoice(_raw(line_items=[]))
    assert rows == []
    assert "line items" in reason


def test_no_date_is_skipped_with_a_reason():
    rows, reason = from_razorpay_invoice(_raw(date=None))
    assert rows == []
    assert "date" in reason


def test_unresolvable_place_of_supply_is_skipped_not_defaulted():
    """An empty place_of_supply would read to classify() as 'not
    interstate' - a silently wrong default, not an honest unknown. Must be
    skipped, never passed through as ''."""
    raw = _raw(customer_details={
        "name": "X", "billing_address": {"state": "Nowhereland"}})
    rows, reason = from_razorpay_invoice(raw)
    assert rows == []
    assert "place of supply" in reason


def test_missing_gstin_is_not_a_skip_reason():
    """Unlike HSN or place of supply, a missing GSTIN is handled downstream
    by classify()'s own existing B2C rule - never a reason to drop the row."""
    raw = _raw(customer_details={
        "name": "Walk-in", "billing_address": {"state": "Tamil Nadu"}})
    rows, reason = from_razorpay_invoice(raw)
    assert reason is None
    assert rows[0].buyer_gstin is None


def test_missing_hsn_code_passes_through_as_empty_not_skipped():
    """classify() already excludes an HSN with no rate on file
    (HSN_RATE_UNCONFIGURED) - an empty HSN code reaches that same path
    naturally, since '' is never a key in any real rate card."""
    raw = _raw(line_items=[{"hsn_code": None, "sac_code": None,
                            "taxable_amount": 400}])
    rows, reason = from_razorpay_invoice(raw)
    assert reason is None
    assert rows[0].hsn_code == ""


def test_falls_back_to_amount_when_taxable_amount_is_absent():
    raw = _raw(line_items=[{"hsn_code": "9403", "amount": 300000}])
    rows, _ = from_razorpay_invoice(raw)
    assert rows[0].taxable_value == 300000


def test_zero_taxable_across_every_line_is_skipped():
    raw = _raw(line_items=[{"hsn_code": "9403", "taxable_amount": 0}])
    rows, reason = from_razorpay_invoice(raw)
    assert rows == []
    assert "taxable amount" in reason


# --- multi-HSN split -------------------------------------------------

def test_line_items_sharing_an_hsn_are_summed_into_one_row():
    raw = _raw(line_items=[
        {"hsn_code": "6109", "taxable_amount": 100000},
        {"hsn_code": "6109", "taxable_amount": 50000},
    ])
    rows, reason = from_razorpay_invoice(raw)
    assert reason is None
    assert len(rows) == 1
    assert rows[0].taxable_value == 150000


def test_different_hsn_codes_split_into_separate_suffixed_rows():
    raw = _raw(line_items=[
        {"hsn_code": "6109", "taxable_amount": 100000},
        {"hsn_code": "8471", "taxable_amount": 200000},
    ])
    rows, reason = from_razorpay_invoice(raw)
    assert reason is None
    assert len(rows) == 2
    ids = {r.invoice_id for r in rows}
    assert ids == {"inv_ABC123-1", "inv_ABC123-2"}
    hsn_values = {r.hsn_code: r.taxable_value for r in rows}
    assert hsn_values == {"6109": 100000, "8471": 200000}


def test_a_single_hsn_invoice_keeps_its_own_invoice_id_unsuffixed():
    rows, _ = from_razorpay_invoice(_raw())
    assert rows[0].invoice_id == "inv_ABC123"


# --- from_razorpay_batch ------------------------------------------------

def test_batch_separates_classified_from_skipped_with_reasons():
    items = [_raw(id="inv_1"), _raw(id="inv_2", line_items=[])]
    invoices, skipped = from_razorpay_batch(items)
    assert len(invoices) == 1
    assert skipped == [("inv_2", "no line items on record")]


def test_an_empty_batch_returns_two_empty_lists():
    assert from_razorpay_batch([]) == ([], [])
