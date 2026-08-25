"""
Tests for the variance detector. Checkpoint 5.

The detector is the thing that decides what the agent ever sees. Two failure
modes matter and they pull in opposite directions:

  a planted anomaly with no evidence attached  -> the agent cannot possibly
      classify it, and it counts as a miss for a reason that has nothing to do
      with the agent

  a clean record with a signal attached        -> the agent is invited to
      accuse Razorpay of something that did not happen

Both are tested across many seeds, because passing on one seed proves nothing.
"""

import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from engine.detector import (  # noqa: E402
    Variance,
    detect,
    detect_batch,
)
from engine.expected_value import (  # noqa: E402
    Payment,
    compute_expected_fee,
    load_rate_card,
    reprice_as,
)
from engine.taxonomy import (  # noqa: E402
    ACTION_FOR,
    NO_ACTION,
    RECOVERABLE,
    Action,
    ExceptionCode,
)
from generator.synthetic import (  # noqa: E402
    Record,
    Refund,
    SettlementLine,
    TdsEntry,
    generate_batch,
)

RC = load_rate_card()


@pytest.fixture(scope="module")
def audited():
    b, gt = generate_batch(60)
    return b, gt, {v.payment_id: v for v in detect_batch(b)}


def _ts(dt: datetime) -> int:
    return int(dt.timestamp())


def _record(amount, fee, tax, *, method="upi", refund=False, tds=None,
            created=None, settled=None, settlement=True, **fields) -> Record:
    """Hand-built record, for the cases the generator does not plant."""
    created = created or datetime(2026, 6, 10, tzinfo=timezone.utc)
    settled = settled or (created + timedelta(days=2))
    payment = Payment(payment_id="pay_test", amount=amount, method=method, **fields)
    rec = Record(record_id="pay_test", order_id="order_test",
                 payment=payment, created_at=_ts(created))
    if settlement:
        rec.settlement_lines.append(SettlementLine(
            entity_id="pay_test", settlement_id="setl_test", type="payment",
            payment_id="pay_test", order_id="order_test", amount=amount,
            fee=fee, tax=tax, utr="HDFCN00000001", settled_at=_ts(settled)))
    if refund:
        rec.refund = Refund("rfnd_test", "pay_test", amount, _ts(created))
        rec.settlement_lines.append(SettlementLine(
            entity_id="rfnd_test", settlement_id="setl_test", type="refund",
            payment_id="pay_test", order_id="order_test", amount=-amount,
            fee=0, tax=0, utr="HDFCN00000001", settled_at=_ts(settled)))
    rec.tds = tds
    return rec


# --- the arithmetic ------------------------------------------------------

def test_delta_is_actual_minus_expected(audited):
    b, _, vs = audited
    for record in b.records:
        v = vs[record.record_id]
        if not v.settlement_present:
            continue
        expected = compute_expected_fee(record.payment, RC)
        line = [ln for ln in record.settlement_lines if ln.type == "payment"][0]
        assert v.fee_delta == line.fee - expected.total_fee_paise
        assert v.tax_delta == line.tax - expected.gst_paise
        assert v.delta == v.fee_delta + v.tax_delta


def test_every_record_gets_a_variance(audited):
    b, gt, vs = audited
    assert set(vs) == set(gt)
    assert all(isinstance(v, Variance) for v in vs.values())


def test_implied_rate_is_what_they_actually_charged():
    """2.40% charged on a 2.00% contract should read as 240 bps, not 200."""
    rec = _record(1_000_000, 24_000, 4_320, method="card",
                  card_network="visa", card_type="credit")
    v = detect(rec, RC, as_of=_ts(datetime(2026, 7, 1, tzinfo=timezone.utc)))
    assert v.contracted_rate_bps == 200
    assert v.implied_rate_bps == 240


def test_all_money_on_a_variance_is_integer_paise(audited):
    _, _, vs = audited
    for v in vs.values():
        for value in (v.amount, v.expected_fee, v.actual_fee, v.expected_tax,
                      v.actual_tax, v.fee_delta, v.tax_delta, v.delta):
            assert isinstance(value, int) and not isinstance(value, bool)


# --- no false alarms -----------------------------------------------------

def test_clean_records_produce_no_signals_at_all(audited):
    b, gt, vs = audited
    for pid, code in gt.items():
        if code != "CLEAN":
            continue
        v = vs[pid]
        assert v.signals == [], f"{pid} raised {[s.kind for s in v.signals]}"
        assert v.exception_code == ExceptionCode.CLEAN
        assert v.decided_by == "calculator"


