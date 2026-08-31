"""
Tests for the DRC-01B response drafter. Mirrors the shape of
agent/vendor_documents.py's own tests, if any existed - none of these call
the API.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.gst_filing_documents import drc01b_response  # noqa: E402
from engine.gst_filing.offset import finding_from_88c_check  # noqa: E402


@pytest.fixture
def breach():
    return finding_from_88c_check("2026-04", 80_000_00, 50_000_00)


def test_the_template_document_never_needs_the_model(breach):
    doc = drc01b_response(breach)
    assert doc.written_by == "template"
    assert doc.error is None
    assert "Rule 88C" in doc.body
    assert "2026-04" in doc.body


def test_no_circular_number_is_ever_cited(breach):
    """Unlike agent/vendor_documents.py's Circular 183/193, no comparable
    circular was found for Rule 88C this session - inventing one would be
    a worse failure than citing none (CLAUDE.md section 16)."""
    doc = drc01b_response(breach)
    assert "circular" not in doc.body.lower()


def test_section_74_fraud_is_never_alleged(breach):
    """s.73 (no fraud) is cited, s.74 (fraud/wilful misstatement) never is -
    fraud is only ever mentioned to explicitly disclaim it, not accuse it."""
    doc = drc01b_response(breach)
    assert "s.73" in doc.body or "Section 73" in doc.body
    assert "not under Section 74" in doc.body
    assert "no allegation of fraud" in doc.body.lower()


def test_the_breach_amount_appears_and_is_not_recomputed(breach):
    doc = drc01b_response(breach)
    assert doc.amount == breach.breach_amount
    assert "Rs 20,000.00" in doc.body


def test_a_supplied_case_is_used_verbatim_and_marks_written_by_agent(breach):
    doc = drc01b_response(breach, case="A short paragraph the model wrote.")
    assert doc.written_by == "agent"
    assert "A short paragraph the model wrote." in doc.body


def test_the_real_instruction_is_cited_precisely_as_an_instruction(breach):
    """CBIC Instruction No. 01/2022-GST, found this session, is the real
    authority behind "explain before recovery" - cited by its real name,
    never relabelled as a circular."""
    doc = drc01b_response(breach)
    assert "Instruction No. 01/2022-GST" in doc.body
    assert "7 January 2022" in doc.body
