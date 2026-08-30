"""
The vocabulary for payout timing. Two layers, not one.

## Why there is no UNEXPLAINED here

Every other taxonomy in this codebase carries a catch-all for the record a
rule cannot resolve. This one does not, because nothing here is genuinely
ambiguous at the record level: a settlement date is a fact, the promised
cycle is a fact, and comparing them is a day-count, not a judgment. See
engine/payout_timing/detector.py's module docstring for what was checked
before concluding this (no hold/reason code exists anywhere in this
codebase's settlement data that could make "late" mean "explained" on some
records and "a miss" on others).

## Where the judgment actually is

Not per record - across the batch. One late settlement in an otherwise
clean month is noise; the same rate held for a quarter is a pattern worth
raising. That is Pattern/PayoutAction below, and it is the one thing an
agent is asked about here, exactly once per run - see
agent/payout_timing_classifier.py.
"""

from __future__ import annotations

from enum import StrEnum


class PayoutCode(StrEnum):
    ON_TIME = "ON_TIME"      # settlement_date <= due_date
    SLA_MISS = "SLA_MISS"    # settlement_date >  due_date
    UNMATCHED = "UNMATCHED"  # billed, no settlement row yet - excluded from
                              # delay/float arithmetic, not separately judged


CODE_LABEL: dict[PayoutCode, str] = {
    PayoutCode.ON_TIME: "Settled on time",
    PayoutCode.SLA_MISS: "Settled late",
    PayoutCode.UNMATCHED: "No settlement yet",
}


class Pattern(StrEnum):
    CLEAN = "CLEAN"                    # no misses, or too few to matter
    ISOLATED_DELAY = "ISOLATED_DELAY"  # misses exist, under the systemic bar
    SYSTEMIC_DELAY = "SYSTEMIC_DELAY"  # miss rate or mean delay crosses it


PATTERN_LABEL: dict[Pattern, str] = {
    Pattern.CLEAN: "Settling on schedule",
    Pattern.ISOLATED_DELAY: "A few late settlements",
    Pattern.SYSTEMIC_DELAY: "A systematic delay pattern",
}


class PayoutAction(StrEnum):
    NONE = "none"
    WATCH = "watch"
    ESCALATE = "escalate"


ACTION_LABEL: dict[PayoutAction, str] = {
    PayoutAction.NONE: "Nothing to do",
    PayoutAction.WATCH: "Worth watching",
    PayoutAction.ESCALATE: "Raise it with Razorpay",
}

# The severity ladder an agent may go further on but never soften - same
# convention as engine/treasury/records.py's ACTION_SEVERITY.
ACTION_SEVERITY: dict[str, int] = {
    str(PayoutAction.NONE): 0,
    str(PayoutAction.WATCH): 1,
    str(PayoutAction.ESCALATE): 2,
}
