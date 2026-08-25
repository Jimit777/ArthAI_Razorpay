"""
Tests for the synthetic generator.

These matter more than they look. If a planted ZERO_MDR_VIOLATION quietly
produces the correct fee, the agent will "miss" it and we will report a false
accuracy number on stage. The generator is the measuring instrument - it has
to be calibrated before anything is measured with it.

Run: .venv/bin/python -m pytest tests/ -v
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from engine.expected_value import (  # noqa: E402
    compute_expected_fee,
    load_rate_card,
    tolerance_paise,
)
from generator.synthetic import (  # noqa: E402
    CANONICAL_MIX,
    CANONICAL_N,
    canonical_truth_mix,
    TDS_NEW_CODE,
    TDS_OLD_CODE,
    Batch,
    generate_batch,
    scale_mix,
)

RC = load_rate_card()


@pytest.fixture(scope="module")
def batch() -> tuple[Batch, dict[str, str]]:
    return generate_batch(60)


def _by_code(batch: Batch, gt: dict[str, str], code: str):
    return [r for r in batch.records if gt[r.record_id] == code]


def _fee_delta(record, rate_card=RC) -> int:
    """actual - expected, in paise, on the payment line only."""
    payment_lines = [ln for ln in record.settlement_lines if ln.type == "payment"]
    if not payment_lines:
        return 0
    expected = compute_expected_fee(record.payment, rate_card)
    return payment_lines[0].fee - expected.total_fee_paise


def _tax_delta(record, rate_card=RC) -> int:
    payment_lines = [ln for ln in record.settlement_lines if ln.type == "payment"]
    if not payment_lines:
        return 0
    expected = compute_expected_fee(record.payment, rate_card)
    return payment_lines[0].tax - expected.gst_paise


# --- shape and composition ----------------------------------------------

def test_batch_has_requested_size(batch):
    b, gt = batch
    assert len(b.records) == 60
    assert len(gt) == 60


def test_composition_matches_the_canonical_mix(batch):
    """
    The number quoted on stage - 14 anomalies, 6 decoys - is this test.

    Compared against the TRUTH mix, not the recipe mix. Two recipes are named
    after how the error is planted rather than what the answer is: an ambiguous
    refund-plus-overcharge should come back as ZERO_MDR_VIOLATION, and an
    unrecognised adjustment should come back as UNEXPLAINED.
    """
    _, gt = batch
    counts: dict[str, int] = {}
    for code in gt.values():
        counts[code] = counts.get(code, 0) + 1
    assert counts == canonical_truth_mix()


def test_canonical_mix_sums_to_sixty():
    assert sum(CANONICAL_MIX.values()) == CANONICAL_N


def test_record_ids_are_unique(batch):
    b, _ = batch
    assert len({r.record_id for r in b.records}) == len(b.records)


def test_every_record_appears_in_the_answer_key(batch):
    b, gt = batch
    assert {r.record_id for r in b.records} == set(gt)


def test_records_carry_no_label_field(batch):
    """
    The answer key must not leak into the data. If a planted code ever becomes
    readable from a Record, the accuracy number stops meaning anything.
    """
    b, gt = batch
    codes = set(gt.values())
    for record in b.records:
        blob = repr(record)
        assert not any(code in blob for code in codes)


# --- determinism ---------------------------------------------------------

def test_same_seed_gives_identical_batches():
    """The demo must produce the same numbers on stage as in rehearsal."""
    a, gt_a = generate_batch(60, seed=7)
    b, gt_b = generate_batch(60, seed=7)
    assert repr(a.records) == repr(b.records)
    assert gt_a == gt_b


def test_different_seed_gives_a_different_batch():
    a, _ = generate_batch(60, seed=7)
    b, _ = generate_batch(60, seed=8)
    assert repr(a.records) != repr(b.records)


# --- the planted errors actually plant something ------------------------

def test_clean_records_have_exactly_zero_variance(batch):
    """
    Not "within tolerance" - exactly zero. Any drift here means the generator
    and the engine disagree about rounding, and the false-accusation rate we
    report would be an artefact of that rather than of the agent.
    """
    b, gt = batch
    for record in _by_code(b, gt, "CLEAN"):
        assert _fee_delta(record) == 0, record.record_id
        assert _tax_delta(record) == 0, record.record_id


def test_rounding_decoys_sit_inside_the_tolerance_band(batch):
    b, gt = batch
    rounding = _by_code(b, gt, "ROUNDING")
    assert rounding
    for record in rounding:
        expected = compute_expected_fee(record.payment, RC)
        tol = tolerance_paise(expected.total_fee_paise, RC)
        delta = _fee_delta(record)
        assert delta != 0, f"{record.record_id} is not actually a rounding case"
        assert abs(delta) <= tol, f"{record.record_id} escapes the tolerance band"


def test_zero_mdr_violations_overcharge_beyond_tolerance(batch):
    b, gt = batch
    violations = _by_code(b, gt, "ZERO_MDR_VIOLATION")
    assert violations
    for record in violations:
        expected = compute_expected_fee(record.payment, RC)
        tol = tolerance_paise(expected.total_fee_paise, RC)
        delta = _fee_delta(record)
        assert delta > tol, f"{record.record_id} is not detectably overcharged"
        # and it has to be on a rail where network MDR is mandated to zero
        assert expected.network_mdr_paise == 0
        assert expected.instrument_key in ("upi", "rupay_debit")


def test_rate_mismatches_overcharge_beyond_tolerance(batch):
    b, gt = batch
    for record in _by_code(b, gt, "RATE_MISMATCH"):
        expected = compute_expected_fee(record.payment, RC)
        assert _fee_delta(record) > tolerance_paise(expected.total_fee_paise, RC)


def test_gst_mismatches_move_the_tax_not_the_fee(batch):
    b, gt = batch
    for record in _by_code(b, gt, "GST_MISMATCH"):
        expected = compute_expected_fee(record.payment, RC)
        assert _fee_delta(record) == 0, "the fee is correct; only GST is wrong"
        assert abs(_tax_delta(record)) > tolerance_paise(expected.gst_paise, RC)


def test_instrument_mislabels_are_invisible_to_arithmetic(batch):
    """
    The subtle one, pinned down deliberately.

    A mislabelled UPI payment is charged the CORRECT card fee, so the fee delta
    is zero and no amount of comparing numbers will find it. It is detectable
    only through rule 9's cross-field note. This test exists so that nobody
    later "fixes" the generator by making the fee wrong - that would turn the
    project's most interesting catch into a trivial one.
    """
    b, gt = batch
    mislabels = _by_code(b, gt, "INSTRUMENT_MISLABEL")
    assert mislabels
    for record in mislabels:
        assert _fee_delta(record) == 0
        assert _tax_delta(record) == 0
        assert record.payment.upi_reference is not None
        assert record.payment.method == "card"
        expected = compute_expected_fee(record.payment, RC)
        assert any("mislabel" in note for note in expected.notes)


def test_missing_records_have_no_settlement_line(batch):
    b, gt = batch
    missing = _by_code(b, gt, "MISSING_FROM_SETTLEMENT")
    assert missing
    for record in missing:
        assert record.settlement_lines == []


def test_refund_decoys_keep_the_fee_and_reverse_the_amount(batch):
    """Rule 8: the retained fee is expected behaviour, not an error."""
    b, gt = batch
    refunds = _by_code(b, gt, "REFUND_MDR_RETAINED")
    assert refunds
    for record in refunds:
        assert record.refund is not None
        assert _fee_delta(record) == 0, "the fee itself is correct"
        refund_lines = [ln for ln in record.settlement_lines if ln.type == "refund"]
        assert len(refund_lines) == 1
        assert refund_lines[0].amount == -record.payment.amount
        assert refund_lines[0].fee == 0, "the gateway does not refund its fee"


def test_period_boundary_settles_in_the_following_month(batch):
    b, gt = batch
    import datetime as _dt
    for record in _by_code(b, gt, "PERIOD_BOUNDARY"):
        created = _dt.datetime.fromtimestamp(record.created_at, _dt.timezone.utc)
        settled = _dt.datetime.fromtimestamp(
            record.settlement_lines[0].settled_at, _dt.timezone.utc)
        assert (created.year, created.month) != (settled.year, settled.month)
        assert _fee_delta(record) == 0, "nothing is wrong with the money"


def test_tds_mismatch_uses_the_repealed_section_code(batch):
    b, gt = batch
    stale = _by_code(b, gt, "TDS_CODE_MISMATCH")
    assert stale
    for record in stale:
        assert record.tds is not None
        assert record.tds.section_code == TDS_OLD_CODE
        assert record.tds.rate_bps == 100          # the old 1%
        assert _fee_delta(record) == 0, "the money is right; the tax code is not"


def test_other_tds_entries_use_the_current_code(batch):
    """Rule 10 needs correct entries to leave alone, or it cannot be wrong."""
    b, gt = batch
    correct = [r for r in b.records
               if r.tds and gt[r.record_id] != "TDS_CODE_MISMATCH"]
    assert correct, "no correct TDS entries - rule 10 has no negative cases"
    for record in correct:
        assert record.tds.section_code == TDS_NEW_CODE
        assert record.tds.rate_bps == 10           # the new 0.1%


# --- the argument in CLAUDE.md section 1.3 ------------------------------

def test_bank_credits_tie_out_to_the_paise(batch):
    """
    THE POINT OF THE WHOLE PROJECT, as a test.

    Every UTR credits exactly the sum of its settlement lines. Layer 1 ("did
    the money arrive?") passes. Layer 2 ("does gross minus deductions equal
    net?") passes. And the batch still contains twelve problems, because
    neither layer ever asks whether the deductions were CORRECT.
    """
    b, _ = batch
    per_utr: dict[str, int] = {}
    for record in b.records:
        for line in record.settlement_lines:
            per_utr[line.utr] = per_utr.get(line.utr, 0) + line.amount - line.fee - line.tax

    assert per_utr, "no settlement lines at all"
    for credit in b.bank_credits:
        assert credit.amount == per_utr[credit.utr], f"{credit.utr} does not reconcile"
    assert len(b.bank_credits) == len(per_utr)


def test_a_missing_record_leaves_no_trace_in_the_bank(batch):
    """The one anomaly that a net-level reconciliation could never surface."""
    b, gt = batch
    missing = _by_code(b, gt, "MISSING_FROM_SETTLEMENT")[0]
    all_ids = {ln.payment_id for r in b.records for ln in r.settlement_lines}
    assert missing.record_id not in all_ids


# --- money hygiene -------------------------------------------------------

def test_all_money_is_integer_paise(batch):
    b, _ = batch
    for record in b.records:
        assert isinstance(record.payment.amount, int)
        for line in record.settlement_lines:
            for value in (line.amount, line.fee, line.tax):
                assert isinstance(value, int) and not isinstance(value, bool)
        if record.refund:
            assert isinstance(record.refund.amount, int)
    for credit in b.bank_credits:
        assert isinstance(credit.amount, int)


def test_no_negative_fees_anywhere(batch):
    b, _ = batch
    for record in b.records:
        for line in record.settlement_lines:
            assert line.fee >= 0 and line.tax >= 0


def test_debit_ticket_bands_respect_the_rbi_boundary(batch):
    """
    Generated debit amounts must fall on the side of Rs 2,000 their instrument
    implies, or a "clean" record would silently be charged the wrong cap.
    """
    b, _ = batch
    for record in b.records:
        expected = compute_expected_fee(record.payment, RC)
        if expected.instrument_key == "debit_card_low":
            assert record.payment.amount <= 200_000
        if expected.instrument_key == "debit_card_high":
            assert record.payment.amount > 200_000


# --- scaling to other batch sizes ---------------------------------------

def test_scale_mix_preserves_the_total():
    for n in (12, 20, 60, 100, 500):
        assert sum(scale_mix(n).values()) == n


def test_scale_mix_always_keeps_one_of_every_anomaly():
    """A batch with no TDS case cannot tell you whether rule 10 works."""
    for n in (12, 20, 60, 500):
        counts = scale_mix(n)
        assert set(counts) == set(CANONICAL_MIX)
        assert all(v >= 1 for v in counts.values())


def test_scale_mix_rejects_batches_too_small_to_measure():
    with pytest.raises(ValueError):
        scale_mix(5)


def test_generator_works_at_the_track_minimum_of_fifty():
    """Track 04 asks for 50+ records. Prove it, do not assume it."""
    b, gt = generate_batch(50)
    assert len(b.records) == 50
    assert set(gt.values()) == set(canonical_truth_mix())


# --- the guard that caught a real bug -----------------------------------

def test_every_planted_anomaly_is_detectable_across_many_seeds():
    """
    A miniature fuzz, kept in the suite on purpose.

    This found a live bug: GST_MISMATCH was being planted as "12% instead of
    18%" on tickets so small that the resulting gap was eight paise - under the
    Rs 1 tolerance floor. An anomaly nobody could detect would have counted
    against the agent in the recall number for no reason.

    An anomaly that cannot be found is not an anomaly, it is a rigged exam
    question. Every seed, every planted error, has to clear the tolerance band.
    """
    for seed in range(1, 41):
        b, gt = generate_batch(60, seed=seed)
        for record in b.records:
            code = gt[record.record_id]
            if code in ("CLEAN", "ROUNDING", "REFUND_MDR_RETAINED",
                        "MISSING_FROM_SETTLEMENT", "PERIOD_BOUNDARY",
                        "TDS_CODE_MISMATCH", "INSTRUMENT_MISLABEL",
                        "UNEXPLAINED"):
                continue  # detectable by other means, or correctly not detectable
            expected = compute_expected_fee(record.payment, RC)
            fee_gap = abs(_fee_delta(record))
            tax_gap = abs(_tax_delta(record))
            assert (fee_gap > tolerance_paise(expected.total_fee_paise, RC)
                    or tax_gap > tolerance_paise(expected.gst_paise, RC)), (
                f"seed {seed}: {code} on {record.record_id} is invisible "
                f"(fee gap {fee_gap}p, tax gap {tax_gap}p)")


def test_clean_records_stay_clean_across_many_seeds():
    """The mirror image: nothing unplanted may drift out of tolerance."""
    for seed in range(1, 41):
        b, gt = generate_batch(60, seed=seed)
        for record in b.records:
            if gt[record.record_id] != "CLEAN":
                continue
            assert _fee_delta(record) == 0 and _tax_delta(record) == 0, (
                f"seed {seed}: a CLEAN record drifted - the false-accusation "
                f"rate would be measuring the generator, not the agent")


# --- the two cases that test the agent rather than the rules ------------

def test_the_ambiguous_case_fires_two_signals_that_disagree(batch):
    """
    A refunded order that was ALSO overcharged.

    Both rules genuinely apply. Rule 8 says a fee retained on a refund is
    expected behaviour and should be dismissed; rules 1/2 say this particular
    fee was never chargeable at all. The dismissal is the comfortable reading
    and the wrong one - if a refund excused any fee on the record, the cheapest
    way to hide an overcharge would be to refund the order.
    """
    b, gt = batch
    from engine.detector import detect

    candidates = [r for r in b.records
                  if r.refund is not None and gt[r.record_id] == "ZERO_MDR_VIOLATION"]
    assert candidates, "no ambiguous refund-plus-overcharge record was planted"

    for record in candidates:
        v = detect(record, RC, as_of=2_000_000_000)
        kinds = {s.kind for s in v.signals}
        assert kinds == {"FEE_RETAINED_ON_REFUND", "ZERO_MDR_RAIL_OVERCHARGED"}
        assert v.needs_agent, "the calculator must not settle a contested record"
        assert v.exception_code is None


def test_the_unexplained_case_matches_no_rule_at_all(batch):
    """
    An adjustment nobody can account for.

    CLAUDE.md section 6.1: you cannot write a rule for the unknown. The only
    correct answer is that it cannot be explained - and inventing a plausible
    explanation is the failure mode, not a near miss.
    """
    b, gt = batch
    from engine.detector import detect

    unexplained = [r for r in b.records if gt[r.record_id] == "UNEXPLAINED"]
    assert unexplained, "no unexplainable record was planted"

    for record in unexplained:
        adjustments = [ln for ln in record.settlement_lines
                       if ln.type not in ("payment", "refund")]
        assert adjustments, "the record carries no adjustment line"
        assert all(ln.fee == 0 and ln.tax == 0 for ln in adjustments)
        assert all(ln.amount < 0 for ln in adjustments), "an adjustment that takes money"

        v = detect(record, RC, as_of=2_000_000_000)
        assert _fee_delta(record) == 0, "the payment fee itself is correct"
        kinds = {s.kind for s in v.signals}
        assert kinds == {"UNRECOGNISED_ADJUSTMENT"}
        assert v.needs_agent


def test_the_unexplained_adjustment_is_not_a_round_number(batch):
    """
    Deliberately awkward. A round number, or a clean fraction of the sale,
    would hand the agent a false explanation to latch onto - and the whole
    point of this record is that no explanation is available.
    """
    b, gt = batch
    for record in [r for r in b.records if gt[r.record_id] == "UNEXPLAINED"]:
        for ln in record.settlement_lines:
            if ln.type in ("payment", "refund"):
                continue
            assert abs(ln.amount) % 100 != 0, "a whole-rupee adjustment invites a guess"
            assert abs(ln.amount) != record.payment.amount


def test_the_adjustment_still_ties_out_to_the_bank(batch):
    """
    An unexplained deduction must still reconcile at the net level. That is the
    whole argument: the money balances perfectly and is still wrong.
    """
    b, gt = batch
    per_utr: dict[str, int] = {}
    for record in b.records:
        for line in record.settlement_lines:
            per_utr[line.utr] = per_utr.get(line.utr, 0) + line.amount - line.fee - line.tax
    for credit in b.bank_credits:
        assert credit.amount == per_utr[credit.utr]


def test_both_new_cases_appear_in_every_seed():
    """A judgement test that only shows up sometimes is not a test."""
    for seed in (1, 7, 20260905, 44, 99):
        _, gt = generate_batch(60, seed=seed)
        codes = list(gt.values())
        assert codes.count("UNEXPLAINED") == 1, f"seed {seed}"
        assert codes.count("ZERO_MDR_VIOLATION") == 4, f"seed {seed}"
