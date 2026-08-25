"""
Unit tests for the input tax credit rules.

CLAUDE.md's discipline for the settlement engine applies here unchanged: every
rule gets a test, and every test names the source it is checking. A wrong rule
is worse than a missing one - it makes the agent confidently tell a merchant to
drop credit they were entitled to, or to claim credit that earns them a notice.
"""

import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from engine.gst import rules  # noqa: E402


# --- rule 3: CGST s.16(4), the claim deadline ----------------------------

def test_the_financial_year_turns_over_in_april():
    assert rules.financial_year_of(date(2026, 3, 31)) == 2025
    assert rules.financial_year_of(date(2026, 4, 1)) == 2026


def test_two_days_apart_can_mean_a_year_apart_in_deadline():
    """
    The trap this rule exists to catch. An invoice dated 30 March and one dated
    2 April look interchangeable and are nearly twelve months apart in urgency.
    """
    assert rules.claim_deadline(date(2026, 3, 30)) == date(2026, 11, 30)
    assert rules.claim_deadline(date(2026, 4, 2)) == date(2027, 11, 30)


def test_an_invoice_past_its_deadline_is_time_barred():
    invoice = date(2024, 6, 1)                  # FY2024-25, due 30 Nov 2025
    assert rules.is_time_barred(invoice, date(2026, 8, 24))
    assert not rules.is_time_barred(invoice, date(2025, 11, 30))


def test_the_deadline_day_itself_is_still_claimable():
    invoice = date(2024, 6, 1)
    assert not rules.is_time_barred(invoice, rules.claim_deadline(invoice))


# --- rule 4: CGST Rule 37, the 180-day supplier payment window -----------

def test_the_supplier_payment_window_is_180_days():
    assert rules.SUPPLIER_PAYMENT_DAYS == 180
    assert rules.payment_due_by(date(2026, 1, 1)) == date(2026, 6, 30)


def test_an_unpaid_supplier_past_180_days_needs_a_reversal():
    invoice = date(2026, 1, 1)
    assert rules.needs_rule_37_reversal(invoice, None, date(2026, 8, 24))
    assert not rules.needs_rule_37_reversal(invoice, None, date(2026, 5, 1))


def test_paying_the_supplier_removes_the_reversal():
    """A paid invoice never reverses, however old it is."""
    invoice = date(2020, 1, 1)
    assert not rules.needs_rule_37_reversal(
        invoice, date(2020, 2, 1), date(2026, 8, 24))


# --- rule 5: CGST s.17(5), blocked credits -------------------------------

def test_blocked_categories_return_their_own_citation():
    reason = rules.blocked_reason("food_beverage")
    assert reason and "17(5)" in reason


def test_an_ordinary_purchase_is_not_blocked():
    assert rules.blocked_reason("fabric") is None
    assert rules.blocked_reason(None) is None


def test_every_blocked_category_cites_a_subsection():
    for category, source in rules.BLOCKED_CATEGORIES.items():
        assert "s.17(5)" in source, f"{category} has no citation"


# --- rule 6: Rule 88D, the automatic notice ------------------------------

def test_the_notice_threshold_is_the_lower_of_the_two_not_the_higher():
    """
    Rule 88D says Rs 1 lakh OR 20%, WHICHEVER IS LOWER. Getting this backwards
    would tell a merchant they are safe while a DRC-01C is already generating.
    """
    # small business: 20% of Rs 3,00,000 is Rs 60,000, which is lower
    assert rules.notice_threshold(300_000_00) == 60_000_00
    # large business: 20% of Rs 90,00,000 is Rs 18,00,000, so Rs 1 lakh is lower
    assert rules.notice_threshold(9_000_000_00) == rules.NOTICE_ABSOLUTE_PAISE


def test_a_gap_above_the_threshold_triggers_the_notice():
    assert rules.triggers_notice(claimed_paise=370_000_00,
                                 available_paise=300_000_00)
    assert not rules.triggers_notice(claimed_paise=350_000_00,
                                     available_paise=300_000_00)


# --- rule 7: CGST s.50, interest -----------------------------------------

def test_interest_runs_at_eighteen_percent_a_year():
    assert rules.INTEREST_PCT_BPS == 1_800
    # Rs 1,00,000 for a full year
    assert rules.interest_on(100_000_00, 365) == 18_000_00


def test_interest_is_zero_before_any_days_pass():
    assert rules.interest_on(100_000_00, 0) == 0
    assert rules.interest_on(0, 365) == 0


def test_interest_is_an_integer_number_of_paise():
    """Money is never a float here. A drifting paise across a batch is exactly
    the kind of thing that makes an otherwise correct finding arguable."""
    for days in (1, 7, 90, 180, 365):
        value = rules.interest_on(73_419_00, days)
        assert isinstance(value, int)


# --- rule 8: GSTIN shape -------------------------------------------------

def test_a_gstin_carries_its_state_in_the_first_two_characters():
    assert rules.gstin_state("27AABCU9603R1ZM") == "27"


def test_a_malformed_gstin_is_rejected():
    assert rules.gstin_well_formed("27AABCU9603R1ZM")
    assert not rules.gstin_well_formed("27AABC")
    assert not rules.gstin_well_formed("")


# --- money formatting ----------------------------------------------------

def test_rupees_uses_indian_digit_grouping():
    """Rs 12,34,567.89 - not Rs 1,234,567.89. A finance tool that groups money
    the American way is telling an Indian merchant it was not built for them."""
    assert rules.rupees(123456789) == "Rs 12,34,567.89"
    assert rules.rupees(100000) == "Rs 1,000.00"
    assert rules.rupees(-4500) == "-Rs 45.00"


# --- the tolerance band --------------------------------------------------

def test_the_tolerance_band_has_a_floor_and_a_percentage():
    tol = rules.Tolerance()
    assert tol.band(100) == 100                  # the Rs 1 floor wins
    assert tol.band(100_000_00) == 50_000        # 0.5% wins
