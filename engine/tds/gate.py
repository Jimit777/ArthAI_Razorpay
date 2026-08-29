"""
The guardrail gate for TDS credit tracking. Mirrors engine/gate.py and
engine/gst/gate.py.

Same principle: pure Python, and the agent gets no vote on whether it should
be trusted.

## What "money at stake" means here

For MISSING_CREDIT the whole deducted amount is what the merchant stands to
lose - nothing on the statement supports any of it. For CODE_MISMATCH the gap
can be zero (the rupee figure matches) while the whole credit is still at
risk, because a return built on a stale section code gets rejected on
submission regardless of whether the number was right - so this is sized by
the deducted amount, not the delta, for the same reason engine/gst/gate.py
sizes a BLOCKED_CREDIT finding by the claim rather than the gap.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from engine.tds import rules
from engine.tds.detector import TdsVariance
from engine.tds.taxonomy import ACTION_FOR, NO_ACTION, TdsCode

DEFAULT_MIN_CONFIDENCE = 0.75
DEFAULT_REVIEW_ABOVE_PAISE = 10_000_00       # Rs 10,000 of TDS credit


@dataclass
class TdsDecision:
    payment_id: str
    exception_code: str
    action: str
    confidence: float
    money_at_stake: int
    queued_for_human: bool
    reasons: list[str] = field(default_factory=list)
    decided_by: str = "agent"
    errored: bool = False

    @property
    def auto_resolved(self) -> bool:
        return not self.queued_for_human


def money_at_stake(variance: TdsVariance, exception_code: str) -> int:
    code = TdsCode(exception_code) if exception_code in {
        str(c) for c in TdsCode} else None

    if code is TdsCode.MISSING_CREDIT:
        return abs(variance.deducted_amount)
    if code is TdsCode.CODE_MISMATCH:
        return abs(variance.deducted_amount)
    if code is TdsCode.RATE_MISMATCH:
        return abs(variance.delta)
    return abs(variance.delta)


def gate_batch(variances, verdicts, *,
               min_confidence: float = DEFAULT_MIN_CONFIDENCE,
               review_above_paise: int = DEFAULT_REVIEW_ABOVE_PAISE
               ) -> list[TdsDecision]:
    """
    One decision per variance. Records the calculator settled pass through
    with full confidence; records the agent judged are checked against both
    thresholds.
    """
    by_id = {v.payment_id: v for v in verdicts}
    out: list[TdsDecision] = []

    for variance in variances:
        verdict = by_id.get(variance.payment_id)

        if verdict is None:
            if variance.exception_code is None:
                out.append(TdsDecision(
                    payment_id=variance.payment_id,
                    exception_code=str(TdsCode.UNEXPLAINED),
                    action=str(ACTION_FOR[TdsCode.UNEXPLAINED]),
                    confidence=0.0,
                    money_at_stake=money_at_stake(
                        variance, str(TdsCode.UNEXPLAINED)),
                    queued_for_human=True,
                    reasons=["no agent decision for a record that needed one"],
                    decided_by="agent"))
                continue

            stake = money_at_stake(variance, variance.exception_code)
            out.append(TdsDecision(
                payment_id=variance.payment_id,
                exception_code=variance.exception_code,
                action=str(ACTION_FOR[TdsCode(variance.exception_code)]),
                confidence=variance.confidence or 1.0,
                money_at_stake=stake,
                queued_for_human=False,
                decided_by="calculator"))
            continue

        stake = money_at_stake(variance, verdict.exception_code)
        reasons: list[str] = []

        if verdict.error:
            reasons.append("the classification call failed")
        if verdict.confidence < min_confidence:
            reasons.append(
                f"confidence {verdict.confidence:.2f} is below the "
                f"{min_confidence:.2f} threshold")
        if stake > review_above_paise:
            reasons.append(
                f"{rules.rupees(stake)} is above the "
                f"{rules.rupees(review_above_paise)} review threshold")
        if verdict.corrections:
            reasons.append("the answer needed correcting: "
                           + "; ".join(verdict.corrections))

        if TdsCode(verdict.exception_code) in NO_ACTION and not verdict.error:
            reasons = [r for r in reasons if "review threshold" not in r]

        out.append(TdsDecision(
            payment_id=variance.payment_id,
            exception_code=verdict.exception_code,
            action=verdict.action,
            confidence=verdict.confidence,
            money_at_stake=stake,
            queued_for_human=bool(reasons),
            reasons=reasons,
            decided_by="agent",
            errored=bool(verdict.error)))

    return out
