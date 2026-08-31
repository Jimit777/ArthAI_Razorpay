"""
Scoring the classified disputes against the generator's answer key.

Produces the SAME Scorecard engine/scoring.py's settlement agent and
engine/vendor_terms/scoring.py's vendor invoice auditor produce, on
purpose - one shape means an accuracy page never has to wonder whether two
agents' numbers mean different things.

Every record here is `by_calculator`: classification is fully mechanical
(taxonomy.py's own docstring, detector.py's module docstring) - the agent
is only ever asked, once per dispute with something to work with, whether
it's worth contesting. That is not a classification with a right/wrong
code to score against a per-dispute answer key, so
`by_agent`/`by_agent_correct` stay at zero here honestly, the same
reasoning engine/vendor_terms/scoring.py gives for its own agent layer.
"""

from __future__ import annotations

from engine.chargeback.taxonomy import NOT_READY, DisputeCode
from engine.scoring import Scorecard

DO_NOTHING = {str(c) for c in NOT_READY}


def score_classification(classified: list, ground_truth: dict[str, str]
                         ) -> Scorecard:
    card = Scorecard(total=len(classified))

    for d in classified:
        truth = ground_truth[d.dispute_id]
        got = d.code
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
                card.false_accusations.append((d.dispute_id, truth, got))
        else:
            card.anomalies += 1
            if got_it_right:
                card.anomalies_caught += 1
                card.recoverable_paise += d.amount_paise
            elif got in DO_NOTHING:
                card.anomalies_missed += 1
                card.misses.append((d.dispute_id, truth, got))
            else:
                card.anomalies_flagged_wrong_code += 1
                card.miscategorised.append((d.dispute_id, truth, got))

    return card
