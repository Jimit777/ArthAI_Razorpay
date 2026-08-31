"""
Tests for the chargeback defence assembler's engine: detection, the
guardrail gate, and scoring against the generator's own answer key.

Everything in detect()/detect_batch() is mechanical (see
engine/chargeback/taxonomy.py's module docstring for why) - these tests
assert exact agreement with the generator's answer key, not merely "mostly
right".
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from engine.chargeback.detector import Dispute, detect, detect_batch  # noqa: E402
from engine.chargeback.gate import gate, gate_batch  # noqa: E402
from engine.chargeback.generator import (AS_OF, CANONICAL_MIX, UNMAPPED_CODE,  # noqa: E402
                                         generate_disputes)
from engine.chargeback.rules import REASON_CODE_EVIDENCE  # noqa: E402
from engine.chargeback.taxonomy import DisputeAction, DisputeCode  # noqa: E402


@pytest.fixture(scope="module")
def batch():
    return generate_disputes(30)


NOW = int(AS_OF.timestamp())


# --- the reason-code table ----------------------------------------------

def test_the_reason_code_table_only_uses_the_real_evidence_vocabulary():
    real_vocabulary = {
        "shipping_proof", "billing_proof", "cancellation_proof",
        "customer_communication", "proof_of_service", "explanation_letter",
        "refund_confirmation", "access_activity_log",
        "refund_cancellation_policy", "term_and_conditions",
    }
    for code, types in REASON_CODE_EVIDENCE.items():
        unknown = set(types) - real_vocabulary
        assert not unknown, f"{code} uses unrecognised evidence types: {unknown}"


def test_every_reason_code_requires_at_least_one_evidence_type():
    for code, types in REASON_CODE_EVIDENCE.items():
        assert types, f"{code} has an empty requirement list"


# --- the generator ---------------------------------------------------------

def test_the_batch_matches_the_canonical_composition(batch):
    _disputes, _evidence, truth = batch
    assert len(truth) == sum(CANONICAL_MIX.values())


def test_the_batch_is_reproducible(batch):
    again_d, again_e, again_truth = generate_disputes(30)
    assert again_truth == batch[2]


def test_the_unmapped_plant_uses_a_code_absent_from_the_rule_table(batch):
    disputes, _evidence, truth = batch
    unmapped_ids = {did for did, code in truth.items()
                    if code == str(DisputeCode.REASON_CODE_UNMAPPED)}
    assert unmapped_ids
    for d in disputes:
        if d.dispute_id in unmapped_ids:
            assert d.reason_code == UNMAPPED_CODE
            assert UNMAPPED_CODE not in REASON_CODE_EVIDENCE


# --- the detector settles every record exactly -------------------------------

def test_every_dispute_matches_the_answer_key_exactly(batch):
    disputes, evidence, truth = batch
    classified = detect_batch(disputes, evidence, now=NOW)
    got = {c.dispute_id: c.code for c in classified}
    wrong = [(k, truth[k], v) for k, v in got.items() if truth[k] != v]
    assert not wrong, f"mismatches: {wrong}"


def test_no_evidence_at_all_is_missing():
    d = Dispute(dispute_id="x", payment_id="p", amount_paise=10_000,
               reason_code="1064", reason_description="Not Received",
               phase="chargeback", status="open", respond_by=NOW + 5 * 86_400)
    result = detect(d, set(), now=NOW)
    assert result.code == str(DisputeCode.EVIDENCE_MISSING)
    assert result.action == str(DisputeAction.GATHER_EVIDENCE)


def test_partial_evidence_is_partial_not_complete():
    d = Dispute(dispute_id="x", payment_id="p", amount_paise=10_000,
               reason_code="1064", reason_description="Not Received",
               phase="chargeback", status="open", respond_by=NOW + 5 * 86_400)
    required = REASON_CODE_EVIDENCE["1064"]
    result = detect(d, {required[0]}, now=NOW)
    assert result.code == str(DisputeCode.EVIDENCE_PARTIAL)
    assert result.action == str(DisputeAction.DRAFT_EVIDENCE_PACK)
    assert result.missing


def test_every_required_type_present_is_complete():
    d = Dispute(dispute_id="x", payment_id="p", amount_paise=10_000,
               reason_code="1064", reason_description="Not Received",
               phase="chargeback", status="open", respond_by=NOW + 5 * 86_400)
    required = REASON_CODE_EVIDENCE["1064"]
    result = detect(d, set(required), now=NOW)
    assert result.code == str(DisputeCode.EVIDENCE_COMPLETE)
    assert not result.missing


def test_an_unmapped_reason_code_is_never_defaulted_to_a_guessed_checklist():
    d = Dispute(dispute_id="x", payment_id="p", amount_paise=10_000,
               reason_code="totally-unknown-code", reason_description="",
               phase="chargeback", status="open", respond_by=NOW + 5 * 86_400)
    result = detect(d, {"shipping_proof"}, now=NOW)
    assert result.code == str(DisputeCode.REASON_CODE_UNMAPPED)
    assert result.action == str(DisputeAction.ESCALATE)
    assert not result.required


def test_extra_evidence_types_outside_the_requirement_list_are_harmless():
    """Evidence entered that isn't required for this reason code should
    not confuse the classifier - it just isn't counted."""
    d = Dispute(dispute_id="x", payment_id="p", amount_paise=10_000,
               reason_code="1064", reason_description="Not Received",
               phase="chargeback", status="open", respond_by=NOW + 5 * 86_400)
    required = REASON_CODE_EVIDENCE["1064"]
    result = detect(d, set(required) | {"proof_of_service"}, now=NOW)
    assert result.code == str(DisputeCode.EVIDENCE_COMPLETE)


