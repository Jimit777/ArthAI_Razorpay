"""
The three-way reconciliation agent, as a registered agent.

The third one, registered through exactly the same interface as the other two -
which is the point of having done the registry before agent two rather than
after.

## What it audits, and why it belongs beside the other two

Every agent on this platform checks the same shape of gap: something was
agreed, something else happened, and nobody routinely compares them. The
settlement auditor checks the RATE a gateway charged. The input credit
reconciler checks whether a supplier filed. This one checks whether the money
arrived at all.

That is a different question from both, and it is the one a finance team is
asked first on any given morning. It is also the one that is answered by
exporting three files and squinting at them in a spreadsheet, which is why
the answer usually arrives a month late.
"""

from __future__ import annotations

from merchant.catalog import AgentSpec, register


def run_three_way_recon(ctx) -> None:
    """
    Present so the registry has a runner and this agent counts as live.

    The work runs through merchant/recon_pipeline.py, driven by the route in
    app.py rather than by the batch context the other two use - it generates
    its own three sources rather than reading a settlement id, so there is no
    target to hand it.
    """
    from engine.recon.generator import generate
    from merchant.recon_pipeline import run

    batch, truth = generate()
    run(batch, truth=truth, use_agent=ctx.use_agent,
        on_progress=lambda **kw: ctx.progress(**kw))


THREE_WAY_RECON = register(AgentSpec(
    id="three_way_recon",
    name="Three-Way Reconciliation Agent",
    short_name="Three-way",
    tagline="Joins your invoices, your gateway settlements and your bank "
            "statement, and reports what did not arrive.",
    question="I billed it, the gateway says it settled it, and the bank shows "
             "something else. Which lines actually disagree, and by how much?",
    status="live",
    reads=["ERP sales invoices", "gateway settlement reports",
           "bank statement credits"],
    produces=["a measured match rate", "an exception list with an action on "
              "every line", "the three ids joined, per matched record"],
    authority="No statute - this one is arithmetic and evidence. What it "
              "argues from is the merchant's own three sources, and it "
              "refuses to close a line those three cannot close.",
    why_unbuilt="Every party holds one third of the answer and none of them "
                "is paid to hold the other two. The gateway reconciles to "
                "its own ledger, the bank to its own, and the merchant is "
                "the only one who needs all three to agree - which is why "
                "the job is done in a spreadsheet, once a month, badly.",
    runner=run_three_way_recon,
))
