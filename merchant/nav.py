"""
The information architecture, in one file.

## Why this exists

The navigation used to be assembled inline inside the frame: fifteen items in
five ad-hoc groups, with agent pages (settlements, purchases, suppliers, ask)
sitting at the root beside account settings. Adding the second agent made the
shape of the problem obvious - every new agent added three more root-level
items, and nothing said which page belonged to which agent.

Putting the whole IA in one small module means the next person to disagree with
it changes a list rather than hunting through a four-thousand-line file. That
is the point; the specific arrangement below matters less than the fact that it
is arrangeable.

## The rule the grouping follows

A root-level item is something you go to REGARDLESS of which agent you are
thinking about. Everything that only makes sense inside one agent lives inside
that agent's workspace, as a tab.

    Home              across everything
    Agents            the hub, and the way into each workspace
    Data              where the numbers come from - shared by every agent
    Settings, Team    the business, not the work
    Admin, Accuracy   the platform, not the business

"Ask" was the clearest offender: a natural-language box that could only ever
answer questions about settlements, sitting at the root as though it spoke for
the whole product.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class NavItem:
    icon: str
    label: str
    href: str
    key: str
    owner_only: bool = False
    operator_only: bool = False


@dataclass(frozen=True)
class NavGroup:
    label: str                      # "" renders with no heading
    items: tuple[NavItem, ...]
    operator_only: bool = False


NAV: tuple[NavGroup, ...] = (
    NavGroup("", (
        NavItem("◈", "Home", "/", "home"),
    )),
    NavGroup("Workspace", (
        NavItem("◉", "Agents", "/agents", "agents"),
        NavItem("⇅", "Data & integrations", "/data", "data"),
    )),
    NavGroup("Settings", (
        NavItem("▣", "Businesses", "/businesses", "businesses"),
        NavItem("⚙", "Business settings", "/settings", "settings"),
        NavItem("&#128100;", "Team", "/people", "people", owner_only=True),
        NavItem("&#9636;", "Activity", "/activity", "activity", owner_only=True),
    )),
    NavGroup("Platform", (
        NavItem("◆", "Admin", "/admin", "admin"),
        NavItem("▲", "Accuracy", "/admin/accuracy", "accuracy"),
    ), operator_only=True),
)


def visible(role: Optional[str], is_operator: bool) -> list[tuple[str, list[NavItem]]]:
    """The groups this person should actually see, with empty ones dropped."""
    out = []
    for group in NAV:
        if group.operator_only and not is_operator:
            continue
        items = [i for i in group.items
                 if not (i.owner_only and role != "owner")
                 and not (i.operator_only and not is_operator)]
        if items:
            out.append((group.label, items))
    return out


# --- inside an agent -------------------------------------------------------
#
# Tabs, not sidebar entries. A tab says "this belongs to the agent you are
# already in", which is exactly the relationship the old root-level items got
# wrong.


@dataclass(frozen=True)
class AgentTab:
    label: str
    slug: str                       # "" is the workspace's own landing tab
    blurb: str = ""


@dataclass(frozen=True)
class AgentRoute:
    agent_id: str
    slug: str                       # the URL segment: /agents/<slug>
    tabs: tuple[AgentTab, ...] = ()

    @property
    def href(self) -> str:
        return f"/agents/{self.slug}"

    def tab_href(self, tab: AgentTab) -> str:
        return self.href if not tab.slug else f"{self.href}/{tab.slug}"


AGENT_ROUTES: dict[str, AgentRoute] = {
    "settlement_audit": AgentRoute(
        agent_id="settlement_audit", slug="settlement",
        tabs=(
            AgentTab("Settlements", "", "every batch and what was found in it"),
            AgentTab("Ask", "ask", "put a question to the auditor"),
            AgentTab("Setup", "setup", "what this agent needs to run"),
        )),
    "gst_itc": AgentRoute(
        agent_id="gst_itc", slug="input-credit",
        tabs=(
            # The tabs are the three ways a merchant can GET supplier filing
            # history, because that - not the analysis - is the only thing
            # that differs between them. Everything downstream is identical:
            # same FilingHistory contract, same arithmetic, same agent, same
            # dashboard. Naming the tabs after the ingestion route makes the
            # choice a merchant actually has to make the first thing they see,
            # instead of burying it behind a screen that changed shape for
            # reasons they could not name.
            AgentTab("Demo Mode", "",
                     "generated suppliers and generated history, in one click"),
            AgentTab("Without API", "without-api",
                     "your register plus GSTR-2B files you download yourself"),
            AgentTab("With API", "with-api",
                     "your register; history fetched per supplier over a GSP"),
        )),
    "three_way_recon": AgentRoute(
        agent_id="three_way_recon", slug="three-way",
        tabs=(
            # Same shape as the input credit agent's tabs, and for the same
            # reason: the only thing that differs between them is where the
            # three sources come from. The join, the arithmetic, the agent and
            # the dashboard are identical whichever tab produced the run.
            AgentTab("Demo Mode", "",
                     "generated invoices, settlements and credits, one click"),
            AgentTab("Upload", "upload",
                     "your own three exports - works with any bank"),
            AgentTab("Connected", "connected",
                     "settlements pulled from Razorpay; you upload two"),
            AgentTab("Matched", "matched",
                     "every line the three sources closed between them"),
        )),
    "cash_forecaster": AgentRoute(
        agent_id="cash_forecaster", slug="cash-forecaster",
        tabs=(
            AgentTab("Demo Mode", "",
                     "a generated month with a crunch planted on day 14"),
            AgentTab("Without API", "upload",
                     "your balances and your payables, as files"),
            AgentTab("With API", "connected",
                     "settlements pulled; balances still yours to supply"),
        )),
    "payout_timing": AgentRoute(
        agent_id="payout_timing", slug="payout-timing",
        tabs=(
            # Both real-data tabs read the same two stored sources the
            # three-way agent already holds - sales and settlements. This
            # agent needs no bank statement: it asks when the gateway settled
            # a sale against when it promised to, and the two dates answer
            # that between them.
            AgentTab("Demo Mode", "",
                     "a generated settlement batch with planted delays"),
            AgentTab("Upload", "upload",
                     "your own sales and settlement exports"),
            AgentTab("Connected", "connected",
                     "settlements pulled from Razorpay; you upload sales"),
        )),
    "vendor_terms": AgentRoute(
        agent_id="vendor_terms", slug="vendor-terms",
        tabs=(
            # Same shape as the input credit agent's tabs, for the same
            # reason: the only thing that differs between them is where the
            # line items come from. The rate-card check, the agent and the
            # results page are identical whichever tab produced the batch.
            AgentTab("Demo Mode", "",
                     "generated invoices and a matching rate card, one click"),
            AgentTab("Without API", "without-api",
                     "your own purchase register, uploaded as a file"),
            AgentTab("With API", "with-api",
                     "purchases pulled from Zoho Books"),
        )),
    "chargeback": AgentRoute(
        agent_id="chargeback", slug="chargeback",
        tabs=(
            # The same honest asymmetry cash_forecaster's own tabs already
            # have: With API pulls the dispute NOTICE for real, but the
            # EVIDENCE behind it - delivery proof, a customer's chat log -
            # is nobody's API, so it stays yours to supply on every tab.
            AgentTab("Demo Mode", "",
                     "generated disputes and generated evidence, scored "
                     "against a planted answer key"),
            AgentTab("Without API", "without-api",
                     "type in a dispute notice yourself, and what "
                     "evidence you have"),
            AgentTab("With API", "with-api",
                     "disputes pulled from Razorpay; evidence still "
                     "yours to supply"),
        )),
    "gst_filing": AgentRoute(
        agent_id="gst_filing", slug="gst-filing",
        tabs=(
            AgentTab("Overview", "",
                     "your GSTR-1 draft, the HSN summary, and what's "
                     "missing an IRN"),
            AgentTab("Corrections", "corrections",
                     "GSTR-1A while the window's open, DRC-03 once it's locked"),
            AgentTab("Offset", "offset",
                     "the ITC utilisation hierarchy and the Rule 88C shield"),
            AgentTab("QRMP", "qrmp",
                     "quarterly filing, fixed-sum vs self-assessment, and IFF"),
        )),
}

SLUG_TO_AGENT = {r.slug: r for r in AGENT_ROUTES.values()}


def route_for(agent_id: str) -> Optional[AgentRoute]:
    return AGENT_ROUTES.get(agent_id)


# --- the business-process flows ---------------------------------------------
#
# A different cut through the same agents: not "what page are you on" but
# "which real process is this part of". Sell and Pay have no agent at all -
# they are Razorpay's own plumbing, already solved, which is exactly why the
# gap this platform exists for starts one stage later. A stage with no
# agent_id renders as a plain, honest placeholder rather than a claim.


@dataclass(frozen=True)
class FlowStage:
    label: str                      # "Sell", "Settle", "Refund/Dispute", ...
    agent_id: Optional[str] = None  # None = plumbing stage, no agent here
    note: str = ""                  # shown only when agent_id is None


@dataclass(frozen=True)
class Flow:
    key: str                        # "income" | "vendor" | "treasury" | "gst"
    label: str                      # "Income Management"
    stages: tuple[FlowStage, ...]


FLOWS: tuple[Flow, ...] = (
    Flow("income", "Income Management", (
        FlowStage("Sell", note="Razorpay collects the payment. Nothing to "
                   "audit yet - the gap starts at settlement."),
        FlowStage("Settle", agent_id="settlement_audit"),
        FlowStage("Payout", agent_id="payout_timing"),
        FlowStage("Refund/Dispute", agent_id="chargeback"),
        FlowStage("Reconcile", agent_id="three_way_recon"),
    )),
    Flow("vendor", "Vendor Management", (
        FlowStage("Purchase", agent_id="vendor_terms"),
        FlowStage("Pay", note="Razorpay pays the vendor. Not a money-moving "
                   "feature here - just where the relationship exits the "
                   "platform."),
        FlowStage("Claim GST Credit", agent_id="gst_itc"),
    )),
    Flow("treasury", "Treasury Management", (
        FlowStage("Forward", agent_id="cash_forecaster"),
    )),
    Flow("gst", "GST Management", (
        FlowStage("File", agent_id="gst_filing"),
    )),
)


# --- where the old URLs went ----------------------------------------------
#
# Kept as redirects rather than deleted. Bookmarks, muscle memory and the test
# suite all point at the old paths, and a refactor that breaks all three at
# once is a refactor nobody can verify.

MOVED: dict[str, str] = {
    # The manual tabs were removed; their URLs land on what replaced them.
    "/agents/input-credit/purchases": "/agents/input-credit",
    "/agents/input-credit/risk": "/agents/input-credit",
    # Setup was folded into the tab it configures: the only thing on it that
    # this agent needed was the GSP connection, and that belongs beside the
    # flow it enables rather than one tab away from it.
    "/agents/input-credit/setup": "/agents/input-credit/with-api",
    "/settlements": "/agents/settlement",
    "/settlements/{run_id}": "/agents/settlement/run/{run_id}",
    "/ask": "/agents/settlement/ask",
    "/purchases": "/agents/input-credit",
    "/suppliers": "/agents/input-credit/reconciliation",
    "/sources": "/data",
    "/simulator": "/data/simulator",
    "/zoho": "/data/zoho",
}
