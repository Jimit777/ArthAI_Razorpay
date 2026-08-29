"""
The exception taxonomy for TDS credit tracking.

Same organising idea as the settlement and ITC taxonomies: a category is
defined by WHAT THE MERCHANT MUST DO. Two payments can miss their expected
credit by an identical rupee amount and need opposite responses - one because
the statement simply has not refreshed yet, one because the deduction was
computed under the wrong regime entirely.

## What this checks that rule 10 does not

The settlement auditor already has a TDS check (engine/detector.py's
`_signal_tds`, rule 10): it looks at a single payment's own settlement report
and asks whether the section code matches the date it was deducted. That is a
one-source check of Razorpay's own paperwork, and it stays exactly as it is.

This taxonomy is a two-source reconciliation: does the credit Razorpay
deducted actually turn up, correctly, on the merchant's OWN government
tax-credit statement (Form 26AS before 1 April 2026, Form 168 after, per
CLAUDE.md section 15)? A payment can pass rule 10 - correctly labelled,
correctly dated - and still have its credit go missing, arrive short, or
land under a stale code on the statement itself. That gap is this agent's
job.

## Two codes mean "do nothing"

CREDIT_CLEAN and ROUNDING mean the credit is fine as it stands. Everything
else means the merchant has to act, or at least look - which is why a
tool that only ever confirms is as important as one that only ever flags.
"""

from __future__ import annotations

from enum import StrEnum


class TdsCode(StrEnum):
    # nothing to do
    CREDIT_CLEAN = "CREDIT_CLEAN"        # deducted and credited agree
    ROUNDING = "ROUNDING"                # differ under the tolerance floor

    # the regime-change errors - mechanical, a pure function of date
    RATE_MISMATCH = "RATE_MISMATCH"      # credit implies the wrong-era rate
    CODE_MISMATCH = "CODE_MISMATCH"      # statement carries the wrong-era code/form

    # judgment - the evidence has to be weighed
    MISSING_CREDIT = "MISSING_CREDIT"    # deducted, absent from the statement
    PERIOD_MISMATCH = "PERIOD_MISMATCH"  # credited in a different FY quarter

    UNEXPLAINED = "UNEXPLAINED"


class TdsAction(StrEnum):
    NONE = "none"
    CHASE = "chase"
    CORRECT_BEFORE_FILING = "correct_before_filing"
    FIX_BOOKS = "fix_books"
    ESCALATE = "escalate"


ACTION_FOR: dict[TdsCode, TdsAction] = {
    TdsCode.CREDIT_CLEAN: TdsAction.NONE,
    TdsCode.ROUNDING: TdsAction.NONE,
    TdsCode.RATE_MISMATCH: TdsAction.CORRECT_BEFORE_FILING,
    TdsCode.CODE_MISMATCH: TdsAction.CORRECT_BEFORE_FILING,
    TdsCode.MISSING_CREDIT: TdsAction.CHASE,
    TdsCode.PERIOD_MISMATCH: TdsAction.FIX_BOOKS,
    TdsCode.UNEXPLAINED: TdsAction.ESCALATE,
}

# Codes that mean the credit stands as it is.
NO_ACTION: frozenset[TdsCode] = frozenset({
    TdsCode.CREDIT_CLEAN,
    TdsCode.ROUNDING,
})

# These need weighing, so they go to the agent even when they are the only
# signal on the record - see engine/gst/detector.py's JUDGMENT_CODES for the
# same convention.
JUDGMENT_CODES = frozenset({
    str(TdsCode.MISSING_CREDIT),
    str(TdsCode.PERIOD_MISMATCH),
})

CODE_LABEL: dict[TdsCode, str] = {
    TdsCode.CREDIT_CLEAN: "Credit is clean",
    TdsCode.ROUNDING: "Rounding difference",
    TdsCode.RATE_MISMATCH: "Wrong-era rate applied",
    TdsCode.CODE_MISMATCH: "Wrong-era section code or form",
    TdsCode.MISSING_CREDIT: "Deducted, no credit on record",
    TdsCode.PERIOD_MISMATCH: "Credited in a different quarter",
    TdsCode.UNEXPLAINED: "Could not be explained",
}
