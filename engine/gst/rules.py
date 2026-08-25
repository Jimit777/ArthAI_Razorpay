"""
The input tax credit rules. Pure Python, no model, never wrong.

Same architectural rule as the settlement engine (CLAUDE.md section 2): the
calculator decides what the law says. The agent decides what a gap MEANS. Ask a
model whether Rs 4,118 of credit is claimable and you get an answer that is
usually right and occasionally, silently, wrong.

## Every rule here is somebody else's writing

| # | Rule | Source |
|---|---|---|
| 1 | Credit only if the supplier actually paid the tax | CGST s.16(2)(c), upheld in Bhandari Scrap Traders |
| 2 | Credit only on an invoice that appears in GSTR-2B | CGST s.16(2)(aa) |
| 3 | Claim deadline: 30 November following the financial year | CGST s.16(4) |
| 4 | Reverse the credit if the supplier is unpaid after 180 days | CGST Rule 37 |
| 5 | Blocked categories are never claimable | CGST s.17(5) |
| 6 | Auto-notice when claimed exceeds available by > Rs 1 lakh or 20% | Rule 88D / DRC-01C |
| 7 | Interest on wrongly claimed credit runs at 18% a year | CGST s.50 |
| 8 | A GSTIN is 15 characters and carries a state code and a checksum | GST registration format |

Rule 4 is this taxonomy's version of the settlement auditor's rule 8: it
prevents a class of false alarms in one direction and catches a real liability
in the other. An unpaid supplier invoice is not a missing credit, it is a
credit you have to give back.

ALL MONEY IN PAISE, AS INTEGERS.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

# --- rule 3: the deadline --------------------------------------------------
CLAIM_DEADLINE_MONTH = 11
CLAIM_DEADLINE_DAY = 30
SOURCE_DEADLINE = "CGST Act s.16(4) - by 30 November following the financial year"

# --- rule 4: supplier payment window ---------------------------------------
SUPPLIER_PAYMENT_DAYS = 180
SOURCE_RULE_37 = "CGST Rule 37 - reverse the credit if the supplier is unpaid after 180 days"

# --- rule 6: the automatic notice ------------------------------------------
# Rule 88D issues a DRC-01C the moment claimed credit exceeds available credit
# by more than Rs 1 lakh OR 20%, WHICHEVER IS LOWER. Lower, not higher - which
# means a small business hits the percentage long before the rupee figure, and
# a large one hits the rupee figure first. Getting this backwards would tell a
# merchant they are safe while a notice is already generating.
NOTICE_ABSOLUTE_PAISE = 100_000_00          # Rs 1,00,000
NOTICE_PCT_BPS = 2_000                      # 20%
SOURCE_RULE_88D = "CGST Rule 88D - DRC-01C is auto-issued above Rs 1 lakh or 20%, whichever is lower"

# --- rule 7 -----------------------------------------------------------------
INTEREST_PCT_BPS = 1_800                    # 18% a year
SOURCE_INTEREST = "CGST Act s.50 - interest at 18% a year on credit wrongly claimed"

SOURCE_SUPPLIER_PAID = ("CGST Act s.16(2)(c), upheld in Bhandari Scrap Traders "
                        "v. Union of India - credit is a statutory concession, "
                        "and the buyer carries the burden of proving the "
                        "supplier paid")
SOURCE_IN_2B = "CGST Act s.16(2)(aa) - the invoice must appear in GSTR-2B"

# --- rule 5: blocked credits ------------------------------------------------
# s.17(5) is a long list. These are the ones an ordinary trading business
# actually encounters; the rest are deliberately absent rather than guessed at,
# because a wrong rule is worse than a missing one (CLAUDE.md section 6).
BLOCKED_CATEGORIES: dict[str, str] = {
    "motor_vehicle": "s.17(5)(a) - motor vehicles seating up to 13 people",
    "food_beverage": "s.17(5)(b)(i) - food, beverages and outdoor catering",
    "club_membership": "s.17(5)(b)(ii) - club, health and fitness centre membership",
    "employee_travel": "s.17(5)(b)(vi) - travel benefits given to employees on leave",
    "works_contract_immovable": "s.17(5)(c) - works contract for immovable property",
    "personal_consumption": "s.17(5)(g) - goods or services for personal consumption",
    "lost_stolen_written_off": "s.17(5)(h) - goods lost, stolen, destroyed or written off",
}

# The GSTIN's first two characters are the state. A supplier filing under a
# GSTIN whose state code differs from the one on the invoice is the single most
# common cause of an invoice that exists on both sides and matches nothing.
GSTIN_LENGTH = 15


@dataclass(frozen=True)
class Tolerance:
    """Below this, a difference is arithmetic noise rather than a finding."""
    floor_paise: int = 100                  # Rs 1
    pct_bps: int = 50                       # 0.5%

    def band(self, amount_paise: int) -> int:
        return max(self.floor_paise, (abs(amount_paise) * self.pct_bps) // 10_000)


def financial_year_of(when: date) -> int:
    """India's financial year runs April to March. FY2026-27 is returned as 2026."""
    return when.year if when.month >= 4 else when.year - 1


