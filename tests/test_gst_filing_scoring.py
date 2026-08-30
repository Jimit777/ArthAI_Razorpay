"""
Tests for engine/gst_filing/scoring.py - the measured-accuracy claim
checkpoint 7 exists to make honest.
"""

import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from engine.gst_filing.classifier import classify_batch  # noqa: E402
from engine.gst_filing.gate import gate  # noqa: E402
from engine.gst_filing.generator import (DEMO_RATE_CARD,  # noqa: E402
                                         generate_cycles, generate_invoices)
from engine.gst_filing.scoring import (score_classification,  # noqa: E402
                                       score_corrections)
from engine.gst_filing.timing import detect_cycles  # noqa: E402

TODAY = date(2026, 8, 24)


# --- layer 1 ----------------------------------------------------------------

def test_the_planted_batch_scores_perfectly_against_its_own_answer_key():
    """The checkpoint's own measured claim: everything the generator
    planted, the classifier is expected to get right - since both sides
    are the same deterministic rules, a real mismatch here is a bug, not
    noise."""
    invoices, truth = generate_invoices(40)
    classified = classify_batch(invoices, home_state="27",
                                rate_card=DEMO_RATE_CARD,
                                e_invoicing_applicable=True)
    card = score_classification(classified, truth)

    assert card.total == 40
    assert card.correct == 40
    assert card.accuracy == 1.0
    assert card.anomalies_caught == card.anomalies
    assert not card.false_accusations
    assert not card.misses
    assert not card.miscategorised


def test_layer_1_is_entirely_calculator_decided():
    """No code layer 1 produces is ever agent-decided - see
    scoring.py's own docstring for why by_agent stays at zero."""
    invoices, truth = generate_invoices(40)
    classified = classify_batch(invoices, home_state="27",
                                rate_card=DEMO_RATE_CARD,
                                e_invoicing_applicable=True)
    card = score_classification(classified, truth)
    assert card.by_calculator == 40
    assert card.by_agent == 0
    assert card.auto_resolved == 40
    assert card.queued_for_human == 0


def test_a_miscategorised_invoice_is_caught_not_averaged_away():
    """Feed the scorer a wrong answer directly - not something the real
    classifier would produce, but the scorer itself must not paper over
    it."""
    invoices, truth = generate_invoices(4)
    classified = classify_batch(invoices, home_state="27",
                                rate_card=DEMO_RATE_CARD,
                                e_invoicing_applicable=True)
    classified[0].code = "HSN_RATE_UNCONFIGURED"  # deliberately corrupted
    truth[classified[0].invoice_id] = "IRN_MISSING"

    card = score_classification(classified, truth)
    assert card.correct == 3
    assert card.miscategorised == [
        (classified[0].invoice_id, "IRN_MISSING", "HSN_RATE_UNCONFIGURED")]


# --- layer 2 ------------------------------------------------------------

def test_the_planted_cycles_score_perfectly_against_their_own_answer_key():
    cycles, truth = generate_cycles("2026-08", 31_776_878)
    findings = detect_cycles(cycles, today=TODAY)
    decisions = {f.period: gate(f) for f in findings}

    card = score_corrections(findings, decisions, truth)
    assert card.total == 5
    assert card.correct == 5
    assert card.accuracy == 1.0
    assert not card.false_accusations
    assert not card.misses


def test_layer_2_exception_code_is_calculator_decided_even_with_a_priority():
    """A period's exception_code stays whatever timing.py computed
    regardless of whether an agent priority was layered on top - so it
    counts as by_calculator for scoring, exactly like the un-agented case."""
    cycles, truth = generate_cycles("2026-08", 31_776_878)
    findings = detect_cycles(cycles, today=TODAY)

    class FakePriority:
        confidence = 0.9
        reasoning = "ok"
        error = None
        invented_figures = []

    open_period = next(f for f in findings
                       if f.exception_code == "CORRECTABLE_VIA_1A")
    decisions = {f.period: gate(f, FakePriority() if f is open_period else None)
                for f in findings}

    card = score_corrections(findings, decisions, truth)
    assert card.correct == 5
    # the one period an agent priority touched is attributed to by_agent,
    # the rest stay by_calculator - the split is about who decided the
    # PRIORITY, not who decided the exception_code (nobody but the engine
    # ever decides that)
    assert card.by_agent == 1
    assert card.by_calculator == 4


def test_a_missed_anomaly_shows_up_as_a_miss_not_a_silent_average():
    cycles, truth = generate_cycles("2026-08", 31_776_878)
    findings = detect_cycles(cycles, today=TODAY)
    locked = next(f for f in findings
                 if f.exception_code == "LOCKED_NEEDS_DRC03")
    locked.exception_code = "PERIOD_CLEAN"      # deliberately corrupted

    decisions = {f.period: gate(f) for f in findings}
    card = score_corrections(findings, decisions, truth)
    assert card.correct == 4
    assert (locked.period, truth[locked.period], "PERIOD_CLEAN") in card.misses
