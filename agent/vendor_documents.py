"""
Two documents a merchant can send, drafted from one supplier's own numbers.

## Why the agent writes the argument and Python writes the facts

Same split as the settlement dispute letters. The model is good at the
paragraph that makes a case and bad at being trusted with a rupee figure, so
every invoice number, date, amount and total in these documents is assembled
here from the supplier's data. The agent supplies the reasoning around them.

If the model is unavailable the document still goes out: the facts are the
useful part, and a merchant chasing a defaulting supplier needs the invoice
list far more than they need a well-turned sentence.

## The two documents, and why they are opposites

    Vendor notice     to the SUPPLIER. "You reported these sales and did not
                      pay the tax. Under s.16(2)(c) our credit does not exist
                      until you do, so we are holding this amount."

    DRC-01C defence   to the DEPARTMENT. "Our claim exceeds GSTR-2B because of
                      these specific invoices, here is what we did about it,
                      and Circular 183/15/2022 covers exactly this mismatch."

One is leverage, the other is a reply to a notice with a seven-day clock on it.
A merchant in trouble usually needs both, in that order.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from engine.gst import rules

MODEL = "claude-opus-5"
DEFAULT_EFFORT = "medium"
MAX_TOKENS = 2_000

# The circular that exists precisely for this argument: it tells officers how
# to deal with a GSTR-3B/2A mismatch for the years when 2B was not yet
# authoritative, and it is the first thing a competent reply cites.
CIRCULAR_183 = ("Circular No. 183/15/2022-GST dated 27 December 2022 "
                "(difference in ITC availed in GSTR-3B against GSTR-2A)")
CIRCULAR_193 = ("Circular No. 193/05/2023-GST dated 17 July 2023 "
                "(the same treatment extended to 01.04.2019 - 31.12.2021)")
RULE_88D = "Rule 88D read with Form GST DRC-01C"


@dataclass
class Document:
    kind: str
    title: str
    body: str
    supplier_name: str
    gstin: str
    invoices: list[dict] = field(default_factory=list)
    amount: int = 0
    written_by: str = "template"        # "agent" when the model wrote the case
    error: Optional[str] = None
    created_at: int = field(default_factory=lambda: int(time.time()))


def _wrap(text: str, width: int = 78) -> str:
    """Formal letters are read in fixed-width mail clients as often as not."""
    import textwrap

    return "\n".join(textwrap.wrap(text, width=width))


def _invoice_table(invoices: list[dict]) -> str:
    """The facts, laid out. Never written by a model."""
    lines = ["    Invoice          Date          Tax"]
    for invoice in invoices:
        lines.append(
            f"    {str(invoice.get('invoice_number', ''))[:14]:<16}"
            f"{str(invoice.get('invoice_date', ''))[:10]:<14}"
            f"{rules.rupees(invoice.get('total_tax', 0))}")
    total = sum(i.get("total_tax", 0) for i in invoices)
    lines.append(f"    {'':<30}{'-' * 16}")
    lines.append(f"    {'Total':<30}{rules.rupees(total)}")
    return "\n".join(lines)


# The closing paragraph depends on what the merchant has actually decided.
# A notice that says "we are holding this pending your filing" to a supplier
# they have stopped buying from is the wrong letter: it offers leverage that
# is no longer on the table and reads as a bluff.
CLOSING = {
    "hold_payment": (
        "We would prefer to release this amount promptly and continue our "
        "commercial relationship on the existing terms. Please treat this as "
        "time-sensitive: credit not supported by your filing before the "
        "deadline under Section 16(4) is lost to us permanently and will be "
        "recovered from amounts otherwise payable to you."),
    "stop_buying": (
        "In view of the above we have suspended further orders with immediate "
        "effect, and the amount stated will be recovered from sums otherwise "
        "payable to you should the returns remain unfiled by the deadline "
        "under Section 16(4). We are willing to review this decision on "
        "production of the filed returns and evidence of payment of the tax."),
    "get_it_in_writing": (
        "Before we place further orders we require a written undertaking as "
        "to the periods for which filing is outstanding and the date by which "
        "it will be completed. Credit not supported by your filing before the "
        "deadline under Section 16(4) is lost to us permanently."),
}


def vendor_notice(supplier: dict, case: str = "") -> Document:
    """
    To the supplier. Formal, specific, and quantified.

    The hold amount is the credit their own record puts in doubt, not the whole
    invoice - a merchant who withholds more than the tax at stake has changed
    a compliance conversation into a commercial dispute.
    """
    invoices = supplier.get("invoices") or []
    hold = supplier.get("at_risk", 0)
    prof = supplier.get("profile") or {}

    action = supplier.get("action", "hold_payment")
    closing = _wrap(CLOSING.get(action, CLOSING["hold_payment"]))
    subject = ("Suspension of further orders and input tax credit held "
               "pending your GSTR-3B filing" if action == "stop_buying"
               else "Input tax credit held pending your GSTR-3B filing")

    argument = case or (
        f"Our records show that against {prof.get('gstr1_filed', 0)} periods in "
        f"which you reported outward supplies, GSTR-3B was filed for "
        f"{prof.get('gstr3b_filed', 0)}. On "
        f"{prof.get('sold_but_did_not_pay', 0)} occasions the supply was "
        f"reported and the corresponding tax was not paid.")

    body = f"""To: {supplier.get('supplier_name', '')}
GSTIN: {supplier.get('gstin', '')}

Subject: {subject}

Dear Sir or Madam,

We are writing regarding the following invoices raised on us, on which we have
been charged GST:

{_invoice_table(invoices)}

{argument}

