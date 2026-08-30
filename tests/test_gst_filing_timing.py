"""
Tests for layer 2: the GSTR-1A / DRC-03 timing state machine.
"""

import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from engine.gst_filing.gate import (DEFAULT_REVIEW_ABOVE_PAISE,  # noqa: E402
                                    gate)
from engine.gst_filing.generator import generate_cycles  # noqa: E402
from engine.gst_filing.taxonomy import CorrectionCode  # noqa: E402
from engine.gst_filing.timing import (FilingCycle, detect_cycles,  # noqa: E402
                                      detect_period, drc03_draft,
                                      gstr1a_draft, window_state)

TODAY = date(2026, 8, 24)


def test_window_state_is_derived_from_gstr3b_filed():
    assert str(window_state(None)) == "open"
    assert str(window_state(date(2026, 7, 20))) == "locked"


def test_a_matching_period_is_clean():
    cycle = FilingCycle(period="2026-08", gstr1_liability=100_000_00,
                        gstr3b_filed=None, gstr3b_paid=100_000_00)
    f = detect_period(cycle, today=TODAY)
    assert f.exception_code == str(CorrectionCode.PERIOD_CLEAN)
    assert f.action == "none"


def test_a_shortfall_while_open_is_correctable_via_1a():
    cycle = FilingCycle(period="2026-08", gstr1_liability=100_000_00,
                        gstr3b_filed=None, gstr3b_paid=90_000_00)
    f = detect_period(cycle, today=TODAY)
    assert f.exception_code == str(CorrectionCode.CORRECTABLE_VIA_1A)
    assert f.action == "file_1a"
    assert f.delta == 10_000_00
    assert f.interest_paise == 0          # no interest before it's even locked


def test_a_shortfall_once_locked_needs_a_drc03_with_normal_interest():
    cycle = FilingCycle(period="2026-06", gstr1_liability=52_300_00,
                        gstr3b_filed=date(2026, 7, 22), gstr3b_paid=48_000_00)
    f = detect_period(cycle, today=TODAY)
    assert f.exception_code == str(CorrectionCode.LOCKED_NEEDS_DRC03)
    assert f.action == "pay_drc03"
    assert f.interest_rate_bps == 1_800          # 18%, ordinary shortfall
    # Rs 4,300 * 18% * 35 days / 365, rounded to the nearest paisa
    assert f.interest_paise == 7_422
    assert f.days_overdue == 35


def test_wrongly_claimed_itc_gets_the_24pct_rate_not_18pct():
    cycle = FilingCycle(period="2026-07", gstr1_liability=61_750_00,
                        gstr3b_filed=date(2026, 8, 21), gstr3b_paid=53_250_00,
                        wrongly_claimed_itc_paise=8_500_00)
    f = detect_period(cycle, today=TODAY)
    assert f.exception_code == str(CorrectionCode.LOCKED_NEEDS_DRC03)
    assert f.interest_rate_bps == 2_400
    assert "s.50(3)" in f.rule_cited


def test_the_category_is_never_inferred_from_a_bare_delta():
    """Two cycles with an identical shortfall get different interest rates
    purely because one names wrongly_claimed_itc_paise and the other
    doesn't - the function must never guess from the amount alone."""
    normal = FilingCycle(period="2026-06", gstr1_liability=60_000_00,
                         gstr3b_filed=date(2026, 7, 22), gstr3b_paid=50_000_00)
    wrong = FilingCycle(period="2026-06", gstr1_liability=60_000_00,
                        gstr3b_filed=date(2026, 7, 22), gstr3b_paid=50_000_00,
                        wrongly_claimed_itc_paise=10_000_00)
    fn = detect_period(normal, today=TODAY)
    fw = detect_period(wrong, today=TODAY)
    assert fn.delta == fw.delta == 10_000_00
    assert fn.interest_rate_bps == 1_800
    assert fw.interest_rate_bps == 2_400
    assert fw.interest_paise > fn.interest_paise


