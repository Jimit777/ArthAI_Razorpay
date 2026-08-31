"""
The Rule 88C response, drafted from one period's own numbers.

## Why the agent writes the argument and Python writes the facts

Same split as agent/vendor_documents.py: the GSTR-1 liability, what GSTR-3B
paid, the gap, the Rule 88C threshold and the breach amount are all computed
by engine/gst_filing/offset.py before the model ever sees them. The agent
supplies the 2-4 sentence paragraph that reads like a formal reply; it is
never handed a figure to compute itself.

If the model is unavailable the document still goes out unfinished but
truthful: the facts and the citations are the useful part of a document with
a clock on it, and a merchant answering a DRC-01B needs the numbers far more
than a well-turned sentence.

## What is cited, and why it stops where it stops

No CBIC *circular* specifically interpreting Rule 88C/DRC-01B was found -
unlike agent/vendor_documents.py's Circular 183/193, a real, findable
circular for that different mismatch. What was found and is cited instead:
CBIC Instruction No. 01/2022-GST (7 January 2022) - "Guidelines for
recovery proceedings under section 79" - which predates Rule 88C itself
(inserted December 2022) but establishes the exact underlying principle
Rule 88C's DRC-01B process now runs automatically: a taxpayer must be
given the chance to explain a GSTR-1/GSTR-3B difference before recovery
action is taken. It is cited precisely as an Instruction, not relabelled
as a circular - CLAUDE.md section 16: a wrong citation is worse than none.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from engine.gst_filing import rules

MODEL = "claude-opus-5"
DEFAULT_EFFORT = "medium"
MAX_TOKENS = 1_200

SOURCE_SECTION_73 = "CGST Act s.73 - determination absent fraud or wilful misstatement"
SOURCE_INSTRUCTION_01_2022 = (
    "CBIC Instruction No. 01/2022-GST dated 7 January 2022 - guidelines for "
    "recovery proceedings under s.79, establishing that a registered person "
    "must be given the opportunity to explain a GSTR-1/GSTR-3B difference "
    "before recovery action is initiated")


@dataclass
class Document:
    kind: str
    title: str
    body: str
    period: str
    amount: int = 0
    written_by: str = "template"        # "agent" when the model wrote the case
    error: Optional[str] = None
    created_at: int = field(default_factory=lambda: int(time.time()))


def drc01b_response(finding, case: str = "") -> Document:
    """
    Reply to the automated Rule 88C intimation (Form GST DRC-01B) for one
    period. `finding` is an engine.gst_filing.offset.OffsetFinding whose
    rule_88c_breach is True - callers only reach for this after a breach
    was actually found.
    """
    argument = case or (
        f"The difference of {rules.rupees(finding.breach_amount)} over the "
        f"Rule 88C threshold arises between what GSTR-1 declared as output "
        f"tax liability for {finding.period} and what GSTR-3B paid for the "
        f"same period. The supplies underlying the GSTR-1 figure are on "
        f"record and the shortfall is being addressed through a voluntary "
        f"DRC-03 payment.")

    body = f"""Reply to intimation under CGST Rule 88C (Form GST DRC-01B)

Subject: Explanation of the difference between liability declared in GSTR-1
         and tax paid in GSTR-3B for {finding.period}

We refer to the intimation issued in Form GST DRC-01B and submit as follows.

1. {finding.reasoning}

2. {argument}

3. This is a self-assessed shortfall with no allegation of fraud or wilful
   misstatement; our position is accordingly taken under {SOURCE_SECTION_73},
   not under Section 74.

4. We rely on {finding.rule_cited}, and on {rules.SOURCE_INTEREST_NORMAL}
   for the interest computed on the amount now being paid voluntarily. We
   further rely on {SOURCE_INSTRUCTION_01_2022}.

5. We accordingly request that the difference of
   {rules.rupees(finding.breach_amount)} over the Rule 88C threshold be
   accepted as explained, and confirm that the underlying shortfall is being
   discharged through Form GST DRC-03.

Yours faithfully,
"""
    return Document(
        kind="drc01b_response", title=f"DRC-01B reply for {finding.period}",
        body=body, period=finding.period, amount=finding.breach_amount,
        written_by="agent" if case else "template")


ARGUMENT_PROMPT = """\
You are drafting one paragraph inside a formal reply an Indian business is
sending to the GST department, answering an automated Rule 88C intimation
(Form GST DRC-01B). Everything else in the document - the figures, the
statutory citations, the closing request - is already written and is not
yours to touch.

Write the paragraph that states the case: what this period's GSTR-1/GSTR-3B
gap actually reflects, in the register of a formal letter rather than a
summary.

Rules:
  - Do no arithmetic. Every figure you need is in the evidence, already
    computed. Quote them exactly or not at all.
  - Two to four sentences. This sits inside a longer document.
  - No greeting, no sign-off, no heading. The paragraph only.
  - Never suggest fraud, wilful misstatement or suppression - this is a
    bona fide shortfall being paid voluntarily, and the document already
    says so under Section 73.
  - State facts. A formal reply that sounds aggrieved is easier to ignore
    than one that reads like a record.
"""


def write_case(finding, client=None, model: str = MODEL,
               effort: str = DEFAULT_EFFORT) -> tuple:
    """
    Ask the agent for the argument paragraph.

    Returns (paragraph, error). On any failure the caller falls back to the
    assembled version - the numbers and citations are the useful part of a
    reply with a clock on it.
    """
    import anthropic

    evidence = "\n".join([
        f"PERIOD                 {finding.period}",
        f"gap over threshold     {rules.rupees(finding.breach_amount)}",
        f"rule cited             {finding.rule_cited}",
        f"engine's own reading   {finding.reasoning}",
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

    from agent.gst_correction_classifier import unverified_figures

    invented = unverified_figures(text, evidence)
    if invented:
        return "", f"the draft contained figures from nowhere: {invented}"
    return text, None
