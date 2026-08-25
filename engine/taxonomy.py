"""
The exception taxonomy. CLAUDE.md section 5.

The organising idea, restated because it is easy to lose: a category is defined
by WHAT THE MERCHANT MUST DO, not by what the error looks like. Two records with
identical arithmetic can land in different categories, and that is correct - a
Rs 40 gap that is a rate breach is a dispute, and a Rs 40 gap that is a retained
refund fee is a cost to book. The number is the same; the action is not.

Three codes mean "do nothing". Those matter as much as the ones that mean
"dispute" - a tool that flags everything is a tool nobody opens twice.
"""

from __future__ import annotations

from enum import StrEnum


class ExceptionCode(StrEnum):
    CLEAN = "CLEAN"
    ROUNDING = "ROUNDING"
    ZERO_MDR_VIOLATION = "ZERO_MDR_VIOLATION"
    INSTRUMENT_MISLABEL = "INSTRUMENT_MISLABEL"
    RATE_MISMATCH = "RATE_MISMATCH"
    GST_MISMATCH = "GST_MISMATCH"
    REFUND_MDR_RETAINED = "REFUND_MDR_RETAINED"
    PERIOD_BOUNDARY = "PERIOD_BOUNDARY"
    TDS_CODE_MISMATCH = "TDS_CODE_MISMATCH"
    MISSING_FROM_SETTLEMENT = "MISSING_FROM_SETTLEMENT"
    UNEXPLAINED = "UNEXPLAINED"


class Action(StrEnum):
    DISMISS = "dismiss"
    DISPUTE = "dispute"
    FIX_BOOKS = "fix_books"
    ESCALATE = "escalate"


ACTION_FOR: dict[ExceptionCode, Action] = {
    ExceptionCode.CLEAN: Action.DISMISS,
    ExceptionCode.ROUNDING: Action.DISMISS,
    ExceptionCode.ZERO_MDR_VIOLATION: Action.DISPUTE,
    ExceptionCode.INSTRUMENT_MISLABEL: Action.DISPUTE,
    ExceptionCode.RATE_MISMATCH: Action.DISPUTE,
    ExceptionCode.GST_MISMATCH: Action.FIX_BOOKS,
    ExceptionCode.REFUND_MDR_RETAINED: Action.DISMISS,
    ExceptionCode.PERIOD_BOUNDARY: Action.FIX_BOOKS,
    ExceptionCode.TDS_CODE_MISMATCH: Action.FIX_BOOKS,
    ExceptionCode.MISSING_FROM_SETTLEMENT: Action.DISPUTE,
    ExceptionCode.UNEXPLAINED: Action.ESCALATE,
}

# Money the merchant can actually get back by asking for it.
RECOVERABLE: frozenset[ExceptionCode] = frozenset({
    ExceptionCode.ZERO_MDR_VIOLATION,
    ExceptionCode.INSTRUMENT_MISLABEL,
    ExceptionCode.RATE_MISMATCH,
    ExceptionCode.MISSING_FROM_SETTLEMENT,
})

# Not a money problem - a filing problem. Separate because the deadline is
# different and the consequence is a lost tax credit, not a lost rupee.
TAX_CREDIT_AT_RISK: frozenset[ExceptionCode] = frozenset({
    ExceptionCode.TDS_CODE_MISMATCH,
    ExceptionCode.GST_MISMATCH,
})

# The "do nothing" codes, named explicitly so the report can count them and
# say so out loud.
NO_ACTION: frozenset[ExceptionCode] = frozenset({
    ExceptionCode.CLEAN,
    ExceptionCode.ROUNDING,
    ExceptionCode.REFUND_MDR_RETAINED,
})

DESCRIPTION: dict[ExceptionCode, str] = {
    ExceptionCode.CLEAN: "Deduction matches the rate card exactly.",
    ExceptionCode.ROUNDING: "Gap sits inside the tolerance band. Noise, not a finding.",
    ExceptionCode.ZERO_MDR_VIOLATION: "Network MDR charged on a rail where it is mandated to zero.",
    ExceptionCode.INSTRUMENT_MISLABEL: "Payment tagged as one instrument, priced as another.",
    ExceptionCode.RATE_MISMATCH: "Charged above the contracted or regulated slab.",
    ExceptionCode.GST_MISMATCH: "GST is not 18% of the fee, or was computed on the wrong base.",
    ExceptionCode.REFUND_MDR_RETAINED: "Fee retained on a refunded order. Expected behaviour at every Indian gateway.",
    ExceptionCode.PERIOD_BOUNDARY: "Ordered in one accounting period, settled in the next.",
    ExceptionCode.TDS_CODE_MISMATCH: "TDS section code does not match the date it was deducted.",
    ExceptionCode.MISSING_FROM_SETTLEMENT: "The order is in the books and in no settlement.",
    ExceptionCode.UNEXPLAINED: "Fits none of the above. A human should look.",
}