def test_an_overpayment_once_locked_is_clean_not_an_exception():
    """GSTR-3B paid MORE than GSTR-1 supports - no cash owed, no DRC-03,
    not flagged as needing action. See timing.py's module docstring."""
    cycle = FilingCycle(period="2026-06", gstr1_liability=50_000_00,
                        gstr3b_filed=date(2026, 7, 20), gstr3b_paid=60_000_00)
    f = detect_period(cycle, today=TODAY)
    assert f.exception_code == str(CorrectionCode.PERIOD_CLEAN)
    assert f.action == "none"
    assert f.interest_paise == 0
    assert "overpayment" in f.reasoning


def test_within_tolerance_is_clean_even_when_locked():
    cycle = FilingCycle(period="2026-06", gstr1_liability=50_000_00,
                        gstr3b_filed=date(2026, 7, 20), gstr3b_paid=49_990_00)
    f = detect_period(cycle, today=TODAY)
    assert f.exception_code == str(CorrectionCode.PERIOD_CLEAN)


def test_generate_cycles_matches_its_own_ground_truth():
    cycles, truth = generate_cycles("2026-08", 31_776_878)
    findings = detect_cycles(cycles, today=TODAY)
    assert len(findings) == 5
    for f in findings:
        assert f.exception_code == truth[f.period], (
            f.period, f.exception_code, truth[f.period])
    codes = {f.exception_code for f in findings}
    assert str(CorrectionCode.PERIOD_CLEAN) in codes
    assert str(CorrectionCode.CORRECTABLE_VIA_1A) in codes
    assert str(CorrectionCode.LOCKED_NEEDS_DRC03) in codes


# --- document drafts ---------------------------------------------------

def test_gstr1a_draft_shows_the_amendment_not_a_filing_claim():
    cycle = FilingCycle(period="2026-08", gstr1_liability=100_000_00,
                        gstr3b_filed=None, gstr3b_paid=88_000_00)
    f = detect_period(cycle, today=TODAY)
    d = gstr1a_draft(f)
    assert d["currently_reflected"] == 88_000_00
    assert d["corrected_to"] == 100_000_00
    assert d["amendment_paise"] == 12_000_00


def test_drc03_draft_totals_tax_plus_interest_only():
    cycle = FilingCycle(period="2026-06", gstr1_liability=52_300_00,
                        gstr3b_filed=date(2026, 7, 22), gstr3b_paid=48_000_00)
    f = detect_period(cycle, today=TODAY)
    d = drc03_draft(f, gstin="27ABCDE1234F1Z5")
    assert d["tax_paise"] == 4_300_00
    assert d["penalty_paise"] == 0
    assert d["total_paise"] == d["tax_paise"] + d["interest_paise"]
    assert d["financial_year"] == "2026-27"
    assert "s.73" in d["cause_of_payment"]


# --- the guardrail gate -------------------------------------------------

def test_a_clean_period_never_queues():
    cycle = FilingCycle(period="2026-05", gstr1_liability=45_000_00,
                        gstr3b_filed=date(2026, 6, 19), gstr3b_paid=45_000_00)
    f = detect_period(cycle, today=TODAY)
    d = gate(f)
    assert not d.queued_for_human
    assert d.decided_by == "calculator"


def test_a_large_shortfall_queues_even_with_no_agent_call():
    cycle = FilingCycle(
        period="2026-06", gstr1_liability=DEFAULT_REVIEW_ABOVE_PAISE * 3,
        gstr3b_filed=date(2026, 7, 22), gstr3b_paid=0)
    f = detect_period(cycle, today=TODAY)
    d = gate(f)
    assert d.queued_for_human
    assert any("review threshold" in r for r in d.reasons)


def test_the_action_shown_is_always_the_findings_own_never_the_agents():
    """gate() never lets a priority object override the mechanical
    action - mirrors payout timing's gate.py."""
    cycle = FilingCycle(period="2026-08", gstr1_liability=100_000_00,
                        gstr3b_filed=None, gstr3b_paid=80_000_00)
    f = detect_period(cycle, today=TODAY)

    class FakePriority:
        confidence = 0.95
        reasoning = "looks fine"
        error = None
        invented_figures = []

    d = gate(f, FakePriority())
    assert d.action == f.action == "file_1a"
    assert d.decided_by == "agent"
    assert not d.queued_for_human
