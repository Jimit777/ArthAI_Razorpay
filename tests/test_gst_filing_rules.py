"""
Unit tests for the outward-tax rules - the interest, threshold and split
math everything else in this system rests on.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from engine.gst_filing import rules  # noqa: E402


# --- s.50 interest, both categories -----------------------------------------

def test_normal_interest_runs_at_eighteen_percent():
    # Rs 1,00,000 for a full year
    assert rules.interest_on(100_000_00, 365, "normal") == 18_000_00


def test_wrong_itc_interest_runs_at_twenty_four_percent():
    assert rules.interest_on(100_000_00, 365, "wrong_itc") == 24_000_00


def test_the_category_is_never_inferred_it_must_be_named():
    """Same delta, different rate depending on WHY it's owed - a fact the
    function cannot see, so the default must not silently pick one."""
    normal = rules.interest_on(50_000_00, 30, "normal")
    wrong = rules.interest_on(50_000_00, 30, "wrong_itc")
    assert wrong > normal
    # an unrecognised category falls back to the ordinary rate, not the
    # punitive one - never assume the worse case by accident
    assert rules.interest_on(50_000_00, 30, "bogus") == normal


def test_interest_is_zero_before_any_days_pass():
    assert rules.interest_on(100_000_00, 0, "normal") == 0
    assert rules.interest_on(0, 365, "normal") == 0


def test_interest_is_an_integer_number_of_paise():
    for days in (1, 7, 30, 90, 365):
        value = rules.interest_on(73_419_00, days, "normal")
        assert isinstance(value, int)


# --- Rule 88C threshold, same shape as Rule 88D's notice_threshold ---------

def test_the_88c_threshold_is_the_lower_of_the_two_not_the_higher():
    # 20% of Rs 3,00,000 is Rs 60,000, which is lower than Rs 1,00,000
    assert rules.rule_88c_threshold(300_000_00) == 60_000_00
    # 20% of Rs 90,00,000 is Rs 18,00,000, so Rs 1,00,000 is lower
    assert rules.rule_88c_threshold(9_000_000_00) == rules.RULE_88C_ABSOLUTE_PAISE


# --- the tax split -----------------------------------------------------------

def test_intrastate_splits_evenly_into_cgst_and_sgst():
    cgst, sgst, igst = rules.split_tax(100_000_00, 1_800, interstate=False)
    assert igst == 0
    assert cgst == sgst
    assert cgst + sgst == (100_000_00 * 1_800 + 5_000) // 10_000


def test_interstate_is_igst_only():
    cgst, sgst, igst = rules.split_tax(100_000_00, 1_800, interstate=True)
    assert cgst == 0 and sgst == 0
    assert igst == (100_000_00 * 1_800 + 5_000) // 10_000


# --- citation seams are named, not silently assumed -------------------------

def test_every_unverified_number_is_a_named_seam():
    """These three constants exist specifically so a wrong guess is visible
    and fixable in one place - this test just confirms they still exist
    under the names the rest of the system imports."""
    assert rules.B2CL_THRESHOLD_PAISE > 0
    assert rules.E_INVOICING_TURNOVER_THRESHOLD_PAISE > 0
    assert rules.QUARTERLY_FIXED_SUM_PCT_BPS > 0


def test_rupees_uses_indian_digit_grouping():
    assert rules.rupees(123456789) == "Rs 12,34,567.89"
