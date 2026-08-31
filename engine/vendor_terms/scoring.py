"""
Scoring the classified line items against the generator's answer key.

Produces the SAME Scorecard engine/scoring.py's settlement agent and
engine/gst_filing/scoring.py's outward-tax controller produce, on purpose
- one shape means an accuracy page never has to wonder whether two agents'
numbers mean different things.

Every record here is `by_calculator`: classification is fully mechanical
(taxonomy.py's own docstring, and detector.py's module docstring) - the
agent is only ever asked, once per supplier, whether an already-confirmed
overbilled batch is worth disputing. That is not a classification with a
right/wrong code to score against a per-line answer key, so
`by_agent`/`by_agent_correct` stay at zero here honestly, the same
reasoning engine/gst_filing/scoring.py gives for its own layers 1 and 2.
"""

from __future__ import annotations

from engine.vendor_terms.taxonomy import TermsCode
from engine.scoring import Scorecard

DO_NOTHING = {str(TermsCode.RATE_CLEAN), str(TermsCode.RATE_UNCONFIGURED)}


def score_classification(classified: list, ground_truth: dict[str, str]
                         ) -> Scorecard:
    card = Scorecard(total=len(classified))

    for item in classified:
        truth = ground_truth[item.line_item_id]
        got = item.code
        bucket = card.by_code.setdefault(truth, {"n": 0, "correct": 0})
        bucket["n"] += 1
        got_it_right = got == truth
        if got_it_right:
            card.correct += 1
            bucket["correct"] += 1

        card.by_calculator += 1
        card.by_calculator_correct += int(got_it_right)
        card.auto_resolved += 1          # classification has no human queue

        if truth in DO_NOTHING:
            card.clean += 1
            if got_it_right:
                card.clean_correct += 1
            elif got not in DO_NOTHING:
                card.false_accusations.append((item.line_item_id, truth, got))
        else:
            card.anomalies += 1
            if got_it_right:
                card.anomalies_caught += 1
                card.recoverable_paise += item.money_at_stake_paise
            elif got in DO_NOTHING:
                card.anomalies_missed += 1
                card.misses.append((item.line_item_id, truth, got))
            else:
                card.anomalies_flagged_wrong_code += 1
                card.miscategorised.append((item.line_item_id, truth, got))

    return card
