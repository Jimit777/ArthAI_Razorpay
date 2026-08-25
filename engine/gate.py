"""
The guardrail gate. Checkpoint 7.

Decides which decisions the system is allowed to close by itself and which go
to a person. Pure Python - the agent does not get a vote on whether it should
be trusted, because a model that is confidently wrong is confidently wrong
about its confidence too.

CLAUDE.md section 10, guardrail 3: confidence below threshold OR delta above a
rupee cap goes to a human review queue, never auto-resolved.

The gate is deliberately paranoid in one direction only. Sending a correct
finding to a human costs someone two minutes. Auto-closing a wrong one costs
the merchant money they never learn they were owed. Those are not symmetrical
and the thresholds are set accordingly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from engine.detector import Variance
from engine.expected_value import rupees
from engine.taxonomy import Action, NO_ACTION, ExceptionCode

# Codes where the finding is real but no money changes hands if it is acted on.
# A June sale settling in July is reclassified between two periods; the rupees
# do not move, so feeding the sale value into a risk threshold overstates it and
# queues a record no one needs to look at. The signal still carries the amount,
# because the report legitimately wants to total how much revenue crossed the
# boundary - that is a reporting figure, not a risk figure.
NO_MONEY_AT_RISK = frozenset({str(ExceptionCode.PERIOD_BOUNDARY)})


@dataclass
class GateDecision:
    payment_id: str
    exception_code: str
    action: str
    confidence: float
    money_at_stake: int             # paise
    queued_for_human: bool
    reasons: list[str] = field(default_factory=list)
    decided_by: str = "agent"
    errored: bool = False           # the call failed; this is not a judgement

    @property
    def auto_resolved(self) -> bool:
        return not self.queued_for_human


def money_at_stake(variance: Variance, exception_code: str) -> int:
    """
    How much money this record is actually about.

    Not the same as the fee delta, and the difference matters. A mislabelled
    instrument has a delta of ZERO - the card fee charged was the correct card
    fee - while the recoverable amount runs into hundreds of rupees. A record
    missing from settlement has no delta at all and the whole sale at stake.
    Gating on delta alone would wave through the two most expensive findings in
    a typical batch.
    """
    if exception_code in NO_MONEY_AT_RISK:
        return abs(variance.delta)

    stake = abs(variance.delta)
    for signal in variance.signals:
        if signal.candidate_code == exception_code:
            stake = max(stake, abs(signal.amount_paise))
    return stake


def apply_gate(variance: Variance, verdict, rate_card: dict) -> GateDecision:
    """
    One record in, one routing decision out.

    `verdict` is an agent Verdict, or None when the calculator already resolved
    the record deterministically.
    """
    policy = rate_card["guardrails"]
    reasons: list[str] = []

    if verdict is None and variance.exception_code is None:
        # Needed the agent, and never got one - the agent was switched off, or
        # the batch was never classified.
        #
        # This used to fall through to the branch below and be treated as a
        # calculator decision: confidence 1.0, decided_by "calculator", and
        # auto-closed unless the sum happened to clear the rupee threshold. So
        # an UNEXPLAINED record nothing had looked at could close itself, which
        # is the exact opposite of guardrail 3 and of the rule the scoring
        # module already follows - a record nobody judged is not a clean one.
        stake = money_at_stake(variance, str(ExceptionCode.UNEXPLAINED))
        return GateDecision(
            payment_id=variance.payment_id,
            exception_code=str(ExceptionCode.UNEXPLAINED),
            action=str(Action.ESCALATE),
            confidence=0.0,
            money_at_stake=stake,
            queued_for_human=True,
            reasons=["needed judgment and no agent decision was made"],
            decided_by="agent",
        )

    if verdict is None:
        # The calculator resolved it. Arithmetic does not need supervision -
        # but a large sum still gets eyes on it.
        code = variance.exception_code
        stake = money_at_stake(variance, code)
        queued = False
        if code not in {str(c) for c in NO_ACTION} and stake > policy["review_above_paise"]:
            queued = True
            reasons.append(
                f"{rupees(stake)} at stake, above the {rupees(policy['review_above_paise'])} "
                f"review threshold")
        return GateDecision(
            payment_id=variance.payment_id,
            exception_code=code,
            action=variance.action or "",
            confidence=1.0,
            money_at_stake=stake,
            queued_for_human=queued,
            reasons=reasons,
            decided_by="calculator",
        )

    stake = money_at_stake(variance, verdict.exception_code)

    if verdict.error:
        reasons.append(f"the classification failed: {verdict.error}")

    if verdict.confidence < policy["min_confidence"]:
        reasons.append(
            f"confidence {verdict.confidence:.2f} is below the "
            f"{policy['min_confidence']:.2f} threshold")

    # A "do nothing" verdict on a big number is the one combination worth being
    # suspicious about, so the threshold applies to actionable findings and to
    # dismissals of large sums alike.
    if stake > policy["review_above_paise"]:
        reasons.append(
            f"{rupees(stake)} at stake, above the "
            f"{rupees(policy['review_above_paise'])} review threshold")

    if verdict.corrections:
        reasons.append("the review step had to correct the agent's answer")

    if verdict.invented_figures:
        reasons.append(
            f"stated figures that were not in the evidence: "
            f"{', '.join(verdict.invented_figures)}")

    if verdict.exception_code == str(ExceptionCode.UNEXPLAINED):
        reasons.append("the agent could not account for this record")

    return GateDecision(
        payment_id=variance.payment_id,
        exception_code=verdict.exception_code,
        action=verdict.action,
        confidence=verdict.confidence,
        money_at_stake=stake,
        queued_for_human=bool(reasons),
        reasons=reasons,
        decided_by="agent",
        errored=bool(verdict.error),
    )


def gate_batch(variances: list[Variance], verdicts: list, rate_card: dict) -> list[GateDecision]:
    """Route a whole batch. Verdicts are matched to variances by payment id."""
    by_payment = {v.payment_id: v for v in verdicts}
    return [apply_gate(variance, by_payment.get(variance.payment_id), rate_card)
            for variance in variances]
