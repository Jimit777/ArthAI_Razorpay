"""
The GST correction run: filing cycles in, one decision per period out.

Mirrors merchant/payout_timing_pipeline.py's shape - detect_cycles()
reduces the whole set of periods to per-period findings, and (only when
there is at least one open period worth prioritising) the agent is asked
once about the whole run's open periods together, never once per period.
See engine/gst_filing/timing.py's module docstring for why.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date
from typing import Callable, Optional


@dataclass
class GSTCorrectionResult:
    findings: list = field(default_factory=list)
    decisions: dict = field(default_factory=dict)
    verdict: Optional[object] = None
    processing_time_ms: int = 0
    used_agent: bool = False

    def as_dict(self) -> dict:
        return {
            "findings": [f.as_dict() for f in self.findings],
            "decisions": {p: d.__dict__ for p, d in self.decisions.items()},
            "verdict": self.verdict.as_dict() if self.verdict else None,
            "judged_by_agent": self.used_agent,
        }


def run(cycles: list, *, today: date, use_agent: bool = True, agent=None,
       business: str = "",
       on_progress: Optional[Callable[..., None]] = None
       ) -> GSTCorrectionResult:
    """Time every cycle, then - only if there is an open period to weigh
    against another - have the agent prioritise them once."""
    from engine.gst_filing.gate import gate
    from engine.gst_filing.taxonomy import CorrectionCode
    from engine.gst_filing.timing import detect_cycles

    def say(**kw):
        if on_progress is not None:
            on_progress(**kw)

    started = time.monotonic()
    out = GSTCorrectionResult(used_agent=use_agent)

    say(phase=f"Timing {len(cycles)} filing period(s) against the GSTR-1A "
              f"window and the lock")
    out.findings = detect_cycles(cycles, today=today)
    open_findings = [f for f in out.findings if f.exception_code
                     == str(CorrectionCode.CORRECTABLE_VIA_1A)]

    priorities: dict = {}
    if use_agent and open_findings:
        say(phase="Asking the agent which open period to file first")
        if agent is None:
            from agent.gst_correction_classifier import ClaudeGSTCorrectionAgent

            agent = ClaudeGSTCorrectionAgent()
        out.verdict = agent.judge(open_findings, business=business)
        priorities = out.verdict.periods

    out.decisions = {f.period: gate(f, priorities.get(f.period))
                     for f in out.findings}
    out.processing_time_ms = int((time.monotonic() - started) * 1000)
    return out
