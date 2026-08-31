"""
The catalogue of financial agents this platform offers.

## The thesis, in one line

Every agent here audits the same kind of gap: **something was agreed or
legislated, something else actually happened, and nobody checks whether they
match.** The settlement auditor does it for gateway fees. The same shape recurs
across Indian merchant finance, which is why this is a platform and not a tool.

## Honesty rule for this file

Eight agents are LIVE: settlement_audit, gst_itc, cash_forecaster,
three_way_recon, payout_timing, gst_filing, vendor_terms, chargeback. The rest are declared with
`status="planned"` and have no implementation, because a convincing mock of a working GST
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
    source: str = "demo"                # "demo" | "connected" - which
                                         # runners branch on it; most ignore it


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

# Nothing is currently advertised as planned. The TDS Credit Tracker used to
# sit here; it was withdrawn rather than shipped as a roadmap promise, because
# neither side of that reconciliation has an API and there was no honest path
# to testing it against real data. Its run logic, engine and tests remain in
# the tree (merchant/agents/tds_credit.py, engine/tds/) as unwired groundwork,
# so re-advertising it later is a registration rather than a rebuild.
#
# The list and the registration loop below stay deliberately: "what is coming"
# is a real product concept, and an empty roadmap is a truthful answer to it.
PLANNED: list[AgentSpec] = []

for _spec in PLANNED:
    register(_spec)
