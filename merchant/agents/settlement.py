"""
The settlement deduction auditor, as a registered agent.

This is the one that works. It is registered through exactly the same interface
a second agent would use, so the seam is real rather than aspirational: the
registry, the context object, the progress reporting and the audit log are all
generic, and none of them know what a settlement is.

The audit logic itself is not here - it lives in engine/ and agent/, is covered
by the test suite, and was working before the platform existed. This module is
the adapter.
"""

from __future__ import annotations

from agent.dispute import attach_disputes
from engine.detector import detect_batch
from engine.expected_value import rupees
from engine.gate import gate_batch, money_at_stake
from merchant import trace
from merchant.catalog import AgentContext, AgentSpec, register


def run_settlement_audit(ctx: AgentContext) -> None:
    """
    Audit one settlement batch.

    Reports progress as it goes so the page can show movement - an agent call
    takes fifteen to twenty seconds and an audience watching a still screen
    assumes it has hung.
    """
    from merchant.ledger import Ledger

    # The same builders `trace.build` uses to replay this run afterwards. Live
    # narration and the replay cannot drift because they are the same code.
    def say(built) -> None:
        for one in (built if isinstance(built, list) else [built]):
            ctx.progress(line=one.as_dict())

    with Ledger(ctx.db) as led:
        batch = led.load_batch(ctx.target_id, ctx.rate_card)
        if batch is None:
            raise ValueError("this settlement has no payments")

        say(trace.opening(ctx.target_id))
        gross = sum(r.payment.amount for r in batch.records)
        say(trace.loaded(len(batch.records), gross,
                         sum(c.amount for c in batch.bank_credits)))
        say(trace.contract(len(ctx.rate_card["instruments"]),
                           ctx.rate_card["gst_rate_bps"],
                           ctx.rate_card["tolerance"]["floor_paise"],
                           ctx.rate_card["tolerance"]["pct_bps"]))
        ctx.progress(phase="Comparing every deduction against the rate card")
        variances = detect_batch(batch)
        open_ones = [v for v in variances if v.needs_agent]

        # Report what the RULES settled before the agent starts, because that
        # is the interesting half and it happens in milliseconds. A merchant
        # watching a progress bar should see that most of their settlement was
        # resolved by arithmetic and never went near a language model.
        settled = [v for v in variances if not v.needs_agent]
        breakdown: dict[str, int] = {}
        for v in settled:
            breakdown[v.exception_code] = breakdown.get(v.exception_code, 0) + 1
        say(trace.comparing(len(variances)))
        say(trace.rules_settled(len(settled), len(variances), breakdown))
        ctx.progress(total=len(open_ones), settled_by_rules=len(settled),
                     rules_breakdown=breakdown,
                     phase=f"{len(settled)} of {len(variances)} settled by the "
                           f"rate card alone")

        verdicts = []
        if ctx.use_agent and open_ones:
            from agent.classifier import ClaudeClassifier

            ctx.progress(phase=f"{len(open_ones)} record(s) need judgment "
                               f"- asking the agent")
            try:
                # What THIS business's own past resolutions say. Scoped by
                # business_id, or a busy merchant's confirmed note could be
                # recalled for someone else's variance with the same code -
                # confirmed once on /agents/settlement/resolve, recalled
                # here on every audit after. CLAUDE.md section 12.
                memory = [dict(row) for row in
                         led.store.resolutions(business_id=ctx.business_id)]
                classifier = ClaudeClassifier(batch, memory=memory)
            except Exception as exc:                        # noqa: BLE001
                # No credentials, or no network. The rules still work and a
                # partial audit beats a blank page in front of an audience.
                ctx.progress(note=f"Agent unavailable ({exc}). "
                                  f"These records were left unresolved.")
                classifier = None
                open_ones = []

            say(trace.needs_judgment(len(open_ones)))
            for i, variance in enumerate(open_ones, 1):
                ctx.progress(done=i - 1, current=variance.payment_id,
                             current_instrument=variance.instrument_label,
                             current_signals=[s.candidate_code
                                              for s in variance.signals])
                say(trace.looking_at(variance.payment_id,
                                     variance.instrument_label, variance.amount))
                say(trace.the_gap(variance.actual_fee, variance.expected_fee,
                                  variance.delta, variance.actual_tax,
                                  variance.expected_tax))
                for signal in variance.signals:
                    say(trace.evidence(signal.rule, str(signal.candidate_code),
                                       signal.source))

                # Streamed from inside the call rather than after it. Emitting
                # tool calls once classify() returns leaves the screen still for
                # the fifteen seconds the interesting part is happening.
                def live(kind: str, detail: str = "") -> None:
                    if kind == "weighing":
                        say(trace.weighing())
                    elif kind == "tool":
                        say(trace.tool_call(detail))

                verdict = classifier.classify(variance, on_event=live)
                verdicts.append(verdict)

                if verdict.error:
                    say(trace.classify_failed(verdict.error))
                else:
                    say(trace.verdict(
                        verdict.exception_code, verdict.action,
                        verdict.confidence,
                        money_at_stake(variance, verdict.exception_code),
                        verdict.output_tokens, verdict.latency_ms / 1000))
                    if verdict.invented_figures:
                        say(trace.reviewed_invented(verdict.invented_figures))
                    elif verdict.corrections:
                        for correction in verdict.corrections:
                            say(trace.reviewed_corrected(correction))
                    else:
                        say(trace.reviewed_clean())

                # Each verdict is streamed as it lands rather than held until
                # the batch finishes. Twenty seconds of nothing followed by
                # everything at once tells a watcher less than the same
                # information arriving as it is decided.
                ctx.progress(done=i, result={
                    "payment_id": variance.payment_id,
                    "instrument": variance.instrument_label,
                    "code": verdict.exception_code,
                    "action": verdict.action,
                    "confidence": verdict.confidence,
                    "stake": rupees(money_at_stake(variance,
                                                   verdict.exception_code)),
                    "corrected": bool(verdict.corrections),
                    "error": verdict.error,
                })
                if "credit balance" in (verdict.error or ""):
                    ctx.progress(note="Stopped: API credit exhausted.")
                    break
        elif open_ones:
            ctx.progress(note="Agent skipped. These records stay unresolved.")
        else:
            say(trace.nothing_to_judge())

        ctx.progress(phase="Applying the guardrail gate")
        decisions = gate_batch(variances, verdicts, ctx.rate_card)
        disputes = attach_disputes(variances, verdicts, decisions)
        led.store.save_findings(ctx.target_id, decisions, variances, verdicts,
                                disputes)

        queued = [d for d in decisions if d.queued_for_human]
        say(trace.gate(len(decisions) - len(queued), len(queued)))
        for decision in queued:
            for reason in decision.reasons:
                say(trace.held(decision.payment_id, reason))
        if disputes:
            say(trace.drafted(len(disputes)))
        say(trace.finished())


SETTLEMENT_AUDITOR = register(AgentSpec(
    id="settlement_audit",
    name="Settlement Deduction Auditor",
    short_name="Settlement",
    tagline="Checks every rupee your payment gateway deducted against the "
            "rate card and the law.",
    question="You got Rs 7,370 instead of Rs 9,000. Where did the difference "
             "go, and which parts should you be angry about?",
    status="live",
    reads=["settlement reports", "your rate card", "bank credits"],
    produces=["recoverable overcharges", "paste-ready dispute letters",
              "tax-credit risks"],
    authority="PSS Act s.10A, RBI/2017-18/105, GST law, Income Tax Act 2025",
    why_unbuilt="A tool that verifies a gateway's fees produces exactly one "
                "kind of output: 'they overcharged you'. No payment company "
                "builds the tool that bills itself.",
    runner=run_settlement_audit,
))
