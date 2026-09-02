"""
The payout timing auditor, as a registered agent.

Same registration interface as every other agent here. What is new is not
the plumbing - it is the shape of the judgment: one verdict per run, not one
per record, because there is only ever one pattern to judge. See
engine/payout_timing/detector.py's module docstring for why, and
merchant/payout_timing_pipeline.py for the run itself.
"""

from __future__ import annotations

import time

from engine.payout_timing import rules
from merchant.catalog import AgentContext, AgentSpec, register


def _line(text: str, kind: str = "info", detail: str = "") -> dict:
    return {"text": text, "kind": kind, "detail": detail,
            "at": int(time.time() * 1000)}


def run_payout_timing(ctx: AgentContext) -> None:
    """
    Runs on generated data or on the merchant's own, decided by
    `ctx.source`: "demo" generates a batch, "connected" reads the sales and
    settlements already on file - uploaded, or pulled from Razorpay. That is
    the field AgentContext already carries for this distinction; target_id is
    the run key here and means nothing to the choice.

    Live mode refuses rather than falls back. Silently auditing generated
    settlements while the screen says the data is real would be the one
    failure this product cannot survive.
    """
    from engine.payout_timing.generator import generate_batch
    from merchant.ledger import Ledger
    from merchant.payout_timing_pipeline import run as run_pipeline

    def say(**kw) -> None:
        ctx.progress(line=_line(**kw))

    live = (ctx.source or "demo") == "connected"

    with Ledger(ctx.db, ctx.business_id) as led:
        if live:
            batch = led.payout_timing_batch()
            if batch is None:
                missing = led.payout_timing_missing()
                raise ValueError(
                    "Nothing to audit yet - still missing your "
                    + " and ".join(
                        {"invoice": "sales", "settlement": "settlements"}[m]
                        for m in missing) + ".")
            say(text=f"Reading {len(batch.invoices)} sales and "
                     f"{len(batch.settlements)} settlements from your own "
                     f"records", kind="start")
        else:
            batch, _truth = generate_batch()
            say(text=f"Checking {len(batch.invoices)} settlements against the "
                     f"T+{rules.SETTLEMENT_WORKING_DAYS} working-day cycle you "
                     f"were promised", kind="start")

        source = "connected" if live else "demo"
        result = run_pipeline(batch, use_agent=ctx.use_agent, source=source,
                              on_progress=lambda **kw: ctx.progress(**kw))
        summary, decision = result.summary, result.decision

        say(text=summary.detail, kind="rules" if not ctx.use_agent else "info")
        if result.verdict is not None:
            say(text=f"Agent: {result.verdict.reasoning}", kind="finding",
                detail=f"[confidence {result.verdict.confidence:.2f}]")

        run_id = led.commit_payout_timing_run(
            summary, decision, result.verdict, source=source)
        led.record_payout_timing_findings(run_id, summary)
        ctx.progress(target_id=run_id, run_id=run_id)

        say(text=f"{summary.n_sla_miss} of {summary.n_settled} settlements "
                 f"missed the promised cycle", kind="total",
            detail=f"assumed float cost {rules.rupees(decision.float_cost_paise)}")
        if decision.queued_for_human:
            say(text="Sent to a person to decide", kind="queued",
                detail="; ".join(decision.reasons) if decision.reasons else "")
        say(text="Nothing was filed, sent or claimed. Every line above is a "
                 "proposal.", kind="done")


PAYOUT_TIMING = register(AgentSpec(
    id="payout_timing",
    name="Payout Timing Auditor",
    short_name="Payout timing",
    tagline="Measures settlement delay against the cycle you were promised.",
    question="Is my money arriving on T+2, and what is the float worth?",
    status="live",
    reads=["settlement reports", "bank statements"],
    produces=["delay distribution", "float cost estimate"],
    authority="The merchant agreement's stated settlement cycle",
    runner=run_payout_timing,
))
