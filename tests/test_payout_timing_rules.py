"""
Unit tests for the payout timing rules - the due-date math everything else
in this agent rests on.
"""

import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from engine.payout_timing import rules  # noqa: E402


def test_due_date_skips_a_weekend_in_the_middle():
    """Friday + T+2 working days should land on Tuesday, not Sunday."""
    friday = date(2026, 7, 3)
    assert friday.weekday() == 4
    assert rules.due_date(friday) == date(2026, 7, 7)   # Tuesday


def test_due_date_on_a_monday_stays_within_the_week():
    monday = date(2026, 7, 6)
    assert rules.due_date(monday) == date(2026, 7, 8)   # Wednesday, no weekend


def test_working_days_between_ignores_a_weekend_gap():
    friday = date(2026, 7, 3)
    monday = date(2026, 7, 6)
    assert rules.working_days_between(friday, monday) == 1


def test_working_days_between_is_zero_for_the_same_date():
    d = date(2026, 7, 6)
    assert rules.working_days_between(d, d) == 0


def test_working_days_between_is_signed():
    a, b = date(2026, 7, 6), date(2026, 7, 8)
    assert rules.working_days_between(a, b) == 2
    assert rules.working_days_between(b, a) == -2


def test_float_cost_is_zero_for_an_on_time_settlement():
    assert rules.float_cost_paise(100_000_00, 0) == 0
    assert rules.float_cost_paise(100_000_00, -3) == 0


def test_float_cost_scales_with_amount_and_days():
    a = rules.float_cost_paise(100_000_00, 5)
    b = rules.float_cost_paise(200_000_00, 5)
    assert b == a * 2
    c = rules.float_cost_paise(100_000_00, 10)
    assert c == a * 2


def test_float_cost_is_an_integer_number_of_paise():
    for days in (1, 3, 7, 30):
        value = rules.float_cost_paise(73_419_00, days)
        assert isinstance(value, int)


def test_the_holiday_seam_is_empty_but_present():
    """No holiday calendar is populated - a stated limitation, not a
    silent one. This test exists so filling it in later is a deliberate
    edit, not an accidental behaviour change nobody notices."""
    assert rules.HOLIDAY_DATES == frozenset()


def test_rupees_uses_indian_digit_grouping():
    assert rules.rupees(123456789) == "Rs 12,34,567.89"
    assert rules.rupees(-4500) == "-Rs 45.00"
