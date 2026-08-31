"""
The vocabulary for the chargeback defence assembler. One layer, four codes -
the checklist for one dispute against one reason code's real requirement
list.

## Why a dispute with SOME evidence still gets a draft

CLAUDE.md's "don't withhold what exists": a merchant with delivery proof but
no customer-communication record has a weaker case than a complete one, not
no case. EVIDENCE_PARTIAL still produces a drafted pack - the draft states
the gap explicitly (see agent/chargeback_documents.py), it never argues
around it silently.

## Why REASON_CODE_UNMAPPED exists

Not every reason code Razorpay's dashboard can show is in the reason-code
table this taxonomy is built from (engine/chargeback/rules.py cites its
exact source and its exact coverage). A code outside that coverage gets no
guessed requirement list - same discipline
engine/gst_filing/taxonomy.py's HSN_RATE_UNCONFIGURED and
engine/vendor_terms/taxonomy.py's RATE_UNCONFIGURED already keep: excluded
from "here's your checklist," flagged for a person instead.
"""

from __future__ import annotations

from enum import StrEnum


class DisputeCode(StrEnum):
    EVIDENCE_COMPLETE = "EVIDENCE_COMPLETE"        # every required type is on file
    EVIDENCE_PARTIAL = "EVIDENCE_PARTIAL"          # some required types are on file
    EVIDENCE_MISSING = "EVIDENCE_MISSING"          # nothing on file yet
    REASON_CODE_UNMAPPED = "REASON_CODE_UNMAPPED"  # no rule for this code


DISPUTE_LABEL: dict[DisputeCode, str] = {
    DisputeCode.EVIDENCE_COMPLETE: "Every required document is on file",
    DisputeCode.EVIDENCE_PARTIAL: "Some required documents are on file",
    DisputeCode.EVIDENCE_MISSING: "No evidence on file yet",
    DisputeCode.REASON_CODE_UNMAPPED: "No requirement list for this reason code",
}


class DisputeAction(StrEnum):
    NONE = "none"
    GATHER_EVIDENCE = "gather_evidence"
    DRAFT_EVIDENCE_PACK = "draft_evidence_pack"
    ESCALATE = "escalate"


DISPUTE_ACTION_FOR: dict[DisputeCode, DisputeAction] = {
    DisputeCode.EVIDENCE_COMPLETE: DisputeAction.DRAFT_EVIDENCE_PACK,
    DisputeCode.EVIDENCE_PARTIAL: DisputeAction.DRAFT_EVIDENCE_PACK,
    DisputeCode.EVIDENCE_MISSING: DisputeAction.GATHER_EVIDENCE,
    DisputeCode.REASON_CODE_UNMAPPED: DisputeAction.ESCALATE,
}

# Two of four codes mean "not ready for a draft" - the same three-vs-rest
# split every taxonomy in this project keeps deliberately visible.
NOT_READY = {DisputeCode.EVIDENCE_MISSING, DisputeCode.REASON_CODE_UNMAPPED}

# The severity ladder an agent may go further on but never soften - same
# convention as engine/treasury/records.py's ACTION_SEVERITY and
# engine/payout_timing/taxonomy.py's ACTION_SEVERITY.
ACTION_SEVERITY: dict[str, int] = {
    str(DisputeAction.NONE): 0,
    str(DisputeAction.GATHER_EVIDENCE): 1,
    str(DisputeAction.DRAFT_EVIDENCE_PACK): 2,
    str(DisputeAction.ESCALATE): 3,
}
