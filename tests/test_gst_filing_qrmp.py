"""
Tests for layer 4: the QRMP method choice and the IFF plan.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from engine.gst_filing import rules  # noqa: E402
from engine.gst_filing.generator import (plant_qrmp_quarter,  # noqa: E402
                                         quarter_of)
from engine.gst_filing.offset import HeadAmounts  # noqa: E402
from engine.gst_filing.qrmp import (build_qrmp_plan,  # noqa: E402
                                    build_quarterly_gstr3b, eligible,
                                    fixed_sum_amount, iff_worth_filing,
                                    recommend_method)
from engine.gst_filing.taxonomy import QRMPMethod  # noqa: E402


# --- quarter_of -----------------------------------------------------------

def test_quarter_of_places_months_in_the_indian_fy_quarters():
    assert quarter_of("2026-04") == ("Q1 FY2026-27", 1)
    assert quarter_of("2026-06") == ("Q1 FY2026-27", 3)
    assert quarter_of("2026-08") == ("Q2 FY2026-27", 2)
    assert quarter_of("2026-01") == ("Q4 FY2025-26", 1)   # Jan belongs to the PRIOR fy
    assert quarter_of("2026-03") == ("Q4 FY2025-26", 3)


# --- eligible ---------------------------------------------------------------

def test_eligibility_is_a_turnover_ceiling():
    assert eligible(rules.QRMP_TURNOVER_THRESHOLD_PAISE)          # exactly at it
    assert eligible(rules.QRMP_TURNOVER_THRESHOLD_PAISE - 1)
    assert not eligible(rules.QRMP_TURNOVER_THRESHOLD_PAISE + 1)


# --- fixed_sum_amount ---------------------------------------------------

def test_fixed_sum_is_35pct_of_the_previous_quarters_cash():
    assert fixed_sum_amount(100_000_00) == 35_000_00


# --- recommend_method -----------------------------------------------------

def test_recommends_whichever_two_month_total_is_smaller():
    # self-assessed total (30k+30k=60k) < fixed-sum total (35k*2=70k)
    assert recommend_method(35_000_00, 30_000_00, 30_000_00) == QRMPMethod.SELF_ASSESSMENT
    # self-assessed total (40k+40k=80k) > fixed-sum total (35k*2=70k)
    assert recommend_method(35_000_00, 40_000_00, 40_000_00) == QRMPMethod.FIXED_SUM


def test_a_tie_defaults_to_the_fixed_sum_safe_harbour():
    assert recommend_method(35_000_00, 35_000_00, 35_000_00) == QRMPMethod.FIXED_SUM


# --- iff_worth_filing -----------------------------------------------------

def test_month_3_never_gets_an_iff_window():
    assert not iff_worth_filing(10_000_00_00, month_in_quarter=3,
                                materiality_paise=1)


def test_below_materiality_is_not_worth_filing():
    assert not iff_worth_filing(1_00, month_in_quarter=1, materiality_paise=200_00)


def test_at_or_above_materiality_is_worth_filing():
    assert iff_worth_filing(200_00, month_in_quarter=2, materiality_paise=200_00)
    assert iff_worth_filing(500_00, month_in_quarter=1, materiality_paise=200_00)


# --- build_qrmp_plan / plant_qrmp_quarter --------------------------------

def test_an_ineligible_business_gets_no_method_or_iff():
    plan = build_qrmp_plan(
        "Q2 FY2026-27", turnover_paise=rules.QRMP_TURNOVER_THRESHOLD_PAISE + 1,
        previous_quarter_cash_paise=100_000_00,
        month1_self_assessed_paise=10_000_00,
        month2_self_assessed_paise=10_000_00, month1_iff_invoices=[],
        month2_iff_invoices=[300_00], materiality_paise=200_00)
    assert not plan.is_eligible
    assert plan.method == ""
    assert plan.iff_used_month1 == 0 and plan.iff_used_month2 == 0


def test_the_planted_quarter_shows_the_correct_method_and_iff_plan():
    """The checkpoint's own 'done when': a planted quarter, run through
    the same seam plant_qrmp_quarter() hands the runner, produces a
    correct method recommendation and a correct IFF count."""
    kwargs, month3 = plant_qrmp_quarter(
        "2026-08", current_month_taxable_paise=15_000_00_00,
        current_month_self_assessed_paise=25_331_83,
        current_month_b2b_tax_paise=[313362, 277900, 202728, 60000])
    plan = build_qrmp_plan(**kwargs, materiality_paise=200_000)

    assert plan.is_eligible                    # well under the turnover ceiling
    assert plan.quarter == "Q2 FY2026-27"
    assert plan.method in (str(QRMPMethod.FIXED_SUM), str(QRMPMethod.SELF_ASSESSMENT))
    # month2 is the real figure, passed straight through
    assert kwargs["month2_self_assessed_paise"] == 25_331_83
    # 3 of the 4 planted invoices clear the Rs 2,000 materiality bar
    assert plan.iff_used_month2 == 3
    assert plan.iff_used_month1 == 0            # no invoice data for the estimate month


def test_month1_is_labelled_an_estimate_not_a_second_real_month():
    kwargs, month3 = plant_qrmp_quarter(
        "2026-08", current_month_taxable_paise=15_000_00_00,
        current_month_self_assessed_paise=25_331_83,
        current_month_b2b_tax_paise=[])
    assert kwargs["month1_iff_invoices"] == []
    assert kwargs["month1_self_assessed_paise"] == (25_331_83 * 85) // 100
    assert month3 == (25_331_83 * 110) // 100


# --- build_quarterly_gstr3b (month 3) -------------------------------------

def test_grand_total_sums_all_three_months():
    month2 = HeadAmounts(igst=15_00_000, cgst=7_50_000, sgst=7_50_000)
    out = build_quarterly_gstr3b(
        "Q2 FY2026-27", month1_liability_paise=20_00_000,
        month2_liability=month2, month3_liability_paise=25_00_000,
        prior_advances_paise=0, gstin="27ABCDE1234F1Z5", ret_period="Q2")
    r = out["reconciliation"]
    assert r["grand_total_liability_paise"] == 20_00_000 + 30_00_000 + 25_00_000
    assert r["month2_liability_paise"] == 30_00_000


def test_head_split_uses_month2s_real_ratio_applied_once_to_the_total():
    """IGST is exactly half of month 2's real liability - the aggregated
    total should keep that same 50% ratio, not re-derive it per month."""
    month2 = HeadAmounts(igst=10_00_000, cgst=5_00_000, sgst=5_00_000)
    out = build_quarterly_gstr3b(
        "Q2 FY2026-27", month1_liability_paise=10_00_000,
        month2_liability=month2, month3_liability_paise=10_00_000,
        prior_advances_paise=0, gstin="27ABCDE1234F1Z5", ret_period="Q2")
    osup = out["sup_details"]["osup_det"]
    total = osup["iamt"] + osup["camt"] + osup["samt"]
    assert total == 40_00_000
    assert osup["iamt"] == total // 2                # 50%, matching month 2


def test_balance_due_when_advances_fall_short():
    month2 = HeadAmounts(igst=10_00_000, cgst=0, sgst=0)
    out = build_quarterly_gstr3b(
        "Q2 FY2026-27", month1_liability_paise=10_00_000,
        month2_liability=month2, month3_liability_paise=10_00_000,
        prior_advances_paise=15_00_000, gstin="27ABCDE1234F1Z5",
        ret_period="Q2")
    r = out["reconciliation"]
    assert r["grand_total_liability_paise"] == 30_00_000
    assert r["balance_due_paise"] == 15_00_000
    assert r["credit_carried_forward_paise"] == 0


def test_credit_carried_forward_when_advances_exceed_liability():
    month2 = HeadAmounts(igst=5_00_000, cgst=0, sgst=0)
    out = build_quarterly_gstr3b(
        "Q2 FY2026-27", month1_liability_paise=5_00_000,
        month2_liability=month2, month3_liability_paise=5_00_000,
        prior_advances_paise=20_00_000, gstin="27ABCDE1234F1Z5",
        ret_period="Q2")
    r = out["reconciliation"]
    assert r["grand_total_liability_paise"] == 15_00_000
    assert r["balance_due_paise"] == 0
    assert r["credit_carried_forward_paise"] == 5_00_000


def test_top_level_shape_matches_the_verified_gstr3b_template():
    """gstin/ret_period/sup_details.osup_det, cross-checked this session
    against resilient-tech/india-compliance's real production
    gstr_3b_report_template.json."""
    month2 = HeadAmounts(igst=1_00_000, cgst=50_000, sgst=50_000)
    out = build_quarterly_gstr3b(
        "Q2 FY2026-27", month1_liability_paise=1_00_000,
        month2_liability=month2, month3_liability_paise=1_00_000,
        prior_advances_paise=0, gstin="27ABCDE1234F1Z5", ret_period="Q2")
    assert set(out) == {"gstin", "ret_period", "sup_details", "reconciliation"}
    osup = out["sup_details"]["osup_det"]
    assert set(osup) == {"txval", "iamt", "camt", "samt", "csamt"}
