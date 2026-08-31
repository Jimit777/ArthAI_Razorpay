"""
Tests for the vendor terms agent's output checking.

None of these call the API. What is being tested is the layer that sits
between the model and the merchant: the checks that catch a model answer
which is confident, well-written and wrong.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.vendor_terms_classifier import (VendorTermsJudgment, review,  # noqa: E402
                                           strict_schema, unverified_figures)
from engine.vendor_terms.detector import detect_batch, group_by_supplier  # noqa: E402
from engine.vendor_terms.generator import generate_line_items  # noqa: E402


@pytest.fixture
def group():
    items, _truth, rate_card = generate_line_items(40)
    classified = detect_batch(items, rate_card=rate_card)
    groups = group_by_supplier(classified)
    return next(g for g in groups if g.overbilled)


def _parsed(**overrides) -> VendorTermsJudgment:
    base = dict(confidence=0.9, reasoning="One line billed above contract.")
    base.update(overrides)
    return VendorTermsJudgment(**base)


# --- the model does not get to invent money ---------------------------------

def test_a_figure_we_supplied_is_accepted():
    assert unverified_figures("Rs 2,000 over", "Rs 2,000") == []


def test_a_figure_from_nowhere_is_caught():
    assert unverified_figures("costing Rs 9,99,999", "Rs 131.16") == ["9,99,999"]


def test_small_bare_integers_are_prose_not_money():
    assert unverified_figures("three items, the 2nd invoice", "") == []


def test_an_invented_figure_discards_the_advice(group):
    parsed = _parsed(confidence=0.95,
                     reasoning="This supplier owes you Rs 9,99,999.")
    verdict = review(group, parsed, evidence="Rs 131.16")
    assert verdict.invented_figures
    assert verdict.confidence == 0.0
    assert group.supplier_name in verdict.reasoning


def test_a_clean_answer_keeps_its_own_reasoning(group):
    parsed = _parsed(reasoning="A single small overcharge, likely a slip.")
    verdict = review(group, parsed, evidence=render_evidence(group))
    assert verdict.reasoning == parsed.reasoning
    assert verdict.confidence == parsed.confidence


def render_evidence(group) -> str:
    from agent.vendor_terms_prompt import render

    return render(group)


# --- the schema the API will accept -----------------------------------------

def test_the_schema_forbids_extra_properties():
    assert strict_schema()["additionalProperties"] is False


def test_the_model_is_never_asked_for_a_rupee_amount():
    fields = set(strict_schema()["properties"])
    for suspicious in ("amount", "paise", "rupees", "stake"):
        assert not any(suspicious in f for f in fields), \
            f"the schema lets the model return a figure: {fields}"


# --- a failed call is never a clean verdict ---------------------------------

def test_a_failed_call_falls_back_to_the_arithmetic(group):
    from agent.vendor_terms_classifier import ClaudeVendorTermsAgent

    agent = ClaudeVendorTermsAgent.__new__(ClaudeVendorTermsAgent)
    agent._model = "claude-opus-5"
    verdict = agent._failed(group, "connection refused", 0.0)

    assert verdict.confidence == 0.0
    assert verdict.error
    assert group.supplier_name in verdict.reasoning
