"""
The guardrail gate for chargeback defence. Mirrors engine/vendor_terms/gate.py,
applied once per dispute - the judgment here (worth contesting, and the
argument) is genuinely a per-dispute question.

## The one addition this gate has that no other gate in this codebase does

A dispute closing within RESPOND_BY_REVIEW_DAYS is queued for a human
REGARDLESS of confidence - a closing deadline is its own reason to want a
person's eyes on a draft before it goes anywhere, the same way
engine/gst_filing/gate.py treats Rule 88C's own absolute floor as a review
trigger on its own, just keyed on time here instead of money. A confident,
well-evidenced draft with two days left is still worth a second look before
it's the only thing standing between a merchant and an automatic clawback.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from engine.chargeback import rules
from engine.chargeback.taxonomy import DisputeAction, NOT_READY, DisputeCode

DEFAULT_MIN_CONFIDENCE = 0.75
# A round number, not derived from any statute (there is none to derive
# from here) - a chargeback above Rs 25,000 is large enough that a person
# should see the draft before it goes out, same threshold vendor_terms
# uses for a credit-note request over its own contract.
DEFAULT_REVIEW_ABOVE_PAISE = 25_000_00


@dataclass
class DisputeDecision:
    dispute_id: str
    action: str
    confidence: float
    money_at_stake: int
    days_to_respond_by: int
    queued_for_human: bool
    reasons: list[str] = field(default_factory=list)
    decided_by: str = "calculator"
    case_reasoning: str = ""


def gate(classified, verdict: Optional[object] = None, *,
        min_confidence: float = DEFAULT_MIN_CONFIDENCE,
        review_above_paise: int = DEFAULT_REVIEW_ABOVE_PAISE,
        review_within_days: int = rules.RESPOND_BY_REVIEW_DAYS
        ) -> DisputeDecision:
    """One decision per dispute: the calculator's own action always stands
    (never softened), the agent's case narrative and confidence layer on
    top of it when one exists."""
    money_at_stake = classified.amount_paise

    if DisputeCode(classified.code) in NOT_READY:
        return DisputeDecision(
            dispute_id=classified.dispute_id, action=classified.action,
            confidence=1.0, money_at_stake=money_at_stake,
            days_to_respond_by=classified.days_to_respond_by,
            queued_for_human=(DisputeCode(classified.code)
                              == DisputeCode.REASON_CODE_UNMAPPED),
            decided_by="calculator")

    reasons: list[str] = []
    confidence = 1.0
    decided_by = "calculator"
    case_reasoning = ""

    if verdict is not None:
        decided_by = "agent"
        confidence = float(getattr(verdict, "confidence", 0.0) or 0.0)
        case_reasoning = getattr(verdict, "reasoning", "") or ""
        if getattr(verdict, "error", None):
            reasons.append("the agent call failed")
        if confidence < min_confidence:
            reasons.append(f"confidence {confidence:.2f} is below the "
                           f"{min_confidence:.2f} threshold")
        if getattr(verdict, "invented_figures", None):
            reasons.append("the reasoning carried figures from nowhere")

    if money_at_stake > review_above_paise:
        reasons.append(
            f"{rules.rupees(money_at_stake)} is above the "
            f"{rules.rupees(review_above_paise)} review threshold")

    if classified.days_to_respond_by <= review_within_days:
        reasons.append(
            f"only {classified.days_to_respond_by} day(s) left to respond")

    return DisputeDecision(
        dispute_id=classified.dispute_id, action=str(DisputeAction.DRAFT_EVIDENCE_PACK),
        confidence=confidence, money_at_stake=money_at_stake,
        days_to_respond_by=classified.days_to_respond_by,
        queued_for_human=bool(reasons), reasons=reasons, decided_by=decided_by,
        case_reasoning=case_reasoning)


def gate_batch(classified_disputes, verdicts: Optional[dict] = None, **kwargs
              ) -> list[DisputeDecision]:
    verdicts = verdicts or {}
    return [gate(d, verdicts.get(d.dispute_id), **kwargs)
           for d in classified_disputes]
