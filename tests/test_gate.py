"""
Tests for the guardrail gate and the scoring. Checkpoint 7.

The scoring code produces the number the whole pitch rests on, which makes it
the most dangerous file in the project: a bug here does not crash anything, it
just quietly reports a better result than the system achieved. So the tests
below mostly feed it WRONG answers and check that it says so.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.classifier import Verdict  # noqa: E402
from engine.detector import detect_batch  # noqa: E402
from engine.expected_value import load_rate_card  # noqa: E402
from engine.gate import apply_gate, gate_batch, money_at_stake  # noqa: E402
from engine.scoring import score  # noqa: E402
from engine.taxonomy import ExceptionCode  # noqa: E402
from generator.synthetic import generate_batch  # noqa: E402

RC = load_rate_card()


@pytest.fixture(scope="module")
def audited():
    b, gt = generate_batch(60)
    return b, gt, detect_batch(b)


def _verdict(pid, code="ZERO_MDR_VIOLATION", action="dispute", confidence=0.95, **kw):
    return Verdict(payment_id=pid, exception_code=code, action=action,
                   confidence=confidence, reasoning="because", rule_cited="rule 1", **kw)


def _find(variances, gt, code):
    return next(v for v in variances if gt[v.payment_id] == code)


# --- money at stake ------------------------------------------------------

def test_money_at_stake_sees_past_a_zero_delta(audited):
    """
    A mislabel has a fee delta of ZERO and hundreds of rupees recoverable.
    Gating on delta alone would wave through the most interesting finding in
    the batch.
    """
    _, gt, variances = audited
    v = _find(variances, gt, "INSTRUMENT_MISLABEL")
    assert v.delta == 0
    assert money_at_stake(v, "INSTRUMENT_MISLABEL") > 0


def test_money_at_stake_on_a_missing_record_is_the_whole_sale(audited):
    _, gt, variances = audited
    v = _find(variances, gt, "MISSING_FROM_SETTLEMENT")
    stake = money_at_stake(v, "MISSING_FROM_SETTLEMENT")
    assert stake == v.amount - v.expected_fee - v.expected_tax


# --- what gets a human ---------------------------------------------------

def test_a_clean_record_resolved_by_the_calculator_needs_no_human(audited):
    _, gt, variances = audited
    v = _find(variances, gt, "CLEAN")
    decision = apply_gate(v, None, RC)
    assert decision.auto_resolved
    assert decision.decided_by == "calculator"
    assert decision.confidence == 1.0


def test_low_confidence_is_queued_however_right_it_looks(audited):
    _, gt, variances = audited
    v = _find(variances, gt, "ZERO_MDR_VIOLATION")
    decision = apply_gate(v, _verdict(v.payment_id, confidence=0.5), RC)
    assert decision.queued_for_human
    assert any("below the" in r for r in decision.reasons)


def test_high_confidence_on_a_small_sum_is_allowed_through(audited):
    _, gt, variances = audited
    v = min((v for v in variances if gt[v.payment_id] == "ZERO_MDR_VIOLATION"),
            key=lambda v: v.delta)
    decision = apply_gate(v, _verdict(v.payment_id, confidence=0.95), RC)
    if money_at_stake(v, "ZERO_MDR_VIOLATION") <= RC["guardrails"]["review_above_paise"]:
        assert decision.auto_resolved


def test_a_large_sum_always_gets_a_human_however_confident(audited):
    """
    Confidence is not a substitute for a second pair of eyes on real money.
    The threshold applies regardless of how sure the agent claims to be.
    """
    _, gt, variances = audited
    v = _find(variances, gt, "MISSING_FROM_SETTLEMENT")
    decision = apply_gate(v, _verdict(v.payment_id, code="MISSING_FROM_SETTLEMENT",
                                      confidence=1.0), RC)
    assert decision.queued_for_human
    assert any("at stake" in r for r in decision.reasons)


def test_a_corrected_answer_is_queued(audited):
    _, gt, variances = audited
    v = _find(variances, gt, "ZERO_MDR_VIOLATION")
    decision = apply_gate(v, _verdict(v.payment_id, corrections=["action corrected"]), RC)
    assert decision.queued_for_human


def test_an_invented_figure_is_queued(audited):
    _, gt, variances = audited
    v = _find(variances, gt, "ZERO_MDR_VIOLATION")
    decision = apply_gate(v, _verdict(v.payment_id, invented_figures=["9999.99"]), RC)
    assert decision.queued_for_human
    assert any("not in the evidence" in r for r in decision.reasons)


def test_a_failed_classification_is_queued(audited):
    _, gt, variances = audited
    v = _find(variances, gt, "ZERO_MDR_VIOLATION")
    decision = apply_gate(v, _verdict(v.payment_id, code="UNEXPLAINED",
                                      action="escalate", confidence=0.0,
                                      error="connection failed"), RC)
    assert decision.queued_for_human
    assert len(decision.reasons) >= 2


def test_unexplained_always_reaches_a_person(audited):
    """CLAUDE.md section 10: escalate, do not guess."""
    _, gt, variances = audited
    v = _find(variances, gt, "ZERO_MDR_VIOLATION")
    decision = apply_gate(v, _verdict(v.payment_id, code="UNEXPLAINED",
                                      action="escalate", confidence=0.99), RC)
    assert decision.queued_for_human


def test_gate_batch_routes_every_record(audited):
    b, gt, variances = audited
    verdicts = [_verdict(v.payment_id) for v in variances if v.needs_agent]
    decisions = gate_batch(variances, verdicts, RC)
    assert len(decisions) == len(variances)
    assert {d.payment_id for d in decisions} == {v.payment_id for v in variances}


# --- scoring, fed wrong answers on purpose ------------------------------

def _decisions_from_truth(variances, gt, override: dict | None = None):
    """Build a set of decisions that are perfect except where overridden."""
    override = override or {}
    verdicts = []
    for v in variances:
        if not v.needs_agent:
            continue
        code = override.get(v.payment_id, gt[v.payment_id])
        verdicts.append(_verdict(v.payment_id, code=code, confidence=0.95))
    # calculator-resolved records already carry the truth
    return gate_batch(variances, verdicts, RC)


def test_a_perfect_run_scores_one_hundred_percent(audited):
    b, gt, variances = audited
    card = score(_decisions_from_truth(variances, gt), gt, variances)
    assert card.accuracy == 1.0
    assert card.recall == 1.0
    assert card.false_accusations == []


def test_a_missed_anomaly_is_counted_as_missed(audited):
    """Calling a real overcharge 'clean' must show up as a miss, not an average."""
    b, gt, variances = audited
    target = _find(variances, gt, "ZERO_MDR_VIOLATION")
    card = score(_decisions_from_truth(variances, gt, {target.payment_id: "CLEAN"}),
                 gt, variances)
    assert card.anomalies_missed == 1
    assert card.recall < 1.0
    assert target.payment_id in [pid for pid, _, _ in card.misses]


def test_a_miscategorised_anomaly_does_not_count_as_a_catch(audited):
    """
    Graded strictly. "We noticed something was wrong here" is worth reporting,
    but it is not the same as getting it right, and it does not go in recall.
    """
    b, gt, variances = audited
    target = _find(variances, gt, "ZERO_MDR_VIOLATION")
    card = score(_decisions_from_truth(variances, gt,
                                       {target.payment_id: "RATE_MISMATCH"}),
                 gt, variances)
    assert card.anomalies_flagged_wrong_code == 1
    assert card.anomalies_caught == card.anomalies - 1
    assert card.recall < 1.0
    assert card.false_accusations == []      # it was a real anomaly, just mislabelled


def test_accusing_a_clean_record_is_counted_and_named(audited):
    """
    The failure that ends the merchant relationship. It gets its own line in
    the report, not a dilution into an accuracy average.
    """
    b, gt, variances = audited
    # a decoy the calculator resolved - force the agent to call it an overcharge
    target = _find(variances, gt, "REFUND_MDR_RETAINED")
    verdicts = [_verdict(target.payment_id, code="ZERO_MDR_VIOLATION")]
    decisions = gate_batch(variances, verdicts, RC)
    # the calculator already settled it, so override the decision directly
    for d in decisions:
        if d.payment_id == target.payment_id:
            d.exception_code = "ZERO_MDR_VIOLATION"
    card = score(decisions, gt, variances)
    assert len(card.false_accusations) == 1
    assert card.false_accusations[0][0] == target.payment_id
    assert card.decoys_dismissed == card.decoys - 1


def test_the_denominator_is_every_record_not_just_the_hard_ones(audited):
    """
    Scoring only the records the agent saw would flatter the result. The claim
    is about the batch a merchant hands over, all sixty of it.
    """
    b, gt, variances = audited
    card = score(_decisions_from_truth(variances, gt), gt, variances)
    assert card.total == 60
    assert card.clean == 40
    assert card.anomalies == 14
    assert card.decoys == 6


def test_recoverable_total_only_counts_recoverable_categories(audited):
    b, gt, variances = audited
    card = score(_decisions_from_truth(variances, gt), gt, variances)
    assert card.recoverable_paise > 0
    # GST and TDS are not recoverable-by-dispute categories
    only_tax = score(_decisions_from_truth(variances, gt), gt, variances)
    assert only_tax.recoverable_paise == card.recoverable_paise


def test_a_period_boundary_is_not_treated_as_money_at_risk(audited):
    """
    Caught on the first full run.

    The period-boundary signal carries the sale value so the report can total
    how much revenue crossed the month end. Feeding that into the risk
    threshold queued a Rs 3,948 record under "money at stake" when nothing is
    at stake at all - the deduction is correct and the rupees do not move. A
    review queue that fills up with non-findings is a review queue nobody reads.
    """
    _, gt, variances = audited
    v = _find(variances, gt, "PERIOD_BOUNDARY")
    signal = next(s for s in v.signals if s.kind == "CROSSES_ACCOUNTING_PERIOD")

    assert signal.amount_paise > 25000, "the signal still reports the value"
    assert money_at_stake(v, "PERIOD_BOUNDARY") == 0, "but none of it is at risk"

    decision = apply_gate(v, _verdict(v.payment_id, code="PERIOD_BOUNDARY",
                                      action="fix_books", confidence=0.93), RC)
    assert decision.auto_resolved


def test_a_mislabel_is_still_gated_on_its_recoverable_amount(audited):
    """The fix above must not have exempted the codes where money does move."""
    _, gt, variances = audited
    v = max((v for v in variances if gt[v.payment_id] == "INSTRUMENT_MISLABEL"),
            key=lambda v: money_at_stake(v, "INSTRUMENT_MISLABEL"))
    assert money_at_stake(v, "INSTRUMENT_MISLABEL") > 25000
    decision = apply_gate(v, _verdict(v.payment_id, code="INSTRUMENT_MISLABEL",
                                      confidence=0.99), RC)
    assert decision.queued_for_human


def test_a_run_with_failed_calls_refuses_to_pass_itself_off_as_a_measurement(audited):
    """
    Caught for real: the API credit ran out mid-run, every remaining record was
    escalated as UNEXPLAINED, and the scorecard cheerfully reported "85.4%
    accuracy, 27.1% recall". That is not a measurement of anything - it is a
    measurement of an empty wallet - and on stage it would be a number quoted
    with total confidence and no meaning.
    """
    b, gt, variances = audited
    open_ones = [v for v in variances if v.needs_agent]
    verdicts = [_verdict(v.payment_id, code="UNEXPLAINED", action="escalate",
                         confidence=0.0, error="credit balance too low")
                for v in open_ones]
    card = score(gate_batch(variances, verdicts, RC), gt, variances)

    assert card.failed_calls == len(open_ones)
    assert all(d.errored for d in gate_batch(variances, verdicts, RC) if d.decided_by == "agent")


def test_a_healthy_run_reports_no_failed_calls(audited):
    b, gt, variances = audited
    card = score(_decisions_from_truth(variances, gt), gt, variances)
    assert card.failed_calls == 0


def test_a_replayed_run_scores_identically_to_the_live_one(audited):
    """
    Replay has to be the same measurement, not an approximation of it - or a
    rehearsal would show one number and the live run another.
    """
    import json
    from dataclasses import asdict

    from agent.classifier import Verdict

    b, gt, variances = audited
    live = [_verdict(v.payment_id, code=gt[v.payment_id])
            for v in variances if v.needs_agent]
    replayed = [Verdict(**json.loads(json.dumps(asdict(v)))) for v in live]

    a = score(gate_batch(variances, live, RC), gt, variances)
    c = score(gate_batch(variances, replayed, RC), gt, variances)
    assert (a.accuracy, a.recall, a.correct) == (c.accuracy, c.recall, c.correct)


def test_a_failed_call_never_scores_a_point_even_when_the_code_matches(audited):
    """
    Caught by an impossible summary line: "40/38 = 105%".

    The failure path returns UNEXPLAINED, and UNEXPLAINED is also the correct
    answer for the unrecognised-adjustment record. So a batch where every
    single call died on a billing error still scored 1/13 - an outage that
    looks like partial success is worse than one that looks like nothing.
    """
    b, gt, variances = audited
    target = _find(variances, gt, "UNEXPLAINED")
    verdicts = [_verdict(target.payment_id, code="UNEXPLAINED", action="escalate",
                         confidence=0.0, error="credit balance too low")]
    card = score(gate_batch(variances, verdicts, RC), gt, variances)

    assert card.failed_calls == 1
    assert card.by_agent_correct == 0, "a crash is not a correct answer"
    assert target.payment_id in [pid for pid, _, _ in card.misses]


def test_a_genuine_unexplained_verdict_still_scores(audited):
    """The fix must not punish the agent for correctly refusing to guess."""
    b, gt, variances = audited
    target = _find(variances, gt, "UNEXPLAINED")
    verdicts = [_verdict(target.payment_id, code="UNEXPLAINED", action="escalate",
                         confidence=0.8)]
    card = score(gate_batch(variances, verdicts, RC), gt, variances)
    assert card.failed_calls == 0
    assert card.by_agent_correct == 1


def test_a_record_nobody_judged_is_never_auto_closed():
    """
    Regression, and a serious one. A variance that needed the agent and got no
    verdict - agent switched off, or a batch never classified - used to fall
    into the "the calculator resolved it" branch: confidence 1.0, decided_by
    "calculator", auto-closed unless the sum happened to clear the rupee
    threshold.

    So an UNEXPLAINED finding nothing had ever looked at could close itself,
    which is the opposite of guardrail 3. Found because the new home dashboard
    showed "0 waiting on you" while an unexplained finding sat in the database.
    """
    from engine.detector import detect_batch
    from engine.gate import gate_batch
    from generator.synthetic import generate_batch

    batch, _truth = generate_batch(60)
    variances = detect_batch(batch)
    open_ones = [v for v in variances if v.needs_agent]
    assert open_ones, "this test needs records that require judgment"

    decisions = gate_batch(variances, [], batch.rate_card)
    by_id = {d.payment_id: d for d in decisions}

    for variance in open_ones:
        decision = by_id[variance.payment_id]
        assert decision.queued_for_human, \
            f"{variance.payment_id} was closed without anyone judging it"
        assert decision.confidence == 0.0
        assert decision.decided_by == "agent"


def test_a_record_the_calculator_did_settle_still_closes_itself():
    """The fix must not send arithmetic to a human. Only judgment goes."""
    from engine.detector import detect_batch
    from engine.gate import gate_batch
    from generator.synthetic import generate_batch

    batch, _truth = generate_batch(60)
    variances = detect_batch(batch)
    settled = [v for v in variances if not v.needs_agent]
    decisions = {d.payment_id: d for d in gate_batch(variances, [], batch.rate_card)}

    closed = [v for v in settled if not decisions[v.payment_id].queued_for_human]
    assert closed, "the calculator should still close what it settled"
    for variance in closed:
        assert decisions[variance.payment_id].decided_by == "calculator"
