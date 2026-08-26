"""
A thirty-day cash scenario with a crunch planted in it.

## Why the crunch is planted rather than hoped for

Same reason every other generator here returns an answer key (CLAUDE.md
section 7): a forecaster that finds nothing demonstrates nothing, and a demo
that only sometimes contains a problem is a demo that sometimes fails in front
of an audience. The scenario below is built so the trough lands on day 14 and
lands below the floor, and `generate()` returns what it planted so the engine
can be checked against it rather than believed.

## The shape of the squeeze, and why this shape

Day 14 is payday. On the same day the quarterly advance tax instalment falls
due, and the month's rent and cloud bill land within a day either side. None
of that is unusual - salary dates cluster with month-end statutory dates
because both are set by the calendar, which is exactly why the last week of a
month is where merchants actually run out of money.

The gateway settlements are the other half. They are real receivables: money
already captured and not yet credited. Two of them land on day 16, forty-eight
hours after the trough, which is what makes this a SCHEDULING problem rather
than a funding one - and the whole point of the demo is that those two
situations look identical on a bank balance and need completely different
responses.

## What the agent should be able to say

Not "you are short". The arithmetic says that. It should be able to say which
of the outflows around day 14 can move, which cannot, and what it costs to
move each - payroll cannot slip, the tax instalment carries interest per
month, the cloud bill is a phone call.
"""

from __future__ import annotations

import random
from datetime import date, timedelta

from engine.treasury.records import (KIND_PAYROLL, KIND_RECURRING,
                                     KIND_STATUTORY, KIND_VENDOR, BankAccount,
                                     ExpectedReceipt, RecurringExpense,
                                     ScheduledPayout, TreasuryInputs)

# Where the squeeze lands. Day 14 rather than day 2 so a merchant has time to
# act on it - a forecast that only warns you the day before is a diary.
CRUNCH_DAY = 14

VENDORS = [
    "Sundaram Packaging", "Ravi Transport", "Meenakshi Printers",
    "Coastal Freight", "Anand Paper Mills", "Bharat Couriers",
    "Nilgiri Supplies", "Deccan Warehousing",
]


def generate(days: int = 30, seed: int = 20260905, *,
             as_of: date | None = None) -> tuple[TreasuryInputs, dict]:
    """
    Build the scenario and return what was planted.

    Returns (inputs, expected) where expected carries the day the trough was
    built to land on and whether it was built to breach the floor - so the
    engine's answer can be checked rather than trusted.
    """
    rng = random.Random(seed)
    today = as_of or date.today()

    inputs = TreasuryInputs(as_of=today)
    inputs.accounts.append(BankAccount(
        account_id="acc_current", nickname="HDFC current account",
        balance=7_05_000_00, as_of=today, overdraft_limit=0))

    # --- money coming in ---------------------------------------------------
    #
    # Gateway settlements, T+2 from capture. Steady, unremarkable, and
    # deliberately NOT enough on their own to cover day 14 - the two large
    # ones land on day 16, which is the entire point of the scenario.
    for offset in range(1, days + 1):
        if offset % 2 == 0:
            continue
        inputs.receipts.append(ExpectedReceipt(
            reference=f"setl_{rng.randrange(16 ** 8):08x}",
            source="gateway settlement",
            amount=rng.randint(35_000, 95_000) * 100,
            expected_on=today + timedelta(days=offset)))

    inputs.receipts.append(ExpectedReceipt(
        reference="setl_bigticket01", source="gateway settlement",
        amount=2_00_000_00, expected_on=today + timedelta(days=CRUNCH_DAY + 2)))
    inputs.receipts.append(ExpectedReceipt(
        reference="setl_bigticket02", source="gateway settlement",
        amount=1_40_000_00, expected_on=today + timedelta(days=CRUNCH_DAY + 2)))

    # --- money going out ---------------------------------------------------
    #
    # The cluster. Payroll and the tax instalment cannot move; the vendor
    # invoices can, by a week. That difference is what turns "you are short"
    # into "move these two and you are fine".
    inputs.payouts.append(ScheduledPayout(
        payout_id="PAY-PAYROLL-08", payee="Payroll - 14 staff",
        amount=6_20_000_00, due_on=today + timedelta(days=CRUNCH_DAY),
        kind=KIND_PAYROLL))
    inputs.payouts.append(ScheduledPayout(
        payout_id="PAY-TDS-Q2", payee="Advance tax instalment",
        amount=1_75_000_00, due_on=today + timedelta(days=CRUNCH_DAY),
        kind=KIND_STATUTORY))

    inputs.payouts.append(ScheduledPayout(
        payout_id="V-1042", payee="Sundaram Packaging",
        amount=1_10_000_00, due_on=today + timedelta(days=CRUNCH_DAY - 1),
        kind=KIND_VENDOR))
    inputs.payouts.append(ScheduledPayout(
        payout_id="V-1051", payee="Coastal Freight",
        amount=68_000_00, due_on=today + timedelta(days=CRUNCH_DAY + 1),
        kind=KIND_VENDOR))

    # Ordinary vendor traffic across the rest of the month, so the curve is a
    # curve rather than a flat line with one cliff in it.
    for i in range(6):
        offset = rng.choice([3, 5, 8, 19, 22, 25, 27])
        inputs.payouts.append(ScheduledPayout(
            payout_id=f"V-{1100 + i}", payee=VENDORS[i % len(VENDORS)],
            amount=rng.randint(12_000, 48_000) * 100,
            due_on=today + timedelta(days=offset), kind=KIND_VENDOR))

    # --- what recurs, inferred from the statement --------------------------
    inputs.recurring.append(RecurringExpense(
        name="Office rent", amount=1_45_000_00,
        day_of_month=(today + timedelta(days=CRUNCH_DAY)).day,
        kind=KIND_RECURRING, seen_in_months=6, confidence=0.98))
    inputs.recurring.append(RecurringExpense(
        name="AWS", amount=82_000_00,
        day_of_month=(today + timedelta(days=CRUNCH_DAY - 1)).day,
        kind=KIND_RECURRING, seen_in_months=6, confidence=0.95))
    inputs.recurring.append(RecurringExpense(
        name="Zoho Books and payroll software", amount=9_400_00,
        day_of_month=(today + timedelta(days=6)).day,
        kind=KIND_RECURRING, seen_in_months=5, confidence=0.9))

    return inputs, {
        "crunch_day": CRUNCH_DAY,
        "expected_finding": "CASH_CRUNCH_WARNING",
        "coverable_by_delay": True,
        "unmovable_on_crunch_day": ["PAY-PAYROLL-08", "PAY-TDS-Q2"],
        "relief_lands_on_day": CRUNCH_DAY + 2,
    }