Under Section 16(2)(c) of the CGST Act, 2017, input tax credit is available to
a recipient only where the tax charged has actually been paid to the
Government. The Supreme Court has upheld this provision, and the burden of
establishing that the tax was paid rests on us as the buyer.

Accordingly, we are holding {rules.rupees(hold)} - being the tax component
whose payment we are presently unable to establish - until you provide either
of the following:

  1. Evidence of GSTR-3B having been filed for the relevant periods, together
     with the challan or ledger reference showing payment of the tax; or
  2. A written confirmation of the periods for which filing is outstanding and
     the date by which it will be completed.

{closing}

Yours faithfully,
"""
    return Document(
        kind="vendor_notice",
        title=f"Notice to {supplier.get('supplier_name', 'supplier')}",
        body=body, supplier_name=supplier.get("supplier_name", ""),
        gstin=supplier.get("gstin", ""), invoices=invoices, amount=hold,
        written_by="agent" if case else "template")


def drc01c_defence(supplier: dict, case: str = "") -> Document:
    """
    To the department, answering the automatic mismatch notice.

    Rule 88D gives seven days, which is why this exists as a button rather than
    as advice to go and see an accountant.
    """
    invoices = supplier.get("invoices") or []
    amount = supplier.get("at_risk", 0)
    prof = supplier.get("profile") or {}

    argument = case or (
        f"The difference arises entirely from supplies received from "
        f"{supplier.get('supplier_name', 'the supplier')} "
        f"({supplier.get('gstin', '')}), whose GSTR-1 filings report these "
        f"invoices but whose GSTR-3B for the corresponding periods has not "
        f"been filed. The supply is genuine, the invoices are held on record, "
        f"and payment to the supplier has been made through banking channels.")

    body = f"""Reply to intimation under {RULE_88D}

Subject: Explanation of the difference between input tax credit availed in
         GSTR-3B and that available in GSTR-2B

We refer to the intimation issued in Form GST DRC-01C and submit as follows.

1. The invoices to which the difference relates are:

{_invoice_table(invoices)}

2. {argument}

3. We have satisfied every condition under Section 16(2) that is within our
   control: we hold a tax invoice, we have received the goods or services, and
   we have paid the supplier including the tax charged. The only condition in
   question is Section 16(2)(c), which depends on an act of the supplier and
   not of the recipient.

4. We rely on {CIRCULAR_183}, which prescribes the procedure to be followed
   where credit availed in GSTR-3B exceeds that reflected against the
   recipient, and directs that the recipient be asked to produce evidence of
   the genuineness of the transaction rather than the credit being denied
   mechanically. We further rely on {CIRCULAR_193}, which extends the same
   treatment to the earlier period.

5. We have issued a written notice to the supplier calling upon them to file
   the outstanding returns and to furnish proof of payment of tax, and we are
   withholding {rules.rupees(amount)} from amounts payable to them pending
   compliance.

6. We accordingly request that the difference of {rules.rupees(amount)} be
   accepted as explained. Documentary evidence - invoices, proof of receipt of
   supply, bank statements evidencing payment to the supplier, and a copy of
   the notice issued to them - is available and will be produced on demand.

Yours faithfully,
"""
    return Document(
        kind="drc01c_defence",
        title=f"DRC-01C reply concerning {supplier.get('supplier_name', '')}",
        body=body, supplier_name=supplier.get("supplier_name", ""),
        gstin=supplier.get("gstin", ""), invoices=invoices, amount=amount,
        written_by="agent" if case else "template")


ARGUMENT_PROMPT = """\
You are drafting one paragraph inside a formal document an Indian business is
sending. Everything else in the document - the invoice list, the amounts, the
totals, the statutory citations - is already written and is not yours to touch.

Write the paragraph that states the case: what this supplier's filing record
actually shows, in the register of a formal letter rather than a summary.

Rules:
  - Do no arithmetic. Every figure you need is in the evidence, already
    computed. Quote them exactly or not at all.
  - Two to four sentences. This sits inside a longer document.
  - No greeting, no sign-off, no heading. The paragraph only.
  - State facts. A formal notice that sounds aggrieved is easier to ignore
    than one that reads like a record.
"""


def write_case(supplier: dict, kind: str, client=None,
               model: str = MODEL, effort: str = DEFAULT_EFFORT) -> tuple:
    """
    Ask the agent for the argument paragraph.

    Returns (paragraph, error). On any failure the caller falls back to the
    assembled version - the facts are the useful part, and a merchant chasing a
    defaulter needs the invoice list far more than a well-turned sentence.
    """
    import anthropic

    prof = supplier.get("profile") or {}
    audience = ("the supplier, to make them file"
                if kind == "vendor_notice"
                else "the tax department, to defend a claim they have queried")

    evidence = "\n".join([
        f"SUPPLIER  {supplier.get('supplier_name')} ({supplier.get('gstin')})",
        f"  audience for this paragraph: {audience}",
        f"  periods of history        {prof.get('periods', 0)}",
        f"  reported sales in         {prof.get('gstr1_filed', 0)}",
        f"  paid the tax in           {prof.get('gstr3b_filed', 0)}"
        f"  ({prof.get('compliance_pct', 0)}% of what they reported)",
        f"  reported and did not pay  {prof.get('sold_but_did_not_pay', 0)}"
        f"  ({prof.get('default_rate_pct', 0)}%)",
        f"  registration              {prof.get('registration_status')}",
        f"  invoices this period      {len(supplier.get('invoices') or [])}",
        f"  amount being held         {rules.rupees(supplier.get('at_risk', 0))}",
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

    # Same check as everywhere else: it may not introduce a figure.
    from agent.risk_agent import unverified_figures

    invented = unverified_figures(text, evidence)
    if invented:
        return "", f"the draft contained figures from nowhere: {invented}"
    return text, None
