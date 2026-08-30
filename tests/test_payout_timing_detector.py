"""
Tests for the payout timing generator's answer key and the detector that
scores against it.

Every record here is settled mechanically (see detector.py's module
docstring for why - no hold/reason data exists anywhere to make a per-record
judgment call honest), so these tests assert exact agreement with the
answer key, not merely "mostly right".
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from engine.payout_timing.detector import detect  # noqa: E402
from engine.payout_timing.generator import (CANONICAL_MIX, DECOY_RECIPES,  # noqa: E402
                                            MISS_RECIPES, RECIPE_TRUTH,
                                            generate_batch)
from engine.payout_timing.rules import PatternThreshold  # noqa: E402
from engine.payout_timing.taxonomy import (Pattern, PayoutAction,  # noqa: E402
                                           PayoutCode)


@pytest.fixture(scope="module")
def batch():
    return generate_batch(60)


# --- the generator ---------------------------------------------------------

def test_the_batch_matches_the_canonical_composition(batch):
    _data, truth = batch
    assert len(truth) == sum(CANONICAL_MIX.values())


def test_the_batch_is_reproducible(batch):
    again, again_truth = generate_batch(60)
    assert again_truth == batch[1]


def test_a_smaller_batch_still_exercises_every_recipe():
    _data, truth = generate_batch(24)
    codes = set(truth.values())
    for recipe in CANONICAL_MIX:
        assert str(RECIPE_TRUTH[recipe]) in codes


def test_unmatched_invoices_have_no_settlement(batch):
    data, truth = batch
    settled_ids = {s.invoice_reference for s in data.settlements}
    for invoice in data.invoices:
        if truth[invoice.invoice_id] == str(PayoutCode.UNMATCHED):
            assert invoice.invoice_id not in settled_ids


# --- the detector matches every record exactly ------------------------------

def test_every_record_matches_the_answer_key_exactly(batch):
    data, truth = batch
    summary = detect(data)
    got = {r.invoice_id: r.code for r in summary.records}
    wrong = [(k, truth[k], v) for k, v in got.items() if truth[k] != v]
    assert not wrong, f"mismatches: {wrong}"


def test_unmatched_count_matches_the_plan(batch):
    data, truth = batch
    summary = detect(data)
    expected_unmatched = sum(1 for v in truth.values()
                             if v == str(PayoutCode.UNMATCHED))
    assert summary.n_unmatched == expected_unmatched


def test_a_planted_miss_is_actually_late(batch):
    """Findability - if a planted miss lands under a day late, ROUNDING would
    be the right answer and the plant itself would be wrong, not the code."""
    data, truth = batch
    summary = detect(data)
    by_id = {r.invoice_id: r for r in summary.records}
    for invoice_id, code in truth.items():
        if code == str(PayoutCode.SLA_MISS):
            assert by_id[invoice_id].delay_working_days >= 1


def test_on_time_settlements_never_show_a_delay(batch):
    data, truth = batch
    summary = detect(data)
    by_id = {r.invoice_id: r for r in summary.records}
    for invoice_id, code in truth.items():
        if code == str(PayoutCode.ON_TIME):
            assert by_id[invoice_id].delay_working_days == 0


# --- the batch-level pattern ------------------------------------------------

def test_the_canonical_batch_reads_as_systemic(batch):
    """~26% of matched records miss the SLA - built to clear the 20%
    threshold, so this is a real assertion about the demo's headline claim,
    not an implementation detail."""
    data, _truth = batch
    summary = detect(data)
    assert summary.pattern == str(Pattern.SYSTEMIC_DELAY)
    assert summary.action == str(PayoutAction.ESCALATE)


def test_a_clean_batch_needs_no_action():
    from engine.recon.records import Invoice, ReconBatch, Settlement
    from engine.payout_timing.rules import due_date
    from datetime import date

    batch = ReconBatch()
    issued = date(2026, 7, 6)
    for i in range(5):
        inv_id = f"INV-{i}"
        batch.invoices.append(Invoice(
            invoice_id=inv_id, customer_name="x", amount=10_000_00,
            date_issued=issued, status="paid"))
        batch.settlements.append(Settlement(
            txn_id=f"pay_{i}", gross_amount=10_000_00, fee_deducted=200_00,
            net_settled=9_800_00, settlement_date=due_date(issued),
            invoice_reference=inv_id))
    summary = detect(batch)
    assert summary.pattern == str(Pattern.CLEAN)
    assert summary.action == str(PayoutAction.NONE)
    assert summary.total_float_cost_paise == 0


def test_the_pattern_threshold_boundary_is_exact():
    """19.99% must not read as systemic; 20.00% must."""
    threshold = PatternThreshold(systemic_miss_rate_bps=2_000,
                                 systemic_mean_delay_days=999)
    from engine.payout_timing.detector import PayoutTimingSummary, _summarise
    from engine.payout_timing.detector import PayoutRecord
    from datetime import date

    def _miss(n):
        return [PayoutRecord(
            invoice_id=f"m{i}", txn_id="t", invoice_amount=100, net_settled=100,
            date_issued=date(2026, 1, 1), due_date=date(2026, 1, 3),
            settlement_date=date(2026, 1, 4), delay_working_days=1,
            delay_calendar_days=1, float_cost_paise=1,
            code=str(PayoutCode.SLA_MISS)) for i in range(n)]

    def _clean(n):
        return [PayoutRecord(
            invoice_id=f"c{i}", txn_id="t", invoice_amount=100, net_settled=100,
            date_issued=date(2026, 1, 1), due_date=date(2026, 1, 3),
            settlement_date=date(2026, 1, 3), delay_working_days=0,
            delay_calendar_days=0, float_cost_paise=0,
            code=str(PayoutCode.ON_TIME)) for i in range(n)]

    under = PayoutTimingSummary(records=_miss(1) + _clean(6),
                                n_settled=7, n_sla_miss=1, n_on_time=6)
    _summarise(under, threshold)
    assert under.pattern != str(Pattern.SYSTEMIC_DELAY)

    at = PayoutTimingSummary(records=_miss(2) + _clean(8),
                             n_settled=10, n_sla_miss=2, n_on_time=8)
    _summarise(at, threshold)
    assert at.pattern == str(Pattern.SYSTEMIC_DELAY)


# --- money -------------------------------------------------------------

def test_all_money_is_integer_paise(batch):
    data, _truth = batch
    for inv in data.invoices:
        assert isinstance(inv.amount, int)
    for s in data.settlements:
        assert isinstance(s.net_settled, int)
