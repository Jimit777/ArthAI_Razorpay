"""
Ledger round trip for the vendor invoice auditor: demo seeding, batch
assembly, detection, the gate, committing a run, and reading findings back
- the same shape tests/test_gst_platform.py exercises for the ITC
reconciler, applied to the new tables.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from engine.vendor_terms.detector import detect_batch, group_by_supplier  # noqa: E402
from engine.vendor_terms.gate import gate_batch  # noqa: E402
from engine.vendor_terms.taxonomy import TermsCode  # noqa: E402
from merchant.ledger import Ledger  # noqa: E402


@pytest.fixture
def shop(tmp_path):
    led = Ledger(str(tmp_path / "shop.db"))
    led.business_id = led.businesses.create("Shop")
    yield led
    led.close()


def test_demo_seeding_plants_a_batch_and_a_matching_rate_card(shop):
    n, truth = shop.seed_vendor_terms_demo(40)
    assert n == 40
    assert len(truth) == 40
    rate_card = shop.vendor_rate_card()
    assert rate_card, "the demo rate card should not be empty"


def test_the_seeded_batch_reproduces_the_answer_key(shop):
    n, truth = shop.seed_vendor_terms_demo(40)
    batch = shop.build_vendor_terms_batch()
    assert batch is not None and len(batch) == n
    rate_card = shop.vendor_rate_card()
    classified = detect_batch(batch, rate_card=rate_card)
    got = {c.line_item_id: c.code for c in classified}
    assert got == truth


def test_a_committed_run_marks_its_line_items_reconciled(shop):
    shop.seed_vendor_terms_demo(40)
    batch = shop.build_vendor_terms_batch()
    run_id = shop.commit_vendor_terms_run(batch)
    assert shop.build_vendor_terms_batch() is None, \
        "every seeded line item should now be reconciled"
    assert shop.latest_vendor_terms_run()["run_id"] == run_id


def test_findings_round_trip_with_the_right_stake_and_action(shop):
    shop.seed_vendor_terms_demo(40)
    batch = shop.build_vendor_terms_batch()
    rate_card = shop.vendor_rate_card()
    classified = detect_batch(batch, rate_card=rate_card)
    groups = group_by_supplier(classified)
    decisions = gate_batch(groups)

    run_id = shop.commit_vendor_terms_run(batch)
    shop.record_vendor_terms_findings(run_id, classified, decisions)

    rows = shop.vendor_terms_findings(run_id)
    assert len(rows) == len(classified)
    overbilled_rows = [r for r in rows if r["code"] == str(TermsCode.OVERBILLED)]
    assert overbilled_rows
    for row in overbilled_rows:
        assert row["money_at_stake_paise"] > 0
        assert row["action"] == "request_credit_note"


def test_setting_a_rate_lets_a_previously_unconfigured_item_be_checked(shop):
    from engine.vendor_terms.detector import LineItem
    from engine.vendor_terms.rules import normalise_item_key
    from datetime import date

    shop.import_purchase_line_items(
        "pur_1", supplier_name="Test Supplier", supplier_gstin="27AABCU9603R1ZM",
        invoice_number="INV-1", invoice_date=str(date.today()),
        items=[{"description": "Widget", "quantity_x100": 100,
               "unit_price_paise": 1_500, "line_total_paise": 1_500}])
    batch = shop.build_vendor_terms_batch()
    rate_card = shop.vendor_rate_card()
    classified = detect_batch(batch, rate_card=rate_card)
    assert classified[0].code == str(TermsCode.RATE_UNCONFIGURED)

    shop.set_vendor_rate("27AABCU9603R1ZM", "Widget", 1_500, source="PO-1")
    rate_card = shop.vendor_rate_card()
    classified = detect_batch(batch, rate_card=rate_card)
    assert classified[0].code == str(TermsCode.RATE_CLEAN)


def test_running_demo_mode_twice_does_not_crash(shop):
    """The demo batch is built from a fixed default seed, so a second run
    regenerates the same line_item_ids - it must refresh the row (a fresh,
    unreconciled item to check again), not hit a UNIQUE-constraint error."""
    shop.seed_vendor_terms_demo(40)
    batch = shop.build_vendor_terms_batch()
    shop.commit_vendor_terms_run(batch)
    assert shop.build_vendor_terms_batch() is None

    shop.seed_vendor_terms_demo(40)
    again = shop.build_vendor_terms_batch()
    assert again is not None and len(again) == 40


def test_a_batch_belongs_to_one_business_only(tmp_path):
    led = Ledger(str(tmp_path / "two.db"))
    first = led.businesses.create("Shop One")
    second = led.businesses.create("Shop Two")

    led.business_id = first
    led.seed_vendor_terms_demo(10)

    led.business_id = second
    assert led.build_vendor_terms_batch() is None
    led.close()
