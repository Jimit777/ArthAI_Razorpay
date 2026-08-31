"""
The guardrail gate for vendor terms. Mirrors engine/gst_filing/gate.py,
applied once PER SUPPLIER rather than once per line item - the judgment
here (dispute this batch, and how hard) is genuinely a supplier-level
question, not a per-line rule, so one supplier's agent call succeeding or
failing should never block another supplier's review.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from engine.vendor_terms import rules

DEFAULT_MIN_CONFIDENCE = 0.75
# A round number, not derived from any statute (there is none to derive
# from here) - a supplier dispute above Rs 25,000 is large enough that a
# person should see it before a letter goes out over their name.
DEFAULT_REVIEW_ABOVE_PAISE = 25_000_00


@dataclass
class TermsDecision:
    supplier_gstin: str
    supplier_name: str
    action: str
    confidence: float
    money_at_stake: int
    queued_for_human: bool
    reasons: list[str] = field(default_factory=list)
    decided_by: str = "calculator"
    dispute_reasoning: str = ""


def gate(group, verdict: Optional[object] = None, *,
        min_confidence: float = DEFAULT_MIN_CONFIDENCE,
        review_above_paise: int = DEFAULT_REVIEW_ABOVE_PAISE) -> TermsDecision:
    """One decision per supplier: the calculator's own action always stands
    (never softened - see taxonomy.py's NO_ACTION split), the agent's
    dispute-worth narrative layers on top of it when one exists."""
    from engine.vendor_terms.taxonomy import TermsAction

    money_at_stake = group.at_stake_paise

    if not group.overbilled:
        action = (str(TermsAction.ADD_TO_RATE_CARD) if group.unconfigured
                  else str(TermsAction.NONE))
        return TermsDecision(
            supplier_gstin=group.supplier_gstin, supplier_name=group.supplier_name,
            action=action, confidence=1.0, money_at_stake=0,
            queued_for_human=False, decided_by="calculator")

    reasons: list[str] = []
    confidence = 1.0
    decided_by = "calculator"
    dispute_reasoning = ""

    if verdict is not None:
        decided_by = "agent"
        confidence = float(getattr(verdict, "confidence", 0.0) or 0.0)
        dispute_reasoning = getattr(verdict, "reasoning", "") or ""
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

    return TermsDecision(
        supplier_gstin=group.supplier_gstin, supplier_name=group.supplier_name,
        action=str(TermsAction.REQUEST_CREDIT_NOTE), confidence=confidence,
        money_at_stake=money_at_stake, queued_for_human=bool(reasons),
        reasons=reasons, decided_by=decided_by,
        dispute_reasoning=dispute_reasoning)


def gate_batch(groups, verdicts: Optional[dict] = None, **kwargs
              ) -> list[TermsDecision]:
    verdicts = verdicts or {}
    return [gate(g, verdicts.get(g.supplier_gstin), **kwargs) for g in groups]
