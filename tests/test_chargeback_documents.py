"""
Tests for the explanation-letter drafter. Mirrors the shape of
tests/test_vendor_terms_documents.py - none of these call the API.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.chargeback_documents import explanation_letter  # noqa: E402


@pytest.fixture
def dispute():
    return {
        "dispute_id": "disp_demo_0001", "payment_id": "pay_demo_0001",
        "reason_code": "1064",
        "reason_description": "Goods/Services Not Received",
        "amount_paise": 8_500_00,
        "required": ["shipping_proof", "customer_communication",
                    "term_and_conditions"],
        "present": ["shipping_proof", "customer_communication"],
        "missing": ["term_and_conditions"],
        "evidence_detail": {
            "shipping_proof": "Delhivery DL4471829, delivered 14 Aug, signed",
            "customer_communication": "WhatsApp: 'got it, thanks!' 15 Aug",
        },
    }


def test_the_template_document_never_needs_the_model(dispute):
    doc = explanation_letter(dispute)
    assert doc.written_by == "template"
    assert doc.error is None
    assert "disp_demo_0001" in doc.body
    assert "Delhivery DL4471829" in doc.body


def test_the_amount_is_not_recomputed(dispute):
    doc = explanation_letter(dispute)
    assert doc.amount == 8_500_00
    assert "Rs 8,500.00" in doc.body


def test_present_evidence_is_marked_on_and_missing_evidence_is_named(dispute):
    doc = explanation_letter(dispute)
    assert "[X] Proof of shipment/delivery" in doc.body
    assert "[ ] Terms and conditions" in doc.body
    assert "not on file" in doc.body


def test_a_gap_note_only_appears_when_something_is_missing(dispute):
    doc = explanation_letter(dispute)
    assert "not available at the time" in doc.body

    complete = {**dispute, "missing": [],
               "required": ["shipping_proof", "customer_communication"]}
    complete_doc = explanation_letter(complete)
    assert "not available at the time" not in complete_doc.body


def test_a_supplied_case_is_used_verbatim_and_marks_written_by_agent(dispute):
    doc = explanation_letter(dispute, case="A short paragraph the model wrote.")
    assert doc.written_by == "agent"
    assert "A short paragraph the model wrote." in doc.body


def test_missing_evidence_is_never_marked_as_present(dispute):
    """Regression-shaped: the checklist must never show a MISSING type as
    [X] just because the letter has a gap-note fallback elsewhere."""
    doc = explanation_letter(dispute)
    body_lines = doc.body.splitlines()
    tc_line = next(l for l in body_lines if "Terms and conditions" in l)
    assert "[X]" not in tc_line
