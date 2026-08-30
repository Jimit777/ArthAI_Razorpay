"""
The whole treasury run: balances and obligations in, a thirty-day curve out.

    A  gather the four inputs, from wherever this business has them
    B  project thirty days forward                         (forecaster)
    C  ask the agent which payout to move                  (classifier)
    D  build the payload                                   (here)

## The convergence point

Everything - the demo simulator, three uploaded CSVs, a bank API - produces a
TreasuryInputs. The forecaster turns that into a list of DailyPosition and
nothing downstream can tell which source it came from. That is the same
guarantee the other three agents make, and it is the reason the dashboard is
written once.

## Why the agent is asked once and not thirty times

There is one decision in a cash forecast: what to do about the low point.
Asking about each of thirty days would be thirty calls to be told twenty-nine
times that nothing happened, and the twenty-ninth would cost the same as the
one that mattered.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from agent.pricing import Usage
from engine.treasury.forecaster import (DEFAULT_DAYS, SAFE_FLOOR_PAISE,
                                        project_cash_flow)
from engine.treasury.records import TreasuryInputs


@dataclass
class TreasuryResult:
    forecast: Optional[object] = None
    verdict: Optional[object] = None
    processing_time_ms: int = 0
    used_agent: bool = False
    failed_calls: int = 0
    usage: Usage = field(default_factory=Usage)
    source: str = "demo"
    inputs_summary: dict = field(default_factory=dict)
    planted: dict = field(default_factory=dict)
    accuracy: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        payload = self.forecast.as_dict() if self.forecast else {}
        return {
            "metadata": {
                "source": self.source,
                "days": len(payload.get("positions", [])),
                "processing_time_ms": self.processing_time_ms,
                "judged_by_agent": self.used_agent,
                "failed_calls": self.failed_calls,
                "usage": self.usage.as_dict(),
                "inputs": self.inputs_summary,
                # Only ever populated for the demo, where the scenario was
                # built to a known shape. Real balances have no answer key.
                "accuracy": self.accuracy,
            },
            "forecast": payload,
            "verdict": self.verdict.as_dict() if self.verdict else None,
        }


def run(inputs: TreasuryInputs, *, days: int = DEFAULT_DAYS,
        floor: int = SAFE_FLOOR_PAISE, use_agent: bool = True, agent=None,
        business: str = "", source: str = "demo", business_id: str = "",
        planted: Optional[dict] = None,
        on_progress: Optional[Callable[..., None]] = None) -> TreasuryResult:
    """Project the curve, then ask what to do about the low point."""
    def say(**kw):
        if on_progress is not None:
            on_progress(**kw)

    started = time.monotonic()
    out = TreasuryResult(used_agent=use_agent, source=source,
                         planted=dict(planted or {}))
    out.inputs_summary = {
        "accounts": len(inputs.accounts),
        "receipts": len(inputs.receipts),
        "payouts": len(inputs.payouts),
        "recurring": len(inputs.recurring),
        "records": inputs.total_records,
    }

    say(phase=f"Projecting {days} days from "
              f"{inputs.total_records} balances and obligations")
    out.forecast = project_cash_flow(inputs, days=days, floor=floor)

    if planted:
        out.accuracy = score(out.forecast, planted)

    say(phase=f"Low point: {out.forecast.trough.balance / 100:,.0f} rupees"
              if out.forecast.trough else "Projected",
        done=1, total=1)

    if not use_agent:
        out.processing_time_ms = int((time.monotonic() - started) * 1000)
        return out

    say(phase="Asking the agent what to move")
    if agent is None:
        from agent.treasury_classifier import ClaudeTreasuryAgent

        agent = ClaudeTreasuryAgent()

    # The inputs go with the forecast so the agent can check a candidate
    # against them instead of picking one off a list and hoping.
    #
    # The cross-agent tools only make sense for a real business - a demo
    # forecast's payment ids and business_id are generated fresh for that
    # scenario and were never audited by anything. See
    # cross_agent_tools.build_tools().
    extra_tools = []
    disputed_receipts: list = []
    at_risk_credit_found: list = []
    recon_flagged: list = []
    at_risk_output_tax_found: list = []
    if source != "demo" and business_id:
        from merchant.cross_agent_tools import build_tools as cross_agent_tools

        extra_tools = cross_agent_tools(
            business_id, found=disputed_receipts,
            credit_found=at_risk_credit_found, recon_found=recon_flagged,
            output_tax_found=at_risk_output_tax_found)

    out.verdict = agent.judge(out.forecast, business=business, inputs=inputs,
                              extra_tools=extra_tools)
    # All four are populated as a side effect of judge() calling the tools
    # above - read back now that the call is done, not threaded through
    # judge()'s own return value, so this stays the merchant layer's business.
    out.verdict.disputed_receipts = disputed_receipts
    out.verdict.at_risk_credit = at_risk_credit_found[-1] if at_risk_credit_found else {}
    out.verdict.recon_flagged = recon_flagged
    out.verdict.at_risk_output_tax = (
        at_risk_output_tax_found[-1] if at_risk_output_tax_found else {})
    out.usage.add(out.verdict)
    if getattr(out.verdict, "error", None):
        out.failed_calls = 1

    out.processing_time_ms = int((time.monotonic() - started) * 1000)
    return out


def score(forecast, planted: dict) -> dict:
    """
    What the engine concluded against what the scenario was built to be.

    The demo only. It is the reason the crunch on the page can be pointed at
    and called a real finding rather than a coincidence - and it fails loudly
    if somebody tunes the generator until the plant stops taking, which has
    already happened once.
    """
    checks = []
    if "crunch_day" in planted and forecast.trough is not None:
        checks.append(("the trough lands on the planted day",
                       forecast.trough.day == planted["crunch_day"]))
    if "expected_finding" in planted:
        checks.append(("the finding is the one planted",
                       forecast.finding == planted["expected_finding"]))
    if "coverable_by_delay" in planted:
        checks.append(("rescheduling covers it, as planted",
                       forecast.coverable_by_delay
                       == planted["coverable_by_delay"]))
    if "unmovable_on_crunch_day" in planted:
        found = {r.get("payout_id") for r in forecast.unmovable_near_trough}
        checks.append((
            "the unmovable payouts are the ones planted",
            set(planted["unmovable_on_crunch_day"]).issubset(found)))

    passed = sum(1 for _, ok in checks if ok)
    return {
        "checks": [{"what": what, "ok": ok} for what, ok in checks],
        "passed": passed, "total": len(checks),
        "all_passed": passed == len(checks) and bool(checks),
    }
