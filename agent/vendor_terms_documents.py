"""
The credit note request - one document, drafted from one supplier's own
overbilled lines.

## Why the agent writes the argument and Python writes the facts

Same split as agent/vendor_documents.py's two letters. The model is good at
the paragraph that makes a case and bad at being trusted with a rupee
figure, so every item, quantity, unit price, contracted price and total in
this document is assembled here from the supplier's own data. The agent
supplies the reasoning around them.

If the model is unavailable the document still goes out: the facts are the
useful part, and a merchant chasing a credit note needs the item list far
more than a well-turned sentence.

Kept as its own file rather than added to agent/vendor_documents.py (which
belongs to the ITC reconciler, a different agent even though both are
nominally "vendor-facing") - same separation this codebase already keeps
between agent/gst_filing_documents.py and agent/vendor_documents.py.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from engine.vendor_terms import rules

MODEL = "claude-opus-5"
DEFAULT_EFFORT = "medium"
MAX_TOKENS = 1_500


@dataclass
class Document:
    kind: str
    title: str
    body: str
    supplier_name: str
    gstin: str
    items: list[dict] = field(default_factory=list)
    amount: int = 0
    written_by: str = "template"        # "agent" when the model wrote the case
    error: Optional[str] = None
    created_at: int = field(default_factory=lambda: int(time.time()))


def _wrap(text: str, width: int = 78) -> str:
    """Formal letters are read in fixed-width mail clients as often as not."""
    import textwrap

    return "\n".join(textwrap.wrap(text, width=width))


def _item_table(items: list[dict]) -> str:
    """The facts, laid out. Never written by a model."""
    lines = ["    Item                      Qty   Billed      Contracted   Over"]
    for item in items:
        lines.append(
            f"    {str(item.get('description', ''))[:24]:<26}"
            f"{item.get('quantity', 0):>5g} "
            f"{rules.rupees(item.get('unit_price_paise', 0)):>12} "
            f"{rules.rupees(item.get('contracted_unit_price_paise', 0)):>12} "
            f"{rules.rupees(item.get('money_at_stake_paise', 0)):>10}")
    total = sum(i.get("money_at_stake_paise", 0) for i in items)
    lines.append(f"    {'':<26}{'':<43}{'-' * 12}")
    lines.append(f"    {'Total credit requested':<69}{rules.rupees(total)}")
    return "\n".join(lines)


def credit_note_request(group: dict, case: str = "") -> Document:
    """
    To the supplier. Formal, specific, and quantified - every line item
    priced above what was agreed, laid out with the contracted price beside
    the billed price so the gap is undeniable.
    """
    items = group.get("items") or []
    amount = sum(i.get("money_at_stake_paise", 0) for i in items)

    argument = case or (
        f"On review of your invoices to us, {len(items)} line item"
        f"{'' if len(items) == 1 else 's'} "
        f"{'was' if len(items) == 1 else 'were'} billed above the unit "
        f"price we agreed. We set out the detail below.")

    body = f"""To: {group.get('supplier_name', '')}
GSTIN: {group.get('gstin', '')}

Subject: Request for a credit note - invoiced price above agreed terms

Dear Sir or Madam,

{argument}

{_item_table(items)}

We would be grateful if you could issue a credit note for the amount above,
or confirm the correction on your next invoice to us. We value our ongoing
relationship and raise this so our records - and yours - reflect the terms
we actually agreed.

Please let us know if you have any questions about the items listed above.

Yours faithfully,
"""
    return Document(
        kind="credit_note_request",
        title=f"Credit note request to {group.get('supplier_name', 'supplier')}",
        body=body, supplier_name=group.get("supplier_name", ""),
        gstin=group.get("gstin", ""), items=items, amount=amount,
        written_by="agent" if case else "template")


ARGUMENT_PROMPT = """\
You are drafting one paragraph inside a formal document an Indian business is
sending to a supplier. Everything else in the document - the item list, the
amounts, the totals - is already written and is not yours to touch.

Write the opening paragraph that introduces the request: what the review
found, in the register of a formal letter rather than a summary.

Rules:
  - Do no arithmetic. Every figure you need is in the evidence, already
    computed. Quote them exactly or not at all.
  - Two to four sentences. This sits inside a longer document.
  - No greeting, no sign-off, no heading. The paragraph only.
  - State facts, plainly. This is a request to a business relationship worth
    keeping, not an accusation.
"""


def write_case(group: dict, client=None, model: str = MODEL,
               effort: str = DEFAULT_EFFORT) -> tuple:
    """
    Ask the agent for the opening paragraph.

    Returns (paragraph, error). On any failure the caller falls back to the
    assembled version - the facts are the useful part.
    """
    import anthropic

    items = group.get("items") or []
    lines = [f"  {i.get('description')}: billed "
            f"{rules.rupees(i.get('unit_price_paise', 0))}/unit, "
            f"contracted {rules.rupees(i.get('contracted_unit_price_paise', 0))}"
            f"/unit, {rules.rupees(i.get('money_at_stake_paise', 0))} over "
            f"({i.get('quantity', 0):g} units)" for i in items]
    amount = sum(i.get("money_at_stake_paise", 0) for i in items)

    evidence = "\n".join([
        f"SUPPLIER  {group.get('supplier_name')} ({group.get('gstin')})",
        f"  overbilled line items    {len(items)}",
        f"  total amount at stake    {rules.rupees(amount)}",
        "",
        "ITEMS:",
        *lines,
    ])

    try:
        client = client or anthropic.Anthropic()
        response = client.messages.create(
            model=model, max_tokens=MAX_TOKENS,
            system=ARGUMENT_PROMPT,
            messages=[{"role": "user", "content": evidence}])
    except Exception as exc:                                # noqa: BLE001
        return "", f"{type(exc).__name__}: {exc}"

    parts = [b.text for b in response.content if getattr(b, "type", "") == "text"]
    text = " ".join(parts).strip()
    if not text:
        return "", "the model returned nothing"

    from agent.risk_agent import unverified_figures

    invented = unverified_figures(text, evidence)
    if invented:
        return "", f"the draft contained figures from nowhere: {invented}"
    return text, None
