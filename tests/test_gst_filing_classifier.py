"""
Tests for layer 1: classifying outward sales and assembling a GSTR-1 draft.

Everything here is mechanical (see engine/gst_filing/taxonomy.py's module
docstring for why) - these tests assert exact agreement with the generator's
answer key, not merely "mostly right".
"""

import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from engine.gst_filing.classifier import (OutwardInvoice, assemble_gstr1,  # noqa: E402
                                          classify, classify_batch)
from engine.gst_filing.generator import (CANONICAL_MIX, DEMO_RATE_CARD,  # noqa: E402
                                         HOME_STATE, UNCONFIGURED_HSN,
                                         generate_invoices)
from engine.gst_filing.taxonomy import GSTR1Code, InvoiceType  # noqa: E402


@pytest.fixture(scope="module")
def batch():
    return generate_invoices(40)


# --- the generator -----------------------------------------------------------

def test_the_batch_matches_the_canonical_composition(batch):
    _invoices, truth = batch
    assert len(truth) == sum(CANONICAL_MIX.values())


def test_the_batch_is_reproducible(batch):
    again, again_truth = generate_invoices(40)
    assert again_truth == batch[1]


# --- the classifier settles every record exactly ----------------------------

def test_every_invoice_matches_the_answer_key_exactly(batch):
    invoices, truth = batch
    classified = classify_batch(invoices, home_state=HOME_STATE,
                                rate_card=DEMO_RATE_CARD,
                                e_invoicing_applicable=True)
    got = {c.invoice_id: c.code for c in classified}
    wrong = [(k, truth[k], v) for k, v in got.items() if truth[k] != v]
    assert not wrong, f"mismatches: {wrong}"


def test_a_well_formed_gstin_always_means_b2b():
    invoice = OutwardInvoice(
        invoice_id="x", invoice_number="x", invoice_date=date(2026, 8, 1),
        buyer_name="Buyer", buyer_gstin="27AABCU9603R1ZM",
        place_of_supply="27", hsn_code="8471", taxable_value=1_000_00)
    result = classify(invoice, home_state=HOME_STATE, rate_card=DEMO_RATE_CARD,
                      e_invoicing_applicable=True)
    assert result.invoice_type == str(InvoiceType.B2B)


def test_an_unregistered_interstate_sale_above_the_threshold_is_b2cl():
    invoice = OutwardInvoice(
        invoice_id="x", invoice_number="x", invoice_date=date(2026, 8, 1),
        buyer_name="Buyer", buyer_gstin=None, place_of_supply="24",
        hsn_code="8471", taxable_value=200_000_00)
    result = classify(invoice, home_state=HOME_STATE, rate_card=DEMO_RATE_CARD,
                      e_invoicing_applicable=True)
    assert result.invoice_type == str(InvoiceType.B2CL)


def test_an_unregistered_sale_under_the_threshold_is_b2cs():
    invoice = OutwardInvoice(
        invoice_id="x", invoice_number="x", invoice_date=date(2026, 8, 1),
        buyer_name="Buyer", buyer_gstin=None, place_of_supply="24",
        hsn_code="8471", taxable_value=5_000_00)
    result = classify(invoice, home_state=HOME_STATE, rate_card=DEMO_RATE_CARD,
                      e_invoicing_applicable=True)
    assert result.invoice_type == str(InvoiceType.B2CS)


def test_an_unconfigured_hsn_is_never_defaulted_to_a_guessed_rate():
    invoice = OutwardInvoice(
        invoice_id="x", invoice_number="x", invoice_date=date(2026, 8, 1),
        buyer_name="Buyer", buyer_gstin=None, place_of_supply=HOME_STATE,
        hsn_code=UNCONFIGURED_HSN, taxable_value=5_000_00)
    result = classify(invoice, home_state=HOME_STATE, rate_card=DEMO_RATE_CARD,
                      e_invoicing_applicable=True)
    assert result.code == str(GSTR1Code.HSN_RATE_UNCONFIGURED)
    assert result.cgst == 0 and result.sgst == 0 and result.igst == 0


def test_e_invoicing_not_applicable_means_no_irn_requirement():
    """A B2B invoice with no IRN is only a finding when e-invoicing actually
    applies to this merchant - below the threshold, silence is correct."""
    invoice = OutwardInvoice(
        invoice_id="x", invoice_number="x", invoice_date=date(2026, 8, 1),
        buyer_name="Buyer", buyer_gstin="27AABCU9603R1ZM",
        place_of_supply="27", hsn_code="8471", taxable_value=1_000_00,
        irn=None)
    result = classify(invoice, home_state=HOME_STATE, rate_card=DEMO_RATE_CARD,
                      e_invoicing_applicable=False)
    assert result.code == str(GSTR1Code.CLASSIFIED)


# --- assembling the draft ----------------------------------------------------

def test_unconfigured_invoices_are_excluded_from_totals_not_dropped(batch):
    invoices, _truth = batch
    classified = classify_batch(invoices, home_state=HOME_STATE,
                                rate_card=DEMO_RATE_CARD,
                                e_invoicing_applicable=True)
    draft = assemble_gstr1(classified, "2026-08")
    assert draft.unconfigured, "the plant should produce at least one"
    unconfigured_ids = {i.invoice_id for i in draft.unconfigured}
    tabled_ids = {i.invoice_id for i in draft.b2b + draft.b2cl + draft.b2cs}
    assert not (unconfigured_ids & tabled_ids), \
        "an unconfigured invoice must not also appear in a tax table"


def test_missing_irn_invoices_are_flagged_but_still_tabled(batch):
    invoices, _truth = batch
    classified = classify_batch(invoices, home_state=HOME_STATE,
                                rate_card=DEMO_RATE_CARD,
                                e_invoicing_applicable=True)
    draft = assemble_gstr1(classified, "2026-08")
    assert draft.missing_irn
    missing_ids = {i.invoice_id for i in draft.missing_irn}
    b2b_ids = {i.invoice_id for i in draft.b2b}
    assert missing_ids <= b2b_ids, \
        "a missing-IRN invoice is still a real B2B sale and belongs in the table"


def test_the_totals_are_the_sum_of_the_tabled_invoices_only(batch):
    invoices, _truth = batch
    classified = classify_batch(invoices, home_state=HOME_STATE,
                                rate_card=DEMO_RATE_CARD,
                                e_invoicing_applicable=True)
    draft = assemble_gstr1(classified, "2026-08")
    tabled = draft.b2b + draft.b2cl + draft.b2cs
    assert draft.total_taxable == sum(i.taxable_value for i in tabled)
    assert draft.total_tax == sum(i.total_tax for i in tabled)


def test_the_hsn_summary_covers_every_hsn_in_the_tabled_invoices(batch):
    invoices, _truth = batch
    classified = classify_batch(invoices, home_state=HOME_STATE,
                                rate_card=DEMO_RATE_CARD,
                                e_invoicing_applicable=True)
    draft = assemble_gstr1(classified, "2026-08")
    tabled_hsns = {i.hsn_code for i in draft.b2b + draft.b2cl + draft.b2cs}
    summary_hsns = {h["hsn_code"] for h in draft.hsn_summary}
    assert tabled_hsns == summary_hsns


# --- money ---------------------------------------------------------------

def test_all_money_is_integer_paise(batch):
    invoices, _truth = batch
    for inv in invoices:
        assert isinstance(inv.taxable_value, int)
