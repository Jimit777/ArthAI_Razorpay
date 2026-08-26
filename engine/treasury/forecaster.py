"""
The forward cash projection. Pure arithmetic - no model touches this.

## The recurrence, stated once

    Balance[D] = Balance[D-1] + Receipts[D] - Payouts[D] - Recurring[D]

Thirty of those, in integer paise, carrying the movements that made each day
so a trough can be explained rather than merely announced.

## Why the threshold is a floor and not a target

A merchant does not run out of money at zero. They run out at whatever balance
their next unmovable obligation needs, and being at Rs 4,000 with payroll due
is the same emergency as being at Rs 0. So the warning fires on a stated safe
floor, and the floor sits here where somebody can argue with it rather than
inside a branch nobody can find.

## What "coverable" means, and why it is arithmetic

When the balance goes under, the first question is whether moving something
would fix it. That is a sum: take the outflows around the trough that CAN
move - see DELAYABLE in records.py, which is a property of the outflow, not
an opinion - and compare their total to the shortfall.

If they cover it, this is a scheduling problem. If they do not, it is a
funding problem, and no amount of rescheduling will make it a scheduling one.
Getting that wrong in either direction is expensive: telling a merchant to
shuffle payments when they need a credit line loses them the week they needed
to arrange it.

Which of the movable ones to actually move is the part left to the agent. That
is a judgment about relationships and consequences, and it is the one thing
here a spreadsheet genuinely cannot do.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

from engine.treasury.records import (ACT_CHASE_RECEIVABLES, ACT_DELAY_PAYOUT,
                                     ACT_DRAW_CREDIT_LINE, ACT_NONE, ACT_WATCH,
                                     CASH_CRUNCH_WARNING, CASH_HEALTHY,
                                     CASH_OVERDRAWN, CASH_TIGHT, DailyPosition,
                                     TreasuryInputs)

DEFAULT_DAYS = 30

# The balance below which a merchant is in trouble whatever the arithmetic
# says. Fifty thousand rupees, in paise. Configurable per business; this is
# the default and it is deliberately not zero.
SAFE_FLOOR_PAISE = 50_00_000

# How far either side of the trough an outflow counts as "around" it. A payout
# three days after the low point is part of the same squeeze; one three weeks
# later is a different problem.
NEAR_TROUGH_DAYS = 3


@dataclass
class Trough:
    """The low point, and everything about it worth acting on."""
    day: int
    on: date
    balance: int
    shortfall: int                      # paise below the floor, 0 if above
    below_zero: bool = False


@dataclass
class Forecast:
    positions: list = field(default_factory=list)
    trough: Optional[Trough] = None
    finding: str = CASH_HEALTHY
    action: str = ACT_NONE
    floor: int = SAFE_FLOOR_PAISE
    opening_balance: int = 0
    overdraft_available: int = 0
    movable_near_trough: list = field(default_factory=list)
    movable_total: int = 0
    unmovable_near_trough: list = field(default_factory=list)
    receipts_after_trough: int = 0
    detail: str = ""

    @property
    def coverable_by_delay(self) -> bool:
        """Whether moving what CAN move would clear the shortfall."""
        if self.trough is None or not self.trough.shortfall:
            return False
        return self.movable_total >= self.trough.shortfall

    @property
    def closing_balance(self) -> int:
        return self.positions[-1].closing if self.positions else 0

    def as_dict(self) -> dict:
        from engine.gst import rules

        from engine.treasury.records import ACTION_LABEL, FINDING_LABEL

        trough = None
        if self.trough is not None:
            trough = {
                "day": self.trough.day, "date": str(self.trough.on),
                "balance": self.trough.balance,
                "balance_display": rules.rupees(self.trough.balance),
                "shortfall": self.trough.shortfall,
                "shortfall_display": rules.rupees(self.trough.shortfall),
                "below_zero": self.trough.below_zero,
            }
        return {
            "positions": [p.as_dict() for p in self.positions],
            "trough": trough,
            "finding_type": self.finding,
            "finding_label": FINDING_LABEL.get(self.finding, self.finding),
            "action": self.action,
            "action_label": ACTION_LABEL.get(self.action, self.action),
            "floor": self.floor,
            "floor_display": rules.rupees(self.floor),
            "opening_balance": self.opening_balance,
            "opening_display": rules.rupees(self.opening_balance),
            "closing_balance": self.closing_balance,
            "closing_display": rules.rupees(self.closing_balance),
            "overdraft_available": self.overdraft_available,
            "movable_near_trough": list(self.movable_near_trough),
            "movable_total": self.movable_total,
            "movable_total_display": rules.rupees(self.movable_total),
            "unmovable_near_trough": list(self.unmovable_near_trough),
            "receipts_after_trough": self.receipts_after_trough,
            "receipts_after_trough_display": rules.rupees(
                self.receipts_after_trough),
            "coverable_by_delay": self.coverable_by_delay,
            "detail": self.detail,
        }


def project_cash_flow(inputs: TreasuryInputs, *, days: int = DEFAULT_DAYS,
                      floor: int = SAFE_FLOOR_PAISE) -> Forecast:
    """
    Thirty days forward, one position per day.

    Every figure a merchant will read comes out of here. The agent is handed
    the result and asked what to do about it; it is never asked what the
    numbers are.
    """
    from engine.gst import rules

    today = inputs.as_of or date.today()
    out = Forecast(floor=floor, opening_balance=inputs.opening_balance,
                   overdraft_available=inputs.overdraft_available)

    receipts_by_day: dict[date, list] = {}
    for receipt in inputs.receipts:
        receipts_by_day.setdefault(receipt.expected_on, []).append(receipt)

    payouts_by_day: dict[date, list] = {}
    for payout in inputs.payouts:
        payouts_by_day.setdefault(payout.due_on, []).append(payout)

    balance = inputs.opening_balance
    for offset in range(1, days + 1):
        on = today + timedelta(days=offset)
        position = DailyPosition(day=offset, on=on, opening=balance)

        for receipt in receipts_by_day.get(on, []):
            position.receipts += receipt.amount
            position.receipt_lines.append({
                "reference": receipt.reference, "source": receipt.source,
                "amount": receipt.amount,
                "amount_display": rules.rupees(receipt.amount),
                "certain": receipt.certain})

        for payout in payouts_by_day.get(on, []):
            position.payouts += payout.amount
            position.payout_lines.append({
                "payout_id": payout.payout_id, "payee": payout.payee,
                "amount": payout.amount,
                "amount_display": rules.rupees(payout.amount),
                "kind": payout.kind, "movable": payout.movable,
                "delay_days": payout.delay_days})

        for expense in inputs.recurring:
            if _falls_on(expense.day_of_month, on):
                position.recurring += expense.amount
                position.recurring_lines.append({
                    "name": expense.name, "amount": expense.amount,
                    "amount_display": rules.rupees(expense.amount),
                    "kind": expense.kind, "movable": True,
                    "confidence": expense.confidence})

        balance = position.closing
        out.positions.append(position)

    _find_trough(out, floor)
    _explain(out, inputs)
    return out


def _falls_on(day_of_month: int, on: date) -> bool:
    """
    Whether a monthly expense lands on this date.

    A charge set for the 31st has to land somewhere in a 30-day month, and the
    last day is where banks actually take it. Dropping it instead would
    understate the outflow in exactly the months that are tightest.
    """
    from calendar import monthrange

    last = monthrange(on.year, on.month)[1]
    return on.day == min(day_of_month, last)


def _find_trough(out: Forecast, floor: int) -> None:
    """The lowest point in the projection, and how far below the floor it is."""
    if not out.positions:
        return
    low = min(out.positions, key=lambda p: p.closing)
    out.trough = Trough(
        day=low.day, on=low.on, balance=low.closing,
        shortfall=max(0, floor - low.closing),
        below_zero=low.closing < 0)

    if low.closing < 0:
        out.finding = CASH_OVERDRAWN
    elif low.closing < floor:
        out.finding = CASH_CRUNCH_WARNING
    elif low.closing < floor * 2:
        out.finding = CASH_TIGHT
    else:
        out.finding = CASH_HEALTHY


def _explain(out: Forecast, inputs: TreasuryInputs) -> None:
    """
    What is around the trough, and whether moving it would be enough.

    All arithmetic. The agent gets these lists and decides which one to
    actually move; it is never asked to add them up.
    """
    from engine.gst import rules

    if out.trough is None:
        return

    near = [p for p in out.positions
            if abs(p.day - out.trough.day) <= NEAR_TROUGH_DAYS]
    for position in near:
        for line in position.payout_lines + position.recurring_lines:
            entry = {**line, "day": position.day, "date": str(position.on)}
            if line.get("movable"):
                out.movable_near_trough.append(entry)
                out.movable_total += line["amount"]
            else:
                out.unmovable_near_trough.append(entry)

    out.receipts_after_trough = sum(
        p.receipts for p in out.positions if p.day > out.trough.day)

    if out.finding == CASH_HEALTHY:
        out.action = ACT_NONE
        out.detail = (f"The balance never falls below "
                      f"{rules.rupees(out.trough.balance)}, on "
                      f"{out.trough.on}. Nothing needs moving.")
        return
    if out.finding == CASH_TIGHT:
        out.action = ACT_WATCH
        out.detail = (f"The balance dips to "
                      f"{rules.rupees(out.trough.balance)} on {out.trough.on} "
                      f"and recovers. It stays above the "
                      f"{rules.rupees(out.floor)} floor, but not by much.")
        return

    # Under the floor. The question is whether it is a scheduling problem or a
    # funding one, and that is a comparison, not a judgment.
    out.detail = (
        f"The balance falls to {rules.rupees(out.trough.balance)} on "
        f"{out.trough.on}, which is {rules.rupees(out.trough.shortfall)} "
        f"below the {rules.rupees(out.floor)} floor. "
        f"{rules.rupees(out.movable_total)} of what falls due around that "
        f"date could be moved; "
        f"{rules.rupees(sum(u['amount'] for u in out.unmovable_near_trough))} "
        f"could not.")

    if out.coverable_by_delay:
        out.action = ACT_DELAY_PAYOUT
    elif out.receipts_after_trough >= out.trough.shortfall:
        # Money is coming, just not soon enough. Pulling it forward is a
        # different job from borrowing, and a cheaper one.
        out.action = ACT_CHASE_RECEIVABLES
    else:
        out.action = ACT_DRAW_CREDIT_LINE


def recommended_action(forecast: Forecast) -> str:
    """The action, from the figures alone. Kept so the agent cannot relax it."""
    return forecast.action
