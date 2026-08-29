"""
Scoring the TDS credit reconciliation against the generator's answer key.

Produces the SAME Scorecard the settlement and ITC agents produce, on
purpose - see engine/gst/scoring.py's docstring for why. The honesty rules
carry across unchanged: a failed call is never a correct answer, and a false
accusation is counted on its own.
"""

from __future__ import annotations

from engine.scoring import Scorecard
from engine.tds.gate import money_at_stake
from engine.tds.taxonomy import NO_ACTION, TdsCode

DO_NOTHING = {str(c) for c in NO_ACTION}
FOUND_MONEY = {str(TdsCode.MISSING_CREDIT), str(TdsCode.RATE_MISMATCH),
              str(TdsCode.CODE_MISMATCH)}


def score(decisions, ground_truth: dict[str, str], variances) -> Scorecard:
    by_id = {v.payment_id: v for v in variances}
    card = Scorecard(total=len(decisions))

    for decision in decisions:
        truth = ground_truth[decision.payment_id]
        got = decision.exception_code
        variance = by_id[decision.payment_id]

        bucket = card.by_code.setdefault(truth, {"n": 0, "correct": 0})
        bucket["n"] += 1

        if decision.queued_for_human:
            card.queued_for_human += 1
        else:
            card.auto_resolved += 1

        errored = bool(decision.errored)
        got_it_right = (got == truth) and not errored

        if decision.decided_by == "calculator":
            card.by_calculator += 1
            card.by_calculator_correct += int(got_it_right)
        else:
            card.by_agent += 1
            card.by_agent_correct += int(got_it_right)
            card.failed_calls += int(errored)

        if got_it_right:
            card.correct += 1
            bucket["correct"] += 1

        if truth == str(TdsCode.CREDIT_CLEAN):
            card.clean += 1
            if got == truth:
                card.clean_correct += 1
        elif truth in DO_NOTHING:
            card.decoys += 1
            if got in DO_NOTHING:
                card.decoys_dismissed += 1
        else:
            card.anomalies += 1
            if got_it_right:
                card.anomalies_caught += 1
                if got in FOUND_MONEY:
                    card.recoverable_paise += money_at_stake(variance, got)
            elif errored:
                card.anomalies_missed += 1
                card.misses.append((decision.payment_id, truth, "call failed"))
            elif got in DO_NOTHING:
                card.anomalies_missed += 1
                card.misses.append((decision.payment_id, truth, got))
            else:
                card.anomalies_flagged_wrong_code += 1
                card.miscategorised.append((decision.payment_id, truth, got))

        # Telling a merchant to chase credit that was fine, or waving off a
        # statement that is genuinely wrong, is the failure that ends the
        # relationship. Counted on its own.
        if truth in DO_NOTHING and got not in DO_NOTHING:
            card.false_accusations.append((decision.payment_id, truth, got))

    return card
