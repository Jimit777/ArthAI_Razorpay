"""
Scoring against ground truth. Checkpoint 7.

This is the number the pitch rests on, so the way it is computed matters as
much as the number itself. Three rules were applied while writing this file:

  1. Nothing is graded generously. An anomaly that was flagged but put in the
     wrong category is NOT a catch. It is reported separately, because "we
     noticed something was wrong here" is worth knowing - but it does not go in
     the recall number.

  2. False accusations are counted against the batch, not hidden in an average.
     A tool that finds eleven of twelve overcharges and wrongly accuses a
     gateway once is not 91% good; it is a tool the merchant will stop trusting
     the first time they get embarrassed.

  3. The denominator is every record, including the ones the calculator
     resolved without the model. Claiming accuracy only on the hard subset
     would be flattering and dishonest.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from engine.expected_value import rupees
from engine.taxonomy import NO_ACTION, RECOVERABLE, ExceptionCode

DO_NOTHING = {str(c) for c in NO_ACTION}
RECOVERABLE_CODES = {str(c) for c in RECOVERABLE}


@dataclass
class Scorecard:
    total: int = 0
    correct: int = 0

    anomalies: int = 0
    anomalies_caught: int = 0            # exact code match
    anomalies_flagged_wrong_code: int = 0
    anomalies_missed: int = 0

    decoys: int = 0
    decoys_dismissed: int = 0

    clean: int = 0
    clean_correct: int = 0

    false_accusations: list[tuple[str, str, str]] = field(default_factory=list)
    misses: list[tuple[str, str, str]] = field(default_factory=list)
    miscategorised: list[tuple[str, str, str]] = field(default_factory=list)

    queued_for_human: int = 0
    auto_resolved: int = 0

    # The split a judge will ask about within thirty seconds: how much of this
    # did the calculator do, and how did the model actually perform on the part
    # it was given? A headline accuracy that quietly includes 49 records the
    # model never saw is a flattering number, not a false one - but only if you
    # say so. Both are reported.
    by_calculator: int = 0
    by_calculator_correct: int = 0
    by_agent: int = 0
    by_agent_correct: int = 0

    # Records where the API call itself failed. These are NOT model mistakes and
    # an accuracy number computed over them measures the network, or the credit
    # balance, and not the system. Counted separately so the report can say so
    # instead of quietly averaging an outage into a percentage.
    failed_calls: int = 0

    recoverable_paise: int = 0
    by_code: dict = field(default_factory=dict)

    @property
    def accuracy(self) -> float:
        return self.correct / self.total if self.total else 0.0

    @property
    def recall(self) -> float:
        return self.anomalies_caught / self.anomalies if self.anomalies else 0.0

    @property
    def decoy_rate(self) -> float:
        return self.decoys_dismissed / self.decoys if self.decoys else 0.0

    @property
    def agent_accuracy(self) -> float:
        """Accuracy on the records the model was actually asked about."""
        return self.by_agent_correct / self.by_agent if self.by_agent else 0.0


def score(decisions, ground_truth: dict[str, str], variances) -> Scorecard:
    """Compare what the system concluded against what was actually planted."""
    by_payment = {v.payment_id: v for v in variances}
    card = Scorecard(total=len(decisions))

    for decision in decisions:
        truth = ground_truth[decision.payment_id]
        got = decision.exception_code
        variance = by_payment[decision.payment_id]

        bucket = card.by_code.setdefault(truth, {"n": 0, "correct": 0})
        bucket["n"] += 1

        if decision.queued_for_human:
            card.queued_for_human += 1
        else:
            card.auto_resolved += 1

        # A failed call is never a correct answer, even when the code matches.
        #
        # The failure path returns UNEXPLAINED, and UNEXPLAINED is also the
        # right answer for the unrecognised-adjustment record. So during a total
        # outage the scorer awarded a point for a record the model never saw -
        # a batch where every single call failed still scored 1/13. An outage
        # that scores above zero is an outage that can be mistaken for a result.
        errored = bool(getattr(decision, "errored", False))
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

        # --- what kind of record was this? -------------------------------
        if truth == str(ExceptionCode.CLEAN):
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
                if got in RECOVERABLE_CODES:
                    from engine.gate import money_at_stake
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

        # --- did we accuse anyone wrongly? -------------------------------
        # Telling a merchant to dispute a deduction that was correct is the
        # failure that ends the relationship. Counted on its own.
        if truth in DO_NOTHING and got not in DO_NOTHING:
            card.false_accusations.append((decision.payment_id, truth, got))

    return card


def print_scorecard(card: Scorecard) -> None:
    print()
    print("=" * 70)
    print("ACCURACY AGAINST GROUND TRUTH")
    print("=" * 70)
    print()
    if card.failed_calls:
        completed = card.by_agent - card.failed_calls
        print(f"  !! {card.failed_calls} of {card.by_agent} classifications FAILED to run.")
        print( "     Those records were escalated, not judged. The percentages below")
        print( "     are measuring an outage, not the system - do not quote them.")
        if completed:
            print(f"     On the {completed} that did run: "
                  f"{card.by_agent_correct}/{completed} correct.")
        print()
    print(f"  {card.total} records audited, {card.correct} classified correctly"
          f"   ({card.accuracy:.1%})")
    print()
    print(f"    of which the rate card settled {card.by_calculator}"
          f"  -> {card.by_calculator_correct} correct")
    print(f"    and the agent was asked about  {card.by_agent}"
          f"  -> {card.by_agent_correct} correct   ({card.agent_accuracy:.1%})")
    print()
    print(f"  planted anomalies      {card.anomalies}")
    print(f"    correctly identified {card.anomalies_caught}"
          f"   ({card.recall:.1%} recall)")
    if card.anomalies_flagged_wrong_code:
        print(f"    flagged, wrong code  {card.anomalies_flagged_wrong_code}")
    if card.anomalies_missed:
        print(f"    MISSED               {card.anomalies_missed}")
    print()
    print(f"  decoys (must not flag) {card.decoys}")
    print(f"    correctly dismissed  {card.decoys_dismissed}   ({card.decoy_rate:.1%})")
    print()
    print(f"  clean records          {card.clean}")
    print(f"    left alone           {card.clean_correct}")
    print()
    print(f"  FALSE ACCUSATIONS      {len(card.false_accusations)}")
    for pid, truth, got in card.false_accusations:
        print(f"    {pid}  was {truth}, called it {got}")
    print()
    print(f"  recoverable identified {rupees(card.recoverable_paise)}")
    print()
    print(f"  auto-resolved          {card.auto_resolved}")
    print(f"  queued for a human     {card.queued_for_human}")

    if card.misses:
        print()
        print("  missed entirely:")
        for pid, truth, got in card.misses:
            print(f"    {pid}  was {truth}, called it {got}")
    if card.miscategorised:
        print()
        print("  found but miscategorised:")
        for pid, truth, got in card.miscategorised:
            print(f"    {pid}  was {truth}, called it {got}")

    print()
    print("  per category:")
    for truth in sorted(card.by_code):
        b = card.by_code[truth]
        print(f"    {truth:<26} {b['correct']}/{b['n']}")
    print()
