"""
The chargeback rules. Pure Python, no model, never wrong.

## The one real rule table this agent has

`REASON_CODE_EVIDENCE` maps a card-network reason code to the evidence
types Razorpay's own documentation says that reason code needs - the exact
same vocabulary the real Contest API's request body uses
(`shipping_proof`, `billing_proof`, ...), so a completed checklist item
maps straight onto the field it would fill with zero translation layer.

Source, fetched and cross-checked directly (not from memory) this session:
https://razorpay.com/docs/payments/disputes/submit-evidence/ - "Dispute
Reason Codes & Evidence Document Requirements," per network.

## What's covered, and what's a citable follow-up rather than a guess

Covered: Razorpay's own codes (RZP00-RZP07), UPI, and RuPay - the two
rails this platform's whole domain focus already centres on
(CLAUDE.md section 14). Visa, Mastercard and Amex are the identical shape,
already fetched and verified in the same session that built this file, and
adding them is transcription into this same dict, not new research - noted
here rather than silently left out, so nobody mistakes an uncovered network
for "checked and clean." A reason code absent from this table produces
`DisputeCode.REASON_CODE_UNMAPPED` (see taxonomy.py) - never a guessed
requirement list. CLAUDE.md section 16: a wrong rule is worse than a
missing one.

## One simplification, stated plainly

The real API also accepts an `others` evidence type - a free-labelled
bucket (`{"type": "...", "document_ids": [...]}`) for anything outside the
fixed vocabulary. A few source rows mention something that doesn't map
cleanly onto a fixed type (e.g. RuPay 108's "customer withdrawal letter"),
and those are folded into the closest fixed type
(`customer_communication`, since it is customer-authored) rather than
introducing a variable-labelled requirement - the merchant-facing checklist
in this v1 has fixed columns, matching the fixed evidence types, and a
variable-labelled row would need UI this build doesn't have yet.
"""

from __future__ import annotations

from engine.gst.rules import rupees

__all__ = ["rupees", "REASON_CODE_EVIDENCE", "RESPOND_BY_REVIEW_DAYS",
          "SOURCE_REASON_CODE_TABLE"]

SOURCE_REASON_CODE_TABLE = ("razorpay.com/docs/payments/disputes/"
                            "submit-evidence/ - reason codes and their "
                            "evidence requirements, by network")

# A dispute closing within this many days is queued for a human regardless
# of confidence - engine/chargeback/gate.py's own addition, see that
# module's docstring.
RESPOND_BY_REVIEW_DAYS = 2

# Evidence-type vocabulary (matches the real Contest API's request body
# exactly): shipping_proof, billing_proof, cancellation_proof,
# customer_communication, proof_of_service, explanation_letter,
# refund_confirmation, access_activity_log, refund_cancellation_policy,
# term_and_conditions.

REASON_CODE_EVIDENCE: dict[str, tuple[str, ...]] = {
    # --- Razorpay's own codes ------------------------------------------
    "RZP00": ("shipping_proof", "billing_proof", "customer_communication",
             "refund_confirmation"),
    "RZP01": ("shipping_proof", "customer_communication", "term_and_conditions"),
    "RZP02": ("access_activity_log", "billing_proof", "shipping_proof"),
    "RZP03": ("access_activity_log", "billing_proof"),
    "RZP04": ("refund_confirmation", "customer_communication",
             "refund_cancellation_policy"),
    "RZP05": ("billing_proof", "access_activity_log", "customer_communication",
             "term_and_conditions"),
    "RZP06": ("shipping_proof", "billing_proof", "customer_communication"),
    "RZP07": ("access_activity_log",),

    # --- UPI --------------------------------------------------------------
    "1061": ("refund_confirmation", "customer_communication",
            "refund_cancellation_policy"),
    "1062": ("billing_proof", "shipping_proof", "customer_communication",
            "refund_cancellation_policy"),
    "1064": ("shipping_proof", "customer_communication", "term_and_conditions"),
    "128": ("access_activity_log", "billing_proof", "shipping_proof"),
    "108": ("shipping_proof", "customer_communication", "term_and_conditions"),
    "1065": ("shipping_proof", "customer_communication", "term_and_conditions"),
    "121": ("shipping_proof", "customer_communication", "term_and_conditions"),
    "1063": ("refund_confirmation", "customer_communication"),
    "1084": ("access_activity_log", "billing_proof"),
    "1085": ("billing_proof", "access_activity_log"),
    "1081": ("access_activity_log", "billing_proof"),

    # --- RuPay (codes not already covered by the UPI block above) ---------
    "1101": ("shipping_proof", "customer_communication", "term_and_conditions"),
    "1102": ("shipping_proof", "customer_communication", "term_and_conditions"),
    "1103": ("shipping_proof", "customer_communication", "term_and_conditions"),
    "1104": ("billing_proof", "access_activity_log"),
    "1141": ("billing_proof", "shipping_proof", "access_activity_log"),
    "1142": ("billing_proof", "shipping_proof", "access_activity_log"),
    "1143": ("billing_proof", "shipping_proof", "access_activity_log"),
    "1121": ("access_activity_log", "billing_proof", "shipping_proof"),
    "1122": ("access_activity_log", "billing_proof"),
    "1123": ("billing_proof", "access_activity_log"),
    "1082": ("access_activity_log", "refund_confirmation", "billing_proof"),
    "1083": ("billing_proof", "access_activity_log"),
}


def evidence_types_for(reason_code: str) -> tuple[str, ...]:
    """The required evidence types for a reason code, or an empty tuple if
    unmapped. Never defaults to a guessed list - see this module's own
    docstring and taxonomy.DisputeCode.REASON_CODE_UNMAPPED."""
    return REASON_CODE_EVIDENCE.get((reason_code or "").strip(), ())
