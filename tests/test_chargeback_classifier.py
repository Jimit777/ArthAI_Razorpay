"""
Tests for the chargeback agent's output checking.

None of these call the API. What is being tested is the layer that sits
between the model and the merchant: the checks that catch a model answer
which is confident, well-written and wrong.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.chargeback_classifier import (ChargebackJudgment, review,  # noqa: E402
                                         strict_schema, unverified_figures)
from engine.chargeback.detector import detect_batch  # noqa: E402
from engine.chargeback.generator import AS_OF, generate_disputes  # noqa: E402
from engine.chargeback.taxonomy import DisputeCode  # noqa: E402


@pytest.fixture
def classified():
    disputes, evidence, _truth = generate_disputes(30)
    results = detect_batch(disputes, evidence, now=int(AS_OF.timestamp()))
    return next(c for c in results if c.code == str(DisputeCode.EVIDENCE_COMPLETE))


def _parsed(**overrides) -> ChargebackJudgment:
    base = dict(confidence=0.9, reasoning="Delivery is well documented.",
               summary="Delivery proof and customer communication on file.")
    base.update(overrides)
    return ChargebackJudgment(**base)


# --- the model does not get to invent money ---------------------------------

def test_a_figure_we_supplied_is_accepted():
    assert unverified_figures("Rs 2,000 in dispute", "Rs 2,000") == []


def test_a_figure_from_nowhere_is_caught():
    assert unverified_figures("costing Rs 9,99,999", "Rs 131.16") == ["9,99,999"]


def test_small_bare_integers_are_prose_not_money():
    assert unverified_figures("two items, day 3", "") == []


def test_an_invented_figure_discards_the_advice(classified):
    parsed = _parsed(confidence=0.95,
                     reasoning="This customer owes Rs 9,99,999 extra.")
    verdict = review(classified, parsed, evidence="Rs 131.16")
    assert verdict.invented_figures
    assert verdict.confidence == 0.0
    assert verdict.summary == ""
    assert classified.reason_code in verdict.reasoning


def test_a_clean_answer_keeps_its_own_reasoning_and_summary(classified):
    from agent.chargeback_prompt import render

    evidence_detail = {t: "some detail" for t in classified.present}
    parsed = _parsed()
    verdict = review(classified, parsed, evidence=render(classified, evidence_detail))
    assert verdict.reasoning == parsed.reasoning
    assert verdict.summary == parsed.summary
    assert verdict.confidence == parsed.confidence


# --- the real API's own summary limit is enforced ----------------------------

def test_a_summary_over_the_real_limit_is_truncated_and_noted(classified):
    long_summary = "x" * 1_200
    parsed = _parsed(summary=long_summary)
    verdict = review(classified, parsed, evidence="")
    assert len(verdict.summary) == 1_000
    assert any("1000-character" in c for c in verdict.corrections)


# --- the schema the API will accept -----------------------------------------

def test_the_schema_forbids_extra_properties():
    assert strict_schema()["additionalProperties"] is False


def test_the_model_is_never_asked_for_a_rupee_amount():
    fields = set(strict_schema()["properties"])
    for suspicious in ("amount", "paise", "rupees", "stake"):
        assert not any(suspicious in f for f in fields), \
            f"the schema lets the model return a figure: {fields}"


# --- a failed call is never a clean verdict ---------------------------------

def test_a_failed_call_falls_back_to_the_arithmetic(classified):
    from agent.chargeback_classifier import ClaudeChargebackAgent

    agent = ClaudeChargebackAgent.__new__(ClaudeChargebackAgent)
    agent._model = "claude-opus-5"
    verdict = agent._failed(classified, "connection refused", 0.0)

    assert verdict.confidence == 0.0
    assert verdict.error
    assert verdict.summary == ""
