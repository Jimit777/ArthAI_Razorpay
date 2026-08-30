"""
The GST offset run: the current period's cash-needed allocation, plus a
Rule 88C check against every LOCKED period layer 2 already timed.

Mirrors merchant/gst_correction_pipeline.py's shape - pure arithmetic
first, and the agent asked only when there is something to draft. Unlike
layer 2's single batched call, layer 3 may make ZERO, ONE or several agent
calls: one per period that actually breaches Rule 88C, since each DRC-01B
reply is a distinct document answering a distinct notice, not one
comparative judgment across periods - see
agent/gst_filing_documents.py::write_case().
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Optional


@dataclass
class GSTOffsetResult:
    findings: list = field(default_factory=list)
    drc01b_bodies: dict = field(default_factory=dict)
    written_by: dict = field(default_factory=dict)   # period -> "agent"/"template"
    processing_time_ms: int = 0
    used_agent: bool = False


def run(*, current_period: str, liability, credit, cash_on_hand,
       locked_findings: list, use_agent: bool = True,
       on_progress: Optional[Callable[..., None]] = None
       ) -> GSTOffsetResult:
    """`locked_findings` are engine.gst_filing.timing.CorrectionFinding
    objects whose exception_code is LOCKED_NEEDS_DRC03 - layer 2's own
    output, reused rather than re-derived (see offset.py's module
    docstring for why Rule 88C shares its two numbers with layer 2)."""
    from engine.gst_filing.offset import (finding_from_88c_check,
                                          finding_from_allocation)

    def say(**kw):
        if on_progress is not None:
            on_progress(**kw)

    started = time.monotonic()
    out = GSTOffsetResult(used_agent=use_agent)

    say(phase=f"Allocating {current_period}'s liability across the credit "
              f"and cash ledgers")
    out.findings.append(finding_from_allocation(
        current_period, liability, credit, cash_on_hand=cash_on_hand))

    for f in locked_findings:
        breach = finding_from_88c_check(f.period, f.gstr1_liability,
                                        f.gstr3b_paid)
        if breach is not None:
            out.findings.append(breach)

    breaches = [f for f in out.findings if f.rule_88c_breach]
    if breaches:
        say(phase=f"Drafting a Rule 88C reply for {len(breaches)} period(s)")
    for f in breaches:
        if use_agent:
            from agent.gst_filing_documents import write_case

            text, error = write_case(f)
        else:
            text, error = "", None
        from agent.gst_filing_documents import drc01b_response

        doc = drc01b_response(f, case=text if not error else "")
        out.drc01b_bodies[f.period] = doc.body
        out.written_by[f.period] = doc.written_by

    out.processing_time_ms = int((time.monotonic() - started) * 1000)
    return out
