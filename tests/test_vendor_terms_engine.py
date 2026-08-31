"""
Tests for the vendor invoice auditor's engine: detection, the guardrail
gate, and scoring against the generator's own answer key.

Everything in detect()/detect_batch() is mechanical (see
engine/vendor_terms/taxonomy.py's module docstring for why) - these tests
assert exact agreement with the generator's answer key, not merely "mostly
right".
"""

import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from engine.vendor_terms.detector import (LineItem, detect, detect_batch,  # noqa: E402
                                          group_by_supplier)
from engine.vendor_terms.gate import gate, gate_batch  # noqa: E402
from engine.vendor_terms.generator import (CANONICAL_MIX, UNCONFIGURED_ITEM,  # noqa: E402
                                           generate_line_items)
from engine.vendor_terms.rules import Tolerance, normalise_item_key  # noqa: E402
from engine.vendor_terms.scoring import score_classification  # noqa: E402
from engine.vendor_terms.taxonomy import TermsAction, TermsCode  # noqa: E402


@pytest.fixture(scope="module")
def batch():
    return generate_line_items(40)


# --- normalise_item_key -----------------------------------------------------

def test_item_key_join_is_case_and_punctuation_insensitive():
    assert (normalise_item_key("Steel Rod - 12mm")
           == normalise_item_key("steel rod   12mm"))


def test_item_key_does_not_collapse_genuinely_different_items():
    assert (normalise_item_key("Steel Rod - 12mm")
           != normalise_item_key("Steel Rod - 16mm"))


# --- the generator -----------------------------------------------------------

def test_the_batch_matches_the_canonical_composition(batch):
    _items, truth, _rate_card = batch
    assert len(truth) == sum(CANONICAL_MIX.values())


def test_the_batch_is_reproducible(batch):
    again_items, again_truth, _rc = generate_line_items(40)
    assert again_truth == batch[1]


def test_the_unconfigured_plant_is_absent_from_its_own_rate_card(batch):
    items, truth, rate_card = batch
    unconfigured_ids = {lid for lid, code in truth.items()
                        if code == str(TermsCode.RATE_UNCONFIGURED)}
    assert unconfigured_ids
    for item in items:
        if item.line_item_id in unconfigured_ids:
            assert item.description == UNCONFIGURED_ITEM
            key = (item.supplier_gstin, item.item_key)
            assert key not in rate_card


# --- the detector settles every record exactly -------------------------------

def test_every_line_item_matches_the_answer_key_exactly(batch):
    items, truth, rate_card = batch
    classified = detect_batch(items, rate_card=rate_card)
    got = {c.line_item_id: c.code for c in classified}
    wrong = [(k, truth[k], v) for k, v in got.items() if truth[k] != v]
    assert not wrong, f"mismatches: {wrong}"


def test_a_price_within_tolerance_is_clean():
    item = LineItem(
        line_item_id="x", purchase_id="p", supplier_name="S",
        supplier_gstin="27AABCU9603R1ZM", invoice_number="INV-1",
        invoice_date=date(2026, 8, 1), description="Widget",
        item_key=normalise_item_key("Widget"), quantity_x100=1_00,
        unit_price_paise=1_000, line_total_paise=1_000)
    rate_card = {("27AABCU9603R1ZM", "widget"): 995}
    result = detect(item, rate_card=rate_card)
    assert result.code == str(TermsCode.RATE_CLEAN)
    assert result.money_at_stake_paise == 0


def test_a_price_below_contracted_is_never_flagged():
    """Undercharging is not a finding - CLAUDE.md's own discipline of never
    flagging what is in the merchant's favour."""
    item = LineItem(
        line_item_id="x", purchase_id="p", supplier_name="S",
        supplier_gstin="27AABCU9603R1ZM", invoice_number="INV-1",
        invoice_date=date(2026, 8, 1), description="Widget",
        item_key=normalise_item_key("Widget"), quantity_x100=1_00,
        unit_price_paise=800, line_total_paise=800)
    rate_card = {("27AABCU9603R1ZM", "widget"): 1_000}
    result = detect(item, rate_card=rate_card)
    assert result.code == str(TermsCode.RATE_CLEAN)
    assert result.money_at_stake_paise == 0


def test_a_price_past_tolerance_is_overbilled_with_the_right_stake():
    item = LineItem(
        line_item_id="x", purchase_id="p", supplier_name="S",
        supplier_gstin="27AABCU9603R1ZM", invoice_number="INV-1",
        invoice_date=date(2026, 8, 1), description="Widget",
        item_key=normalise_item_key("Widget"), quantity_x100=10_00,
        unit_price_paise=1_200, line_total_paise=12_000)
    rate_card = {("27AABCU9603R1ZM", "widget"): 1_000}
    result = detect(item, rate_card=rate_card)
    assert result.code == str(TermsCode.OVERBILLED)
    assert result.money_at_stake_paise == 2_000     # (1200-1000) * 10 units
    assert result.action == str(TermsAction.REQUEST_CREDIT_NOTE)


