"""
The guardrail gate for input tax credit. Mirrors engine/gate.py.

Same principle: pure Python, and the agent gets no vote on whether it should be
trusted - a model that is confidently wrong is confidently wrong about its
confidence too.

## What "money at stake" means here, and why it is not the gap

The settlement gate had to be careful that a delta of zero could still be a
large recoverable. This one has the opposite trap. On a SUPPLIER_NOT_FILED
invoice the gap equals the whole tax amount, because GSTR-2B supports nothing -
but on a BLOCKED_CREDIT invoice that matched perfectly the gap is ZERO, and the
amount the merchant must stop claiming is the entire tax on the invoice.

Sizing a blocked credit by its gap would rate the riskiest records at nothing
and auto-close them. So the amount at stake is what the merchant's return
changes by, which for anything in OVERCLAIMED is the tax claimed, not the gap.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from engine.gst import rules
from engine.gst.detector import ITCVariance
from engine.gst.taxonomy import (ACTION_FOR, AT_RISK, ITCCode, NO_ACTION,
                                 OVERCLAIMED)

DEFAULT_MIN_CONFIDENCE = 0.75
DEFAULT_REVIEW_ABOVE_PAISE = 25_000_00      # Rs 25,000 of tax


@dataclass
class ITCDecision:
    invoice_id: str
    exception_code: str
    action: str
    confidence: float
    money_at_stake: int
    queued_for_human: bool
    reasons: list[str] = field(default_factory=list)
    decided_by: str = "agent"
    errored: bool = False

    # scoring reads this name on both agents
    @property
    def payment_id(self) -> str:
        return self.invoice_id

    @property
    def auto_resolved(self) -> bool:
        return not self.queued_for_human


def money_at_stake(variance: ITCVariance, exception_code: str) -> int:
    """
    How much the merchant's return changes if this finding is acted on.

    For a credit at risk, that is the tax they will not get. For a credit they
    must stop claiming, it is the tax they have to give back - which on a
    perfectly matched blocked invoice is the whole amount while the gap is zero.
    """
    code = ITCCode(exception_code) if exception_code in {
        str(c) for c in ITCCode} else None

    if code in OVERCLAIMED:
        return abs(variance.claimed_tax)
    if code is ITCCode.AMOUNT_MISMATCH:
        # Partly supported. Only the unsupported part is at risk - the rest
        # will arrive. Returning the whole claim here overstated the headline
        # by the amount the supplier DID report.
        return abs(variance.delta)
    if code in AT_RISK:
        return abs(variance.claimed_tax) or abs(variance.delta)
    if code is ITCCode.NOT_IN_BOOKS:
        return abs(variance.available_tax)
    return abs(variance.delta)


def gate_batch(variances, verdicts, *,
               min_confidence: float = DEFAULT_MIN_CONFIDENCE,
               review_above_paise: int = DEFAULT_REVIEW_ABOVE_PAISE
               ) -> list[ITCDecision]:
    """
    One decision per variance. Records the calculator settled pass through with
    full confidence; records the agent judged are checked against both
    thresholds.
    """
    by_id = {v.invoice_id: v for v in verdicts}
    out: list[ITCDecision] = []

    for variance in variances:
        verdict = by_id.get(variance.invoice_id)

        if verdict is None:
            if variance.exception_code is None:
                # Needed the agent and never got one. Not a clean claim.
                out.append(ITCDecision(
                    invoice_id=variance.invoice_id,
                    exception_code=str(ITCCode.UNEXPLAINED),
                    action=str(ACTION_FOR[ITCCode.UNEXPLAINED]),
                    confidence=0.0,
                    money_at_stake=money_at_stake(
                        variance, str(ITCCode.UNEXPLAINED)),
                    queued_for_human=True,
                    reasons=["no agent decision for a record that needed one"],
                    decided_by="agent"))
                continue

            stake = money_at_stake(variance, variance.exception_code)
            out.append(ITCDecision(
                invoice_id=variance.invoice_id,
                exception_code=variance.exception_code,
                action=str(ACTION_FOR[ITCCode(variance.exception_code)]),
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

        # A finding that changes nothing does not need a person, however large.
        if ITCCode(verdict.exception_code) in NO_ACTION and not verdict.error:
            reasons = [r for r in reasons if "review threshold" not in r]

        out.append(ITCDecision(
            invoice_id=variance.invoice_id,
            exception_code=verdict.exception_code,
            action=verdict.action,
            confidence=verdict.confidence,
            money_at_stake=stake,
            queued_for_human=bool(reasons),
            reasons=reasons,
            decided_by="agent",
            errored=bool(verdict.error)))

    return out
