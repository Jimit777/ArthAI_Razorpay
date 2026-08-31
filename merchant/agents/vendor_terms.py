"""
The vendor invoice auditor, as a registered agent.

Registered through exactly the same interface as every other agent here.
Checks what a supplier billed against the rate the merchant agreed to pay
- the same shape the settlement auditor applies to gateway fees, one door
down the purchase side. See engine/vendor_terms/detector.py for why
classification is fully mechanical, and agent/vendor_terms_classifier.py
for the one genuine judgment call: is an overbilled supplier's batch worth
pursuing, once per supplier rather than once per line.
"""

from __future__ import annotations

import time

from engine.vendor_terms import rules
from engine.vendor_terms.detector import detect_batch, group_by_supplier
from engine.vendor_terms.gate import gate_batch
from engine.vendor_terms.scoring import score_classification
from engine.vendor_terms.taxonomy import TermsCode
from merchant.catalog import AgentContext, AgentSpec, register


def _line(text: str, kind: str = "info", detail: str = "") -> dict:
    return {"text": text, "kind": kind, "detail": detail,
            "at": int(time.time() * 1000)}


def run_vendor_terms_audit(ctx: AgentContext) -> None:
    """
    Check one batch of billed line items against the merchant's own vendor
    rate card.

    Progress is reported as it goes, same reason every other agent here does
    it: the calculator finishes in milliseconds and each supplier's agent
    call takes several seconds, so without narration the slow half looks
    like a hang.
    """
    from merchant.ledger import Ledger

    def say(**kw) -> None:
        ctx.progress(line=_line(**kw))

    with Ledger(ctx.db, ctx.business_id) as led:
        truth = None
        if ctx.source == "demo":
            n, truth = led.seed_vendor_terms_demo(40)
            say(text=f"Recorded {n} billed line items and a matching "
                     f"vendor rate card", kind="start")
        else:
            say(text="Checking your billed line items against the vendor "
                     "rate card on file", kind="start")

        batch = led.build_vendor_terms_batch()
        if batch is None:
            raise ValueError("there are no unreconciled purchase line items")

        rate_card = led.vendor_rate_card()
        classified = detect_batch(batch, rate_card=rate_card)
        groups = group_by_supplier(classified)

        overbilled = [i for i in classified if i.code == str(TermsCode.OVERBILLED)]
        unconfigured = [i for i in classified
                        if i.code == str(TermsCode.RATE_UNCONFIGURED)]
        say(text=f"{len(classified)} line items checked - {len(overbilled)} "
                 f"billed above the contracted price, {len(unconfigured)} "
                 f"with no rate on file", kind="rules",
            detail="the rate check is arithmetic; no model was involved")

        verdicts: dict[str, object] = {}
        disputable = [g for g in groups if g.overbilled]
        if disputable and ctx.use_agent:
            say(text=f"{len(disputable)} supplier(s) have overbilled lines. "
                     f"Asking the agent whether each reads as a pattern "
                     f"worth pursuing.", kind="agent",
                detail="a rule cannot tell an isolated billing slip from a "
                       "supplier-wide price change")
            from agent.vendor_terms_classifier import ClaudeVendorTermsAgent

            classifier = ClaudeVendorTermsAgent()
            for n, group in enumerate(disputable, 1):
                ctx.progress(phase=f"Judging {group.supplier_name} "
                                   f"({n} of {len(disputable)})")
                verdict = classifier.judge(group)
                verdicts[group.supplier_gstin] = verdict
                say(text=f"{group.supplier_name}: {rules.rupees(group.at_stake_paise)} "
                         f"at stake", kind="finding",
                    detail=f"{verdict.reasoning}  [confidence "
                           f"{verdict.confidence:.2f}]")
        elif disputable:
            say(text=f"{len(disputable)} supplier(s) have overbilled lines "
                     f"and the agent is switched off", kind="warn",
                detail="every one is still shown; they will be queued for "
                       "a person rather than judged")

        decisions = gate_batch(groups, verdicts)

        run_id = led.commit_vendor_terms_run(batch, source=ctx.source)

        credit_notes: dict[str, str] = {}
        for group in disputable:
            from agent.vendor_terms_documents import credit_note_request

            payload = {
                "supplier_name": group.supplier_name,
                "gstin": group.supplier_gstin,
                "items": [i.as_dict() for i in group.overbilled],
            }
            doc = credit_note_request(payload)
            credit_notes[group.supplier_gstin] = doc.body

        led.record_vendor_terms_findings(run_id, classified, decisions,
                                         credit_notes)
        ctx.progress(target_id=run_id, run_id=run_id)

        if truth is not None:
            card = score_classification(classified, truth)
            say(text=f"Measured against the planted answer key: "
                     f"{card.correct}/{card.total} line items classified "
                     f"correctly ({card.accuracy:.0%}), "
                     f"{card.anomalies_caught}/{card.anomalies} planted "
                     f"anomalies caught", kind="rules")

        at_stake = sum(g.at_stake_paise for g in groups)
        queued = [d for d in decisions if d.queued_for_human]
        say(text=f"{rules.rupees(at_stake)} of overbilling found across "
                 f"{len(disputable)} supplier(s)", kind="total",
            detail="a credit note request is drafted for each")
        if unconfigured:
            say(text=f"{len(unconfigured)} line item(s) excluded - no "
                     f"contracted price on file", kind="finding",
                detail="add a rate to check them, rather than guessing")
        if queued:
            say(text=f"{len(queued)} supplier(s) sent to a person to decide",
                kind="queued",
                detail="; ".join(queued[0].reasons) if queued[0].reasons else "")
        say(text="Nothing was sent or claimed. Every credit note request is "
                 "a draft waiting for you.", kind="done")


VENDOR_TERMS = register(AgentSpec(
    id="vendor_terms",
    name="Vendor Invoice Auditor",
    short_name="Vendor terms",
    tagline="Checks supplier invoices against the terms you agreed.",
    question="Am I being billed the rates in my contract?",
    status="live",
    reads=["purchase register line items", "vendor rate card"],
    produces=["overbilled line items", "credit note requests"],
    authority="The purchase agreement you and your supplier signed",
    runner=run_vendor_terms_audit,
))
