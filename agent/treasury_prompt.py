"""
What the treasury agent is told, and the evidence it is given.

## The one rule this prompt exists to enforce

Every number in a cash forecast has already been computed. The balance on each
of thirty days, the trough, the shortfall against the floor, the total of what
can be moved and the total of what cannot - all of it comes out of
engine/treasury/forecaster.py before this module is called.

That is not a stylistic preference. A forecast is a chain of thirty additions
where each day depends on the last, and a model that is right 99% of the time
per step is wrong about the month. CLAUDE.md section 2 is the rule and a
running balance is the single worst place to break it.

## So what is the agent actually for

The arithmetic can say: you are Rs 43,311 short on the 14th, Rs 4,05,000 of
what falls due that week could move, Rs 7,95,000 could not.

It cannot say: move the packaging invoice rather than the cloud bill, because
the packaging supplier has been paid early three months running and will not
mind, and turning off the cloud bill turns off the business. It cannot weigh a
supplier relationship against an interest charge. It cannot notice that the
tax instalment carries 1% a month and the vendor carries a phone call.

That is the judgment, and it is the whole reason a model is here at all.

## Naming a specific payout

The agent is asked to name ONE payout to move, by id, from the list it is
given. Naming an id that was not in the list is treated as an invention and
the recommendation is discarded - the same treatment a made-up figure gets,
because "delay V-9999" is a made-up figure wearing an identifier.
"""

from __future__ import annotations

SYSTEM_PROMPT = """\
You advise a finance controller on a thirty-day cash forecast that has
already been computed.

## What you are NOT for

Every figure in front of you is the output of a deterministic engine: the
daily balances, the low point, the shortfall against the safe floor, the
totals of what can and cannot be moved. Do not add, subtract, scale, estimate
or re-derive any of them. Quote them exactly as given. A figure you compute
yourself will be caught and your whole recommendation discarded.

You are also not being asked to predict anything. The receipts listed are
money already earned and not yet credited, not a sales forecast.

## What you ARE for

Deciding what the controller should actually do, and saying it specifically
enough to act on this afternoon.

The engine can tell them they are short on a date and how much of that week's
outflow is movable. It cannot weigh which one to move. That turns on things
only a person or a well-briefed agent knows:

  payroll and statutory dues do not move. Delaying payroll is a legal and
  human problem; tax instalments carry interest by the month.

  a vendor invoice usually moves by a few days for the cost of a phone call,
  and some suppliers mind far more than others.

  a subscription or a cloud bill can often be moved, but stopping it may stop
  the business rather than merely annoy somebody.

So name ONE payout to move, by its id, from the list of payouts that fall ON
OR BEFORE the low point. Say how many days, and say what lands that makes it
safe. If nothing movable is enough, say that instead and say what the
controller should arrange.

Some payouts are movable but fall AFTER the low point. Moving those changes
nothing about it - you cannot fix the day you run short by deferring money you
have not spent yet. They are listed so you can say they would not help, not so
you can name one.

## Rules

Name only payout ids that appear in the evidence. Inventing one is the same
offence as inventing a number.

If the movable total does not cover the shortfall, do not pretend it does.
Saying "this cannot be solved by rescheduling, you need to arrange credit
this week" is the useful answer, and a week's notice is the whole value of a
forward forecast.

Never suggest delaying payroll or a statutory payment. If those are the only
things left, the answer is credit, not creative scheduling.

## Your output

Write to the controller, not about them. Two or three sentences. Lead with
what to do, then why.
"""


def render(forecast, *, business: str = "") -> str:
    """
    The evidence for one forecast.

    Ordered so the decision is at the top: what is wrong, then what is movable,
    then what is not, then what relief is coming. A model reading this should
    be able to answer without scrolling back.
    """
    from engine.gst import rules

    trough = forecast.trough
    lines = [
        f"CASH FORECAST for {business or 'this business'} - "
        f"{len(forecast.positions)} days",
        f"FINDING: {forecast.finding}",
        "",
        f"Opening balance today   {rules.rupees(forecast.opening_balance)}",
        f"Safe floor              {rules.rupees(forecast.floor)}",
    ]

    if trough is not None:
        lines += [
            f"LOWEST POINT            {rules.rupees(trough.balance)} on "
            f"{trough.on} (day {trough.day})",
            f"SHORTFALL BELOW FLOOR   {rules.rupees(trough.shortfall)}",
        ]
        if trough.below_zero:
            lines.append("THE ACCOUNT GOES NEGATIVE on that date.")

    lines += ["", "WHAT FALLS DUE AROUND THE LOW POINT AND CAN BE MOVED:"]
    if forecast.movable_near_trough:
        for row in forecast.movable_near_trough:
            name = row.get("payee") or row.get("name") or "?"
            ident = row.get("payout_id") or f"recurring:{row.get('name')}"
            lines.append(
                f"  [{ident}] {name} - {row['amount_display']} on "
                f"{row['date']} (day {row['day']}), movable up to "
                f"{row.get('delay_days', 5)} days")
        lines.append(
            f"  TOTAL MOVABLE: {rules.rupees(forecast.movable_total)}")
    else:
        lines.append("  nothing around that date can be moved")

    if forecast.movable_after_trough:
        lines += ["",
                  "MOVABLE, BUT FALLING AFTER THE LOW POINT - so moving these "
                  "changes nothing about it:"]
        for row in forecast.movable_after_trough:
            ident = row.get("payout_id") or f"recurring:{row.get('name')}"
            lines.append(
                f"  [{ident}] {row.get('payee') or row.get('name', '?')} - "
                f"{row['amount_display']} on {row['date']} "
                f"(day {row['day']})")

    lines += ["", "WHAT FALLS DUE AROUND THE LOW POINT AND CANNOT BE MOVED:"]
    if forecast.unmovable_near_trough:
        for row in forecast.unmovable_near_trough:
            lines.append(
                f"  [{row.get('payout_id', '?')}] {row.get('payee', '?')} - "
                f"{row['amount_display']} on {row['date']} "
                f"({row.get('kind', '')})")
        lines.append(
            f"  TOTAL UNMOVABLE: "
            f"{rules.rupees(sum(r['amount'] for r in forecast.unmovable_near_trough))}")
    else:
        lines.append("  nothing")

    lines += [
        "",
        f"MONEY ARRIVING AFTER THE LOW POINT: "
        f"{rules.rupees(forecast.receipts_after_trough)}",
    ]
    after = [p for p in forecast.positions
             if trough is not None and p.day > trough.day and p.receipt_lines]
    for position in after[:4]:
        for line in position.receipt_lines:
            lines.append(
                f"  {line['amount_display']} on {position.on} "
                f"(day {position.day}) - {line['reference']}")

    lines += [
        "",
        f"THE ARITHMETIC'S CONCLUSION: {forecast.detail}",
        f"COVERABLE BY RESCHEDULING: "
        f"{'yes' if forecast.coverable_by_delay else 'no'}",
    ]
    return "\n".join(lines)
