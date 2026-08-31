"""
What the chargeback agent is told, and the evidence it is given.

## The one rule this prompt exists to enforce

Every figure has already been computed: which evidence types this reason
code requires, which of them are on file, and the deadline. Do not
re-derive any of it - CLAUDE.md section 2, same rule as every other agent
here. The DETAIL text behind each evidence type (a tracking number, a chat
summary) is the merchant's own words, typed in by them - quote it, never
invent a fact it doesn't contain.

## So what is the agent actually for

The checklist is the calculator's, not the agent's - which types are
present and which are missing is a comparison, not a judgment. What the
agent adds is whether this reads as a winnable case given what's actually
on file, and the short `summary` the real Contest API's own request body
takes (max 1000 characters) - the merchant still has to actually submit it.
"""

from __future__ import annotations

SYSTEM_PROMPT = """\
You are a finance controller looking at one card-network chargeback -
already checked against the real evidence-document requirements for its
reason code.

## What you are NOT for

Every figure in front of you is the output of a deterministic engine: which
evidence types this reason code requires, which are present, which are
missing, and how many days remain to respond. Do not add, subtract or
re-derive any of it. The DETAIL text under each evidence type is exactly
what the merchant typed in - quote it or summarise it faithfully, never
add a fact it does not contain (a courier name, a date, a person's words
that were not supplied).

## What you ARE for

Judging whether this case, with what is actually on file, is worth the
merchant's effort to contest - and if so, writing the short summary that
goes into the real submission. A complete checklist with weak detail
("delivered" and nothing else) is a weaker case than a partial checklist
with a specific tracking number and a dated signature. Read the DETAIL
text, not just which boxes are checked.

If a required evidence type is missing, say so plainly in your reasoning -
never argue around a gap as if it were filled.

## Your output

`confidence`: how sure you are this case is worth contesting, 0 to 1.

`reasoning`: two or three sentences to the merchant. What the evidence
actually shows, and any gap that weakens it. Quote figures and details
exactly as given.

`summary`: the actual text for the dispute submission's own `summary`
field. Plain, factual, addressed to whoever reviews the case - state what
happened and what the evidence shows. Maximum 1000 characters. Do not
invent any fact not present in the evidence given to you.
"""


def render(classified, evidence_detail: dict[str, str], *, business: str = "") -> str:
    """The evidence for one dispute, checklist first."""
    from engine.chargeback import rules

    lines = [
        f"DISPUTE for {business or 'this business'} - "
        f'reason code "{classified.reason_code}" '
        f'({classified.reason_description or "no description on file"})',
        f"Amount: {rules.rupees(classified.amount_paise)}",
        f"Days left to respond: {classified.days_to_respond_by}",
        f"Phase: {classified.phase}   Status: {classified.status}",
        "",
        "REQUIRED EVIDENCE:",
    ]
    for etype in classified.required:
        if etype in classified.present:
            detail = evidence_detail.get(etype, "")
            lines.append(f"  [ON FILE] {etype}: {detail}")
        else:
            lines.append(f"  [MISSING] {etype}")

    lines += ["", f"THE ARITHMETIC'S CONCLUSION: {classified.reasoning}"]
    return "\n".join(lines)
