"""
The promised cycle, and how far a settlement missed it. Pure Python, no
model, never wrong.

## What is reused, and why

`SETTLEMENT_WORKING_DAYS`/`add_working_days` come from `engine.expected_value`
- the canonical version already used by the settlement engine and the live
ledger's own `build_settlement()`. `generator/synthetic.py` keeps a private
copy (`_add_working_days`) that hard-codes the day count instead of
importing the constant; that duplicate is a pre-existing landmine in a file
this module does not touch, not something reused here.

## The holiday calendar this module does NOT have

No Indian bank-holiday calendar exists anywhere in this codebase. "Working
day" here means Monday-Friday, full stop. A real settlement calendar follows
RBI's clearing-corporation notification for the relevant year, not a simple
national list - and a hand-typed approximation would be exactly the kind of
uncited rule CLAUDE.md section 16 warns against ("a wrong rule is worse than
a missing rule"). `HOLIDAY_DATES` is the seam for fixing this properly
later - add real, RBI-sourced dates and every due-date calculation picks
them up with no other change. Until then, a settlement due the day after a
real bank holiday will read as late when it was not the merchant's fault -
a stated limitation, not a silent one. Say so on the results page.

## Why float cost is an assumption, not a citation

There is no statutory interest rate for a late gateway settlement, unlike
GST s.50's 18% on clawed-back input credit. Inventing one here would be the
same mistake as inventing the holiday list. `ASSUMED_COST_OF_CAPITAL_BPS_PER_ANNUM`
is labelled as what it is - a commonly-quoted Indian SME working-capital
rate - shown to the merchant as an assumption they can adjust, never as law.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from engine.expected_value import SETTLEMENT_WORKING_DAYS, add_working_days

# The seam. Empty today; add real, sourced dates and due_date() picks them
# up automatically.
HOLIDAY_DATES: frozenset = frozenset()


def due_date(date_issued: date) -> date:
    """The date a settlement is promised by, given when it was billed."""
    d = add_working_days(date_issued, SETTLEMENT_WORKING_DAYS)
    while d.weekday() >= 5 or d in HOLIDAY_DATES:
        d = add_working_days(d, 1)
    return d


def working_days_between(start: date, end: date) -> int:
    """
    How many working days separate two dates, signed.

    Positive when `end` is after `start`. Counts calendar days and subtracts
    the weekend days in the span - close enough for a delay measured in
    single or low double digits of days, which is the only range this agent
    ever reports.
    """
    from datetime import timedelta

    if end == start:
        return 0
    sign = 1 if end > start else -1
    lo, hi = (start, end) if end > start else (end, start)
    days = (hi - lo).days
    weekends = sum(1 for i in range(days)
                   if (lo + timedelta(days=i)).weekday() >= 5)
    return sign * (days - weekends)


@dataclass(frozen=True)
class PatternThreshold:
    """Where a few late settlements becomes a pattern worth raising."""
    systemic_miss_rate_bps: int = 2_000   # 20% of settled records missing SLA
    systemic_mean_delay_days: int = 2     # or averaging 2+ working days late


# There is no statutory rate for this - see module docstring. 12% p.a. is a
# commonly-quoted Indian SME working-capital cost, stated as an assumption.
ASSUMED_COST_OF_CAPITAL_BPS_PER_ANNUM = 1_200


def float_cost_paise(amount_paise: int, delay_calendar_days: int) -> int:
    """
    What holding this money an extra `delay_calendar_days` cost the merchant,
    at the assumed cost of capital. Calendar days, not working days - float
    is held every day, the SLA clock is the only thing that skips weekends.
    """
    if delay_calendar_days <= 0:
        return 0
    return (amount_paise * ASSUMED_COST_OF_CAPITAL_BPS_PER_ANNUM
            * delay_calendar_days) // (10_000 * 365)


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
