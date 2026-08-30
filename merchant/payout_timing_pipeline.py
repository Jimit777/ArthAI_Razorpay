"""
The whole payout timing run: a batch in, one verdict out.

Mirrors merchant/treasury_pipeline.py's shape - a demo generator or (later)
real data produces the same ReconBatch shape, detect() reduces it to one
summary, and the agent is asked once about that summary, never once per
record. See engine/payout_timing/detector.py's module docstring for why
there is only ever one thing to judge here.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Optional


@dataclass
class PayoutTimingResult:
    summary: Optional[object] = None
    decision: Optional[object] = None
    verdict: Optional[object] = None
    processing_time_ms: int = 0
    used_agent: bool = False
    source: str = "demo"

    def as_dict(self) -> dict:
        payload = self.summary.as_dict() if self.summary else {}
        return {
            "metadata": {
                "source": self.source,
                "processing_time_ms": self.processing_time_ms,
                "judged_by_agent": self.used_agent,
            },
            "summary": payload,
            "decision": (self.decision.__dict__
                        if self.decision is not None else None),
            "verdict": self.verdict.as_dict() if self.verdict else None,
        }


def run(batch, *, use_agent: bool = True, agent=None, business: str = "",
       source: str = "demo",
       on_progress: Optional[Callable[..., None]] = None
       ) -> PayoutTimingResult:
    """Detect the pattern, then - if asked - have the agent narrate it."""
    from engine.payout_timing.detector import detect
    from engine.payout_timing.gate import gate

    def say(**kw):
        if on_progress is not None:
            on_progress(**kw)

    started = time.monotonic()
    out = PayoutTimingResult(used_agent=use_agent, source=source)

    say(phase=f"Checking {len(batch.invoices)} settlements against the "
              f"promised cycle")
    out.summary = detect(batch)
    say(phase=f"{out.summary.n_sla_miss} of {out.summary.n_settled} "
             f"settled late", done=1, total=1)

    if not use_agent:
        out.decision = gate(out.summary, None)
        out.processing_time_ms = int((time.monotonic() - started) * 1000)
        return out

    say(phase="Asking the agent what the pattern means")
    if agent is None:
        from agent.payout_timing_classifier import ClaudePayoutTimingAgent

        agent = ClaudePayoutTimingAgent()

    out.verdict = agent.judge(out.summary, business=business)
    out.decision = gate(out.summary, out.verdict)
    out.processing_time_ms = int((time.monotonic() - started) * 1000)
    return out
