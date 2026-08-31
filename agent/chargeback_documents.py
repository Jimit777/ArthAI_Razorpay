"""
The explanation letter - one document, drafted from one dispute's own
evidence checklist.

## Why this is its own file, not agent/vendor_documents.py

Same reasoning agent/vendor_terms_documents.py already gives for staying
separate from agent/vendor_documents.py: a different owning agent, even
though both produce formal letters from a Document dataclass.

## Why the agent writes the argument and Python writes the facts

Same split as every dispute-adjacent letter in this codebase. The model is
good at the paragraph that makes a case and bad at being trusted with a
figure, so every evidence type, its detail text, the amount and the
deadline in this document are assembled here from what the merchant
actually entered. The agent supplies the connecting paragraph.

If the model is unavailable the document still goes out: the checklist is
the useful part, and a merchant racing a representment deadline needs the
evidence list far more than a well-turned sentence.

## What this produces, matching the real API's own shape

The real `PATCH /disputes/{id}/contest` body takes a `summary` (short,
&le;1000 chars - see agent/chargeback_classifier.py, which writes that
field directly) and, separately, document IDs for an `explanation_letter`
evidence type. This module drafts the LETTER text a merchant would turn
into that document - the summary and the letter are two different fields
in the real API, and this build keeps them two different outputs for the
same reason.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from engine.chargeback import rules

MODEL = "claude-opus-5"
DEFAULT_EFFORT = "medium"
MAX_TOKENS = 1_500

EVIDENCE_LABEL: dict[str, str] = {
    "shipping_proof": "Proof of shipment/delivery",
    "billing_proof": "Proof of order/billing",
    "cancellation_proof": "Proof of cancellation",
    "customer_communication": "Customer communication",
    "proof_of_service": "Proof of service",
    "explanation_letter": "Explanation letter",
    "refund_confirmation": "Refund confirmation",
    "access_activity_log": "Access/activity log",
    "refund_cancellation_policy": "Refund/cancellation policy",
    "term_and_conditions": "Terms and conditions",
}


@dataclass
class Document:
    kind: str
    title: str
    body: str
    dispute_id: str
    reason_code: str
    amount: int = 0
    written_by: str = "template"        # "agent" when the model wrote the case
    error: Optional[str] = None
    created_at: int = field(default_factory=lambda: int(time.time()))


def _wrap(text: str, width: int = 78) -> str:
    """Formal letters are read in fixed-width mail clients as often as not."""
    import textwrap

    return "\n".join(textwrap.wrap(text, width=width))


def _checklist(required: list[str], present: list[str],
               evidence_detail: dict[str, str]) -> str:
    """The facts, laid out. Never written by a model."""
    lines = []
    for etype in required:
        label = EVIDENCE_LABEL.get(etype, etype)
        if etype in present:
            detail = evidence_detail.get(etype, "").strip() or "(on file)"
            lines.append(f"    [X] {label}: {detail}")
        else:
            lines.append(f"    [ ] {label}: not on file")
    return "\n".join(lines)


def explanation_letter(dispute: dict, case: str = "") -> Document:
    """
    A formal letter laying out the evidence behind one dispute - addressed
    to whoever reviews the representment (the card network, via Razorpay's
    own dispute process), not to Razorpay support the way
    agent/dispute.py's settlement letters are.
    """
    required = dispute.get("required") or []
    present = dispute.get("present") or []
    missing = dispute.get("missing") or []
    evidence_detail = dispute.get("evidence_detail") or {}
    amount = dispute.get("amount_paise", 0)

    argument = case or (
        f"Of the {len(required)} evidence type(s) required for reason code "
        f'"{dispute.get("reason_code", "")}", {len(present)} are on file. '
        + (f"The following remain outstanding: "
          f"{', '.join(EVIDENCE_LABEL.get(m, m) for m in missing)}."
          if missing else "The full requirement list is met."))

    gap_note = ""
    if missing:
        gap_note = _wrap(
            "The following evidence types were not available at the time "
            "this letter was prepared: "
            + ", ".join(EVIDENCE_LABEL.get(m, m) for m in missing) + ".")

    body = f"""Dispute reference: {dispute.get('dispute_id', '')}
Payment reference: {dispute.get('payment_id', '')}
Reason code: {dispute.get('reason_code', '')} - {dispute.get('reason_description', '')}
Amount in dispute: {rules.rupees(amount)}

Subject: Representment - evidence in response to the above dispute

To whom it may concern,

We are submitting the following evidence in response to the dispute raised
against the payment referenced above.

{argument}

Evidence on file:

{_checklist(required, present, evidence_detail)}

{gap_note}

We respectfully request that this evidence be considered in full before a
final determination is made.

Yours faithfully,
"""
    return Document(
        kind="explanation_letter",
        title=f"Explanation letter for {dispute.get('dispute_id', '')}",
        body=body, dispute_id=dispute.get("dispute_id", ""),
        reason_code=dispute.get("reason_code", ""), amount=amount,
        written_by="agent" if case else "template")


ARGUMENT_PROMPT = """\
You are drafting one paragraph inside a formal representment letter an
Indian merchant is sending in response to a card-network dispute.
Everything else in the document - the evidence checklist, the amounts, the
reference numbers - is already written and is not yours to touch.

Write the paragraph that states the case: what the evidence on file
actually shows, in the register of a formal letter rather than a summary.

Rules:
  - Do no arithmetic. Every figure you need is in the evidence, already
    computed. Quote them exactly or not at all.
  - Only describe evidence marked ON FILE. Never claim something is proven
    by evidence marked MISSING.
  - Two to four sentences. This sits inside a longer document.
  - No greeting, no sign-off, no heading. The paragraph only.
  - State facts, plainly. A letter reads as more credible than an appeal.
"""


def write_case(classified, evidence_detail: dict[str, str], client=None,
               model: str = MODEL, effort: str = DEFAULT_EFFORT) -> tuple:
    """
    Ask the agent for the argument paragraph.

    Returns (paragraph, error). On any failure the caller falls back to the
    assembled version - the checklist is the useful part, and a merchant
    racing a deadline needs it far more than a well-turned sentence.
    """
    import anthropic

    from agent.chargeback_prompt import render

    evidence = render(classified, evidence_detail)

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
