"""
The exception taxonomy for input tax credit.

Same organising idea as the settlement taxonomy: a category is defined by WHAT
THE MERCHANT MUST DO, not by what the mismatch looks like. An invoice missing
from GSTR-2B and an invoice whose supplier quoted the wrong GSTIN produce the
same arithmetic gap and need completely different actions - chase the supplier
to file, versus get one character corrected.

## The inversion worth noticing

The settlement auditor finds money you can get BACK. This one mostly finds
money you are about to LOSE, and money you are about to wrongly CLAIM.

That second kind matters more than it sounds. Since the Supreme Court upheld
CGST s.16(2)(c) in Bhandari Scrap Traders, credit is a statutory concession
rather than a right: claim it when your supplier never paid the tax and you owe
it back with interest at 18% a year. So a finding that says "do not claim this"
is worth exactly as much as one that says "chase this" - it is the difference
between a clean return and a DRC-01C.

## Four codes mean "do not claim"

CLAIM_CLEAN and ROUNDING mean the claim is fine. BLOCKED_CREDIT, TIME_BARRED
and RULE_37_REVERSAL mean stop - and stopping is the finding. A tool that only
ever tells you to claim more is a tool that gets you a notice.
"""

from __future__ import annotations

from enum import StrEnum


class ITCCode(StrEnum):
    # nothing to do
    CLAIM_CLEAN = "CLAIM_CLEAN"          # books and GSTR-2B agree
    ROUNDING = "ROUNDING"                # differ under the tolerance floor

    # chase somebody
    SUPPLIER_NOT_FILED = "SUPPLIER_NOT_FILED"      # in books, absent from 2B
    SUPPLIER_LATE_FILED = "SUPPLIER_LATE_FILED"    # appeared in a later period
    GSTIN_MISMATCH = "GSTIN_MISMATCH"              # filed against the wrong GSTIN
    AMOUNT_MISMATCH = "AMOUNT_MISMATCH"            # both sides, different tax

    # stop claiming
    BLOCKED_CREDIT = "BLOCKED_CREDIT"              # s.17(5) - never claimable
    TIME_BARRED = "TIME_BARRED"                    # past the s.16(4) deadline
    RULE_37_REVERSAL = "RULE_37_REVERSAL"          # supplier unpaid past 180 days
    DUPLICATE_CLAIM = "DUPLICATE_CLAIM"            # same invoice claimed twice

    # look into it
    NOT_IN_BOOKS = "NOT_IN_BOOKS"                  # in 2B, no purchase invoice
    UNEXPLAINED = "UNEXPLAINED"


class ITCAction(StrEnum):
    NONE = "none"
    CHASE_SUPPLIER = "chase_supplier"
    DO_NOT_CLAIM = "do_not_claim"
    REVERSE = "reverse"
    FIX_BOOKS = "fix_books"
    ESCALATE = "escalate"


ACTION_FOR: dict[ITCCode, ITCAction] = {
    ITCCode.CLAIM_CLEAN: ITCAction.NONE,
    ITCCode.ROUNDING: ITCAction.NONE,
    ITCCode.SUPPLIER_NOT_FILED: ITCAction.CHASE_SUPPLIER,
    ITCCode.SUPPLIER_LATE_FILED: ITCAction.FIX_BOOKS,
    ITCCode.GSTIN_MISMATCH: ITCAction.CHASE_SUPPLIER,
    ITCCode.AMOUNT_MISMATCH: ITCAction.CHASE_SUPPLIER,
    ITCCode.BLOCKED_CREDIT: ITCAction.DO_NOT_CLAIM,
    ITCCode.TIME_BARRED: ITCAction.DO_NOT_CLAIM,
    ITCCode.RULE_37_REVERSAL: ITCAction.REVERSE,
    ITCCode.DUPLICATE_CLAIM: ITCAction.DO_NOT_CLAIM,
    ITCCode.NOT_IN_BOOKS: ITCAction.FIX_BOOKS,
    ITCCode.UNEXPLAINED: ITCAction.ESCALATE,
}

# Credit the merchant should get but currently will not, if nobody acts.
AT_RISK: frozenset[ITCCode] = frozenset({
    ITCCode.SUPPLIER_NOT_FILED,
    ITCCode.GSTIN_MISMATCH,
    ITCCode.AMOUNT_MISMATCH,
})

# Credit the merchant is claiming or about to claim that they are not entitled
# to. Left unfixed this becomes a demand with 18% interest under s.50, so it is
# a finding in the merchant's favour even though it reduces what they claim.
OVERCLAIMED: frozenset[ITCCode] = frozenset({
    ITCCode.BLOCKED_CREDIT,
    ITCCode.TIME_BARRED,
    ITCCode.RULE_37_REVERSAL,
    ITCCode.DUPLICATE_CLAIM,
})

# Codes that mean the claim stands as filed.
NO_ACTION: frozenset[ITCCode] = frozenset({
    ITCCode.CLAIM_CLEAN,
    ITCCode.ROUNDING,
})

CODE_LABEL: dict[ITCCode, str] = {
    ITCCode.CLAIM_CLEAN: "Claim is clean",
    ITCCode.ROUNDING: "Rounding difference",
    ITCCode.SUPPLIER_NOT_FILED: "Supplier has not filed",
    ITCCode.SUPPLIER_LATE_FILED: "Supplier filed late",
    ITCCode.GSTIN_MISMATCH: "Filed against the wrong GSTIN",
    ITCCode.AMOUNT_MISMATCH: "Tax amount does not agree",
    ITCCode.BLOCKED_CREDIT: "Blocked credit under s.17(5)",
    ITCCode.TIME_BARRED: "Past the claim deadline",
    ITCCode.RULE_37_REVERSAL: "Supplier unpaid past 180 days",
    ITCCode.DUPLICATE_CLAIM: "Same invoice claimed twice",
    ITCCode.NOT_IN_BOOKS: "In GSTR-2B, not in your books",
    ITCCode.UNEXPLAINED: "Could not be explained",
}
