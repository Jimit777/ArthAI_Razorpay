"""
Tests for the watch agent's decision layer.

None of these call the API. What is tested is the part between the model and
the merchant - the checks that catch an answer which is fluent, confident and
internally contradictory.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.watch_agent import (Raised, WatchJudgment, render_change,  # noqa: E402
                               review, strict_schema, unverified_figures)
from engine.gst.watch import STOPPED_FILING, Change  # noqa: E402


@pytest.fixture
def change():
    return Change(
        kind=STOPPED_FILING, gstin="24RWIZN6453L6ZT",
        name="Deepak Packaging",
        detail=("Deepak Packaging filed reliably up to 2026-05 and has filed "
                "nothing for 2 periods since. Rs 75,600.00 of your credit "
                "depends on them."),
        exposed_paise=75_600_00, was="last filed 2026-05",
        now="silent 2 periods", days_to_deadline=490)


def _judgment(**overrides) -> WatchJudgment:
    base = dict(raise_it=True, urgency="this_month", action="chase_supplier",
                headline="Deepak Packaging has gone quiet",
                reasoning="They have filed nothing for 2 periods.",
                supplier_message=None)
    base.update(overrides)
    return WatchJudgment(**base)


# --- the agent may never produce a number --------------------------------

def test_the_schema_has_nowhere_to_put_a_figure():
    fields = set(strict_schema()["properties"])
    for suspicious in ("amount", "paise", "rupee", "total", "exposed", "sum"):
        assert not any(suspicious in f for f in fields), fields


def test_a_figure_we_supplied_is_accepted(change):
    verdict = review(change, _judgment(
        reasoning="Rs 75,600.00 of credit is unsupported."),
        render_change(change))
    assert not verdict.invented_figures


def test_a_figure_from_nowhere_is_caught(change):
    verdict = review(change, _judgment(
        reasoning="You will lose Rs 9,99,999.00 if they never file."),
        render_change(change))
    assert verdict.invented_figures
    assert any("appear in no input" in c for c in verdict.corrections)


def test_a_made_up_figure_in_the_supplier_message_is_caught_too(change):
    """The message is the part that gets SENT. A wrong number there is worse
    than a wrong number in reasoning nobody forwards."""
    verdict = review(change, _judgment(
        supplier_message="Please file. Rs 4,44,444.00 is outstanding."),
        render_change(change))
    assert verdict.invented_figures


def test_statute_references_are_not_mistaken_for_money(change):
    assert unverified_figures("under s.16(4), 180 days, Rule 88D", "") == []


# --- a decision may not contradict itself --------------------------------

def test_choosing_not_to_raise_cannot_carry_an_urgency(change):
    verdict = review(change, _judgment(raise_it=False, urgency="now"),
                     render_change(change))
    assert verdict.urgency == "no_action"
    assert any("chose not to raise" in c for c in verdict.corrections)


def test_choosing_not_to_raise_cannot_carry_an_action(change):
    verdict = review(change,
                     _judgment(raise_it=False, action="chase_supplier"),
                     render_change(change))
    assert verdict.action == "nothing"


def test_raising_something_with_no_action_is_flagged(change):
    verdict = review(change, _judgment(raise_it=True, action="nothing"),
                     render_change(change))
    assert any("no action" in c for c in verdict.corrections)


def test_a_clean_decision_needs_no_correcting(change):
    verdict = review(change, _judgment(), render_change(change))
    assert not verdict.corrections


def test_staying_quiet_is_a_valid_clean_decision(change):
    verdict = review(change,
                     _judgment(raise_it=False, urgency="no_action",
                               action="nothing"),
                     render_change(change))
    assert not verdict.corrections
    assert not verdict.raise_it


# --- a broken watch must not look like a quiet one -----------------------

def test_a_failed_call_raises_rather_than_going_silent(change):
    """
    The opposite default from the classifier, deliberately. Silence is what a
    broken watch produces naturally, and it is indistinguishable from a watch
    that is working and has nothing to say. So a failure speaks up.
    """
    from agent.watch_agent import ClaudeWatchAgent

    agent = ClaudeWatchAgent.__new__(ClaudeWatchAgent)
    agent._model = "claude-opus-5"
    verdict = agent._failed(change, "connection refused", 0.0)

    assert verdict.raise_it is True
    assert verdict.error
    assert "could not judge" in verdict.reasoning
    assert change.detail in verdict.reasoning


# --- the evidence carries its own numbers --------------------------------

def test_the_change_is_rendered_with_every_figure_precomputed(change):
    text = render_change(change)
    assert "Rs 75,600.00" in text
    assert "490 days" in text
    assert "Deepak Packaging" in text


def test_a_passed_deadline_is_stated_as_passed():
    text = render_change(Change(
        kind=STOPPED_FILING, gstin="X", name="Late Ltd", detail="d",
        days_to_deadline=-12))
    assert "ALREADY PASSED" in text
