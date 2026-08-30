"""
The guardrail gate for payout timing. Mirrors engine/tds/gate.py, at the
scale of one decision per run rather than one per record - there is only
ever one pattern to judge, see detector.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from engine.payout_timing import rules
from engine.payout_timing.detector import PayoutTimingSummary

DEFAULT_MIN_CONFIDENCE = 0.75
DEFAULT_REVIEW_ABOVE_PAISE = 25_000_00        # Rs 25,000 of assumed float cost


@dataclass
class PayoutTimingDecision:
    pattern: str
    action: str
    confidence: float
    float_cost_paise: int
    queued_for_human: bool
    reasons: list[str] = field(default_factory=list)
    decided_by: str = "calculator"
    errored: bool = False


def gate(summary: PayoutTimingSummary, verdict: Optional[object] = None, *,
         min_confidence: float = DEFAULT_MIN_CONFIDENCE,
         review_above_paise: int = DEFAULT_REVIEW_ABOVE_PAISE
         ) -> PayoutTimingDecision:
    """One decision for the run: the calculator's own if there is no
    verdict (or it failed), the agent's otherwise - checked, never trusted
    outright."""
    if verdict is None:
        return PayoutTimingDecision(
            pattern=summary.pattern, action=summary.action,
            confidence=1.0, float_cost_paise=summary.total_float_cost_paise,
            queued_for_human=False, decided_by="calculator")

    reasons: list[str] = []
    if getattr(verdict, "error", None):
        reasons.append("the classification call failed")
    if verdict.confidence < min_confidence:
        reasons.append(f"confidence {verdict.confidence:.2f} is below the "
                       f"{min_confidence:.2f} threshold")
    if summary.total_float_cost_paise > review_above_paise:
        reasons.append(
            f"{rules.rupees(summary.total_float_cost_paise)} is above the "
            f"{rules.rupees(review_above_paise)} review threshold")
    if getattr(verdict, "corrections", None):
        reasons.append("the answer needed correcting: "
                       + "; ".join(verdict.corrections))

    if summary.pattern == "CLEAN" and not getattr(verdict, "error", None):
        reasons = [r for r in reasons if "review threshold" not in r]

    return PayoutTimingDecision(
        pattern=summary.pattern, action=verdict.action,
        confidence=verdict.confidence,
        float_cost_paise=summary.total_float_cost_paise,
        queued_for_human=bool(reasons), reasons=reasons, decided_by="agent",
        errored=bool(getattr(verdict, "error", None)))
