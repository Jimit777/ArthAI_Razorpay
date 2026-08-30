"""
The GST output-tax controller, as a registered agent.

Same registration interface as every other agent here, and one workspace
for four layers rather than four catalogue entries - see
engine/gst_filing/taxonomy.py and this plan's own reasoning: the layers
share one subject (a business's outward GST position) and layers 2-4 are
sequential consumers of layer 1's own output, not four independent
capabilities. `merchant/agents/gst.py` stays the ITC (purchase-side) agent
unchanged; this file is entirely the output-tax (sales-side) agent.

This wires all four layers: GSTR-1 assembly, timing each filing period
against the GSTR-1A window and the post-lock DRC-03 route, the ITC offset
hierarchy and the Rule 88C shield, and the QRMP method choice with its IFF
plan. Layer 2's cycles are seeded from layer 1's own just-assembled total,
layer 3's ledger snapshot is sized off layer 1's own per-head liability, and
layer 4's quarter is built from layer 3's own cash-needed figure - one demo
run tells a single consistent story rather than four unrelated ones.
"""

from __future__ import annotations

import time

from engine.gst_filing import rules
from engine.gst_filing.classifier import assemble_gstr1, classify_batch
from engine.gst_filing.generator import AS_OF, plant_qrmp_quarter
from engine.gst_filing.offset import HeadAmounts, liability_from_invoices
from engine.gst_filing.qrmp import build_qrmp_plan
from engine.gst_filing.scoring import score_classification, score_corrections
from engine.gst_filing.taxonomy import CorrectionCode
from merchant.catalog import AgentContext, AgentSpec, register

FILING_PERIOD = "2026-08"


def _line(text: str, kind: str = "info", detail: str = "") -> dict:
    return {"text": text, "kind": kind, "detail": detail,
            "at": int(time.time() * 1000)}


