"""
The GST input credit reconciler, as a registered agent.

Registered through exactly the same interface as the settlement auditor, which
was the point of building the registry before agent two rather than after. The
run plumbing, the progress terminal, the audit log and the access log all work
unchanged; what is new here is a different subject, not different machinery.

The engine in engine/gst/ knows nothing about businesses, sessions or the web.
It takes an ITCBatch and returns findings, whether that batch came from the
synthetic generator or from a merchant typing invoices into a form.
"""

from __future__ import annotations

import time

from engine.gst import rules
from engine.gst.detector import detect_batch
from engine.gst.gate import gate_batch
from engine.gst.taxonomy import (ACTION_FOR, AT_RISK, CODE_LABEL, ITCCode,
                                 NO_ACTION, OVERCLAIMED)
from merchant.catalog import AgentContext, AgentSpec, register


def _line(text: str, kind: str = "info", detail: str = "") -> dict:
    return {"text": text, "kind": kind, "detail": detail,
            "at": int(time.time() * 1000)}


def run_itc_reconciliation(ctx: AgentContext) -> None:
    """
    Reconcile one batch of purchase invoices against GSTR-2B.

    Progress is reported as it goes for the same reason the settlement auditor
    does it: the calculator finishes in milliseconds and the agent calls take
    fifteen seconds each, so without narration the interesting half is
    invisible and the slow half looks like a hang.
    """
    from merchant.ledger import Ledger

    def say(**kw) -> None:
        ctx.progress(line=_line(**kw))

    with Ledger(ctx.db, ctx.business_id) as led:
        batch = led.build_itc_batch()
        if batch is None:
            raise ValueError("there are no unreconciled purchase invoices")

        claimed = sum(p.total_tax for p in batch.purchases)
        say(text=f"Reconciling {len(batch.purchases)} purchase invoices "
                 f"for {batch.period}", kind="start")
        say(text=f"Your books claim {rules.rupees(claimed)} of input credit",
            detail=f"across {len(batch.purchases)} invoices")
        say(text=f"GSTR-2B holds {len(batch.gstr2b)} lines from your suppliers",
            detail="the government's record of what they actually reported")

        ctx.progress(phase="Matching every invoice against GSTR-2B")
        variances = detect_batch(batch)
        settled = [v for v in variances if not v.needs_agent]
        open_ones = [v for v in variances if v.needs_agent]

        say(text=f"The rate card and the statute settled {len(settled)} of "
                 f"{len(variances)} outright", kind="rules",
            detail="deadlines, blocked categories and missing filings are "
                   "arithmetic, not judgment - no model was involved")

        for variance in settled:
            code = ITCCode(variance.exception_code)
            if code in NO_ACTION:
                continue
            say(text=f"{variance.supplier_name}: {CODE_LABEL[code]}",
                kind="finding", detail=variance.reasoning or "")

        verdicts = []
        if open_ones and ctx.use_agent:
            say(text=f"{len(open_ones)} invoices need judgment. Asking the "
                     f"agent.", kind="agent",
                detail="a rule cannot weigh whether a supplier filed against "
                       "the wrong registration or simply did not file")
            from agent.gst_classifier import ClaudeITCClassifier

            classifier = ClaudeITCClassifier(batch)
            for n, variance in enumerate(open_ones, 1):
                ctx.progress(phase=f"Judging {variance.invoice_number} "
                                   f"({n} of {len(open_ones)})")

                def live(kind: str, detail: str = "") -> None:
                    if kind == "tool":
                        say(text=f"  looking up {detail}", kind="tool")

                verdict = classifier.classify(variance, on_event=live)
                verdicts.append(verdict)
                code = ITCCode(verdict.exception_code)
                say(text=f"{variance.supplier_name}: {CODE_LABEL[code]}",
                    kind="finding",
                    detail=f"{verdict.reasoning}  [confidence "
                           f"{verdict.confidence:.2f}]")
        elif open_ones:
            say(text=f"{len(open_ones)} invoices need judgment and the agent "
                     f"is switched off", kind="warn",
                detail="they will be queued for a person rather than guessed at")

        decisions = gate_batch(variances, verdicts)
        at_risk = sum(d.money_at_stake for d in decisions
                      if ITCCode(d.exception_code) in AT_RISK)
        overclaimed = sum(d.money_at_stake for d in decisions
                          if ITCCode(d.exception_code) in OVERCLAIMED)
        queued = [d for d in decisions if d.queued_for_human]

        run_id = led.commit_itc_run(batch)
        led.record_itc_findings(run_id, variances, decisions, verdicts)
        ctx.progress(target_id=run_id, run_id=run_id)

        say(text=f"{rules.rupees(at_risk)} of credit is at risk", kind="total",
            detail="claimed in your books, not yet supported by GSTR-2B")
        say(text=f"{rules.rupees(overclaimed)} should not be claimed at all",
            kind="total",
            detail="claiming it invites a Rule 88D notice and interest at 18%")
        if queued:
            say(text=f"{len(queued)} sent to a person to decide", kind="queued",
                detail="; ".join(queued[0].reasons) if queued[0].reasons else "")
        say(text="Nothing was filed, amended or claimed. Every line above is a "
                 "proposal.", kind="done")


