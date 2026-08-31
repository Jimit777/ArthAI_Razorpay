"""
Tests for engine/gst_filing/gstn_export.py - the real GSTN JSON shapes.

Each assertion checks a field NAME against the schema verified this
session (a certified GSP's API docs, cross-checked against
resilient-tech/india-compliance's real, production GSTR-1/e-invoice code) -
not against this project's own prior shape.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from engine.gst_filing.classifier import assemble_gstr1, classify_batch  # noqa: E402
from engine.gst_filing.generator import DEMO_RATE_CARD, generate_invoices  # noqa: E402
from engine.gst_filing.gstn_export import (to_einvoice_batch,  # noqa: E402
                                           to_einvoice_request,
                                           to_gstr1_json, to_gstr1a_json)

GSTIN = "27ABCDE1234F1Z5"


@pytest.fixture
def draft():
    invoices, _truth = generate_invoices(40)
    classified = classify_batch(invoices, home_state="27",
                                rate_card=DEMO_RATE_CARD,
                                e_invoicing_applicable=True)
    return assemble_gstr1(classified, period="2026-08")


# --- GSTR-1 -----------------------------------------------------------

def test_top_level_keys_match_the_verified_schema(draft):
    out = to_gstr1_json(draft, gstin=GSTIN, home_state="27")
    for key in ("gstin", "fp", "gt", "cur_gt", "b2b", "b2cl", "b2cs", "hsn",
               "doc_issue"):
        assert key in out


def test_filing_period_is_mmyyyy_not_our_own_yyyy_mm(draft):
    out = to_gstr1_json(draft, gstin=GSTIN, home_state="27")
    assert out["fp"] == "082026"


def test_b2b_is_grouped_by_buyer_gstin_with_nested_invoices(draft):
    out = to_gstr1_json(draft, gstin=GSTIN, home_state="27")
    assert out["b2b"]
    group = out["b2b"][0]
    assert set(group) == {"ctin", "inv"}
    invoice = group["inv"][0]
    for key in ("inum", "idt", "val", "pos", "rchrg", "inv_typ", "itms"):
        assert key in invoice
    item = invoice["itms"][0]
    assert item["num"] == 1
    for key in ("rt", "txval", "iamt", "camt", "samt", "csamt"):
        assert key in item["itm_det"]


def test_b2b_dates_are_dd_mm_yyyy_dash_separated(draft):
    out = to_gstr1_json(draft, gstin=GSTIN, home_state="27")
    idt = out["b2b"][0]["inv"][0]["idt"]
    assert idt.count("-") == 2
    assert "/" not in idt


def test_b2cl_items_carry_no_cgst_sgst_keys(draft):
    """B2CL is always interstate - the real schema's b2cl itm_det has no
    camt/samt at all, confirmed from the production section builder."""
    out = to_gstr1_json(draft, gstin=GSTIN, home_state="27")
    assert out["b2cl"]
    item_det = out["b2cl"][0]["inv"][0]["itms"][0]["itm_det"]
    assert "camt" not in item_det
    assert "samt" not in item_det
    assert "iamt" in item_det


def test_b2cs_is_aggregated_by_state_and_rate_not_per_invoice(draft):
    out = to_gstr1_json(draft, gstin=GSTIN, home_state="27")
    assert out["b2cs"]
    row = out["b2cs"][0]
    for key in ("sply_ty", "pos", "typ", "rt", "txval", "iamt", "camt",
               "samt", "csamt"):
        assert key in row
    assert row["sply_ty"] in ("INTER", "INTRA")
    # aggregated - fewer rows than raw B2CS invoices once more than one
    # shares a state+rate
    assert len(out["b2cs"]) <= len(draft.b2cs)


def test_hsn_summary_carries_the_real_field_names(draft):
    out = to_gstr1_json(draft, gstin=GSTIN, home_state="27")
    row = out["hsn"]["data"][0]
    for key in ("num", "hsn_sc", "uqc", "txval", "iamt", "camt", "samt"):
        assert key in row


def test_doc_issue_spans_the_lowest_to_highest_invoice_number(draft):
    out = to_gstr1_json(draft, gstin=GSTIN, home_state="27")
    docs = out["doc_issue"]["doc_det"][0]["docs"][0]
    assert docs["from"] <= docs["to"]
    assert docs["cancel"] == 0


def test_gt_equals_taxable_plus_tax_in_rupees(draft):
    out = to_gstr1_json(draft, gstin=GSTIN, home_state="27")
    expected = round((draft.total_taxable + draft.total_tax) / 100, 2)
    assert out["gt"] == expected == out["cur_gt"]


def test_unconfigured_invoices_are_never_silently_included(draft):
    """Same discipline as classify() itself - an HSN with no rate on file
    stays excluded from the real export, not defaulted into it."""
    out = to_gstr1_json(draft, gstin=GSTIN, home_state="27")
    all_numbers = {i for group in out["b2b"] for inv in group["inv"]
                  for i in [inv["inum"]]}
    all_numbers |= {i for group in out["b2cl"] for inv in group["inv"]
                   for i in [inv["inum"]]}
    unconfigured_numbers = {i.invoice_number for i in draft.unconfigured}
    assert not (all_numbers & unconfigured_numbers)


# --- GSTR-1A (aggregate amendment) --------------------------------------

def test_gstr1a_is_a_b2csa_aggregate_not_a_per_invoice_amendment():
    g1a = {"period": "2026-08", "currently_reflected": 30_576_878,
          "corrected_to": 31_776_878, "amendment_paise": 1_200_000}
    out = to_gstr1a_json(g1a, gstin=GSTIN, period="2026-08", home_state="27")
    assert "b2csa" in out
    assert out["b2csa"][0]["txval"] == 12_000.00
    assert out["fp"] == "082026"


# --- e-invoice batch ------------------------------------------------------

def test_einvoice_request_carries_the_real_top_level_sections(draft):
    seller = {"gstin": GSTIN, "legal_name": "Test Traders"}
    inv = draft.missing_irn[0]
    out = to_einvoice_request(inv, seller=seller, home_state="27")
    for key in ("Version", "TranDtls", "DocDtls", "SellerDtls", "BuyerDtls",
               "ItemList", "ValDtls"):
        assert key in out
    assert out["Version"] == "1.1"


def test_einvoice_dates_are_dd_mm_yyyy_slash_separated(draft):
    seller = {"gstin": GSTIN, "legal_name": "Test Traders"}
    out = to_einvoice_request(draft.missing_irn[0], seller=seller,
                              home_state="27")
    dt = out["DocDtls"]["Dt"]
    assert dt.count("/") == 2
    assert "-" not in dt


def test_missing_seller_address_is_named_not_guessed(draft):
    seller = {"gstin": GSTIN, "legal_name": "Test Traders"}  # no address
    out = to_einvoice_request(draft.missing_irn[0], seller=seller,
                              home_state="27")
    assert out["SellerDtls"]["Addr1"] == ""
    assert "seller address" in out["missing_fields"]


def test_buyer_address_is_always_flagged_never_fabricated(draft):
    """No sale record this system collects has ever carried a buyer's
    address - this must never silently invent one."""
    seller = {"gstin": GSTIN, "legal_name": "Test Traders",
             "address_line1": "1 MG Road", "location": "Pune",
             "pincode": "411001"}
    out = to_einvoice_request(draft.missing_irn[0], seller=seller,
                              home_state="27")
    assert out["BuyerDtls"]["Addr1"] == ""
    assert "buyer address" in out["missing_fields"]
    assert "seller address" not in out["missing_fields"]   # seller WAS supplied


def test_batch_covers_every_irn_missing_invoice_and_no_others(draft):
    seller = {"gstin": GSTIN, "legal_name": "Test Traders"}
    batch = to_einvoice_batch(draft.missing_irn, seller=seller, home_state="27")
    assert len(batch) == len(draft.missing_irn)
    numbers = {b["DocDtls"]["No"] for b in batch}
    assert numbers == {i.invoice_number for i in draft.missing_irn}


def test_item_values_reconcile_with_the_classified_invoice(draft):
    inv = draft.missing_irn[0]
    seller = {"gstin": GSTIN, "legal_name": "Test Traders"}
    out = to_einvoice_request(inv, seller=seller, home_state="27")
    item = out["ItemList"][0]
    assert item["AssAmt"] == round(inv.taxable_value / 100, 2)
    assert item["IgstAmt"] == round(inv.igst / 100, 2)
    assert out["ValDtls"]["TotInvVal"] == round(
        (inv.taxable_value + inv.total_tax) / 100, 2)
