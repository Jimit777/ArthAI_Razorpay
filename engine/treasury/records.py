"""
What a cash position is made of, and what can go wrong with it.

## The four things that move a balance

    RECEIPTS   money on its way in. For a merchant this is mostly gateway
               settlements that have been captured and not yet credited -
               already earned, already known, not yet arrived.
    PAYOUTS    scheduled outflows with a date on them: payroll, a vendor
               invoice due, an advance tax instalment.
    RECURRING  the outflows nobody schedules because they happen every month
               anyway - rent, AWS, a SaaS subscription. Inferred from the
               bank statement rather than entered.
    BALANCE    what is in the account today.

## Why "delayable" is a field and not a judgment

The whole point of this agent is to answer "what do I move?" when the money
does not stretch. That question turns on which outflows CAN move, and that is
a property of the outflow, not an opinion:

    payroll   cannot move. Delaying it is a legal problem and a human one.
    statutory cannot move. TDS and GST have penalties with dates attached.
    vendor    usually can, by a few days, at the cost of a phone call.
    recurring usually can, at the cost of a service interruption.

Recording it as data means the arithmetic can work out whether a shortfall is
coverable, and the agent is left with the part that is genuinely judgment:
WHICH of the movable ones to move, and what it will cost to move it.

## Money

Paise, as integers. A thirty-day running balance built from floats accumulates
error every day and produces a trough that is off by rupees on day 30 - which
is exactly the figure somebody would act on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional

# --- what an outflow is, and whether it can move --------------------------

KIND_PAYROLL = "payroll"
KIND_STATUTORY = "statutory"
KIND_VENDOR = "vendor"
KIND_RECURRING = "recurring"
KIND_LOAN = "loan"

KIND_LABEL = {
    KIND_PAYROLL: "Payroll",
    KIND_STATUTORY: "Tax or statutory dues",
    KIND_VENDOR: "Vendor payment",
    KIND_RECURRING: "Recurring subscription or rent",
    KIND_LOAN: "Loan repayment",
}

# Which kinds can be moved, and by how many days before it stops being a
# short delay and becomes a default. Stated here to be argued with rather
# than buried in a branch.
DELAYABLE = {
    KIND_PAYROLL: 0,
    KIND_STATUTORY: 0,
    KIND_LOAN: 0,
    KIND_VENDOR: 7,
    KIND_RECURRING: 5,
}


@dataclass
class BankAccount:
    account_id: str
    nickname: str
    balance: int                        # paise, as of `as_of`
    as_of: date
    overdraft_limit: int = 0            # paise of agreed OD, 0 if none


@dataclass
class ExpectedReceipt:
    """Money already earned and not yet in the account."""
    reference: str                      # settlement id, invoice number
    source: str                         # "gateway settlement" | "customer"
    amount: int                         # paise
    expected_on: date
    certain: bool = True                # False for a customer who may be late


@dataclass
class ScheduledPayout:
    """An outflow with a date and a name on it."""
    payout_id: str
    payee: str
    amount: int                         # paise
    due_on: date
    kind: str = KIND_VENDOR

    @property
    def delay_days(self) -> int:
        """How many days this could move, if it has to."""
        return DELAYABLE.get(self.kind, 0)

    @property
    def movable(self) -> bool:
        return self.delay_days > 0


@dataclass
class RecurringExpense:
    """
    An outflow nobody schedules, inferred from the statement.

    `day_of_month` rather than a date, because that is what recurrence
    actually is - and `confidence` because an inference from three months of
    statement is not the same claim as an invoice with a due date on it.
    """
    name: str
    amount: int                         # paise
    day_of_month: int
    kind: str = KIND_RECURRING
    seen_in_months: int = 0
    confidence: float = 0.0


@dataclass
class TreasuryInputs:
    """Everything the forecast is computed from."""
    accounts: list = field(default_factory=list)
    receipts: list = field(default_factory=list)
    payouts: list = field(default_factory=list)
    recurring: list = field(default_factory=list)
    as_of: Optional[date] = None

    @property
    def opening_balance(self) -> int:
        return sum(a.balance for a in self.accounts)

    @property
    def overdraft_available(self) -> int:
        return sum(a.overdraft_limit for a in self.accounts)

    @property
    def total_records(self) -> int:
        return (len(self.accounts) + len(self.receipts) + len(self.payouts)
                + len(self.recurring))


# --- one day of the projection --------------------------------------------


@dataclass
class DailyPosition:
    """
    One day. The unified contract every source converges on.

    Carries the movements as well as the balance, because a trough with no
    explanation is a number a merchant can panic about and not act on.
    """
    day: int                            # 1..30, 0 is today's opening
    on: date
    opening: int
    receipts: int = 0
    payouts: int = 0
    recurring: int = 0
    receipt_lines: list = field(default_factory=list)
    payout_lines: list = field(default_factory=list)
    recurring_lines: list = field(default_factory=list)

    @property
    def outflow(self) -> int:
        return self.payouts + self.recurring

    @property
    def net(self) -> int:
        return self.receipts - self.outflow

    @property
    def closing(self) -> int:
        return self.opening + self.net

    def as_dict(self) -> dict:
        from engine.gst import rules

        return {
            "day": self.day, "date": str(self.on),
            "opening": self.opening, "receipts": self.receipts,
            "payouts": self.payouts, "recurring": self.recurring,
            "net": self.net, "closing": self.closing,
            "closing_display": rules.rupees(self.closing),
            "receipt_lines": list(self.receipt_lines),
            "payout_lines": list(self.payout_lines),
            "recurring_lines": list(self.recurring_lines),
        }


# --- what the forecast can conclude ---------------------------------------

CASH_HEALTHY = "CASH_HEALTHY"
CASH_TIGHT = "CASH_TIGHT"
CASH_CRUNCH_WARNING = "CASH_CRUNCH_WARNING"
CASH_OVERDRAWN = "CASH_OVERDRAWN"

FINDING_LABEL = {
    CASH_HEALTHY: "Comfortable",
    CASH_TIGHT: "Tight, but it holds",
    CASH_CRUNCH_WARNING: "Cash runs short",
    CASH_OVERDRAWN: "The account goes negative",
}

# What to do about it. Same discipline as every other agent here: a code is
# named after the action a person takes, not after the shape of the problem.
ACT_NONE = "none"
ACT_WATCH = "watch"
ACT_DELAY_PAYOUT = "delay_payout"
ACT_CHASE_RECEIVABLES = "chase_receivables"
ACT_DRAW_CREDIT_LINE = "draw_credit_line"

ACTION_LABEL = {
    ACT_NONE: "Nothing to do",
    ACT_WATCH: "Watch it",
    ACT_DELAY_PAYOUT: "Move a payout",
    ACT_CHASE_RECEIVABLES: "Chase what is owed to you",
    ACT_DRAW_CREDIT_LINE: "Arrange credit now",
}

# How serious each action is, so "the agent would go further" can be told
# from "the agent disagrees".
ACTION_SEVERITY = {ACT_NONE: 0, ACT_WATCH: 1, ACT_DELAY_PAYOUT: 2,
                   ACT_CHASE_RECEIVABLES: 3, ACT_DRAW_CREDIT_LINE: 4}
