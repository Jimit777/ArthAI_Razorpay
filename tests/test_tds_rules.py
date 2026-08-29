"""
Unit tests for the TDS credit rules - the date/rate/code/form table that
everything else in this agent is built on.

The whole pitch for this agent rests on getting the 1 April 2026 boundary
exactly right (CLAUDE.md section 15), so that boundary gets tested on both
sides and on the day itself.
"""

import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from engine.tds import rules  # noqa: E402


def test_the_day_before_the_change_is_the_old_regime():
    d = date(2026, 3, 31)
    assert rules.expected_rate_bps(d) == 100
    assert rules.expected_section_code(d) == "194O"
    assert rules.expected_form(d) == "Form 26AS"


def test_the_day_of_the_change_is_already_the_new_regime():
    d = date(2026, 4, 1)
    assert rules.expected_rate_bps(d) == 10
    assert rules.expected_section_code(d) == "1035"
    assert rules.expected_form(d) == "Form 168"


def test_well_after_the_change_stays_on_the_new_regime():
    d = date(2026, 12, 1)
    assert rules.expected_rate_bps(d) == 10
    assert rules.expected_section_code(d) == "1035"
    assert rules.expected_form(d) == "Form 168"


def test_well_before_the_change_stays_on_the_old_regime():
    d = date(2025, 1, 1)
    assert rules.expected_rate_bps(d) == 100
    assert rules.expected_section_code(d) == "194O"
    assert rules.expected_form(d) == "Form 26AS"


def test_the_rate_cut_is_a_ten_x_drop_not_a_rounding_change():
    """
    The whole point of RATE_MISMATCH: 1% to 0.1% is not a nudge, it is a
    factor of ten. Applying the wrong one is always findable, never a
    tolerance-band question.
    """
    assert rules.OLD_RATE_BPS == 100
    assert rules.NEW_RATE_BPS == 10
    assert rules.OLD_RATE_BPS == rules.NEW_RATE_BPS * 10


def test_the_provision_citation_changes_with_the_regime():
    assert "194O" in rules.expected_provision(date(2026, 3, 31))
    assert "393" in rules.expected_provision(date(2026, 4, 1))


# --- quarters --------------------------------------------------------------

def test_the_financial_year_quarter_turns_over_in_april():
    assert rules.quarter_of(date(2026, 4, 15)) == "FY2026-27 Q1"
    assert rules.quarter_of(date(2026, 3, 15)) == "FY2025-26 Q4"


def test_all_four_quarters_are_distinct_within_one_financial_year():
    quarters = {rules.quarter_of(date(2026, m, 15)) for m in (4, 7, 10, 1)}
    assert len(quarters) == 4


# --- the tolerance band ------------------------------------------------------

def test_the_tolerance_band_has_a_floor_and_a_percentage():
    tol = rules.Tolerance()
    assert tol.band(1_000) == 50                  # the Rs 0.50 floor wins
    assert tol.band(100_000_00) == 50_000          # 0.5% wins


# --- money formatting --------------------------------------------------------

def test_rupees_uses_indian_digit_grouping():
    assert rules.rupees(123456789) == "Rs 12,34,567.89"
    assert rules.rupees(-4500) == "-Rs 45.00"
