"""
Tests for the ITC agent's output checking.

None of these call the API. What is being tested is the layer that sits between
the model and the merchant: the checks that catch a model answer which is
confident, well-written and wrong.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.gst_classifier import (ITCClassification, ITCVerdict,  # noqa: E402
                                  review, strict_schema, unverified_figures)
from engine.gst.detector import Signal, detect_batch  # noqa: E402
from engine.gst.generator import generate_batch  # noqa: E402
from engine.gst.taxonomy import ACTION_FOR, ITCAction, ITCCode  # noqa: E402


@pytest.fixture
def variance():
    data, _truth = generate_batch(60)
    return next(v for v in detect_batch(data) if v.needs_agent)


def _parsed(**overrides) -> ITCClassification:
    base = dict(
        exception_code=str(ITCCode.AMOUNT_MISMATCH),
        action=str(ITCAction.CHASE_SUPPLIER),
        confidence=0.9,
        reasoning="The supplier reported less tax than your books claim.",
        rule_cited="rule 2 - CGST Act s.16(2)(aa)",
        evidence_used=[],
        supplier_message=None)
    base.update(overrides)
    return ITCClassification(**base)


# --- the model does not get to invent money ------------------------------

def test_a_figure_we_supplied_is_accepted():
    assert unverified_figures("you claimed Rs 5,344.70", "Rs 5,344.70") == []


def test_a_figure_from_nowhere_is_caught():
    assert unverified_figures("claim Rs 9,999.99", "Rs 5,344.70") == ["9,999.99"]


def test_separators_do_not_hide_a_match():
    """"5,344.70" and "5344.70" are the same number and must not be flagged."""
    assert unverified_figures("Rs 5344.70 is short", "Rs 5,344.70") == []


def test_statute_references_are_not_treated_as_money():
    """s.16(4), Rule 88D and "180 days" are citations, not computed figures."""
    loose = unverified_figures(
        "under s.16(4) and Rule 88D, 180 days have passed", "")
    assert loose == []


def test_an_invented_figure_caps_confidence(variance):
    parsed = _parsed(confidence=0.98,
                     reasoning="You may still claim Rs 8,88,888.88 of this.")
    verdict = review(variance, parsed, evidence="Rs 5,344.70")
    assert verdict.invented_figures
    assert verdict.confidence <= 0.4
    assert any("appear in no input" in c for c in verdict.corrections)


# --- the action must match the code --------------------------------------

def test_an_action_that_contradicts_the_code_is_corrected(variance):
    parsed = _parsed(exception_code=str(ITCCode.BLOCKED_CREDIT),
                     action=str(ITCAction.CHASE_SUPPLIER))
    verdict = review(variance, parsed, evidence="")
    assert verdict.action == str(ITCAction.DO_NOT_CLAIM)
    assert any("always means" in c for c in verdict.corrections)


def test_every_code_has_exactly_one_correct_action():
    for code in ITCCode:
        assert code in ACTION_FOR


# --- cited evidence must exist -------------------------------------------

def test_evidence_the_model_made_up_is_caught(variance):
    parsed = _parsed(evidence_used=["a_signal_that_does_not_exist"])
    verdict = review(variance, parsed, evidence="")
    assert any("not supplied" in c for c in verdict.corrections)
    assert verdict.confidence <= 0.4


def test_naming_a_tool_is_not_phantom_evidence(variance):
    """
    The model legitimately reports which tools it used. Counting those as
    invented evidence capped confidence on correct answers when the settlement
    agent first shipped.
    """
    parsed = _parsed(evidence_used=["find_invoice_number",
                                    "supplier_filing_history"])
    verdict = review(variance, parsed, evidence="")
    assert not verdict.corrections
    assert verdict.confidence == 0.9


def test_a_real_signal_kind_is_accepted(variance):
    kind = variance.signals[0].kind
    verdict = review(variance, _parsed(evidence_used=[kind]), evidence="")
    assert not verdict.corrections


# --- the schema the API will accept --------------------------------------

def test_the_schema_forbids_extra_properties():
    assert strict_schema()["additionalProperties"] is False


def test_the_schema_carries_no_constraint_keywords():
    """Strict json_schema mode rejects these, and Pydantic emits them."""
    import json

    text = json.dumps(strict_schema())
    for keyword in ("minimum", "maximum", "minLength", "pattern"):
        assert f'"{keyword}"' not in text


def test_the_model_is_never_asked_for_a_rupee_amount():
    """
    The architectural rule, enforced at the schema. If the model could return a
    number, the number could be wrong, and the product IS accuracy.
    """
    fields = set(strict_schema()["properties"])
    for suspicious in ("amount", "paise", "rupees", "tax", "delta", "total"):
        assert not any(suspicious in f for f in fields), \
            f"the schema lets the model return a figure: {fields}"


# --- confidence ----------------------------------------------------------

def test_confidence_is_clamped_into_range(variance):
    assert review(variance, _parsed(confidence=1.7), evidence="").confidence == 1.0
    assert review(variance, _parsed(confidence=-2.0), evidence="").confidence == 0.0


# --- a failed call is never a clean claim --------------------------------

def test_a_failed_call_escalates_rather_than_passing(variance):
    """
    The worst bug this system could have would be a network error that quietly
    became CLAIM_CLEAN. Silence is not absolution.
    """
    from agent.gst_classifier import ClaudeITCClassifier

    data, _truth = generate_batch(60)
    classifier = ClaudeITCClassifier.__new__(ClaudeITCClassifier)
    classifier._model = "claude-opus-5"
    verdict = classifier._failed(variance, "connection refused", 0.0)

    assert verdict.exception_code == str(ITCCode.UNEXPLAINED)
    assert verdict.action == str(ITCAction.ESCALATE)
    assert verdict.confidence == 0.0
    assert verdict.error
    assert verdict.exception_code != str(ITCCode.CLAIM_CLEAN)


# --- the tools must not assert something the evidence contradicts --------

def test_filing_history_reports_what_the_supplier_filed_not_what_was_booked():
    """
    The first live run caught this. The tool summed the merchant's own invoice
    amounts and called the total "tax they reported", so on a short-reported
    invoice it claimed supplier and books agreed while the 2B line said
    otherwise. The agent noticed the contradiction and lowered its confidence -
    correct behaviour, wrong tool.
    """
    import json

    from agent.gst_tools import build_tools
    from engine.gst.detector import detect_batch
    from engine.gst.generator import generate_batch

    data, truth = generate_batch(60)
    tools = {t.name: t for t in build_tools(data)}

    short = next(v for v in detect_batch(data)
                 if truth.get(v.invoice_id) == str(ITCCode.AMOUNT_MISMATCH))
    reply = json.loads(tools["supplier_filing_history"].call(
        {"gstin": short.supplier_gstin}))

    booked = reply["tax_you_booked"]["paise"]
    reported = reply["tax_they_reported"]["paise"]
    assert reported < booked, \
        "a supplier who short-reported must not show as having reported in full"
