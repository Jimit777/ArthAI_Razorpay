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
            # Supplier risk is the front door now. The manual Suppliers and
            # Purchases tabs are gone: the workflow is file-driven, and a form
            # asking a merchant to type invoices one at a time was the demo
            # scaffolding, not the product.
            AgentTab("Supplier risk", "",
                     "who your credit depends on, and how they have behaved"),
            AgentTab("Reconciliation", "reconciliation",
                     "this period's invoices against GSTR-2B"),
            AgentTab("Setup", "setup", "what this agent needs to run"),
        )),
}

SLUG_TO_AGENT = {r.slug: r for r in AGENT_ROUTES.values()}


def route_for(agent_id: str) -> Optional[AgentRoute]:
    return AGENT_ROUTES.get(agent_id)


# --- where the old URLs went ----------------------------------------------
#
# Kept as redirects rather than deleted. Bookmarks, muscle memory and the test
# suite all point at the old paths, and a refactor that breaks all three at
# once is a refactor nobody can verify.

MOVED: dict[str, str] = {
    # The manual tabs were removed; their URLs land on what replaced them.
    "/agents/input-credit/purchases": "/agents/input-credit",
    "/agents/input-credit/risk": "/agents/input-credit",
    "/settlements": "/agents/settlement",
    "/settlements/{run_id}": "/agents/settlement/run/{run_id}",
    "/ask": "/agents/settlement/ask",
    "/purchases": "/agents/input-credit",
    "/suppliers": "/agents/input-credit/reconciliation",
    "/sources": "/data",
    "/simulator": "/data/simulator",
    "/zoho": "/data/zoho",
}
