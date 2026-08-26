"""
The forward cash forecaster, as a registered agent.

The fourth, through the same interface as the other three.

## What it audits

The other three check something that already happened: was the fee right, did
the supplier file, did the money arrive. This one is the only agent here that
looks forward - and it is the only one whose finding has an expiry date on it.

A cash crunch found on the day it happens is not a finding, it is a crisis.
Found two weeks out it is a phone call to a supplier. That gap is the entire
product, which is why the demo scenario puts the trough on day 14 rather than
day 2: a forecast that only warns you the day before is a diary.

## Why it is not the Payout Timing Auditor

That one is still planned, and deliberately. It measures settlement DELAY
against the promised cycle and prices the float - a different question with a
different answer, and promoting it to ship this would have put one product out
under another's name.
"""

from __future__ import annotations

from merchant.catalog import AgentSpec, register


def run_cash_forecast(ctx) -> None:
    """
    Present so the registry has a runner and this agent counts as live.

    The work runs through merchant/treasury_pipeline.py, driven by the route
    in app.py - it builds its own inputs rather than reading a settlement id,
    so there is no target to hand it.
    """
    from generator.synthetic_treasury import generate
    from merchant.treasury_pipeline import run

    inputs, planted = generate()
    run(inputs, use_agent=ctx.use_agent, planted=planted,
        on_progress=lambda **kw: ctx.progress(**kw))


CASH_FORECASTER = register(AgentSpec(
    id="cash_forecaster",
    name="Forward Cash Forecaster",
    short_name="Cash",
    tagline="Projects thirty days of cash and says which payment to move "
            "before the money runs out.",
    question="Will I make payroll on the 14th, and if not, what do I move?",
    status="live",
    reads=["bank balances", "pending gateway settlements",
           "scheduled payouts", "recurring charges from your statement"],
    produces=["a thirty-day cash curve", "the date and size of the shortfall",
              "which specific payout to move, and by how many days"],
    authority="No statute - this is arithmetic on obligations the merchant "
              "already has. What it will not do is suggest moving payroll or "
              "a statutory due date, because those are not movable and "
              "advising otherwise would be advising a default.",
    why_unbuilt="Every piece of it sits in a different system. The balance is "
                "at the bank, the receivable is at the gateway, the payables "
                "are in the accounting package and the recurring charges are "
                "nowhere at all - so the forecast gets rebuilt by hand in a "
                "spreadsheet each month, or more often not at all.",
    runner=run_cash_forecast,
))