def run_supplier_watch(ctx: AgentContext) -> None:
    """
    Check what changed since last time, and decide what is worth saying.

    Not registered as a separate agent. It is the same agent doing a different
    job on the same data, and adding it to the catalogue as a third entry would
    inflate the count without adding a capability - the exact dishonesty the
    catalogue's docstring exists to prevent.
    """
    from engine.gst.watch import (diff, ranked, snapshot, total_exposure,
                                  CHANGE_LABEL)
    from merchant.ledger import Ledger
    from merchant.suppliers import current_period

    def say(**kw) -> None:
        ctx.progress(line=_line(**kw))

    with Ledger(ctx.db, ctx.business_id) as led:
        batch = led.build_itc_batch(only_unreconciled=False)
        if batch is None:
            raise ValueError("there are no purchase invoices to watch")

        period = current_period()
        say(text=f"Checking {len(batch.purchases)} invoices against what your "
                 f"suppliers have filed", kind="start")

        # Registration statuses come from the shared lookup cache, and only
        # the fresh ones - a stale "active" is excluded rather than served,
        # so it can never mask a cancellation that happened since.
        from merchant.gstin_lookup import GstinStatus

        gstins = {p.supplier_gstin.strip().upper() for p in batch.purchases}
        known = GstinStatus(led.conn).statuses_for(gstins)
        if known:
            dead = sum(1 for v in known.values()
                       if v["status"] in ("cancelled", "suspended"))
            say(text=f"{len(known)} of {len(gstins)} registrations checked "
                     f"recently"
                     + (f", {dead} no longer active" if dead else ""),
                detail="unchecked registrations are reported as unchecked, "
                       "not assumed to be fine")

        current = snapshot(batch, statuses=known)
        previous = led.last_snapshot()
        exposure = total_exposure(current)

        say(text=f"{len(current)} suppliers, {rules.rupees(exposure)} of credit "
                 f"not yet supported by GSTR-2B",
            detail="ranked by how much of your money each one is holding")

        for state in ranked(current)[:5]:
            if not state.exposed_paise:
                continue
            say(text=f"{state.name}: filed {state.invoices_filed} of "
                     f"{state.invoices_booked}, "
                     f"{rules.rupees(state.exposed_paise)} exposed",
                kind="finding",
                detail=f"last filed {state.last_filed_period or 'never'}"
                       + (f", {state.days_to_earliest_deadline} days to the "
                          f"deadline"
                          if state.days_to_earliest_deadline is not None else ""))

        if not previous:
            say(text="This is the first check, so there is nothing to compare "
                     "against yet", kind="info",
                detail="run it again after next month's purchases and it will "
                       "report what moved rather than what is")
            led.record_check(current, [], period=period,
                             used_agent=ctx.use_agent)
            say(text="Nothing was filed, amended or claimed.", kind="done")
            return

        changes = diff(previous, current)
        if not changes:
            say(text="Nothing changed since the last check", kind="done",
                detail="which is the answer most of the time, and saying so is "
                       "the point - a watch that speaks every day gets muted")
            led.record_check(current, [], period=period,
                             used_agent=ctx.use_agent)
            return

        say(text=f"{len(changes)} thing"
                 f"{'' if len(changes) == 1 else 's'} moved since last time",
            kind="rules",
            detail="; ".join(CHANGE_LABEL.get(c.kind, c.kind) for c in changes))

        raised = []
        if ctx.use_agent:
            from agent.watch_agent import ClaudeWatchAgent

            say(text="Deciding which of these is worth interrupting you for",
                kind="agent",
                detail="the amount alone does not decide it - a smaller sum "
                       "with interest already running outranks a larger one "
                       "with a year left to claim")
            agent = ClaudeWatchAgent()
            for n, change in enumerate(changes, 1):
                ctx.progress(phase=f"Weighing {change.name} "
                                   f"({n} of {len(changes)})")
                verdict = agent.judge(change)
                raised.append(verdict)
                if verdict.raise_it:
                    say(text=f"{verdict.headline}", kind="finding",
                        detail=f"{verdict.reasoning}  [{verdict.urgency}, "
                               f"{verdict.action}]")
                else:
                    say(text=f"Not worth raising: {change.name}", kind="quiet",
                        detail=verdict.headline)
        else:
            say(text="The agent is switched off, so every change is being "
                     "shown unfiltered", kind="warn",
                detail="deciding which of these matters is the part that needs "
                       "judgment")
            from agent.watch_agent import Raised

            for change in changes:
                raised.append(Raised(
                    kind=change.kind, gstin=change.gstin, name=change.name,
                    raise_it=True, urgency="this_week", action="watch",
                    headline=CHANGE_LABEL.get(change.kind, change.kind),
                    reasoning=change.detail,
                    exposed_paise=change.exposed_paise))

        check_id = led.record_check(current, raised, period=period,
                                    used_agent=ctx.use_agent)
        ctx.progress(target_id=check_id)

        spoke = sum(1 for r in raised if r.raise_it)
        quiet = len(raised) - spoke
        say(text=f"{spoke} raised, {quiet} left alone", kind="total",
            detail="staying quiet about the rest is the difference between a "
                   "watch you read and one you mute")
        say(text="Nothing was filed, amended or claimed. Every line above is a "
                 "proposal.", kind="done")


ITC_RECONCILER = register(AgentSpec(
    id="gst_itc",
    name="GST Input Credit Reconciler",
    short_name="Input credit",
    tagline="Finds input tax credit you are entitled to and have not claimed, "
            "and credit you are claiming and should not.",
    question="Which of my suppliers did not file, what is it costing me, and "
             "what am I claiming that will come back as a notice?",
    status="live",
    reads=["purchase register", "GSTR-2B"],
    produces=["credit at risk", "credit to stop claiming",
              "supplier follow-up letters"],
    authority="CGST Act s.16(2)(c) as upheld in Bhandari Scrap Traders, "
              "s.16(4), s.17(5), Rule 37, Rule 88D",
    why_unbuilt="The credit is lost silently. Nothing tells a merchant a "
                "supplier failed to file, and since the Supreme Court put the "
                "burden of proof on the buyer, nobody whose incentive is "
                "aligned is going to.",
    runner=run_itc_reconciliation,
))