# --- days to respond ----------------------------------------------------

def test_days_to_respond_by_is_computed_correctly():
    d = Dispute(dispute_id="x", payment_id="p", amount_paise=10_000,
               reason_code="1064", reason_description="Not Received",
               phase="chargeback", status="open", respond_by=NOW + 3 * 86_400)
    result = detect(d, set(), now=NOW)
    assert result.days_to_respond_by == 3


# --- the guardrail gate -------------------------------------------------------

def test_evidence_missing_is_never_queued_by_itself():
    d = Dispute(dispute_id="x", payment_id="p", amount_paise=10_000,
               reason_code="1064", reason_description="Not Received",
               phase="chargeback", status="open", respond_by=NOW + 10 * 86_400)
    classified = detect(d, set(), now=NOW)
    decision = gate(classified)
    assert not decision.queued_for_human
    assert decision.action == str(DisputeAction.GATHER_EVIDENCE)


def test_an_unmapped_code_is_always_queued():
    d = Dispute(dispute_id="x", payment_id="p", amount_paise=10_000,
               reason_code="unknown", reason_description="",
               phase="chargeback", status="open", respond_by=NOW + 10 * 86_400)
    classified = detect(d, set(), now=NOW)
    decision = gate(classified)
    assert decision.queued_for_human


def test_a_near_deadline_dispute_is_queued_even_with_full_confidence():
    d = Dispute(dispute_id="x", payment_id="p", amount_paise=1_000,
               reason_code="1064", reason_description="Not Received",
               phase="chargeback", status="open", respond_by=NOW + 1 * 86_400)
    classified = detect(d, set(REASON_CODE_EVIDENCE["1064"]), now=NOW)

    class _Verdict:
        confidence = 0.99
        reasoning = "strong case"
        error = None
        invented_figures = []

    decision = gate(classified, _Verdict())
    assert decision.queued_for_human
    assert any("day(s) left" in r for r in decision.reasons)


def test_a_dispute_with_plenty_of_time_and_high_confidence_is_not_queued():
    d = Dispute(dispute_id="x", payment_id="p", amount_paise=1_000,
               reason_code="1064", reason_description="Not Received",
               phase="chargeback", status="open", respond_by=NOW + 10 * 86_400)
    classified = detect(d, set(REASON_CODE_EVIDENCE["1064"]), now=NOW)

    class _Verdict:
        confidence = 0.9
        reasoning = "strong case"
        error = None
        invented_figures = []

    decision = gate(classified, _Verdict())
    assert not decision.queued_for_human


def test_a_large_stake_is_queued_regardless_of_confidence():
    d = Dispute(dispute_id="x", payment_id="p", amount_paise=30_000_00,
               reason_code="1064", reason_description="Not Received",
               phase="chargeback", status="open", respond_by=NOW + 10 * 86_400)
    classified = detect(d, set(REASON_CODE_EVIDENCE["1064"]), now=NOW)

    class _Verdict:
        confidence = 0.95
        reasoning = "strong case"
        error = None
        invented_figures = []

    decision = gate(classified, _Verdict())
    assert decision.queued_for_human


def test_the_action_is_never_softened_by_a_low_confidence_agent():
    d = Dispute(dispute_id="x", payment_id="p", amount_paise=1_000,
               reason_code="1064", reason_description="Not Received",
               phase="chargeback", status="open", respond_by=NOW + 10 * 86_400)
    classified = detect(d, set(REASON_CODE_EVIDENCE["1064"]), now=NOW)

    class _Verdict:
        confidence = 0.1
        reasoning = "not sure"
        error = None
        invented_figures = []

    decision = gate(classified, _Verdict())
    assert decision.action == str(DisputeAction.DRAFT_EVIDENCE_PACK)
    assert decision.queued_for_human


# --- money -------------------------------------------------------------------

def test_all_money_is_integer_paise(batch):
    disputes, _evidence, _truth = batch
    for d in disputes:
        assert isinstance(d.amount_paise, int)


# --- scoring -------------------------------------------------------------

def test_scoring_a_perfect_run_has_full_recall_and_no_false_accusations(batch):
    disputes, evidence, truth = batch
    classified = detect_batch(disputes, evidence, now=NOW)
    from engine.chargeback.scoring import score_classification

    card = score_classification(classified, truth)
    assert card.accuracy == 1.0
    assert card.recall == 1.0
    assert not card.false_accusations
