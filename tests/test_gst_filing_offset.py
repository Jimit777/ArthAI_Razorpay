"""
Tests for layer 3: the ITC utilisation hierarchy and the Rule 88C shield.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from engine.gst_filing.offset import (HeadAmounts, allocate,  # noqa: E402
                                      finding_from_88c_check,
                                      finding_from_allocation, pmt06_draft,
                                      rule_88c_check)


# --- the utilisation hierarchy -------------------------------------------

def test_igst_credit_clears_igst_first_then_spills_to_cgst_then_sgst():
    """The scenario that proves the hierarchy is doing something: a naive
    per-head-only allocator (no spillover) would ask for the FULL Rs 30,000
    CGST liability in cash. The real hierarchy only asks for Rs 12,000."""
    liability = HeadAmounts(igst=50_000_00, cgst=30_000_00, sgst=20_000_00)
    credit = HeadAmounts(igst=68_000_00, cgst=0, sgst=8_000_00)
    plan = allocate(liability, credit)

    assert plan.offset_igst_to_igst == 50_000_00
    assert plan.offset_igst_to_cgst == 18_000_00      # the spillover
    assert plan.offset_igst_to_sgst == 0              # pool exhausted first
    assert plan.offset_cgst_to_cgst == 0              # no direct CGST credit
    assert plan.offset_sgst_to_sgst == 8_000_00

    assert plan.cash_igst_needed == 0
    assert plan.cash_cgst_needed == 12_000_00         # NOT 30,000 - the point
    assert plan.cash_sgst_needed == 12_000_00
    assert plan.total_cash_needed == 24_000_00


def test_cgst_credit_never_offsets_sgst_and_vice_versa():
    liability = HeadAmounts(igst=0, cgst=10_000_00, sgst=10_000_00)
    credit = HeadAmounts(igst=0, cgst=15_000_00, sgst=0)
    plan = allocate(liability, credit)
    assert plan.offset_cgst_to_cgst == 10_000_00
    assert plan.cash_cgst_needed == 0
    assert plan.cash_sgst_needed == 10_000_00         # unaffected by CGST's surplus


def test_cash_already_on_hand_is_applied_per_head_with_no_spillover():
    liability = HeadAmounts(igst=0, cgst=10_000_00, sgst=10_000_00)
    credit = HeadAmounts()
    cash = HeadAmounts(igst=50_000_00, cgst=0, sgst=4_000_00)
    plan = allocate(liability, credit, cash_on_hand=cash)
    assert plan.cash_applied_cgst == 0                # IGST cash cannot spill
    assert plan.cash_applied_sgst == 4_000_00
    assert plan.cash_cgst_needed == 10_000_00
    assert plan.cash_sgst_needed == 6_000_00


def test_a_clean_allocation_still_produces_a_finding():
    """Needing some cash is normal, not an exception - allocate() never
    signals a taxonomy exception on its own."""
    liability = HeadAmounts(igst=10_000_00)
    credit = HeadAmounts()
    f = finding_from_allocation("2026-08", liability, credit)
    assert f.exception_code == "OFFSET_CLEAN"
    assert not f.rule_88c_breach
    assert f.plan.total_cash_needed == 10_000_00


# --- Rule 88C ------------------------------------------------------------

def test_rule_88c_uses_whichever_is_lower_of_the_two_caps():
    # 20% of Rs 48,000 = Rs 9,600, well under the Rs 1 lakh cap - the
    # percentage line governs here, not the absolute one.
    assert rule_88c_check(52_300_00, 48_000_00) == (False, 0)   # Rs 4,300 gap
    breach, excess = rule_88c_check(80_000_00, 50_000_00)       # Rs 30,000 gap
    assert breach
    assert excess == 20_000_00                                  # 30,000 - 10,000


def test_an_overpayment_never_breaches_rule_88c():
    assert rule_88c_check(40_000_00, 50_000_00) == (False, 0)


def test_finding_from_88c_check_is_none_when_clean():
    assert finding_from_88c_check("2026-06", 52_300_00, 48_000_00) is None


def test_finding_from_88c_check_cites_rule_88c_only():
    f = finding_from_88c_check("2026-04", 80_000_00, 50_000_00)
    assert f.rule_88c_breach
    assert f.exception_code == "RULE_88C_BREACH"
    assert "Rule 88C" in f.rule_cited
    assert f.plan is None                 # no per-head data for a locked period


# --- PMT-06 ---------------------------------------------------------------

def test_pmt06_draft_matches_the_allocation_exactly():
    liability = HeadAmounts(igst=50_000_00, cgst=30_000_00, sgst=20_000_00)
    credit = HeadAmounts(igst=68_000_00, cgst=0, sgst=8_000_00)
    f = finding_from_allocation("2026-08", liability, credit)
    draft = pmt06_draft(f, gstin="27ABCDE1234F1Z5")
    assert draft["igst_paise"] == 0
    assert draft["cgst_paise"] == 12_000_00
    assert draft["sgst_paise"] == 12_000_00
    assert draft["total_paise"] == 24_000_00


def test_pmt06_draft_is_empty_for_a_period_with_no_allocation():
    f = finding_from_88c_check("2026-04", 80_000_00, 50_000_00)
    assert pmt06_draft(f) == {}