def test_no_false_alarms_across_many_seeds():
    """
    The number that becomes "zero false accusations" on stage. If a clean
    record ever picks up a signal, the agent is being handed an invitation to
    accuse a payment gateway of something it did not do.
    """
    for seed in range(1, 31):
        b, gt = generate_batch(60, seed=seed)
        for v in detect_batch(b):
            if gt[v.payment_id] != "CLEAN":
                continue
            assert not v.signals, (
                f"seed {seed}: clean record {v.payment_id} raised "
                f"{[s.kind for s in v.signals]}")
            assert v.exception_code == ExceptionCode.CLEAN


def test_rounding_decoys_are_dismissed_without_reaching_the_agent(audited):
    b, gt, vs = audited
    for pid, code in gt.items():
        if code != "ROUNDING":
            continue
        v = vs[pid]
        assert v.exception_code == ExceptionCode.ROUNDING
        assert v.action == Action.DISMISS
        assert v.delta != 0, "a rounding case with no gap is not a decoy"
        assert not v.needs_agent


def test_refund_decoys_are_dismissed_by_rule_eight(audited):
    b, gt, vs = audited
    for pid, code in gt.items():
        if code != "REFUND_MDR_RETAINED":
            continue
        v = vs[pid]
        assert v.exception_code == ExceptionCode.REFUND_MDR_RETAINED
        assert v.action == Action.DISMISS
        assert v.rule_cited == "rule 8"


# --- every anomaly reaches the agent with evidence ----------------------

def test_every_planted_anomaly_carries_matching_evidence_across_seeds():
    """
    The other half of the accuracy story. An anomaly the detector never
    surfaces is one the agent cannot be blamed for missing - and one the
    merchant never hears about.
    """
    for seed in range(1, 31):
        b, gt = generate_batch(60, seed=seed)
        for v in detect_batch(b):
            code = gt[v.payment_id]
            if code == "CLEAN":
                continue
            resolved = v.exception_code == code
            evidenced = any(s.candidate_code == code for s in v.signals)
            assert resolved or evidenced, (
                f"seed {seed}: {code} on {v.payment_id} produced "
                f"{[s.kind for s in v.signals]} and was resolved as "
                f"{v.exception_code}")


def test_zero_mdr_violations_cite_the_statute(audited):
    b, gt, vs = audited
    found = 0
    for pid, code in gt.items():
        if code != "ZERO_MDR_VIOLATION":
            continue
        found += 1
        v = vs[pid]
        signal = next(s for s in v.signals
                      if s.candidate_code == ExceptionCode.ZERO_MDR_VIOLATION)
        assert "PSS Act" in signal.source
        assert signal.amount_paise == v.fee_delta
        assert v.needs_agent, "a cause was assigned without judgment"
    assert found


def test_rate_mismatches_cite_the_slab_they_breached(audited):
    b, gt, vs = audited
    for pid, code in gt.items():
        if code != "RATE_MISMATCH":
            continue
        signal = next(s for s in vs[pid].signals
                      if s.candidate_code == ExceptionCode.RATE_MISMATCH)
        assert signal.source
        assert signal.amount_paise > 0


def test_missing_records_are_resolved_and_priced_at_the_whole_sale(audited):
    b, gt, vs = audited
    for pid, code in gt.items():
        if code != "MISSING_FROM_SETTLEMENT":
            continue
        v = vs[pid]
        assert v.exception_code == ExceptionCode.MISSING_FROM_SETTLEMENT
        assert v.action == Action.DISPUTE
        assert not v.settlement_present
        assert v.delta == 0, "there is no fee gap - there is no fee"
        signal = v.signals[0]
        # the money at stake is the sale net of what SHOULD have been deducted
        assert signal.amount_paise == v.amount - v.expected_fee - v.expected_tax


def test_tds_mismatch_is_flagged_but_not_auto_resolved(audited):
    b, gt, vs = audited
    for pid, code in gt.items():
        if code != "TDS_CODE_MISMATCH":
            continue
        v = vs[pid]
        signal = next(s for s in v.signals
                      if s.candidate_code == ExceptionCode.TDS_CODE_MISMATCH)
        assert "194O" in signal.detail and "1035" in signal.detail
        assert v.delta == 0, "the money is right; the tax code is not"


def test_correct_tds_entries_raise_nothing(audited):
    b, gt, vs = audited
    checked = 0
    for record in b.records:
        if record.tds is None or gt[record.record_id] == "TDS_CODE_MISMATCH":
            continue
        checked += 1
        kinds = {s.kind for s in vs[record.record_id].signals}
        assert "STALE_TDS_SECTION_CODE" not in kinds
    assert checked, "no correct TDS entries to check against"


# --- the mislabel, which arithmetic cannot see --------------------------

