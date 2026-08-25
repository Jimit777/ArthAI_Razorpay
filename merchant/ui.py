"""
Shared components, so a status badge means the same thing on every page.

## What this replaces

Badges were being assembled inline wherever they were needed, which is how
CLEAN ended up neutral on one page and green on another, and how "not checked"
became a pill on one screen and muted text on the next. A reader learning the
vocabulary of a product should only have to learn it once.

Everything here returns a string of HTML. There is no framework, no build step
and no client-side state - a page is a function of the database at the moment
it was requested, which is the whole reason this application can be understood
by reading it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from merchant.views import esc

# --- status vocabulary -----------------------------------------------------
#
# Two axes, deliberately separate. TONE is how alarming something is; it never
# encodes what kind of thing it is. A settlement finding and a supplier
# registration can both be "bad", and the reader should recognise that at a
# glance without having to know which page they are on.

TONE_NEUTRAL = ""
TONE_GOOD = "good"
TONE_WARN = "warn"
TONE_BAD = "danger"
TONE_BRAND = "brand"

# Every exception code either product raises, and how alarmed to be about it.
# One table rather than per-page guesses.
CODE_TONE: dict[str, str] = {
    # settlement
    "CLEAN": TONE_GOOD,
    "ROUNDING": TONE_NEUTRAL,
    "REFUND_MDR_RETAINED": TONE_NEUTRAL,
    "PERIOD_BOUNDARY": TONE_NEUTRAL,
    "ZERO_MDR_VIOLATION": TONE_BAD,
    "INSTRUMENT_MISLABEL": TONE_BAD,
    "RATE_MISMATCH": TONE_BAD,
    "MISSING_FROM_SETTLEMENT": TONE_BAD,
    "GST_MISMATCH": TONE_WARN,
    "TDS_CODE_MISMATCH": TONE_WARN,
    "UNEXPLAINED": TONE_WARN,
    # input credit
    "CLAIM_CLEAN": TONE_GOOD,
    "SUPPLIER_NOT_FILED": TONE_BAD,
    "GSTIN_MISMATCH": TONE_BAD,
    "AMOUNT_MISMATCH": TONE_WARN,
    "SUPPLIER_LATE_FILED": TONE_NEUTRAL,
    "BLOCKED_CREDIT": TONE_WARN,
    "TIME_BARRED": TONE_WARN,
    "RULE_37_REVERSAL": TONE_WARN,
    "DUPLICATE_CLAIM": TONE_WARN,
    "NOT_IN_BOOKS": TONE_NEUTRAL,
}


def badge(text: str, tone: str = TONE_NEUTRAL, title: str = "") -> str:
    attrs = f' title="{esc(title)}"' if title else ""
    return f'<span class="pill {tone}"{attrs}>{esc(text)}</span>'


def code_badge(code: str, label: str = "") -> str:
    """
    An exception code, in the reader's words with the machine's in the tooltip.

    Showing INSTRUMENT_MISLABEL to a merchant is showing them our enum. The
    code still travels, because a support conversation needs it - it just does
    not lead.
    """
    return badge(label or code.replace("_", " ").capitalize(),
                 CODE_TONE.get(code, TONE_NEUTRAL), title=code)


# --- how an agent is doing -------------------------------------------------

STATE_ACTIVE = "active"
STATE_DEMO = "demo"
STATE_SETUP = "setup"
STATE_OFF = "off"
STATE_SOON = "soon"

STATE_LABEL = {
    STATE_ACTIVE: "Live data",
    STATE_DEMO: "Demo data",
    STATE_SETUP: "Setup needed",
    STATE_OFF: "Switched off",
    STATE_SOON: "Coming soon",
}
STATE_TONE = {
    STATE_ACTIVE: TONE_GOOD,
    STATE_DEMO: TONE_WARN,
    STATE_SETUP: TONE_BRAND,
    STATE_OFF: TONE_NEUTRAL,
    STATE_SOON: TONE_NEUTRAL,
}


def state_badge(state: str) -> str:
    return badge(STATE_LABEL.get(state, state), STATE_TONE.get(state, ""))


# --- containers ------------------------------------------------------------

def card(body: str, *, title: str = "", aside: str = "", flush: bool = False,
         tone: str = "", footnote: str = "") -> str:
    """
    The one card. Every panel in the product is this, so they line up.

    `tone` draws a coloured left edge for something that needs attention -
    used sparingly, because a page where everything is urgent has nothing
    urgent on it.
    """
    edge = f"border-left:3px solid var(--{tone});" if tone else ""
    head = ""
    if title or aside:
        head = (f'<div class="card-head"><h2>{esc(title)}</h2>'
                f'{f"<span class=\'sub\'>{aside}</span>" if aside else ""}</div>')
    foot = ""
    if footnote:
        foot = (f'<div style="padding:11px 16px;border-top:1px solid '
                f'var(--line-2);color:var(--muted);font-size:11.5px">'
                f'{footnote}</div>')
    classes = "card flush" if flush else "card"
    inner = body if flush else f'<div style="padding:0">{body}</div>'
    return (f'<div class="{classes}" style="{edge}padding:'
            f'{"0" if flush else "16px 18px"}">{head}{inner}{foot}</div>')


def metric_bar(metrics: list[tuple[str, str]]) -> str:
    """
    The numbers that matter, before any detail.

    A dashboard that opens with a table makes the reader do the summarising.
    """
    cells = "".join(
        f'<div class="stat"><b>{value}</b><span>{esc(label)}</span></div>'
        for value, label in metrics)
    return (f'<div class="card" style="padding:0;overflow:hidden">'
            f'<div class="stats">{cells}</div></div>')


def empty(headline: str, hint: str = "", action: str = "", span: int = 1) -> str:
    """An empty state that says what to do, not just that there is nothing."""
    return (f'<tr><td colspan="{span}" class="empty">'
            f'<div style="font-weight:560;color:var(--ink);margin-bottom:4px">'
            f'{esc(headline)}</div>{esc(hint)}'
            f'{f"<div style=\'margin-top:11px\'>{action}</div>" if action else ""}'
            f'</td></tr>')


def blank_slate(headline: str, hint: str = "", action: str = "") -> str:
    """The same idea outside a table."""
    return (f'<div class="card"><div class="empty" style="padding:36px 18px">'
            f'<div style="font-weight:560;color:var(--ink);margin-bottom:5px;'
            f'font-size:14px">{esc(headline)}</div>'
            f'<div style="max-width:46ch;margin:0 auto">{esc(hint)}</div>'
            f'{f"<div style=\'margin-top:15px\'>{action}</div>" if action else ""}'
            f'</div></div>')


def tabs(items: list[tuple[str, str, bool]]) -> str:
    """(label, href, is_current) - the row of tabs inside an agent workspace."""
    links = "".join(
        f'<a class="tab {"on" if current else ""}" href="{esc(href)}">'
        f'{esc(label)}</a>' for label, href, current in items)
    return f'<div class="tabs">{links}</div>'


def agent_header(name: str, tagline: str, state: str, tab_row: str = "",
                 action: str = "") -> str:
    """The top of an agent workspace: who it is, how it is doing, where to go."""
    return f"""
