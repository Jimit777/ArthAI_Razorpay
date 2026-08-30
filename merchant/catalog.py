"""
The catalogue of financial agents this platform offers.

## The thesis, in one line

Every agent here audits the same kind of gap: **something was agreed or
legislated, something else actually happened, and nobody checks whether they
match.** The settlement auditor does it for gateway fees. The same shape recurs
across Indian merchant finance, which is why this is a platform and not a tool.

## Honesty rule for this file

Five agents are LIVE: settlement_audit, gst_itc, cash_forecaster,
three_way_recon, payout_timing. The rest are declared with `status="planned"`
and have no implementation, because a convincing mock of a working GST
reconciler is not a roadmap, it is a lie with a progress bar. The UI renders
planned agents as plainly unavailable and they cannot be run.

Adding a new live agent means writing an implementation and flipping a status.
The registry, the per-business enablement table, the run plumbing and the audit
log are already generic - that was the point of doing this before agent two
rather than after.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional, Protocol


@dataclass
class AgentContext:
    """Everything an agent needs to do one piece of work."""
    business_id: str
    rate_card: dict
    db: str
    target_id: str                      # what to work on; a settlement id here
    use_agent: bool = True              # False = deterministic rules only
    progress: Callable[..., None] = lambda **kw: None


class AgentRunner(Protocol):
    def __call__(self, ctx: AgentContext) -> None: ...


@dataclass
class AgentSpec:
    id: str
    name: str
    tagline: str                        # what it does, one line
    question: str                       # the question it answers for the merchant
    status: str                         # "live" | "planned"
    short_name: str = ""                # for the rail, where space is 14 chars
    reads: list[str] = field(default_factory=list)
    produces: list[str] = field(default_factory=list)
    authority: str = ""                 # the law or contract it argues from
    why_unbuilt: str = ""               # why this gap exists at all
    runner: Optional[AgentRunner] = None

    @property
    def rail_label(self) -> str:
        """
        What the sidebar calls it.

        Explicit rather than derived. This used to strip " Deduction" and
        " Auditor" from the name, which shortened exactly one agent's name and
        left the second one wrapping onto two lines.
        """
        return self.short_name or self.name.split()[0]

    @property
    def is_live(self) -> bool:
        return self.status == "live" and self.runner is not None


CATALOG: dict[str, AgentSpec] = {}


def register(spec: AgentSpec) -> AgentSpec:
    CATALOG[spec.id] = spec
    return spec


def get(agent_id: str) -> Optional[AgentSpec]:
    return CATALOG.get(agent_id)


def live_agents() -> list[AgentSpec]:
    return [a for a in CATALOG.values() if a.is_live]


def all_agents() -> list[AgentSpec]:
    return sorted(CATALOG.values(), key=lambda a: (a.status != "live", a.name))


# --- what is coming, stated as intent rather than as product -------------
#
# Each of these is a real, documented gap in Indian merchant finance with the
# same shape as the settlement problem: an agreed or legislated number on one
# side, an actual number on the other, and no routine check between them.
# None of them are built.

PLANNED = [
    AgentSpec(
        id="tds_credit",
        name="TDS Credit Tracker",
        short_name="TDS credit",
        tagline="Checks that tax withheld from you actually reached the department.",
        question="Was TDS deducted from my payouts, and did it show up as my credit?",
        status="planned",
        reads=["Form 26AS", "Form 168", "settlement reports"],
        produces=["missing credit claims", "corrected section codes"],
        authority="Income Tax Act 2025 s.393 - and the 1 April 2026 code change",
        why_unbuilt="Razorpay's settlement report carries no TDS line - the "
                    "only real documents are a quarterly Form 16A certificate "
                    "and Form 26AS/168, neither with an API. Testing this "
                    "against real data would mean a merchant manually "
                    "cross-referencing both documents before the tool ever "
                    "runs, which does the tool's one job for them.",
    ),
    AgentSpec(
        id="chargeback",
        name="Chargeback Defence Assembler",
        tagline="Builds the evidence pack before the window closes.",
        question="Which disputes can I actually win, and what do I need to send?",
        status="planned",
        reads=["chargeback notices", "order records", "delivery proof"],
        produces=["evidence packs", "deadline tracking"],
        authority="Card network dispute rules and their representment windows",
        why_unbuilt="The deadline is short and the paperwork is per-case. Small "
                    "merchants forfeit by not replying.",
    ),
    AgentSpec(
        id="vendor_terms",
        name="Vendor Invoice Auditor",
        tagline="Checks supplier invoices against the terms you agreed.",
        question="Am I being billed the rates and discounts in my contract?",
        status="planned",
        reads=["supplier invoices", "purchase contracts"],
        produces=["term breaches", "credit note requests"],
        authority="The purchase agreement",
        why_unbuilt="Same incentive problem as the gateway: nobody who issues "
                    "an invoice builds the tool that audits it.",
    ),
    AgentSpec(
        id="gst_filing",
        name="GST Output Tax Reconciler",
        tagline="Checks that what you declared in GSTR-1 matches what you "
                "paid in GSTR-3B.",
        question="Do my GSTR-1 and GSTR-3B agree, and would Rule 88C catch "
                 "the gap before I do?",
        status="planned",
        reads=["GSTR-1", "GSTR-3B", "sales register"],
        produces=["mismatch report", "Rule 88C explanation draft"],
        authority="CGST Rule 88C - the GSTR-1/GSTR-3B liability mismatch check",
        why_unbuilt="The auto-populated GSTR-3B and the GSTR-1 actually filed "
                    "can drift for a dozen small reasons, and Rule 88C now "
                    "makes that drift the department's business before it is "
                    "yours.",
    ),
]

for _spec in PLANNED:
    register(_spec)