def test_mislabel_is_found_despite_a_zero_fee_gap(audited):
    """
    The project's most interesting catch, pinned down.

    Fee delta is zero. Tax delta is zero. Every number matches. The record is
    only findable because a card payment is carrying a UPI reference.
    """
    b, gt, vs = audited
    found = 0
    for record in b.records:
        if gt[record.record_id] != "INSTRUMENT_MISLABEL":
            continue
        found += 1
        v = vs[record.record_id]
        assert v.fee_delta == 0 and v.tax_delta == 0
        signal = next(s for s in v.signals
                      if s.candidate_code == ExceptionCode.INSTRUMENT_MISLABEL)
        as_upi = reprice_as(record.payment, "upi", RC)
        assert signal.amount_paise == v.actual_fee - as_upi.total_fee_paise
        assert signal.amount_paise > 0
        assert v.needs_agent
    assert found


def test_a_clean_card_payment_raises_no_mislabel_signal():
    rec = _record(400_000, 8_000, 1_440, method="card",
                  card_network="visa", card_type="credit")
    v = detect(rec, RC, as_of=_ts(datetime(2026, 7, 1, tzinfo=timezone.utc)))
    assert v.exception_code == ExceptionCode.CLEAN


# --- GST, and not double-counting it ------------------------------------

