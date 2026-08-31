"""
Classify one dispute's evidence checklist against its reason code's real
requirement list. Fully mechanical - see taxonomy.py's module docstring for
why. The judgment this agent has (is this worth contesting, and the
argument) happens once per dispute that has something to work with - see
agent/chargeback_classifier.py.

ALL MONEY IN PAISE, AS INTEGERS.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from engine.chargeback import rules
from engine.chargeback.taxonomy import (DISPUTE_ACTION_FOR, DISPUTE_LABEL,
                                        DisputeCode)


@dataclass
class Dispute:
    """One dispute notice, as recorded - before classification."""
    dispute_id: str
    payment_id: str
    amount_paise: int
    reason_code: str
    reason_description: str
    phase: str
    status: str
    respond_by: int             # unix ts


@dataclass
class ClassifiedDispute:
    """The same dispute, with its checklist worked out and its code
    decided."""
    dispute_id: str
    payment_id: str
    amount_paise: int
    reason_code: str
    reason_description: str
    phase: str
    status: str
    respond_by: int
    required: tuple[str, ...]
    present: tuple[str, ...]
    missing: tuple[str, ...]
    days_to_respond_by: int
    code: str                   # DisputeCode
    action: str                 # DisputeAction
    reasoning: str = ""

    def as_dict(self) -> dict:
        return {
            "dispute_id": self.dispute_id, "payment_id": self.payment_id,
            "amount_paise": self.amount_paise,
            "amount_display": rules.rupees(self.amount_paise),
            "reason_code": self.reason_code,
            "reason_description": self.reason_description,
            "phase": self.phase, "status": self.status,
            "respond_by": self.respond_by,
            "days_to_respond_by": self.days_to_respond_by,
            "required": list(self.required), "present": list(self.present),
            "missing": list(self.missing),
            "code": self.code,
            "code_label": DISPUTE_LABEL.get(DisputeCode(self.code), self.code),
            "action": self.action, "reasoning": self.reasoning,
        }


def detect(dispute: Dispute, evidence_types_present: set[str], *,
          now: int) -> ClassifiedDispute:
    """One dispute, classified. `evidence_types_present` is the set of
    evidence-type keys the merchant has actually entered something for -
    a type absent from it is never assumed present."""
    required = rules.evidence_types_for(dispute.reason_code)
    days = (dispute.respond_by - now) // 86_400

    if not required:
        code = DisputeCode.REASON_CODE_UNMAPPED
        present: tuple[str, ...] = ()
        missing: tuple[str, ...] = ()
        reasoning = (f'Reason code "{dispute.reason_code}" has no evidence '
                    f"requirement list on file - checked against "
                    f"{rules.SOURCE_REASON_CODE_TABLE}, not found there "
                    f"either. Needs a person to look at the actual notice.")
    else:
        present = tuple(t for t in required if t in evidence_types_present)
        missing = tuple(t for t in required if t not in evidence_types_present)
        if not present:
            code = DisputeCode.EVIDENCE_MISSING
            reasoning = (f"{len(required)} evidence type(s) required for "
                        f'"{dispute.reason_description or dispute.reason_code}"'
                        f", none on file yet.")
        elif missing:
            code = DisputeCode.EVIDENCE_PARTIAL
            reasoning = (f"{len(present)} of {len(required)} required "
                        f"evidence type(s) on file - missing "
                        f"{', '.join(missing)}.")
        else:
            code = DisputeCode.EVIDENCE_COMPLETE
            reasoning = (f"All {len(required)} required evidence type(s) "
                        f"are on file.")

    return ClassifiedDispute(
        dispute_id=dispute.dispute_id, payment_id=dispute.payment_id,
        amount_paise=dispute.amount_paise, reason_code=dispute.reason_code,
        reason_description=dispute.reason_description, phase=dispute.phase,
        status=dispute.status, respond_by=dispute.respond_by,
        required=required, present=present, missing=missing,
        days_to_respond_by=days, code=str(code),
        action=str(DISPUTE_ACTION_FOR[code]), reasoning=reasoning)


def detect_batch(disputes: list[Dispute], evidence_by_dispute: dict[str, set[str]],
                 *, now: int) -> list[ClassifiedDispute]:
    return [detect(d, evidence_by_dispute.get(d.dispute_id, set()), now=now)
            for d in disputes]
