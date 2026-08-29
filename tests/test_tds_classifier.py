"""
Tests for the TDS agent's output checking.

None of these call the API. What is being tested is the layer that sits
between the model and the merchant: the checks that catch a model answer
which is confident, well-written and wrong.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.tds_classifier import (TdsClassification, TdsVerdict,  # noqa: E402
                                  review, strict_schema, unverified_figures)
from engine.tds.detector import detect_batch  # noqa: E402
from engine.tds.generator import generate_batch  # noqa: E402
from engine.tds.taxonomy import ACTION_FOR, TdsAction, TdsCode  # noqa: E402


@pytest.fixture
def variance():
    data, _truth = generate_batch(60)
    return next(v for v in detect_batch(data) if v.needs_agent)


def _parsed(**overrides) -> TdsClassification:
    base = dict(
        exception_code=str(TdsCode.MISSING_CREDIT),
        action=str(TdsAction.CHASE),
        confidence=0.9,
        reasoning="Nothing has shown up on the statement for this payment yet.",
        rule_cited="two-source match - Form 168",
        evidence_used=[],
        supplier_message=None)
    base.update(overrides)
    return TdsClassification(**base)


# --- the model does not get to invent money ---------------------------------

def test_a_figure_we_supplied_is_accepted():
    assert unverified_figures("you were credited Rs 111.36", "Rs 111.36") == []


def test_a_figure_from_nowhere_is_caught():
    assert unverified_figures("credit Rs 9,999.99", "Rs 111.36") == ["9,999.99"]


def test_separators_do_not_hide_a_match():
    assert unverified_figures("Rs 111.36 is short", "Rs 111.36") == []


def test_statute_references_are_not_treated_as_money():
    loose = unverified_figures(
        "under s.393(1) Sl. 8(v), code 1035 applies", "")
    assert loose == []


def test_an_invented_figure_caps_confidence(variance):
    parsed = _parsed(confidence=0.98,
                     reasoning="You should be credited Rs 8,88,888.88 more.")
    verdict = review(variance, parsed, evidence="Rs 111.36")
    assert verdict.invented_figures
    assert verdict.confidence <= 0.4
    assert any("appear in no input" in c for c in verdict.corrections)


# --- the action must match the code -----------------------------------------

def test_an_action_that_contradicts_the_code_is_corrected(variance):
    parsed = _parsed(exception_code=str(TdsCode.CODE_MISMATCH),
                     action=str(TdsAction.CHASE))
    verdict = review(variance, parsed, evidence="")
    assert verdict.action == str(TdsAction.CORRECT_BEFORE_FILING)
    assert any("always means" in c for c in verdict.corrections)


def test_every_code_has_exactly_one_correct_action():
    for code in TdsCode:
        assert code in ACTION_FOR


# --- cited evidence must exist -----------------------------------------------

def test_evidence_the_model_made_up_is_caught(variance):
    parsed = _parsed(evidence_used=["a_signal_that_does_not_exist"])
    verdict = review(variance, parsed, evidence="")
    assert any("not supplied" in c for c in verdict.corrections)
    assert verdict.confidence <= 0.4


def test_naming_a_tool_is_not_phantom_evidence(variance):
    parsed = _parsed(evidence_used=["find_credit_by_payment",
                                    "expected_tds_treatment"])
    verdict = review(variance, parsed, evidence="")
    assert not verdict.corrections
    assert verdict.confidence == 0.9


def test_a_real_signal_kind_is_accepted(variance):
    kind = variance.signals[0].kind
    verdict = review(variance, _parsed(evidence_used=[kind]), evidence="")
    assert not verdict.corrections


# --- the schema the API will accept -----------------------------------------

def test_the_schema_forbids_extra_properties():
    assert strict_schema()["additionalProperties"] is False


def test_the_schema_carries_no_constraint_keywords():
    import json

    text = json.dumps(strict_schema())
    for keyword in ("minimum", "maximum", "minLength", "pattern"):
        assert f'"{keyword}"' not in text


def test_the_model_is_never_asked_for_a_rupee_amount():
    fields = set(strict_schema()["properties"])
    for suspicious in ("amount", "paise", "rupees", "tax", "delta", "total"):
        assert not any(suspicious in f for f in fields), \
            f"the schema lets the model return a figure: {fields}"


# --- confidence --------------------------------------------------------------

def test_confidence_is_clamped_into_range(variance):
    assert review(variance, _parsed(confidence=1.7), evidence="").confidence == 1.0
    assert review(variance, _parsed(confidence=-2.0), evidence="").confidence == 0.0


# --- a failed call is never a clean credit ------------------------------------

def test_a_failed_call_escalates_rather_than_passing(variance):
    from agent.tds_classifier import ClaudeTdsClassifier

    classifier = ClaudeTdsClassifier.__new__(ClaudeTdsClassifier)
    classifier._model = "claude-opus-5"
    verdict = classifier._failed(variance, "connection refused", 0.0)

    assert verdict.exception_code == str(TdsCode.UNEXPLAINED)
    assert verdict.action == str(TdsAction.ESCALATE)
    assert verdict.confidence == 0.0
    assert verdict.error
    assert verdict.exception_code != str(TdsCode.CREDIT_CLEAN)


# --- the tools must not assert something the evidence contradicts ------------

def test_find_credit_by_payment_reports_the_exact_line_when_it_exists():
    import json

    from agent.tds_tools import build_tools

    data, truth = generate_batch(60)
    tools = {t.name: t for t in build_tools(data)}

    clean_pid = next(pid for pid, code in truth.items()
                     if code == str(TdsCode.CREDIT_CLEAN))
    reply = json.loads(tools["find_credit_by_payment"].call(
        {"payment_id": clean_pid}))
    assert reply["found_under_this_id"] == 1


def test_find_credit_by_payment_reports_nothing_for_a_missing_credit():
    import json

    from agent.tds_tools import build_tools

    data, truth = generate_batch(60)
    tools = {t.name: t for t in build_tools(data)}

    missing_pid = next(pid for pid, code in truth.items()
                       if code == str(TdsCode.MISSING_CREDIT))
    reply = json.loads(tools["find_credit_by_payment"].call(
        {"payment_id": missing_pid}))
    assert reply["found_under_this_id"] == 0