def claim_deadline(invoice_date: date) -> date:
    """
    The last day credit on this invoice can be claimed.

    s.16(4): by 30 November following the end of the financial year the invoice
    belongs to. An invoice dated 2 April 2026 is in FY2026-27, so the deadline
    is 30 November 2027 - nearly twenty months. One dated 30 March 2026 is in
    FY2025-26 and the deadline is 30 November 2026, eight months. Same-looking
    invoices, wildly different urgency, which is exactly the sort of thing that
    gets missed by hand.
    """
    return date(financial_year_of(invoice_date) + 1,
                CLAIM_DEADLINE_MONTH, CLAIM_DEADLINE_DAY)


def is_time_barred(invoice_date: date, today: date) -> bool:
    return today > claim_deadline(invoice_date)


def days_to_deadline(invoice_date: date, today: date) -> int:
    return (claim_deadline(invoice_date) - today).days


def payment_due_by(invoice_date: date) -> date:
    """Rule 37: 180 days from the invoice date to pay the supplier."""
    return invoice_date + timedelta(days=SUPPLIER_PAYMENT_DAYS)


def needs_rule_37_reversal(invoice_date: date, paid_on: Optional[date],
                           today: date) -> bool:
    if paid_on is not None:
        return False
    return today > payment_due_by(invoice_date)


def blocked_reason(category: Optional[str]) -> Optional[str]:
    """The s.17(5) citation for a category, or None if it is claimable."""
    if not category:
        return None
    return BLOCKED_CATEGORIES.get(category)


def notice_threshold(available_paise: int) -> int:
    """
    How far claimed credit may exceed available credit before Rule 88D fires.

    Whichever is LOWER of Rs 1 lakh and 20% of what is available.
    """
    pct = (abs(available_paise) * NOTICE_PCT_BPS) // 10_000
    return min(NOTICE_ABSOLUTE_PAISE, pct)


def triggers_notice(claimed_paise: int, available_paise: int) -> bool:
    excess = claimed_paise - available_paise
    return excess > notice_threshold(available_paise)


def interest_on(amount_paise: int, days: int) -> int:
    """
    Interest under s.50 on credit wrongly claimed, in paise.

    Simple interest at 18% a year, day-counted on 365. Rounded half-up, and
    computed in integers throughout - a float here would drift by paise across
    a batch and the whole point is that the arithmetic is not arguable.
    """
    if amount_paise <= 0 or days <= 0:
        return 0
    return (abs(amount_paise) * INTEREST_PCT_BPS * days + 365 * 10_000 // 2) \
        // (365 * 10_000)


def gstin_state(gstin: str) -> Optional[str]:
    if not gstin or len(gstin) < 2:
        return None
    return gstin[:2]


def gstin_well_formed(gstin: str) -> bool:
    return bool(gstin) and len(gstin) == GSTIN_LENGTH and gstin[:2].isdigit()


def orphan_id(gstin: str, invoice_number: str) -> str:
    """
    The id for a GSTR-2B line that no purchase invoice claims.

    GSTR-2B has no row id of its own - a line is identified by whose GSTIN
    filed it and what they numbered it. Both the generator's answer key and the
    detector derive the id here so they cannot drift apart; when they did, two
    records scored as wrong that were actually right.
    """
    return f"2b_{gstin.strip().upper()}_{invoice_number.strip().upper()}"


def rupees(paise: int) -> str:
    """Indian digit grouping. Rs 12,34,567.89, not Rs 1,234,567.89."""
    sign = "-" if paise < 0 else ""
    whole, frac = divmod(abs(paise), 100)
    s = str(whole)
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        s = ",".join(parts + [tail])
    return f"{sign}Rs {s}.{frac:02d}"
