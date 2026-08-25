"""
Tests for dispute-text generation. Checkpoint 8.

The bar for this checkpoint is "paste-ready into a support ticket". That means
two separate things and both are tested: the message has to be complete enough
to send without editing, and every figure in it has to be one we computed.

A merchant who sends their gateway a claim containing an invented number does
not just lose that claim. They lose the next one too. CLAUDE.md 1.5 puts the
dispute window at 60-180 days; credibility spent early does not come back
inside it.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.classifier import Verdict, review, unverified_figures  # noqa: E402
from agent.dispute import (  # noqa: E402
    SENDABLE,
    attach_disputes,
    build_dispute,
    reference_block,
    subject_line,
)
from agent.prompt import render_variance  # noqa: E402
from engine.detector import detect_batch  # noqa: E402
from engine.expected_value import load_rate_card, rupees  # noqa: E402
from engine.gate import gate_batch, money_at_stake  # noqa: E402
from engine.taxonomy import ExceptionCode  # noqa: E402
from generator.synthetic import generate_batch  # noqa: E402

RC = load_rate_card()


@pytest.fixture(scope="module")
def audited():
    b, gt = generate_batch(60)
    return b, gt, detect_batch(b)


def _find(variances, gt, code):
    return next(v for v in variances if gt[v.payment_id] == code and v.needs_agent)


def _verdict(v, code="ZERO_MDR_VIOLATION", action="dispute", text=None, **kw):
    return Verdict(payment_id=v.payment_id, exception_code=code, action=action,
                   confidence=0.95, reasoning="because", rule_cited="rule 1",
                   dispute_text=text, **kw)


# --- what makes it sendable ---------------------------------------------

def test_the_message_carries_everything_needed_to_find_the_transaction(audited):
    """
    A support agent who has to reply asking "which payment?" has turned a
    two-day correction into a two-week one.
    """
    b, gt, variances = audited
    v = _find(variances, gt, "ZERO_MDR_VIOLATION")
    msg = build_dispute(v, _verdict(v, text="Please review this deduction."),
                        money_at_stake(v, "ZERO_MDR_VIOLATION"))

    assert v.payment_id in msg
    assert v.order_id in msg
    assert v.raw["settlement_id"] in msg
    assert v.raw["utr"] in msg
    assert "Subject:" in msg
    assert rupees(v.actual_fee) in msg
    assert rupees(v.expected_fee) in msg


def test_the_message_states_the_provision_it_relies_on(audited):
    """
    A claim with no citation is an opinion. CLAUDE.md guardrail 2 and the
    reason a statute beats a contract term in an argument with a gateway.
    """
    b, gt, variances = audited
    v = _find(variances, gt, "ZERO_MDR_VIOLATION")
    msg = build_dispute(v, _verdict(v, text="x"), money_at_stake(v, "ZERO_MDR_VIOLATION"))
    assert "Basis" in msg
    assert "PSS Act" in msg


def test_the_subject_line_says_what_it_is_about(audited):
    b, gt, variances = audited
    for code in ("ZERO_MDR_VIOLATION", "RATE_MISMATCH", "GST_MISMATCH"):
        line = subject_line(code, "pay_x")
        assert "pay_x" in line
        assert len(line) > 20
        assert code not in line, "the gateway does not know our taxonomy codes"


def test_a_missing_record_says_so_instead_of_showing_a_zero_fee(audited):
    """
    Reporting "Fee charged: Rs 0.00" on a payment that never settled would be
    technically true and completely misleading.
    """
    b, gt, variances = audited
    v = next(x for x in variances if not x.settlement_present)
    block = reference_block(v, "MISSING_FROM_SETTLEMENT",
                            money_at_stake(v, "MISSING_FROM_SETTLEMENT"))
    assert "No settlement line exists" in block
    assert "Fee charged" not in block


# --- when there is nothing to send --------------------------------------

def test_nothing_is_generated_for_a_dismissal(audited):
    """A dismissal has no recipient. Rule 8 is not something you send anyone."""
    b, gt, variances = audited
    v = _find(variances, gt, "ZERO_MDR_VIOLATION")
    assert build_dispute(v, _verdict(v, code="REFUND_MDR_RETAINED", action="dismiss",
                                     text="please refund this"), 100) is None


def test_nothing_is_generated_for_an_escalation(audited):
    """An escalation goes to a colleague, not to the gateway."""
    b, gt, variances = audited
    v = _find(variances, gt, "ZERO_MDR_VIOLATION")
    assert build_dispute(v, _verdict(v, code="UNEXPLAINED", action="escalate",
                                     text="we don't know"), 100) is None


def test_only_actionable_codes_are_sendable():
    assert SENDABLE == {"dispute", "fix_books"}
    assert "dismiss" not in SENDABLE and "escalate" not in SENDABLE


def test_an_absent_paragraph_produces_no_message(audited):
    """No prose, no message. We do not paper over a missing answer."""
    b, gt, variances = audited
    v = _find(variances, gt, "ZERO_MDR_VIOLATION")
    assert build_dispute(v, _verdict(v, text=None), 100) is None


# --- the figures ---------------------------------------------------------

def test_the_figure_check_covers_the_dispute_text_too(audited):
    """
    The reasoning is read by the merchant. The dispute text is read by their
    gateway. If only one of them were checked, it should be this one.
    """
    from agent.classifier import Classification

    b, gt, variances = audited
    v = _find(variances, gt, "ZERO_MDR_VIOLATION")
    answer = Classification(
        exception_code="ZERO_MDR_VIOLATION", action="dispute", confidence=0.95,
        reasoning="A network MDR was charged on a zero-MDR rail.",
        rule_cited="rule 1", evidence_used=[],
        dispute_text="Please credit the Rs 8,888.88 overcharged on this payment.")
    verdict = review(v, answer, render_variance(v))
    assert verdict.invented_figures == ["8888.88"]
    assert verdict.confidence <= 0.3


def test_a_verdict_with_invented_figures_is_never_turned_into_a_message(audited):
    """
    Belt and braces. Even if such a verdict reached this far, it must not
    become something the merchant can paste and send.
    """
    b, gt, variances = audited
    v = _find(variances, gt, "ZERO_MDR_VIOLATION")
    decisions = gate_batch(variances, [], RC)
    bad = _verdict(v, text="Please credit Rs 9,999.99.",
                   invented_figures=["9999.99"])
    assert attach_disputes(variances, [bad], decisions) == {}


def test_the_reference_block_is_not_written_by_a_model(audited):
    """
    Every figure in the block comes from stored data, so it cannot drift from
    what the database says however the prose is phrased.
    """
    b, gt, variances = audited
    v = _find(variances, gt, "RATE_MISMATCH")
    block = reference_block(v, "RATE_MISMATCH", money_at_stake(v, "RATE_MISMATCH"))
    evidence = render_variance(v)
    assert unverified_figures(block, evidence) == []


# --- across a whole run --------------------------------------------------

def test_a_message_is_produced_for_every_actionable_finding(audited):
    b, gt, variances = audited
    open_ones = [v for v in variances if v.needs_agent]
    verdicts = [_verdict(v, code=gt[v.payment_id],
                         action="dispute" if gt[v.payment_id] in
                         ("ZERO_MDR_VIOLATION", "RATE_MISMATCH",
                          "INSTRUMENT_MISLABEL") else "fix_books",
                         text="Please review this deduction.")
                for v in open_ones if gt[v.payment_id] != "UNEXPLAINED"]
    decisions = gate_batch(variances, verdicts, RC)
    messages = attach_disputes(variances, verdicts, decisions)

    assert len(messages) == len(verdicts)
    for pid, msg in messages.items():
        assert msg.startswith("Subject:")
        assert "--- Reference details ---" in msg
        assert pid in msg