def test_an_unconfigured_item_is_never_defaulted_to_a_guessed_price():
    item = LineItem(
        line_item_id="x", purchase_id="p", supplier_name="S",
        supplier_gstin="27AABCU9603R1ZM", invoice_number="INV-1",
        invoice_date=date(2026, 8, 1), description="Mystery Widget",
        item_key=normalise_item_key("Mystery Widget"), quantity_x100=1_00,
        unit_price_paise=1_000, line_total_paise=1_000)
    result = detect(item, rate_card={})
    assert result.code == str(TermsCode.RATE_UNCONFIGURED)
    assert result.contracted_unit_price_paise is None
    assert result.money_at_stake_paise == 0
    assert result.action == str(TermsAction.ADD_TO_RATE_CARD)


def test_a_different_supplier_with_the_same_item_never_matches_by_accident():
    """The rate-card key is (supplier_gstin, item_key), not item_key alone -
    two suppliers billing the same item at different agreed prices must
    never be conflated."""
    item = LineItem(
        line_item_id="x", purchase_id="p", supplier_name="S2",
        supplier_gstin="24AABCU9603R1ZM", invoice_number="INV-1",
        invoice_date=date(2026, 8, 1), description="Widget",
        item_key=normalise_item_key("Widget"), quantity_x100=1_00,
        unit_price_paise=1_000, line_total_paise=1_000)
    rate_card = {("27AABCU9603R1ZM", "widget"): 1_000}     # a different supplier
    result = detect(item, rate_card=rate_card)
    assert result.code == str(TermsCode.RATE_UNCONFIGURED)


# --- grouping by supplier ----------------------------------------------------

def test_grouping_ranks_suppliers_by_money_at_stake(batch):
    items, _truth, rate_card = batch
    classified = detect_batch(items, rate_card=rate_card)
    groups = group_by_supplier(classified)
    stakes = [g.at_stake_paise for g in groups]
    assert stakes == sorted(stakes, reverse=True)


# --- the guardrail gate -------------------------------------------------------

def test_a_supplier_with_nothing_overbilled_is_never_queued(batch):
    items, _truth, rate_card = batch
    classified = detect_batch(items, rate_card=rate_card)
    groups = group_by_supplier(classified)
    clean_groups = [g for g in groups if not g.overbilled]
    assert clean_groups, "the plant should leave at least one supplier clean"
    for group in clean_groups:
        decision = gate(group)
        assert not decision.queued_for_human
        assert decision.money_at_stake == 0


def test_an_overbilled_supplier_above_the_review_cap_is_queued(batch):
    items, _truth, rate_card = batch
    classified = detect_batch(items, rate_card=rate_card)
    groups = group_by_supplier(classified)
    worst = max(groups, key=lambda g: g.at_stake_paise)
    assert worst.at_stake_paise > 0
    decision = gate(worst, review_above_paise=0)
    assert decision.queued_for_human
    assert decision.action == str(TermsAction.REQUEST_CREDIT_NOTE)


def test_the_action_is_never_softened_by_a_low_confidence_agent(batch):
    items, _truth, rate_card = batch
    classified = detect_batch(items, rate_card=rate_card)
    groups = group_by_supplier(classified)
    overbilled_group = next(g for g in groups if g.overbilled)

    class _Verdict:
        confidence = 0.1
        reasoning = "not sure"
        error = None
        invented_figures = []

    decision = gate(overbilled_group, _Verdict())
    assert decision.action == str(TermsAction.REQUEST_CREDIT_NOTE)
    assert decision.queued_for_human       # low confidence forces review


# --- money -------------------------------------------------------------------

def test_all_money_and_quantity_is_integer(batch):
    items, _truth, _rc = batch
    for item in items:
        assert isinstance(item.unit_price_paise, int)
        assert isinstance(item.quantity_x100, int)
        assert isinstance(item.line_total_paise, int)


def test_tolerance_floor_is_at_least_one_rupee():
    tolerance = Tolerance()
    assert tolerance.band(0) == 100


# --- scoring -------------------------------------------------------------

def test_scoring_a_perfect_run_has_full_recall_and_no_false_accusations(batch):
    items, truth, rate_card = batch
    classified = detect_batch(items, rate_card=rate_card)
    card = score_classification(classified, truth)
    assert card.accuracy == 1.0
    assert card.recall == 1.0
    assert not card.false_accusations
