"""
Scoring layers 1 and 2 against the generator's answer key.

Produces the SAME Scorecard engine/scoring.py's settlement agent and
engine/gst/scoring.py's ITC reconciler produce, on purpose - see
engine/gst/scoring.py's own docstring for why that matters: one shape means
an accuracy page or a benchmark table never has to wonder whether two
agents' numbers mean different things.

## Why only layers 1 and 2 get scored

Layers 1 and 2 classify PER RECORD (an invoice, a filing period) against a
taxonomy with a real "wrong answer" - exactly the shape score() already
exists for elsewhere in this codebase. Layers 3 and 4 do not: allocate()'s
cash-needed split and the QRMP method choice are each a single computation
per run, not a classification among several codes with a planted decoy to
catch or miss - there is nothing an answer key would be checking beyond
"is the arithmetic right", which the unit tests in
tests/test_gst_filing_offset.py and tests/test_gst_filing_qrmp.py already
do directly. Scoring them here would manufacture a percentage with nothing
uncertain behind it.

## Why every record here is `by_calculator`, and that is not a bug

Unlike engine/scoring.py's settlement agent or engine/gst/scoring.py's ITC
reconciler, no code produced by layers 1 or 2 is ever decided by a model -
CLAUDE.md section 2, and taxonomy.py's own docstring: layer 1 is a pure
rule, and layer 2's agent may only reorder which open period to file first,
never touch the exception_code. So `by_agent`/`by_agent_correct` stay at
zero for both scorecards produced here, honestly, rather than forcing a
split that does not exist onto a shape built for agents where it does.
"""

from __future__ import annotations

from engine.gst_filing.taxonomy import CorrectionCode, GSTR1Code
from engine.scoring import Scorecard

DO_NOTHING_L1 = {str(GSTR1Code.CLASSIFIED)}
DO_NOTHING_L2 = {str(CorrectionCode.PERIOD_CLEAN)}


def score_classification(classified: list, ground_truth: dict[str, str]
                         ) -> Scorecard:
    """Layer 1: every classified invoice against generate_invoices()'s own
    answer key."""
    card = Scorecard(total=len(classified))

    for inv in classified:
        truth = ground_truth[inv.invoice_id]
        got = inv.code
        _tally(card, truth, got, inv.invoice_id, do_nothing=DO_NOTHING_L1)
        card.by_calculator += 1
        card.by_calculator_correct += int(got == truth)
        card.auto_resolved += 1          # layer 1 has no human queue at all

    return card


def score_corrections(findings: list, decisions: dict, ground_truth: dict[str, str]
                      ) -> Scorecard:
    """Layer 2: every period's finding against generate_cycles()'s own
    answer key. `decisions` maps period -> engine.gst_filing.gate's
    CorrectionDecision, read only for decided_by/queued_for_human - the
    exception_code being scored always comes from the finding itself."""
    card = Scorecard(total=len(findings))

    for f in findings:
        truth = ground_truth[f.period]
        got = f.exception_code
        d = decisions.get(f.period)
        _tally(card, truth, got, f.period, do_nothing=DO_NOTHING_L2)

        if d is not None and d.decided_by == "agent":
            card.by_agent += 1
            card.by_agent_correct += int(got == truth)
        else:
            card.by_calculator += 1
            card.by_calculator_correct += int(got == truth)

        if d is not None and d.queued_for_human:
            card.queued_for_human += 1
        else:
            card.auto_resolved += 1

    return card


def _tally(card: Scorecard, truth: str, got: str, record_id: str, *,
          do_nothing: set[str]) -> None:
    """The bucket-counting and clean/anomaly split every score() function
    in this codebase repeats - factored out here since both layers score
    against exactly the same three-way shape (clean / anomaly-caught /
    anomaly-missed-or-miscategorised), just with different codes."""
    bucket = card.by_code.setdefault(truth, {"n": 0, "correct": 0})
    bucket["n"] += 1
    got_it_right = got == truth
    if got_it_right:
        card.correct += 1
        bucket["correct"] += 1

    if truth in do_nothing:
        card.clean += 1
        if got_it_right:
            card.clean_correct += 1
        elif got not in do_nothing:
            # Called a clean record an exception - the failure this
            # project treats most seriously, tracked on its own.
            card.false_accusations.append((record_id, truth, got))
    else:
        card.anomalies += 1
        if got_it_right:
            card.anomalies_caught += 1
        elif got in do_nothing:
            card.anomalies_missed += 1
            card.misses.append((record_id, truth, got))
        else:
            card.anomalies_flagged_wrong_code += 1
            card.miscategorised.append((record_id, truth, got))
