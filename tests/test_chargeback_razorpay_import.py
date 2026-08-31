"""
Tests for mapping a real Razorpay dispute JSON item into this engine's
Dispute shape. Field names verified directly against Razorpay's own API
docs this session - see engine/chargeback/razorpay_import.py's docstring.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from engine.chargeback.razorpay_import import (from_razorpay_batch,  # noqa: E402
                                               from_razorpay_dispute)

REAL_SHAPED = {
    "id": "disp_Esz7KAitoYM7PJ", "entity": "dispute",
    "payment_id": "pay_EsyWjHrfzb59eR", "amount": 10_000, "currency": "INR",
    "amount_deducted": 0, "reason_code": "1064",
    "reason_description": "Goods/Services Not Received",
    "respond_by": 1_790_604_200, "status": "open", "phase": "chargeback",
    "created_at": 1_790_059_211, "evidence": {},
}


def test_a_well_formed_dispute_maps_cleanly():
    dispute, reason = from_razorpay_dispute(REAL_SHAPED)
    assert reason is None
    assert dispute.dispute_id == "disp_Esz7KAitoYM7PJ"
    assert dispute.payment_id == "pay_EsyWjHrfzb59eR"
    assert dispute.amount_paise == 10_000
    assert dispute.reason_code == "1064"
    assert dispute.respond_by == 1_790_604_200


def test_amount_is_an_integer_not_a_float():
    dispute, _reason = from_razorpay_dispute(REAL_SHAPED)
    assert isinstance(dispute.amount_paise, int)


@pytest.mark.parametrize("missing", ["id", "payment_id", "reason_code", "respond_by"])
def test_a_dispute_missing_a_required_field_is_skipped_not_guessed(missing):
    raw = {**REAL_SHAPED, missing: None}
    dispute, reason = from_razorpay_dispute(raw)
    assert dispute is None
    assert reason


def test_a_batch_separates_the_good_from_the_skipped():
    bad = {**REAL_SHAPED, "id": "disp_bad", "reason_code": None}
    disputes, skipped = from_razorpay_batch([REAL_SHAPED, bad])
    assert len(disputes) == 1
    assert len(skipped) == 1
    assert skipped[0][0] == "disp_bad"
