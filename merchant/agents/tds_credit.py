"""
The TDS credit tracker, as a registered agent.

Same registration interface as settlement and GST, and the same reason it
was worth building before agent two: the run plumbing, the progress
terminal, the audit log all work unchanged.

engine/tds knows nothing about businesses, sessions or the web. It takes a
TdsBatch and returns findings, whether that batch came from the synthetic
generator or from a merchant's own imported deduction history.
"""

from __future__ import annotations

import time

from engine.tds import rules
from engine.tds.detector import detect_batch
from engine.tds.gate import gate_batch
from engine.tds.taxonomy import CODE_LABEL, NO_ACTION, TdsCode
from merchant.catalog import AgentContext, AgentSpec, register


def _line(text: str, kind: str = "info", detail: str = "") -> dict:
    return {"text": text, "kind": kind, "detail": detail,
            "at": int(time.time() * 1000)}


def run_tds_reconciliation(ctx: AgentContext) -> None:
    """
    Reconcile one batch of TDS deductions against the merchant's own
    credit statement.

    Progress is reported as it goes for the same reason the settlement
    auditor does it: the calculator finishes in milliseconds and the agent
    calls take seconds each, so without narration the interesting half is
    invisible and the slow half looks like a hang.
    """
    from merchant.ledger import Ledger

    def say(**kw) -> None:
        ctx.progress(line=_line(**kw))

    with Ledger(ctx.db, ctx.business_id) as led:
        batch = led.build_tds_batch()
        if batch is None:
            raise ValueError("there are no unreconciled TDS deductions")

        deducted = sum(d.amount for d in batch.deductions)
        pre_change = sum(1 for d in batch.deductions
                         if d.deducted_at < rules.REGIME_CHANGE)
        post_change = len(batch.deductions) - pre_change
        say(text=f"Checking {len(batch.deductions)} TDS deductions against "
                 f"your credit statement", kind="start")
        say(text=f"Razorpay withheld {rules.rupees(deducted)} across these "
                 f"payments", detail=f"{pre_change} before 1 April 2026, "
                 f"{post_change} after")
        say(text=f"{len(batch.credits)} lines found on your credit statement",
            detail="Form 26AS before the regime change, Form 168 after")

        ctx.progress(phase="Matching every deduction to its credit line")
        variances = detect_batch(batch)
        settled = [v for v in variances if not v.needs_agent]
        open_ones = [v for v in variances if v.needs_agent]

        say(text=f"The date table settled {len(settled)} of {len(variances)} "
                 f"outright", kind="rules",
            detail="the correct rate, code and form are a pure function of "
                   "the deduction date - no judgment involved")

        for variance in settled:
            code = TdsCode(variance.exception_code)
            if code in NO_ACTION:
                continue
            say(text=f"{variance.payment_id}: {CODE_LABEL[code]}",
                kind="finding", detail=variance.reasoning or "")

        verdicts = []
        if open_ones and ctx.use_agent:
            say(text=f"{len(open_ones)} payments need judgment. Asking the "
                     f"agent.", kind="agent",
                detail="a rule cannot weigh whether a missing credit is "
                       "ordinary statement lag or a genuine loss")
            from agent.tds_classifier import ClaudeTdsClassifier

            classifier = ClaudeTdsClassifier(batch)
            for n, variance in enumerate(open_ones, 1):
                ctx.progress(phase=f"Judging {variance.payment_id} "
                                   f"({n} of {len(open_ones)})")

                def live(kind: str, detail: str = "") -> None:
                    if kind == "tool":
                        say(text=f"  looking up {detail}", kind="tool")

                verdict = classifier.classify(variance, on_event=live)
                verdicts.append(verdict)
                code = TdsCode(verdict.exception_code)
                say(text=f"{variance.payment_id}: {CODE_LABEL[code]}",
                    kind="finding",
                    detail=f"{verdict.reasoning}  [confidence "
                           f"{verdict.confidence:.2f}]")
        elif open_ones:
            say(text=f"{len(open_ones)} payments need judgment and the agent "
                     f"is switched off", kind="warn",
                detail="they will be queued for a person rather than guessed at")

        decisions = gate_batch(variances, verdicts)
        at_risk = sum(d.money_at_stake for d in decisions
                      if TdsCode(d.exception_code) not in NO_ACTION)
        queued = [d for d in decisions if d.queued_for_human]

        run_id = led.commit_tds_run(batch)
        led.record_tds_findings(run_id, variances, decisions, verdicts)
        ctx.progress(target_id=run_id, run_id=run_id)

        say(text=f"{rules.rupees(at_risk)} of credit needs your attention",
            kind="total",
            detail="missing, short, or filed under the wrong regime")
        if queued:
            say(text=f"{len(queued)} sent to a person to decide", kind="queued",
                detail="; ".join(queued[0].reasons) if queued[0].reasons else "")
        say(text="Nothing was filed, amended or claimed. Every line above is a "
                 "proposal.", kind="done")


TDS_CREDIT_TRACKER = register(AgentSpec(
    id="tds_credit",
    name="TDS Credit Tracker",
    tagline="Checks that tax withheld from you actually reached the department.",
    question="Was TDS deducted from my payouts, and did it show up as my credit?",
    status="live",
    reads=["settlement reports (TDS lines)", "Form 26AS", "Form 168"],
    produces=["missing credit claims", "corrected section codes",
              "rate corrections"],
    authority="Income Tax Act 2025 s.393 - and the 1 April 2026 code change",
    runner=run_tds_reconciliation,
))
