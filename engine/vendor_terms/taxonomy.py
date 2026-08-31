"""
The vocabulary for the vendor invoice auditor. One layer, three codes -
deliberately fewer than every other taxonomy in this codebase, because the
question here is narrower than GST classification or ITC eligibility: is
this one line item priced the way the merchant agreed it would be.

## Why there is no UNDERBILLED and no QUANTITY_MISMATCH

A supplier who charges LESS than the contracted price is not a finding -
same "don't flag what's in the merchant's favour" discipline
engine/gst_filing/timing.py already applies to a GST overpayment, and
engine/gst/rules.py's rule 4 applies to Rule 37 (a reversal is a real
liability, being undercharged never is). It stays RATE_CLEAN.

A quantity mismatch (billed 12, delivery note says 10) would need a
delivery-note or goods-received-note data source this platform does not
have and has no import path for - inventing a check with no real data
behind it is the exact failure CLAUDE.md section 16 warns against for a
missing rule. Left out entirely rather than guessed at.

## Why RATE_UNCONFIGURED is not an error

An item whose description does not match anything on the merchant's own
rate card is not evidence of overbilling - it is evidence the rate card is
incomplete. Same discipline as GSTR1Code.HSN_RATE_UNCONFIGURED: excluded
from the found-money total, never guessed at, fixed by the merchant adding
a rate-card row.
"""

from __future__ import annotations

from enum import StrEnum


class TermsCode(StrEnum):
    RATE_CLEAN = "RATE_CLEAN"                # at or below the contracted price
    RATE_UNCONFIGURED = "RATE_UNCONFIGURED"   # no contracted price on file
    OVERBILLED = "OVERBILLED"                 # above the contracted price,
                                               # past tolerance


TERMS_LABEL: dict[TermsCode, str] = {
    TermsCode.RATE_CLEAN: "Matches the contracted price",
    TermsCode.RATE_UNCONFIGURED: "No rate on file for this item",
    TermsCode.OVERBILLED: "Billed above the contracted price",
}


class TermsAction(StrEnum):
    NONE = "none"
    ADD_TO_RATE_CARD = "add_to_rate_card"
    REQUEST_CREDIT_NOTE = "request_credit_note"


TERMS_ACTION_FOR: dict[TermsCode, TermsAction] = {
    TermsCode.RATE_CLEAN: TermsAction.NONE,
    TermsCode.RATE_UNCONFIGURED: TermsAction.ADD_TO_RATE_CARD,
    TermsCode.OVERBILLED: TermsAction.REQUEST_CREDIT_NOTE,
}

# Two of three codes mean "no dispute" - the same three-vs-rest split every
# taxonomy in this project keeps deliberately visible (CLAUDE.md section 5).
NO_ACTION = {TermsCode.RATE_CLEAN, TermsCode.RATE_UNCONFIGURED}
