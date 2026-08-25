"""
Ask the auditor a question about your own books.

The classifier answers a question nobody asked: "what kind of discrepancy is
this record?" That is the right question for a batch and the wrong one for a
merchant sitting in front of their settlements at 9pm wondering why the payout
was short.

So: a question box. Same agent, same read-only tools, same rule that it may not
do arithmetic - and one additional rule that matters more here than anywhere
else in the product.

## Why this needed its own guardrail

A classification is constrained: the model picks from eleven codes and every
figure it quotes is checked against evidence we computed. A free-text answer has
neither constraint. Ask "how much did the gateway overcharge me this month?" and
a model with no discipline will happily add up some numbers and give you a
total - which is arithmetic, which is the one thing this system does not let a
model do.

The fix is the same one used everywhere else, applied harder: the totals are
computed in Python and put IN FRONT of the question, and the answer is checked
afterwards for figures that were not in the evidence. If it states a number we
did not give it, the answer is shown with that flagged rather than quietly
trusted.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Optional

from agent.classifier import MODEL, TOOL_NAMES, unverified_figures
from engine.expected_value import rupees
from engine.taxonomy import ACTION_FOR, DESCRIPTION, ExceptionCode

MAX_QUESTION = 500

SYSTEM = """\
You are a settlement auditor answering a merchant's question about their own
books. You are talking to the business owner, not to an engineer.

## The absolute rule

DO NOT DO ARITHMETIC. Not addition, not percentages, not "which is bigger", not
"that adds up to". Every figure you could need has already been computed and is
in the briefing below, formatted. Quote those figures exactly as they appear.

If the merchant asks for a number that is not in the briefing, say that you do
not have it and name what you would need. Do not derive it. A deterministic
engine produced every figure you have been given; it is unit-tested against RBI
circulars and cannot be wrong about a number. You can.

## What you know

Only what is in the briefing. It contains this business's real settlements,
findings and totals. If the answer is not there, say so plainly rather than
generalising from what a payment gateway usually does.

## How to answer

Two to five sentences, in plain language, addressed to the owner. No preamble,
no "great question", no bullet lists unless the answer is genuinely a list.

Name the rule and its source when a finding is involved - a merchant who is
going to argue with their gateway needs to know what they are arguing from.

If something is uncertain, say which part. If a finding was held for human
review, say so rather than presenting it as settled.

