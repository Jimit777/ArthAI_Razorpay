"""
Tests for the TDS generator's answer key and the detector that scores
against it.

The generator is simultaneously the test harness, the demo data and the
scoreboard (CLAUDE.md section 7). Several of these check findability rather
than behaviour - if a planted error sits under the tolerance band or on the
wrong side of the regime-change date, the answer key itself is wrong, not
the detector.
"""

import sys
from collections import Counter
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from engine.tds import rules  # noqa: E402
from engine.tds.detector import JUDGMENT_CODES, detect_batch  # noqa: E402
from engine.tds.generator import (CANONICAL_MIX, CLEAN_RECIPES,  # noqa: E402
                                  DECOY_RECIPES, RECIPE_TRUTH, generate_batch)
from engine.tds.taxonomy import ACTION_FOR, NO_ACTION, TdsAction, TdsCode  # noqa: E402


@pytest.fixture(scope="module")
def batch():
    return generate_batch(60)


# --- the taxonomy ----------------------------------------------------------

def test_every_code_says_what_the_merchant_must_do():
    assert set(ACTION_FOR) == set(TdsCode)


def test_some_codes_mean_nothing_to_do():
    assert NO_ACTION
    assert all(ACTION_FOR[c] is TdsAction.NONE for c in NO_ACTION)


def test_judgment_codes_are_a_subset_of_the_taxonomy():
    assert JUDGMENT_CODES <= {str(c) for c in TdsCode}


# --- the generator -----------------------------------------------------------

def test_the_batch_and_the_answer_key_cover_the_same_records(batch):
    data, truth = batch
    found = {v.payment_id for v in detect_batch(data)}
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
    _data, truth = generate_batch(24)
    codes = set(truth.values())
    for recipe in CANONICAL_MIX:
        if recipe in CLEAN_RECIPES:
            continue
        assert str(RECIPE_TRUTH[recipe]) in codes


def test_the_batch_spans_both_sides_of_the_regime_change(batch):
    """
    The demo's whole point. A batch that only ever tests one era would still
    pass every rule check while proving nothing about the transition.
    """
    data, _truth = batch
    assert any(d.deducted_at < rules.REGIME_CHANGE for d in data.deductions)
    assert any(d.deducted_at >= rules.REGIME_CHANGE for d in data.deductions)


def test_clean_records_exist_on_both_sides_of_the_change(batch):
    data, truth = batch
    clean_dates = [d.deducted_at for d in data.deductions
                   if truth[d.payment_id] == str(TdsCode.CREDIT_CLEAN)]
    assert any(d < rules.REGIME_CHANGE for d in clean_dates)
    assert any(d >= rules.REGIME_CHANGE for d in clean_dates)


# --- the calculator ----------------------------------------------------------

def test_everything_the_calculator_settles_it_settles_correctly(batch):
    data, truth = batch
    settled = [v for v in detect_batch(data) if not v.needs_agent]
    wrong = [(v.payment_id, truth[v.payment_id], v.exception_code)
             for v in settled if truth[v.payment_id] != v.exception_code]
    assert not wrong, f"calculator got these wrong: {wrong}"


def test_the_calculator_settles_most_of_the_batch(batch):
    data, _truth = batch
    variances = detect_batch(data)
    settled = [v for v in variances if not v.needs_agent]
    assert len(settled) / len(variances) > 0.7


def test_a_settled_record_carries_its_reasoning_and_its_citation(batch):
    data, _truth = batch
    for variance in detect_batch(data):
        if variance.needs_agent:
            continue
        assert variance.reasoning
        assert variance.rule_cited
        assert variance.decided_by == "calculator"


# --- what goes to the agent, and why ----------------------------------------

def test_only_judgment_records_reach_the_agent(batch):
    data, _truth = batch
    for variance in detect_batch(data):
        if variance.needs_agent:
            kinds = {s.candidate_code for s in variance.signals}
            assert kinds & JUDGMENT_CODES, \
                f"{variance.payment_id} went to the agent with nothing to weigh"


def test_the_agent_is_asked_about_something(batch):
    data, _truth = batch
    assert any(v.needs_agent for v in detect_batch(data))


# --- precedence and the regime-change findings ------------------------------

def test_a_stale_code_after_the_change_is_caught():
    data, truth = generate_batch(60)
    mismatches = [v for v in detect_batch(data)
                  if v.exception_code == str(TdsCode.CODE_MISMATCH)]
    assert mismatches
    for v in mismatches:
        assert v.credited_code != v.expected_code or v.credited_form != v.expected_form


def test_a_rate_mismatch_outranks_a_matching_code():
    """
    Code correctness is checked before amount - a statement whose code is
    already wrong is not worth calling clean just because a stray amount
    happened to line up.
    """
    data, _truth = generate_batch(60)
    for v in detect_batch(data):
        if v.exception_code == str(TdsCode.RATE_MISMATCH):
            assert v.credited_code == v.expected_code
            assert v.credited_form == v.expected_form


def test_a_planted_rate_mismatch_is_bigger_than_the_tolerance():
    data, truth = generate_batch(60)
    for v in detect_batch(data):
        if truth[v.payment_id] == str(TdsCode.RATE_MISMATCH):
            assert abs(v.delta) > v.tolerance


def test_missing_credit_has_nothing_on_the_statement():
    data, truth = generate_batch(60)
    for v in detect_batch(data):
        if truth[v.payment_id] == str(TdsCode.MISSING_CREDIT):
            assert not v.has_credit


# --- decoys ------------------------------------------------------------------

def test_rounding_differences_are_dismissed_not_flagged(batch):
    data, truth = batch
    rounding = [v for v in detect_batch(data)
                if truth[v.payment_id] == str(TdsCode.ROUNDING)]
    assert rounding
    for variance in rounding:
        assert variance.exception_code == str(TdsCode.ROUNDING)
        assert ACTION_FOR[TdsCode(variance.exception_code)] is TdsAction.NONE


# --- money ---------------------------------------------------------------

def test_all_money_is_integer_paise(batch):
    data, _truth = batch
    for d in data.deductions:
        assert isinstance(d.amount, int)
        assert isinstance(d.gross_amount, int)
    for c in data.credits:
        assert isinstance(c.amount, int)
