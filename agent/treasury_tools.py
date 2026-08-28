"""
The cash forecaster's tools. Three of them, all read-only.

## Why this file exists at all

Until now this agent was handed a page of evidence and asked for a verdict in
one shot. It could not check anything. It picked a payout off a list and hoped,
and the platform printed the result as though it had been verified.

That is the difference between a classifier with a narration layer and an
agent. These tools close it: the model can now ask a question, get a computed
answer back, and ask another before it commits to advice.

## Read-only by construction, not by instruction

Same rule as agent/tools.py: guardrail 1 says the agent never writes to a
ledger, and that is enforced by never giving it a tool that can write. There
is no move, no schedule, no pay. `what_if_delayed` SIMULATES a delay against a
copy and returns what would happen. Nothing it does survives the call.

## Every figure arrives already computed

The tools return JSON with the arithmetic done and the money formatted. The
agent reads figures; it never derives them. A forecast is thirty additions
where each day depends on the last, and that is the single worst place to let
a model do sums - so the boundary is drawn here rather than trusted to a
prompt.
"""

from __future__ import annotations

import json
from typing import Callable, Optional

from anthropic import beta_tool

from engine.gst import rules
from engine.treasury.forecaster import project_with


def _money(paise: int) -> dict:
    """Money crosses the boundary as both paise and a formatted string.

    The agent quotes the string. It never sees a number it has to format, and
    it never has to divide by a hundred - which is arithmetic, and banned.
    """
    return {"paise": paise, "display": rules.rupees(paise)}


def build_tools(inputs, forecast, *, floor: Optional[int] = None
                ) -> list[Callable]:
    """
    Build the tool set bound to one forecast.

    Closures over this run's inputs rather than a global, so two forecasts can
    be judged side by side without seeing each other's money.
    """
    floor = forecast.floor if floor is None else floor
    days = len(forecast.positions)
    by_id = {p.payout_id: p for p in inputs.payouts}
    base_low = forecast.trough.balance if forecast.trough else 0
    base_day = forecast.trough.day if forecast.trough else 0

    @beta_tool
    def what_if_delayed(payout_id: str, days_later: int) -> str:
        """Re-run the whole 30-day forecast with one payout moved later, and
        report what the low point becomes.

        Use this to CHECK a recommendation before you make it. Moving a payout
        that falls after the low point will not raise it, and moving one can
        create a new shortfall on the day it lands instead. Both show up here.

        Args:
            payout_id: The id of the payout to move, exactly as given in the
                evidence.
            days_later: How many days to push it back.
        """
        payout = by_id.get(payout_id)
        if payout is None:
            return json.dumps({
                "error": f"no payout {payout_id} in this forecast",
                "known_ids": sorted(by_id)[:20]})
        if not payout.movable:
            return json.dumps({
                "payout_id": payout_id, "payee": payout.payee,
                "kind": payout.kind, "refused": True,
                "why": "This cannot be moved. Payroll and statutory dues have "
                       "dates somebody else set, and delaying one is a "
                       "default rather than a scheduling decision."})

        capped = min(int(days_later), payout.delay_days)
        after = project_with(inputs, move={payout_id: capped}, days=days,
                             floor=floor)
        low = after.trough
        return json.dumps({
            "payout_id": payout_id, "payee": payout.payee,
            "moved_by_days": capped,
            "capped": capped < int(days_later),
            "furthest_it_can_move_days": payout.delay_days,
            "amount": _money(payout.amount),
            "was_due": str(payout.due_on),
            "low_point_before": {"day": base_day, **_money(base_low)},
            "low_point_after": {"day": low.day, **_money(low.balance)},
            "shortfall_after": _money(low.shortfall),
            "clears_the_floor": low.shortfall == 0,
            # The reason this tool beats a filter: a move that fixes the low
            # point can create a new one on the day it lands.
            "low_point_moved_to_a_different_day": low.day != base_day,
            "verdict": ("this clears the floor" if low.shortfall == 0 else
                        "still short by " + rules.rupees(low.shortfall)),
        })

    @beta_tool
    def payout_detail(payout_id: str) -> str:
        """Everything known about one scheduled payout: who it is to, how much,
        when it is due, and whether it can be moved at all.

        Args:
            payout_id: The id of the payout, exactly as given in the evidence.
        """
        payout = by_id.get(payout_id)
        if payout is None:
            return json.dumps({"error": f"no payout {payout_id}",
                               "known_ids": sorted(by_id)[:20]})
        return json.dumps({
            "payout_id": payout.payout_id, "payee": payout.payee,
            "amount": _money(payout.amount), "due_on": str(payout.due_on),
            "kind": payout.kind, "movable": payout.movable,
            "furthest_it_can_move_days": payout.delay_days,
            "why_fixed": None if payout.movable else
                "Payroll and statutory dues have dates somebody else set.",
        })

    @beta_tool
    def movements_on(day: int) -> str:
        """What money comes in and goes out on one day of the forecast, and
        what the balance is at the end of it.

        Args:
            day: Which day of the forecast, from 1 to the last day.
        """
        found = [p for p in forecast.positions if p.day == int(day)]
        if not found:
            return json.dumps({"error": f"day {day} is outside this forecast",
                               "days": f"1 to {days}"})
        position = found[0]
        return json.dumps({
            "day": position.day, "date": str(position.on),
            "opening": _money(position.opening),
            "money_in": _money(position.receipts),
            "money_out": _money(position.payouts + position.recurring),
            "closing": _money(position.closing),
            "below_the_floor": position.closing < floor,
            "in_detail": position.receipt_lines,
            "out_detail": position.payout_lines + position.recurring_lines,
        })

    return [what_if_delayed, payout_detail, movements_on]