def run_gst_filing_pipeline(ctx: AgentContext) -> None:
    """Demo Mode only for this pass - generates its own outward-sales batch,
    HSN rate card and filing-cycle history, ignores ctx.target_id, same
    shape as the other demo runners this session."""
    from merchant.gst_correction_pipeline import run as run_corrections
    from merchant.gst_offset_pipeline import run as run_offsets
    from merchant.ledger import Ledger

    def say(**kw) -> None:
        ctx.progress(line=_line(**kw))

    with Ledger(ctx.db, ctx.business_id) as led:
        n, l1_truth = led.seed_gst_filing_demo(40)
        say(text=f"Recorded {n} outward sales and a demo HSN rate card",
            kind="start")

        batch = led.build_gstr1_batch()
        if batch is None:
            raise ValueError("there are no unfiled outward invoices")

        rate_card = led.hsn_rate_card()
        classified = classify_batch(
            batch, home_state="27", rate_card=rate_card,
            e_invoicing_applicable=True)
        draft = assemble_gstr1(classified, period=FILING_PERIOD)

        say(text=f"{len(draft.b2b)} B2B, {len(draft.b2cl)} B2CL, "
                 f"{len(draft.b2cs)} B2CS - {rules.rupees(draft.total_tax)} "
                 f"of output tax", kind="rules")
        if draft.missing_irn:
            say(text=f"{len(draft.missing_irn)} B2B invoices are missing an "
                     f"e-invoice IRN", kind="finding")
        if draft.unconfigured:
            say(text=f"{len(draft.unconfigured)} invoices sit on an HSN with "
                     f"no rate on file - excluded from the draft, not guessed",
                kind="finding")

        l1_card = score_classification(classified, l1_truth)
        say(text=f"Measured against the planted answer key: "
                 f"{l1_card.correct}/{l1_card.total} invoices classified "
                 f"correctly ({l1_card.accuracy:.0%}), "
                 f"{l1_card.anomalies_caught}/{l1_card.anomalies} planted "
                 f"anomalies caught", kind="rules")

        run_id = led.commit_gstr1_run(classified, draft, period=FILING_PERIOD)
        ctx.progress(target_id=run_id, run_id=run_id)

        n_cycles, l2_truth = led.seed_gst_correction_demo(
            FILING_PERIOD, draft.total_tax)
        say(text=f"Timing {n_cycles} filing periods against the GSTR-1A "
                 f"window", kind="rules")
        cycles = led.filing_cycles()
        result = run_corrections(
            cycles, today=AS_OF, use_agent=ctx.use_agent,
            on_progress=lambda **kw: ctx.progress(**kw))
        led.record_correction_findings(run_id, result.findings, result.decisions)

        l2_card = score_corrections(result.findings, result.decisions, l2_truth)
        say(text=f"Measured against the planted answer key: "
                 f"{l2_card.correct}/{l2_card.total} periods timed "
                 f"correctly ({l2_card.accuracy:.0%})", kind="rules")

        n_1a = sum(1 for f in result.findings if f.action == "file_1a")
        n_drc03 = sum(1 for f in result.findings if f.action == "pay_drc03")
        if n_1a:
            say(text=f"{n_1a} period(s) can still be corrected via GSTR-1A",
                kind="finding")
        if n_drc03:
            say(text=f"{n_drc03} period(s) are locked and need a DRC-03 "
                     f"payment", kind="finding")
        if result.verdict is not None:
            say(text=f"Agent: {result.verdict.overall_reasoning}",
                kind="finding")
        n_queued = sum(1 for d in result.decisions.values()
                       if d.queued_for_human)
        if n_queued:
            say(text=f"{n_queued} period(s) sent to a person to decide",
                kind="queued")

        liability = liability_from_invoices(draft.b2b + draft.b2cl + draft.b2cs)
        led.seed_gst_offset_demo(liability, str(AS_OF))
        balance = led.latest_gst_ledger_balance()
        credit = HeadAmounts(balance["credit_igst"], balance["credit_cgst"],
                             balance["credit_sgst"])
        cash_on_hand = HeadAmounts(balance["cash_igst"], balance["cash_cgst"],
                                   balance["cash_sgst"])
        locked = [f for f in result.findings
                 if f.exception_code == str(CorrectionCode.LOCKED_NEEDS_DRC03)]

        offset_result = run_offsets(
            current_period=FILING_PERIOD, liability=liability, credit=credit,
            cash_on_hand=cash_on_hand, locked_findings=locked,
            use_agent=ctx.use_agent,
            on_progress=lambda **kw: ctx.progress(**kw))
        led.record_offset_findings(run_id, offset_result.findings,
                                   offset_result.drc01b_bodies)

        current = offset_result.findings[0]
        say(text=current.reasoning, kind="rules")
        breaches = [f for f in offset_result.findings if f.rule_88c_breach]
        if breaches:
            say(text=f"{len(breaches)} already-filed period(s) breach Rule "
                     f"88C and now have a DRC-01B reply drafted",
                kind="finding")

        qrmp_kwargs = plant_qrmp_quarter(
            FILING_PERIOD, current_month_taxable_paise=draft.total_taxable,
            current_month_self_assessed_paise=current.plan.total_cash_needed,
            current_month_b2b_tax_paise=[i.total_tax for i in draft.b2b])
        qrmp_plan = build_qrmp_plan(
            **qrmp_kwargs, materiality_paise=led.iff_materiality())
        led.record_qrmp_finding(run_id, qrmp_plan)
        say(text=qrmp_plan.reasoning, kind="rules")

        say(text="Nothing has been filed or paid. This is a draft, laid out "
                 "like GSTR-1, GSTR-1A, DRC-03, PMT-06 and DRC-01B - never "
                 "an upload or a submission.", kind="done")


GST_FILING = register(AgentSpec(
    id="gst_filing",
    name="GST Output Tax Reconciler",
    short_name="Output tax",
    tagline="Assembles your GSTR-1, times your corrections against the "
            "GSTR-1A window, and finds the minimum cash you actually owe.",
    question="What does my outward tax return say, is it still correctable, "
             "and how little cash do I actually need to pay it?",
    status="live",
    reads=["sales/invoice register", "HSN rate card", "GSTR-1 filing history",
          "GSTR-3B filing history", "electronic credit and cash ledger balances"],
    produces=["GSTR-1-shaped filing draft", "e-invoice IRN gap list",
             "GSTR-1A amendment draft", "DRC-03 voluntary-payment draft",
             "PMT-06 challan draft", "Rule 88C / DRC-01B response draft",
             "QRMP method recommendation and IFF plan"],
    authority="CGST Rule 88C, CGST Act s.50, s.73/74, and the QRMP scheme",
    runner=run_gst_filing_pipeline,
))
