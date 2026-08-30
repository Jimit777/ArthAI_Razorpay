"""
The guardrail gate for GST corrections. Mirrors engine/payout_timing/gate.py,
applied once PER PERIOD rather than once per run - each open or locked
period carries its own money at stake and its own confidence, and a merchant
reviewing one period's DRC-03 should not be blocked on another period's
agent call succeeding.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from engine.gst_filing import rules
from engine.gst_filing.taxonomy import CorrectionCode

DEFAULT_MIN_CONFIDENCE = 0.75
# Rule 88C's own absolute floor (rules.RULE_88C_ABSOLUTE_PAISE), reused here
# as the review cap - a shortfall large enough to risk a Rule 88C notice on
# its own is large enough to want a person's eyes on it before it's acted on.
DEFAULT_REVIEW_ABOVE_PAISE = rules.RULE_88C_ABSOLUTE_PAISE


@dataclass
class CorrectionDecision:
    period: str
    exception_code: str
    action: str
    confidence: float
    money_at_stake: int
    queued_for_human: bool
    reasons: list[str] = field(default_factory=list)
    decided_by: str = "calculator"
    priority_reasoning: str = ""


def gate(finding, priority: Optional[object] = None, *,
        min_confidence: float = DEFAULT_MIN_CONFIDENCE,
        review_above_paise: int = DEFAULT_REVIEW_ABOVE_PAISE
        ) -> CorrectionDecision:
    """One decision per period: the calculator's own action always stands
    (never softened - see timing.py's module docstring), the agent's
    priority narrative and confidence layer on top of it when one exists."""
    money_at_stake = abs(finding.delta) + finding.interest_paise

    if finding.exception_code == str(CorrectionCode.PERIOD_CLEAN):
        return CorrectionDecision(
            period=finding.period, exception_code=finding.exception_code,
            action=finding.action, confidence=1.0,
            money_at_stake=money_at_stake, queued_for_human=False,
            decided_by="calculator")

    reasons: list[str] = []
    confidence = 1.0
    decided_by = "calculator"
    priority_reasoning = ""

    if priority is not None:
        decided_by = "agent"
        confidence = float(getattr(priority, "confidence", 0.0) or 0.0)
        priority_reasoning = getattr(priority, "reasoning", "") or ""
        if getattr(priority, "error", None):
            reasons.append("the priority call failed")
        if confidence < min_confidence:
            reasons.append(f"confidence {confidence:.2f} is below the "
                           f"{min_confidence:.2f} threshold")
        if getattr(priority, "invented_figures", None):
            reasons.append("the reasoning carried figures from nowhere")

    if money_at_stake > review_above_paise:
        reasons.append(
            f"{rules.rupees(money_at_stake)} is above the "
            f"{rules.rupees(review_above_paise)} review threshold")

    return CorrectionDecision(
        period=finding.period, exception_code=finding.exception_code,
        action=finding.action, confidence=confidence,
        money_at_stake=money_at_stake, queued_for_human=bool(reasons),
        reasons=reasons, decided_by=decided_by,
        priority_reasoning=priority_reasoning)
