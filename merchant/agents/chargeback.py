"""
The chargeback defence assembler, as a registered agent.

Registered through exactly the same interface as every other agent here.
Checks a dispute's evidence checklist against the real reason-code
requirement list, then - for any dispute with something on file - asks
whether the case is worth contesting and drafts the letter. See
engine/chargeback/detector.py for why classification is fully mechanical,
and agent/chargeback_classifier.py for the one genuine judgment call.
"""

from __future__ import annotations

import time

from engine.chargeback import rules
from engine.chargeback.detector import detect_batch
from engine.chargeback.gate import gate_batch
from engine.chargeback.scoring import score_classification
from engine.chargeback.taxonomy import DisputeCode
from merchant.catalog import AgentContext, AgentSpec, register


def _line(text: str, kind: str = "info", detail: str = "") -> dict:
    return {"text": text, "kind": kind, "detail": detail,
            "at": int(time.time() * 1000)}


def run_chargeback_defence(ctx: AgentContext) -> None:
    """
    Check one batch of disputes against the merchant's own evidence on
    file.

    Progress is reported as it goes, same reason every other agent here
    does it: the calculator finishes in milliseconds and each dispute's
    agent call takes several seconds, so without narration the slow half
    looks like a hang.
    """
    from merchant.ledger import Ledger

    def say(**kw) -> None:
        ctx.progress(line=_line(**kw))

    with Ledger(ctx.db, ctx.business_id) as led:
        truth = None
        if ctx.source == "demo":
            n, truth = led.seed_chargeback_demo(30)
            say(text=f"Recorded {n} dispute notices and matching evidence",
                kind="start")
        else:
            say(text="Checking your disputes against the real evidence "
                     "requirement list", kind="start")

        batch = led.build_chargeback_batch()
        if batch is None:
            raise ValueError("there are no unreconciled disputes")
        disputes, evidence_by_dispute = batch

        # Demo respond_by deadlines are planted relative to the
        # generator's own fixed AS_OF, not real time - scoring "days left"
        # against wall-clock time would make every demo dispute read as
        # overdue the moment this demo was recorded.
        if ctx.source == "demo":
            from engine.chargeback.generator import AS_OF
            now = int(AS_OF.timestamp())
        else:
            now = int(time.time())

        classified = detect_batch(disputes, evidence_by_dispute, now=now)

        complete = [c for c in classified if c.code == str(DisputeCode.EVIDENCE_COMPLETE)]
        partial = [c for c in classified if c.code == str(DisputeCode.EVIDENCE_PARTIAL)]
        missing = [c for c in classified if c.code == str(DisputeCode.EVIDENCE_MISSING)]
        unmapped = [c for c in classified if c.code == str(DisputeCode.REASON_CODE_UNMAPPED)]

        say(text=f"{len(classified)} dispute(s) checked - {len(complete)} "
                 f"fully evidenced, {len(partial)} partly, {len(missing)} "
                 f"with nothing on file yet", kind="rules",
            detail="the evidence checklist is arithmetic; no model was "
                   "involved")
        if unmapped:
            say(text=f"{len(unmapped)} dispute(s) use a reason code with "
                     f"no requirement list on file", kind="finding",
                detail="sent to a person rather than guessed at")

        disputable = complete + partial
        verdicts: dict[str, object] = {}
        if disputable and ctx.use_agent:
            say(text=f"{len(disputable)} dispute(s) have evidence on file. "
                     f"Asking the agent whether each is worth contesting.",
                kind="agent",
                detail="a rule cannot tell a strong case from a weak one - "
                       "that needs reading what the merchant actually wrote")
            from agent.chargeback_classifier import ClaudeChargebackAgent

            evidence_rows = led.dispute_evidence
            classifier = ClaudeChargebackAgent()
            for n_idx, c in enumerate(disputable, 1):
                ctx.progress(phase=f"Judging {c.dispute_id} "
                                   f"({n_idx} of {len(disputable)})")
                detail_map = {r["evidence_type"]: r["detail"]
                             for r in evidence_rows(c.dispute_id)}
                verdict = classifier.judge(c, detail_map)
                verdicts[c.dispute_id] = verdict
                say(text=f"{c.dispute_id}: {rules.rupees(c.amount_paise)}, "
                         f"{c.days_to_respond_by} day(s) left", kind="finding",
                    detail=f"{verdict.reasoning}  [confidence "
                           f"{verdict.confidence:.2f}]")
        elif disputable:
            say(text=f"{len(disputable)} dispute(s) have evidence on file "
                     f"and the agent is switched off", kind="warn",
                detail="every one is still shown; they will be queued for "
                       "a person rather than judged")

        decisions = gate_batch(classified, verdicts)

        run_id = led.commit_chargeback_run(disputes, source=ctx.source)

        evidence_packs: dict[str, dict] = {}
        for c in disputable:
            from agent.chargeback_documents import explanation_letter

            detail_map = {r["evidence_type"]: r["detail"]
                         for r in led.dispute_evidence(c.dispute_id)}
            verdict = verdicts.get(c.dispute_id)
            case = getattr(verdict, "reasoning", "") if verdict and not \
                getattr(verdict, "error", None) else ""
            payload = {
                "dispute_id": c.dispute_id, "payment_id": c.payment_id,
                "reason_code": c.reason_code,
                "reason_description": c.reason_description,
                "amount_paise": c.amount_paise, "required": list(c.required),
                "present": list(c.present), "missing": list(c.missing),
                "evidence_detail": detail_map,
            }
            doc = explanation_letter(payload, case=case)
            evidence_packs[c.dispute_id] = {
                "summary": getattr(verdict, "summary", "") if verdict else "",
                "explanation_letter": doc.body,
            }

        led.record_chargeback_findings(run_id, classified, decisions,
                                       evidence_packs)
        ctx.progress(target_id=run_id, run_id=run_id)

        if truth is not None:
            card = score_classification(classified, truth)
            say(text=f"Measured against the planted answer key: "
                     f"{card.correct}/{card.total} disputes classified "
                     f"correctly ({card.accuracy:.0%}), "
                     f"{card.anomalies_caught}/{card.anomalies} planted "
                     f"anomalies caught", kind="rules")

        at_stake = sum(c.amount_paise for c in disputable)
        queued = [d for d in decisions if d.queued_for_human]
        say(text=f"{rules.rupees(at_stake)} across {len(disputable)} "
                 f"dispute(s) has a drafted evidence pack", kind="total",
            detail="a representment letter is drafted for each")
        if missing:
            say(text=f"{len(missing)} dispute(s) have no evidence on file "
                     f"yet - nothing to draft until you add some",
                kind="finding")
        if queued:
            say(text=f"{len(queued)} dispute(s) sent to a person to decide",
                kind="queued",
                detail="; ".join(queued[0].reasons) if queued[0].reasons else "")
        say(text="Nothing was submitted to Razorpay or any network. Every "
                 "draft is a proposal waiting for you.", kind="done")


CHARGEBACK = register(AgentSpec(
    id="chargeback",
    name="Chargeback Defence Assembler",
    tagline="Builds the evidence pack before the window closes.",
    question="Which disputes can I actually win, and what do I need to send?",
    status="live",
    reads=["chargeback notices", "evidence you have on file"],
    produces=["an evidence checklist per reason code", "a drafted "
             "representment letter", "deadline tracking"],
    authority="Card network dispute rules and Razorpay's own reason-code "
             "evidence requirements",
    runner=run_chargeback_defence,
))
