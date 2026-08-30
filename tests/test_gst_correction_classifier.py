"""
Tests for the GST correction agent's output checking.

None of these call the API. What is being tested is the layer that sits
between the model and the merchant: the checks that catch a model answer
which is confident, well-written and wrong. Mirrors
tests/test_payout_timing_classifier.py exactly.
"""

import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.gst_correction_classifier import (GSTCorrectionJudgment,  # noqa: E402
                                             PeriodPriority, review,
                                             strict_schema,
                                             unverified_figures)
from engine.gst_filing.generator import generate_cycles  # noqa: E402
from engine.gst_filing.timing import detect_cycles  # noqa: E402

TODAY = date(2026, 8, 24)


@pytest.fixture
def open_findings():
    cycles, _truth = generate_cycles("2026-08", 31_776_878)
    findings = detect_cycles(cycles, today=TODAY)
    return [f for f in findings if f.exception_code == "CORRECTABLE_VIA_1A"]


def _parsed(periods, overall="Filed the largest gap first.") -> GSTCorrectionJudgment:
    return GSTCorrectionJudgment(
        periods=[PeriodPriority(**p) for p in periods],
        overall_reasoning=overall)


# --- the model does not get to invent money ---------------------------------

def test_a_figure_we_supplied_is_accepted():
    assert unverified_figures("Rs 12,000.00 gap", "Rs 12,000.00") == []


def test_a_figure_from_nowhere_is_caught():
    assert unverified_figures("costing Rs 9,99,999", "Rs 12,000.00") == ["9,99,999"]


def test_small_bare_integers_are_prose_not_money():
    assert unverified_figures("two periods, the 11th", "") == []


def test_an_invented_figure_zeroes_that_periods_confidence(open_findings):
    parsed = _parsed([{
        "period": open_findings[0].period, "priority": "file_first",
        "reasoning": "This one is worth Rs 9,99,999 - file it first."}])
    verdict = review(open_findings, parsed, evidence="Rs 12,000.00")
    v = verdict.periods[open_findings[0].period]
    assert v.invented_figures
    assert v.confidence == 0.0


def test_a_clean_reasoning_gets_normal_confidence(open_findings):
    parsed = _parsed([{
        "period": open_findings[0].period, "priority": "file_first",
        "reasoning": "The largest gap in this run - file it first."}])
    verdict = review(open_findings, parsed, evidence="")
    v = verdict.periods[open_findings[0].period]
    assert not v.invented_figures
    assert v.confidence > 0


# --- every period must be accounted for --------------------------------

def test_a_missing_period_is_flagged_and_falls_back(open_findings):
    parsed = _parsed([])                     # said nothing about it
    verdict = review(open_findings, parsed, evidence="")
    assert any("no priority given" in c for c in verdict.corrections)
    v = verdict.periods[open_findings[0].period]
    assert v.error
    assert v.confidence == 0.0


def test_a_priority_for_an_unknown_period_is_flagged(open_findings):
    parsed = _parsed([
        {"period": open_findings[0].period, "priority": "file_first",
         "reasoning": "ok"},
        {"period": "1999-01", "priority": "file_first", "reasoning": "ok"},
    ])
    verdict = review(open_findings, parsed, evidence="")
    assert any("not in this run" in c for c in verdict.corrections)
    assert "1999-01" not in verdict.periods


# --- priority is never a reason to skip filing ---------------------------

def test_priority_field_only_accepts_the_three_ordering_values():
    with pytest.raises(Exception):
        PeriodPriority(period="2026-08", priority="skip", reasoning="no")


def test_schema_has_no_lever_to_soften_the_action():
    schema = strict_schema()
    period_props = schema["$defs"]["PeriodPriority"]["properties"]
    assert "action" not in period_props
    assert set(period_props["priority"]["enum"]) == {
        "file_first", "file_next", "low_priority"}
