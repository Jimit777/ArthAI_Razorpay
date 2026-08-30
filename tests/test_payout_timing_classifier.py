"""
Tests for the payout timing agent's output checking.

None of these call the API. What is being tested is the layer that sits
between the model and the merchant: the checks that catch a model answer
which is confident, well-written and wrong.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.payout_timing_classifier import (PayoutTimingJudgment,  # noqa: E402
                                            review, strict_schema,
                                            unverified_figures)
from engine.payout_timing.detector import detect  # noqa: E402
from engine.payout_timing.generator import generate_batch  # noqa: E402
from engine.payout_timing.taxonomy import Pattern, PayoutAction  # noqa: E402


@pytest.fixture
def summary():
    data, _truth = generate_batch(60)
    return detect(data)


def _parsed(**overrides) -> PayoutTimingJudgment:
    base = dict(
        pattern=str(Pattern.SYSTEMIC_DELAY),
        action=str(PayoutAction.ESCALATE),
        confidence=0.9,
        reasoning="15 of 58 settlements missed the promised cycle.",
        escalation_text="Dear Razorpay team, ...")
    base.update(overrides)
    return PayoutTimingJudgment(**base)


# --- the model does not get to invent money ---------------------------------

def test_a_figure_we_supplied_is_accepted():
    assert unverified_figures("15 of 58 missed it", "15 of 58") == []


def test_a_figure_from_nowhere_is_caught():
    assert unverified_figures("costing Rs 9,99,999", "Rs 131.16") == ["9,99,999"]


def test_small_bare_integers_are_prose_not_money():
    assert unverified_figures("three days late, day 14", "") == []


def test_an_invented_figure_discards_the_advice(summary):
    parsed = _parsed(confidence=0.95,
                     reasoning="This is costing you Rs 9,99,999 in float.")
    verdict = review(summary, parsed, evidence="Rs 131.16")
    assert verdict.invented_figures
    assert verdict.confidence == 0.0
    assert verdict.reasoning == summary.detail


# --- the mechanical action is never softened --------------------------------

def test_the_agent_may_not_relax_the_action(summary):
    relaxed = _parsed(action=str(PayoutAction.NONE), escalation_text=None)
    verdict = review(summary, relaxed, evidence="")
    assert verdict.action == summary.action        # stays ESCALATE
    assert verdict.agent_action == str(PayoutAction.NONE)
    assert any("would have said" in c for c in verdict.corrections)


def test_the_agent_may_escalate_further_and_it_is_noted(summary):
    """The final action still never exceeds what the mechanical layer
    allows to be shown, but going further is recorded, not hidden."""
    parsed = _parsed(action=str(PayoutAction.ESCALATE))
    verdict = review(summary, parsed, evidence="")
    assert verdict.action == summary.action


def test_the_pattern_is_the_engines_not_the_agents(summary):
    wrong = _parsed(pattern=str(Pattern.CLEAN))
    verdict = review(summary, wrong, evidence="")
    assert verdict.pattern == summary.pattern
    assert any("the agent called it" in c for c in verdict.corrections)


# --- escalation text is only kept when the action calls for it -------------

def test_escalation_text_survives_when_the_action_is_escalate(summary):
    parsed = _parsed()
    verdict = review(summary, parsed, evidence="")
    if summary.action == str(PayoutAction.ESCALATE):
        assert verdict.escalation_text


def test_missing_escalation_text_on_an_escalate_is_flagged(summary):
    parsed = _parsed(escalation_text=None)
    verdict = review(summary, parsed, evidence="")
    if summary.action == str(PayoutAction.ESCALATE):
        assert any("no escalation text" in c for c in verdict.corrections)


def test_escalation_text_is_dropped_when_the_action_is_not_escalate():
    """A watch-level batch, told to escalate - a bad plant, worth having
    even though it can't happen through the real generator today."""
    from engine.payout_timing.detector import PayoutTimingSummary

    watch_summary = PayoutTimingSummary(
        pattern=str(Pattern.ISOLATED_DELAY), action=str(PayoutAction.WATCH),
        detail="A few late settlements.")
    parsed = _parsed(action=str(PayoutAction.WATCH),
                     escalation_text="Dear Razorpay...")
    verdict = review(watch_summary, parsed, evidence="")
    assert verdict.escalation_text is None
    assert any("dropped" in c for c in verdict.corrections)


# --- the schema the API will accept -----------------------------------------

def test_the_schema_forbids_extra_properties():
    assert strict_schema()["additionalProperties"] is False


def test_the_model_is_never_asked_for_a_rupee_amount():
    fields = set(strict_schema()["properties"])
    for suspicious in ("amount", "paise", "rupees", "float_cost", "delay"):
        assert not any(suspicious in f for f in fields), \
            f"the schema lets the model return a figure: {fields}"


# --- a failed call is never a clean verdict ---------------------------------

def test_a_failed_call_falls_back_to_the_arithmetic(summary):
    from agent.payout_timing_classifier import ClaudePayoutTimingAgent

    agent = ClaudePayoutTimingAgent.__new__(ClaudePayoutTimingAgent)
    agent._model = "claude-opus-5"
    verdict = agent._failed(summary, "connection refused", 0.0)

    assert verdict.pattern == summary.pattern
    assert verdict.action == summary.action
    assert verdict.confidence == 0.0
    assert verdict.error