You cannot change anything. If asked to raise a dispute, dismiss a finding or
edit a rate, explain what the merchant should do in the app instead.
"""


@dataclass
class Answer:
    question: str
    text: str
    invented_figures: list[str] = field(default_factory=list)
    error: Optional[str] = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    latency_ms: int = 0
    asked_at: int = field(default_factory=lambda: int(time.time()))

    @property
    def trustworthy(self) -> bool:
        return not self.invented_figures and not self.error


def build_briefing(led, limit_settlements: int = 6) -> str:
    """
    Everything the agent is allowed to know, with every total precomputed.

    This is where the arithmetic ban is actually enforced. If a merchant asks
    "how much am I owed?", the answer has to already be sitting in the briefing
    as a formatted string, because the alternative is the model adding numbers
    up - and a model that adds up its own evidence will eventually add it up
    wrong in front of someone about to email their gateway.
    """
    business = led.businesses.get(led.business_id)
    card = led.rate_card()
    runs = led.settlements()[:limit_settlements]

    lines = [
        f"# Briefing for {business['name']}",
        "",
        "## Their contracted rates",
        "",
    ]
    for key, spec in card["instruments"].items():
        total_bps = spec["network_mdr_bps"] + spec["platform_fee_bps"]
        capped = spec.get("network_mdr_cap_bps") is not None
        lines.append(
            f"  {spec['label']}: {total_bps / 100:.2f}% "
            f"(network {spec['network_mdr_bps'] / 100:.2f}%, platform "
            f"{spec['platform_fee_bps'] / 100:.2f}%)"
            + ("  [capped by regulation]" if capped else "")
            + f"  source: {spec['network_mdr_source']}")
    lines += [
        f"  GST: {card['gst_rate_bps'] / 100:.0f}% of the fee, never of the sale.",
        "",
    ]

    if not runs:
        lines += ["## Settlements", "",
                  "  None yet. Nothing has been settled or audited."]
        return "\n".join(lines)

    grand = {"records": 0, "recoverable": 0, "queued": 0, "findings": 0}
    lines += ["## Settlements and what was found", ""]

    for run in runs:
        totals = led.store.totals(run["run_id"])
        findings = led.store.findings(run["run_id"])
        grand["records"] += totals["n"]
        grand["recoverable"] += totals["recoverable_paise"]
        grand["queued"] += totals["queued"]

        lines.append(f"### Settlement {run['run_id']}"
                     f"  ({run['n_records']} payments)")
        if not findings:
            lines += ["  Not audited yet.", ""]
            continue

        lines.append(f"  {totals['n']} records audited, "
                     f"{totals['by_calculator']} settled by the rate card alone, "
                     f"{rupees(totals['recoverable_paise'])} identified as "
                     f"recoverable, {totals['queued']} held for a human.")

        actionable = [f for f in findings if f["exception_code"] != "CLEAN"]
        grand["findings"] += len(actionable)
        for f in actionable:
            code = f["exception_code"]
            lines.append(
                f"  - {f['payment_id']}: {code} ({DESCRIPTION.get(ExceptionCode(code), '')})"
                f" money at stake {rupees(f['money_at_stake'])},"
                f" action {f['action']}"
                + (", HELD FOR HUMAN REVIEW" if f["queued_for_human"] else ""))
            lines.append(f"      fee charged {rupees(f['actual_fee'])} against "
                         f"expected {rupees(f['expected_fee'])}; GST charged "
                         f"{rupees(f['actual_tax'])} against expected "
                         f"{rupees(f['expected_tax'])}; difference "
                         f"{rupees(f['delta'])}")
            if f["rule_cited"]:
                lines.append(f"      rule: {f['rule_cited']}")
            if f["reasoning"]:
                lines.append(f"      finding: {f['reasoning']}")
            if f["dispute_text"]:
                lines.append("      a dispute letter has already been drafted "
                             "for this one and is on its settlement page.")
        lines.append("")

    # Precomputed totals. The model must never need to add anything up.
    lines += [
        "## Totals, already computed - quote these, never recompute them",
        "",
        f"  Settlements shown: {len(runs)}",
        f"  Records audited in total: {grand['records']}",
        f"  Findings that need action: {grand['findings']}",
        f"  Total identified as recoverable: {rupees(grand['recoverable'])}",
        f"  Held for human review: {grand['queued']}",
    ]
    return "\n".join(lines)


def ask(led, question: str, client=None, model: str = MODEL,
        effort: str = "medium") -> Answer:
    """One question, one answer, checked before it is shown."""
    question = (question or "").strip()
    if not question:
        return Answer(question, "", error="no question was asked")
    if len(question) > MAX_QUESTION:
        question = question[:MAX_QUESTION]

    briefing = build_briefing(led)
    started = time.monotonic()

    try:
        import anthropic

        client = client or anthropic.Anthropic()
        response = client.messages.create(
            model=model,
            max_tokens=2_000,
            system=[{"type": "text",
                     "text": SYSTEM + "\n\n" + briefing,
                     "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": question}],
            thinking={"type": "adaptive"},
            output_config={"effort": effort},
        )
    except Exception as exc:                                # noqa: BLE001
        return Answer(question, "", error=f"{type(exc).__name__}: {exc}",
                      latency_ms=int((time.monotonic() - started) * 1000))

    text = "".join(b.text for b in response.content if b.type == "text").strip()
    if not text:
        return Answer(question, "", error="the agent returned nothing")

    answer = Answer(
        question=question,
        text=text,
        # Same check as the classifier: a figure that was not in the briefing
        # was derived or invented, and both are disqualifying for a number a
        # merchant might put in front of their gateway.
        invented_figures=unverified_figures(text, briefing),
        latency_ms=int((time.monotonic() - started) * 1000),
    )
    usage = getattr(response, "usage", None)
    if usage is not None:
        answer.input_tokens = getattr(usage, "input_tokens", 0) or 0
        answer.output_tokens = getattr(usage, "output_tokens", 0) or 0
        answer.cache_read_tokens = getattr(usage, "cache_read_input_tokens", 0) or 0
    return answer


SUGGESTIONS = [
    "Why was my last payout smaller than my sales?",
    "How much am I owed, and what should I do about it?",
    "Is anything here a tax problem rather than a money problem?",
    "Which of these should I actually argue with my gateway about?",
]
