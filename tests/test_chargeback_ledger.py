"""
Ledger round trip for the chargeback defence assembler: demo seeding,
manual dispute entry, evidence entry, batch assembly, detection, the gate,
committing a run, and reading findings back - the same shape
tests/test_vendor_terms_ledger.py exercises, applied to the new tables.
"""

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from engine.chargeback.detector import detect_batch  # noqa: E402
from engine.chargeback.gate import gate_batch  # noqa: E402
from engine.chargeback.taxonomy import DisputeCode  # noqa: E402
from merchant.ledger import Ledger  # noqa: E402


@pytest.fixture
def shop(tmp_path):
    led = Ledger(str(tmp_path / "shop.db"))
    led.business_id = led.businesses.create("Shop")
    yield led
    led.close()


def test_demo_seeding_plants_a_batch_with_matching_evidence(shop):
    n, truth = shop.seed_chargeback_demo(30)
    assert n == 30
    assert len(truth) == 30


def test_the_seeded_batch_reproduces_the_answer_key(shop):
    n, truth = shop.seed_chargeback_demo(30)
    batch = shop.build_chargeback_batch()
    assert batch is not None
    disputes, evidence = batch
    assert len(disputes) == n
    classified = detect_batch(disputes, evidence, now=int(time.time()) + 999_999_999)
    # A far-future "now" makes every days_to_respond_by negative but does
    # not change the evidence-completeness classification - only the gate's
    # deadline trigger cares about "now".
    got = {c.dispute_id: c.code for c in classified}
    assert got == truth


def test_a_committed_run_marks_its_disputes_reconciled(shop):
    shop.seed_chargeback_demo(30)
    disputes, _evidence = shop.build_chargeback_batch()
    shop.commit_chargeback_run(disputes)
    assert shop.build_chargeback_batch() is None, \
        "every seeded dispute should now be reconciled"


def test_running_demo_mode_twice_does_not_crash(shop):
    shop.seed_chargeback_demo(30)
    disputes, _evidence = shop.build_chargeback_batch()
    shop.commit_chargeback_run(disputes)
    assert shop.build_chargeback_batch() is None

    shop.seed_chargeback_demo(30)
    again = shop.build_chargeback_batch()
    assert again is not None and len(again[0]) == 30


def test_findings_round_trip_with_the_right_action(shop):
    shop.seed_chargeback_demo(30)
    disputes, evidence = shop.build_chargeback_batch()
    classified = detect_batch(disputes, evidence, now=int(time.time()))
    decisions = gate_batch(classified)

    run_id = shop.commit_chargeback_run(disputes)
    shop.record_chargeback_findings(run_id, classified, decisions)

    rows = shop.chargeback_findings(run_id)
    assert len(rows) == len(classified)
    complete_rows = [r for r in rows if r["code"] == str(DisputeCode.EVIDENCE_COMPLETE)]
    assert complete_rows
    for row in complete_rows:
        assert row["action"] == "draft_evidence_pack"


def test_a_manual_dispute_can_be_entered_and_evidence_added(shop):
    dispute_id = shop.record_manual_dispute(
        payment_id="pay_1", amount_paise=8_500_00, reason_code="1064",
        respond_by=int(time.time()) + 5 * 86_400,
        reason_description="Goods/Services Not Received")
    batch = shop.build_chargeback_batch()
    disputes, evidence = batch
    assert len(disputes) == 1
    assert disputes[0].dispute_id == dispute_id
    assert evidence[dispute_id] == set()

    shop.record_evidence_item(dispute_id, "shipping_proof",
                              "Delhivery DL4471829, delivered 14 Aug")
    disputes, evidence = shop.build_chargeback_batch()
    assert evidence[dispute_id] == {"shipping_proof"}

    classified = detect_batch(disputes, evidence, now=int(time.time()))
    assert classified[0].code == str(DisputeCode.EVIDENCE_PARTIAL)
    assert "shipping_proof" in classified[0].present


def test_setting_evidence_twice_updates_rather_than_duplicates(shop):
    dispute_id = shop.record_manual_dispute(
        payment_id="pay_1", amount_paise=1_000_00, reason_code="1064",
        respond_by=int(time.time()) + 5 * 86_400)
    shop.record_evidence_item(dispute_id, "shipping_proof", "first draft")
    shop.record_evidence_item(dispute_id, "shipping_proof", "corrected detail")
    rows = shop.dispute_evidence(dispute_id)
    assert len(rows) == 1
    assert rows[0]["detail"] == "corrected detail"


def test_a_batch_belongs_to_one_business_only(tmp_path):
    led = Ledger(str(tmp_path / "two.db"))
    first = led.businesses.create("Shop One")
    second = led.businesses.create("Shop Two")

    led.business_id = first
    led.seed_chargeback_demo(10)

    led.business_id = second
    assert led.build_chargeback_batch() is None
    led.close()