<div class="agent-head">
  <div class="agent-title">
    <div>
      <h1>{esc(name)}</h1>
      <p class="sub" style="margin:3px 0 0">{esc(tagline)}</p>
    </div>
    <div class="agent-meta">{state_badge(state)}{action}</div>
  </div>
  {tab_row}
</div>"""


@dataclass
class AgentCardData:
    name: str
    tagline: str
    state: str
    href: str
    metrics: list[tuple[str, str]]
    cta: str = "Open workspace"
    why_unbuilt: str = ""
    control: str = ""               # e.g. the on/off form


def agent_card(data: AgentCardData) -> str:
    """
    One agent on the hub.

    A planned agent gets the same card, greyed, with the reason the gap exists
    rather than a fake metric. A convincing mock of a working reconciler is not
    a roadmap.
    """
    live = data.state != STATE_SOON
    figures = "".join(
        f'<div class="mini"><b>{v}</b><span>{esc(l)}</span></div>'
        for v, l in data.metrics)
    if live:
        foot = (f'<a class="btn ghost small" href="{esc(data.href)}">'
                f'{esc(data.cta)}</a>')
    else:
        foot = (f'<span class="sub" style="font-size:11.5px">'
                f'{esc(data.why_unbuilt)}</span>')
    return f"""
<div class="card agent-card {"" if live else "muted"}">
  <div class="agent-card-head">
    <div>
      <div class="agent-card-name">{esc(data.name)}</div>
      <p class="sub" style="margin:3px 0 0">{esc(data.tagline)}</p>
    </div>
    {state_badge(data.state)}
  </div>
  {f'<div class="minis">{figures}</div>' if figures else ''}
  <div class="agent-card-foot" style="display:flex;gap:8px;
    align-items:center">{foot}
    <span style="margin-left:auto">{data.control}</span></div>
</div>"""


def grid(cards: list[str], min_width: int = 300) -> str:
    return (f'<div style="display:grid;gap:13px;grid-template-columns:'
            f'repeat(auto-fill,minmax({min_width}px,1fr))">'
            f'{"".join(cards)}</div>')
