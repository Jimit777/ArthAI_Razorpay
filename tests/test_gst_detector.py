"""
Tests for the generator's answer key and the detector that scores against it.

The generator is simultaneously the test harness, the demo data and the
scoreboard (CLAUDE.md section 7). If it plants an error the detector cannot
find, the accuracy number is measuring the generator rather than the system -
so several of these check findability rather than behaviour.
"""

import sys
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from engine.gst import rules  # noqa: E402
from engine.gst.detector import JUDGMENT_CODES, detect_batch  # noqa: E402
from engine.gst.generator import (CANONICAL_MIX, CLEAN_RECIPES,  # noqa: E402
                                  DECOY_RECIPES, RECIPE_TRUTH, generate_batch)
from engine.gst.taxonomy import (ACTION_FOR, AT_RISK, ITCAction,  # noqa: E402
                                 ITCCode, NO_ACTION, OVERCLAIMED)


@pytest.fixture(scope="module")
def batch():
    return generate_batch(60)


# --- the taxonomy --------------------------------------------------------

def test_every_code_says_what_the_merchant_must_do():
    assert set(ACTION_FOR) == set(ITCCode)


def test_a_code_cannot_be_both_at_risk_and_overclaimed():
    assert not (AT_RISK & OVERCLAIMED)


def test_some_codes_mean_the_claim_stands():
    """
    A tool that finds something wrong with every invoice is a tool nobody opens
    twice. Same reasoning as the settlement taxonomy's three no-action codes.
    """
    assert NO_ACTION
    assert all(ACTION_FOR[c] is ITCAction.NONE for c in NO_ACTION)


def test_the_overclaimed_codes_all_stop_a_claim():
    for code in OVERCLAIMED:
        assert ACTION_FOR[code] in (ITCAction.DO_NOT_CLAIM, ITCAction.REVERSE)


# --- the generator -------------------------------------------------------

def test_the_batch_and_the_answer_key_cover_the_same_records(batch):
    data, truth = batch
    found = {v.invoice_id for v in detect_batch(data)}
    assert found == set(truth), "a record with no answer key scores as wrong"


def test_the_answer_key_contains_every_anomaly_it_promised(batch):
    _data, truth = batch
    counts = Counter(truth.values())
    for recipe, expected in CANONICAL_MIX.items():
        if recipe in CLEAN_RECIPES:
            continue
        code = str(RECIPE_TRUTH[recipe])
        assert counts[code] >= 1, f"{recipe} planted nothing findable"


def test_the_batch_is_reproducible(batch):
    again, again_truth = generate_batch(60)
    assert again_truth == batch[1]


def test_a_smaller_batch_still_exercises_every_rule():
    _data, truth = generate_batch(25)
    codes = set(truth.values())
    for recipe in CANONICAL_MIX:
        if recipe in CLEAN_RECIPES:
            continue
        assert str(RECIPE_TRUTH[recipe]) in codes


# --- the calculator ------------------------------------------------------

def test_everything_the_calculator_settles_it_settles_correctly(batch):
    """
    The core claim of the architecture: where the law leaves no room, the
    answer is arithmetic and arithmetic does not have an accuracy rate.
    """
    data, truth = batch
    settled = [v for v in detect_batch(data) if not v.needs_agent]
    wrong = [(v.invoice_id, truth[v.invoice_id], v.exception_code)
             for v in settled if truth[v.invoice_id] != v.exception_code]
    assert not wrong, f"calculator got these wrong: {wrong}"


def test_the_calculator_settles_most_of_the_batch(batch):
    data, _truth = batch
    variances = detect_batch(data)
    settled = [v for v in variances if not v.needs_agent]
    assert len(settled) / len(variances) > 0.8


def test_a_settled_record_carries_its_reasoning_and_its_citation(batch):
    data, _truth = batch
    for variance in detect_batch(data):
        if variance.needs_agent:
            continue
        assert variance.reasoning
        assert variance.rule_cited
        assert variance.decided_by == "calculator"


# --- what goes to the agent, and why -------------------------------------

def test_only_judgment_records_reach_the_agent(batch):
    data, _truth = batch
    for variance in detect_batch(data):
        if variance.needs_agent:
            kinds = {s.candidate_code for s in variance.signals}
            assert kinds & JUDGMENT_CODES, \
                f"{variance.invoice_id} went to the agent with nothing to weigh"


def test_the_agent_is_asked_about_something(batch):
    """A pipeline that never consults the agent has no agent in it."""
    data, _truth = batch
    assert any(v.needs_agent for v in detect_batch(data))


# --- precedence ----------------------------------------------------------

def test_a_blocked_invoice_is_not_reported_clean_however_well_it_was_filed():
    """
    The error class that turns a helpful tool into a Rule 88D notice. A
    perfectly matched invoice for restaurant catering is still not claimable.
    """
    data, _truth = generate_batch(60)
    blocked = [v for v in detect_batch(data)
               if v.exception_code == str(ITCCode.BLOCKED_CREDIT)]
    assert blocked
    for variance in blocked:
        assert variance.in_2b, "this test is only meaningful on a filed invoice"
        assert variance.exception_code != str(ITCCode.CLAIM_CLEAN)


def test_a_time_barred_invoice_outranks_a_clean_match():
    data, _truth = generate_batch(60)
    barred = [v for v in detect_batch(data)
              if v.exception_code == str(ITCCode.TIME_BARRED)]
    assert barred
    for variance in barred:
        assert variance.days_to_deadline < 0


def test_only_the_second_booking_of_a_pair_is_the_duplicate():
    """
    Calling both copies duplicates would tell the merchant to drop credit they
    are entitled to. A reconciliation reports the later of a pair.
    """
    data, truth = generate_batch(60)
    dupes = [k for k, v in truth.items() if v == str(ITCCode.DUPLICATE_CLAIM)]
    assert dupes
    for invoice_id in dupes:
        assert invoice_id.endswith("b"), "the original should not be flagged"


# --- decoys --------------------------------------------------------------

def test_rounding_differences_are_dismissed_not_flagged(batch):
    data, truth = batch
    rounding = [v for v in detect_batch(data)
                if truth[v.invoice_id] == str(ITCCode.ROUNDING)]
    assert rounding
    for variance in rounding:
        assert variance.exception_code == str(ITCCode.ROUNDING)
        assert ACTION_FOR[ITCCode(variance.exception_code)] is ITCAction.NONE


def test_a_planted_amount_mismatch_is_bigger_than_the_tolerance(batch):
    """
    Findability. If a planted mismatch sits under the tolerance band, the
    correct answer is ROUNDING and the answer key is the thing that is wrong.
    """
    data, truth = batch
    for variance in detect_batch(data):
        if truth[variance.invoice_id] == str(ITCCode.AMOUNT_MISMATCH):
            assert abs(variance.delta) > variance.tolerance


# --- money ---------------------------------------------------------------

def test_all_money_is_integer_paise(batch):
    data, _truth = batch
    for variance in detect_batch(data):
        for value in (variance.claimed_tax, variance.available_tax,
                      variance.delta, variance.tolerance):
            assert isinstance(value, int)


def test_every_signal_quotes_its_source(batch):
    data, _truth = batch
    for variance in detect_batch(data):
        for signal in variance.signals:
            assert signal.source, f"{signal.kind} argues from nothing"
            assert signal.detail