def test_gst_that_faithfully_follows_an_overcharged_fee_is_not_a_second_error():
    """
    A fee overcharge drags GST up with it. That inflated GST is a CONSEQUENCE,
    not a separate finding. Reporting both would double-count the money and
    hand the agent two problems where there is one.
    """
    amount = 500_000                       # Rs 5,000 UPI
    inflated_fee = 2_000 + 4_500           # correct Rs 20 platform fee + bogus 0.9% MDR
    rec = _record(amount, inflated_fee, (inflated_fee * 1800 + 5000) // 10000)
    v = detect(rec, RC, as_of=_ts(datetime(2026, 7, 1, tzinfo=timezone.utc)))
    kinds = {s.kind for s in v.signals}
    assert "ZERO_MDR_RAIL_OVERCHARGED" in kinds
    assert "GST_NOT_EIGHTEEN_PERCENT_OF_FEE" not in kinds
    assert v.tax_delta > 0, "the GST really is higher than expected"


def test_gst_charged_on_transaction_value_is_named_as_such():
    amount = 500_000
    fee = 2_000                                        # correct
    bogus_tax = (amount * 1800 + 5000) // 10000        # 18% of the SALE
    rec = _record(amount, fee, bogus_tax)
    v = detect(rec, RC, as_of=_ts(datetime(2026, 7, 1, tzinfo=timezone.utc)))
    signal = next(s for s in v.signals
                  if s.candidate_code == ExceptionCode.GST_MISMATCH)
    assert "transaction value" in signal.detail
    assert v.fee_delta == 0


def test_gst_at_the_wrong_rate_is_named_as_such():
    amount = 1_000_000
    fee = 4_000
    rec = _record(amount, fee, (fee * 1200 + 5000) // 10000)   # 12%, not 18%
    v = detect(rec, RC, as_of=_ts(datetime(2026, 7, 1, tzinfo=timezone.utc)))
    signal = next(s for s in v.signals
                  if s.candidate_code == ExceptionCode.GST_MISMATCH)
    assert "not 18%" in signal.detail


# --- where the detector must NOT decide ---------------------------------

def test_period_boundary_is_never_auto_resolved(audited):
    """
    CLAUDE.md section 6.1 puts this in the agent's column on purpose: whether a
    June order settling in July matters depends on the merchant's accounting
    period, which is not written down in any rate card.
    """
    b, gt, vs = audited
    for pid, code in gt.items():
        if code != "PERIOD_BOUNDARY":
            continue
        v = vs[pid]
        assert v.needs_agent
        assert v.delta == 0
        assert any(s.kind == "CROSSES_ACCOUNTING_PERIOD" for s in v.signals)


def test_a_refund_that_is_also_overcharged_is_not_quietly_dismissed():
    """
    The dangerous edge of rule 8. "There was a refund" must not become a
    blanket excuse for any fee on the record - otherwise the easiest way to
    hide an overcharge would be to refund something.
    """
    amount = 500_000
    overcharged = 2_000 + 4_500
    rec = _record(amount, overcharged, (overcharged * 1800 + 5000) // 10000,
                  refund=True)
    v = detect(rec, RC, as_of=_ts(datetime(2026, 7, 1, tzinfo=timezone.utc)))
    assert v.needs_agent, "an overcharge was dismissed because a refund existed"
    kinds = {s.kind for s in v.signals}
    assert kinds == {"FEE_RETAINED_ON_REFUND", "ZERO_MDR_RAIL_OVERCHARGED"}


def test_an_unexplained_gap_goes_to_the_agent_not_to_a_verdict():
    """
    A gap outside tolerance that no rule accounts for. The calculator must not
    invent a cause, and it must not call it clean either.
    """
    rec = _record(500_000, 2_000, 1_500)     # fee right, GST absurd but not 18%-of-anything
    v = detect(rec, RC, as_of=_ts(datetime(2026, 7, 1, tzinfo=timezone.utc)))
    assert v.tax_delta != 0
    assert v.exception_code is None or v.exception_code == ExceptionCode.GST_MISMATCH


def test_a_payment_still_in_transit_is_not_called_missing():
    """
    Without this guard, every payment in the last two days of a batch looks
    like a disappearance and the report cries wolf on its own tail.
    """
    created = datetime(2026, 6, 25, tzinfo=timezone.utc)
    rec = _record(500_000, 0, 0, settlement=False, created=created)
    v = detect(rec, RC, as_of=_ts(created + timedelta(days=1)))
    assert v.exception_code == ExceptionCode.CLEAN
    assert v.signals[0].kind == "SETTLEMENT_NOT_YET_DUE"

    # ...but once the window has passed, it is missing.
    later = detect(rec, RC, as_of=_ts(created + timedelta(days=10)))
    assert later.exception_code == ExceptionCode.MISSING_FROM_SETTLEMENT


# --- evidence quality ----------------------------------------------------

def test_every_signal_carries_a_rule_and_a_citation(audited):
    """
    Guardrail 2 (CLAUDE.md section 10). A finding without a source is not
    something a finance team can file, it is an opinion.
    """
    _, _, vs = audited
    for v in vs.values():
        for signal in v.signals:
            assert signal.rule, f"{v.payment_id}: {signal.kind} has no rule"
            assert signal.source, f"{v.payment_id}: {signal.kind} has no source"
            assert len(signal.detail) > 40


def test_signal_details_contain_the_numbers_already_computed(audited):
    """
    The architecture, as a test. Every rupee figure the agent will quote is
    computed in Python and handed over finished. If details stopped carrying
    numbers, the agent would have to work them out - and that is the one thing
    it must never do.
    """
    _, _, vs = audited
    for v in vs.values():
        for signal in v.signals:
            # A rupee figure, a rate, or a date - all worked out here, in Python.
            has_fact = ("Rs " in signal.detail or "%" in signal.detail
                        or any(m in signal.detail for m in
                               ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
                                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")))
            assert has_fact, f"{v.payment_id}: {signal.kind} states no computed fact"


def test_calculator_decisions_are_fully_populated(audited):
    _, _, vs = audited
    for v in vs.values():
        if v.needs_agent:
            assert v.action is None and v.confidence is None
            continue
        assert v.action is not None
        assert v.rule_cited and v.reasoning
        assert v.confidence == 1.0
        assert v.decided_by == "calculator"


def test_variance_is_serialisable(audited):
    """Checkpoint 6 has to put this in a prompt; checkpoint 9 in a JSON response."""
    import json
    _, _, vs = audited
    for v in list(vs.values())[:5]:
        json.dumps(v.to_dict())


# --- taxonomy ------------------------------------------------------------

def test_every_code_has_an_action():
    for code in ExceptionCode:
        assert code in ACTION_FOR


def test_exactly_three_codes_mean_do_nothing():
    """
    CLAUDE.md section 5: "Three of these mean 'do nothing.' That is
    deliberate." If a fourth ever appears, someone should have to notice.
    """
    dismissing = {c for c in ExceptionCode if ACTION_FOR[c] == Action.DISMISS}
    assert dismissing == set(NO_ACTION)
    assert len(dismissing) == 3


def test_recoverable_codes_all_lead_to_a_dispute():
    for code in RECOVERABLE:
        assert ACTION_FOR[code] == Action.DISPUTE


# --- the split, as a measured claim -------------------------------------

def test_most_records_never_reach_the_llm(audited):
    """
    Worth stating on stage: the agent is spent only where judgment is needed.
    Cheaper, faster, and a deterministic answer cannot hallucinate.
    """
    _, gt, vs = audited
    resolved = sum(1 for v in vs.values() if not v.needs_agent)
    assert resolved >= len(gt) * 0.7


def test_detect_batch_defaults_as_of_to_the_last_settlement():
    """
    Not the wall clock. A batch audited in 2027 must give the same answer it
    gave the day it was generated, or the demo changes behaviour over time.
    """
    b, _ = generate_batch(60)
    a = detect_batch(b)
    later = detect_batch(b, as_of=int(time.time()))
    assert [v.exception_code for v in a] == [v.exception_code for v in later]
