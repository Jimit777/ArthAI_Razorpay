"""
Explaining what the three-way join could not resolve.

## The division of labour, again

The matcher in engine/recon/matcher.py decides WHETHER three records are the
same money, and what the gap is. That is arithmetic and a bounded search, it is
deterministic, and no model touches it - the whole feature exists to state a
match rate out loud, and a match rate that changes between runs is worthless.

What is left over is judgment, and this is where it lives:

    what does this gap MEAN                a Rs 59 shortfall on a credit that
                                           otherwise ties out is a bank charge
                                           somebody did not mention; a Rs 880
                                           one is not
    what should the merchant DO            chase it today, dispute it, book it
                                           as a cost, or look into it
    how urgent is it, and against what     settlement queries expire

## The action is not the agent's to choose

Same rule the supplier risk agent follows, and for the same reason it was
introduced there: on a borderline record the model's recommendation changed
between two runs on identical data, and a merchant who refreshes and gets
different advice stops believing all of it.

So `recommended_action` below is a ladder over figures already computed. The
agent's own view is kept beside it and surfaced only when it is MORE cautious,
because "the agent would go further" is worth knowing and "the agent would
relax this" is not something to act on.

## What the schema does not contain

Any number. Not the variance, not the match rate, not a confidence in rupees.
Every figure a merchant reads was computed before this module was called, and
`unverified_figures` checks the prose for any the model introduced on its own.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Callable, Literal, Optional

from pydantic import BaseModel, Field

from agent import failures
from engine.recon.records import (ACTION_CHASE, ACTION_DISPUTE,
                                  ACTION_INVESTIGATE, ACTION_NONE,
                                  ACTION_WRITE_OFF, AMOUNT_MISMATCH,
                                  FINDING_LABEL, MISSING_IN_BANK,
                                  MISSING_IN_GATEWAY, ORPHAN_BANK_CREDIT,
                                  UNEXPLAINED_FEE, ReconRow)

MODEL = "claude-opus-5"
DEFAULT_EFFORT = "medium"
MAX_TOKENS = 1_600
MAX_WORKERS = 6

ACTIONS = (ACTION_NONE, ACTION_CHASE, ACTION_DISPUTE, ACTION_WRITE_OFF,
           ACTION_INVESTIGATE)

# How much caution each action carries, so "the agent agrees" can be told from
# "the agent would go further".
ACTION_SEVERITY = {ACTION_NONE: 0, ACTION_WRITE_OFF: 1, ACTION_INVESTIGATE: 2,
                   ACTION_DISPUTE: 3, ACTION_CHASE: 4}

# Below this, chasing a shortfall costs more in somebody's time than the money
# is worth, and the honest advice is to book it and move on. Stated here to be
# argued with rather than buried in a branch.
WRITE_OFF_CEILING = 5_000               # paise: Rs 50


def recommended_action(row: ReconRow) -> str:
    """
    What to do about this row, from the figures alone.

    Ordered by what would be worst to get wrong. Money that left the gateway
    and never arrived outranks everything, because settlement queries have a
    window and a merchant who finds out in March about a January credit has
    lost the argument before making it.
    """
    if row.resolved:
        return ACTION_NONE
    if row.finding == MISSING_IN_BANK:
        return ACTION_CHASE
    if row.finding == AMOUNT_MISMATCH:
        return ACTION_DISPUTE
    if row.finding == UNEXPLAINED_FEE:
        # A small shortfall is a bank charge. Disputing one costs more than it
        # recovers, and saying so is more useful than flagging it.
        return (ACTION_WRITE_OFF if row.at_stake <= WRITE_OFF_CEILING
                else ACTION_DISPUTE)
    if row.finding in (MISSING_IN_GATEWAY, ORPHAN_BANK_CREDIT):
        return ACTION_INVESTIGATE
    return ACTION_INVESTIGATE


SYSTEM_PROMPT = """\
You explain one unreconciled line to the merchant whose money it is.

Three sources were joined: what they billed (the ERP), what their payment
gateway says it settled after its fee, and what their bank actually credited.
The join has already been done and the gap already computed. You are not being
asked to match anything or to work anything out arithmetically.

## What you are for

Say what this line MEANS and what it is likely to be. A shortfall of a few
rupees on a credit that otherwise ties out is almost always a bank charge
nobody mentioned. A large one is not. A settlement with no credit against it
is money that left one party and did not arrive at the other, and that is
urgent in a way nothing else here is.

## Rules

Every figure you use is in the evidence. Do not compute, estimate, scale or
round anything, and do not introduce a number that is not in front of you - a
figure you invent will be caught and the whole line discarded.

If two explanations fit, say so and say which is likelier and why. Do not
manufacture certainty; "this is either a bank charge or a partial reversal,
and the amount points to the first" is a better answer than a confident guess.

Never suggest a balancing entry, a plug, or writing something off to make the
books tie. If it does not reconcile, it does not reconcile.

## Your output

Write to the merchant, not about them. Two or three sentences. Name figures
exactly as given.
"""


class ReconJudgment(BaseModel):
    """No number anywhere. The schema gives one nowhere to hide."""
    action: Literal[ACTIONS] = Field(  # type: ignore[valid-type]
        description="What the merchant should do about this line.")
    headline: str = Field(
        description="One line under 90 characters, addressed to the merchant.")
    reasoning: str = Field(
        description="Two or three sentences. Quote figures exactly as the "
                    "evidence gives them.")
    likeliest_cause: Optional[str] = Field(
        default=None,
        description="The most probable explanation in a few words, or null "
                    "if the evidence does not support one.")


UNSUPPORTED_SCHEMA_KEYS = (
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
    "minLength", "maxLength", "pattern", "format", "minItems", "maxItems",
)


def _strip_unsupported(node, keys_are_field_names: bool = False):
    """
    Drop constraint keywords strict mode rejects - and only those.

    Filtering by key name at every depth would delete a FIELD called `pattern`,
    because `pattern` is also a JSON Schema keyword. Inside a `properties`
    object the keys are field names and nothing there is filtered. This bug
    shipped once already, in the supplier risk agent.
    """
    if isinstance(node, dict):
        out = {}
        for key, value in node.items():
            if not keys_are_field_names and key in UNSUPPORTED_SCHEMA_KEYS:
                continue
            out[key] = _strip_unsupported(
                value, keys_are_field_names=(key == "properties"))
        return out
    if isinstance(node, list):
        return [_strip_unsupported(v) for v in node]
    return node


def strict_schema() -> dict:
    schema = _strip_unsupported(ReconJudgment.model_json_schema())
    schema["additionalProperties"] = False
    return schema


@dataclass
class ReconVerdict:
    key: str
    action: str
    headline: str
    reasoning: str
    likeliest_cause: Optional[str] = None
    agent_action: str = ""
    goes_further: bool = False
    corrections: list[str] = field(default_factory=list)
    invented_figures: list[str] = field(default_factory=list)

    model: str = MODEL
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    latency_ms: int = 0
    error: Optional[str] = None


_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


def unverified_figures(text: str, supplied: str) -> list[str]:
    """
    Every number in the model's prose that we did not hand it.

    Whole-rupee equivalents count as known: "Rs 27,000" against a supplied
    "Rs 27,000.00" is the same figure differently formatted, and flagging it
    would train everyone to ignore the check.
    """
    if not text:
        return []
    known = set(_NUMBER.findall(supplied or ""))
    for figure in list(known):
        if figure.endswith(".00"):
            known.add(figure[:-3])
        known.add(figure.replace(",", ""))
    out = []
    for found in _NUMBER.findall(text):
        if found in known or found.replace(",", "") in known:
            continue
        # Bare small integers are ordinal prose ("all three agree"), not money.
        if found.isdigit() and len(found) <= 2:
            continue
        out.append(found)
    return out


def render(row: ReconRow) -> str:
    """The evidence for one line, as the merchant's own three sources."""
    from engine.gst import rules

    parts = [
        f"FINDING: {FINDING_LABEL.get(row.finding, row.finding)} "
        f"({row.finding})",
        f"WHAT THE JOIN FOUND: {row.detail}",
        "",
    ]
    if row.invoice:
        parts.append(
            f"ERP INVOICE   {row.invoice.invoice_id} to "
            f"{row.invoice.customer_name}, {rules.rupees(row.invoice.amount)}, "
            f"issued {row.invoice.date_issued}")
    else:
        parts.append("ERP INVOICE   none - nothing in the books matches this")

    if row.settlement:
        s = row.settlement
        parts.append(
            f"GATEWAY       {s.txn_id}, gross {rules.rupees(s.gross_amount)}, "
            f"fee {rules.rupees(s.fee_deducted)}, "
            f"net settled {rules.rupees(s.net_settled)} on {s.settlement_date}"
            f", UTR {s.utr or '(none given)'}")
    else:
        parts.append("GATEWAY       none - the gateway has no settlement for this")

    if row.bank:
        b = row.bank
        parts.append(
            f"BANK          {rules.rupees(b.credit_amount)} credited "
            f"{b.transaction_date}, reference {b.utr_number}, "
            f'narration "{b.description}"')
    else:
        parts.append("BANK          none - no credit on the statement")

    if row.variance:
        parts += ["", f"AT STAKE: {rules.rupees(row.at_stake)}"]
    if row.matched_by:
        parts.append(f"JOINED BY: {row.matched_by}")
    return "\n".join(parts)


def review(row: ReconRow, parsed: ReconJudgment, evidence: str) -> ReconVerdict:
    """Check the model against the arithmetic, and correct it where it drifts."""
    corrections: list[str] = []
    invented = unverified_figures(parsed.reasoning, evidence)
    if invented:
        corrections.append(
            "the explanation carried figures from nowhere: "
            + ", ".join(invented[:4]))

    action = recommended_action(row)
    mine, theirs = (ACTION_SEVERITY.get(action, 0),
                    ACTION_SEVERITY.get(parsed.action, 0))
    if theirs < mine:
        corrections.append(
            f"the figures call for {action}; the agent would have said "
            f"{parsed.action}")

    return ReconVerdict(
        key=_key(row), action=action, agent_action=parsed.action,
        goes_further=theirs > mine,
        headline="" if invented else parsed.headline,
        reasoning=row.detail if invented else parsed.reasoning,
        likeliest_cause=None if invented else parsed.likeliest_cause,
        corrections=corrections, invented_figures=invented)


def _key(row: ReconRow) -> str:
    if row.invoice:
        return row.invoice.invoice_id
    if row.settlement:
        return row.settlement.txn_id
    return row.bank.utr_number if row.bank else "?"


class ClaudeReconAgent:
    """One exception per call, several calls at a time."""

    def __init__(self, client=None, model: str = MODEL,
                 effort: str = DEFAULT_EFFORT, max_workers: int = MAX_WORKERS):
        import anthropic

        self._model = model
        self._effort = effort
        self._max_workers = max_workers
        self._unavailable: Optional[str] = None
        if client is not None:
            self._client = client
            return
        try:
            self._client = anthropic.Anthropic()
        except Exception as exc:                            # noqa: BLE001
            self._client = None
            self._unavailable = str(exc)

    _fatal: Optional[str] = None

    def judge(self, row: ReconRow) -> ReconVerdict:
        evidence = render(row)
        started = time.monotonic()

        blocked = self._unavailable or self._fatal
        if blocked:
            return self._failed(row, blocked, started)

        try:
            runner = self._client.beta.messages.tool_runner(
                model=self._model, max_tokens=MAX_TOKENS,
                system=SYSTEM_PROMPT, tools=[],
                messages=[{"role": "user", "content": evidence}],
                output_format=ReconJudgment,
                thinking={"type": "adaptive"},
                output_config={"effort": self._effort},
                cache_control={"type": "ephemeral"})
            response = runner.until_done()
        except Exception as exc:                            # noqa: BLE001
            message = f"{type(exc).__name__}: {exc}"
            if failures.is_fatal(message):
                self._fatal = message
            return self._failed(row, message, started)

        parsed = getattr(response, "parsed_output", None)
        if parsed is None:
            return self._failed(row, "model returned no structured output",
                                started)

        verdict = review(row, parsed, evidence)
        verdict.model = self._model
        verdict.latency_ms = int((time.monotonic() - started) * 1000)
        usage = getattr(response, "usage", None)
        if usage is not None:
            verdict.input_tokens = getattr(usage, "input_tokens", 0) or 0
            verdict.output_tokens = getattr(usage, "output_tokens", 0) or 0
            verdict.cache_read_tokens = getattr(
                usage, "cache_read_input_tokens", 0) or 0
        return verdict

    def judge_all(self, rows: list[ReconRow],
                  on_each: Optional[Callable] = None) -> list[ReconVerdict]:
        """
        Several at a time, one exception per call.

        Never batched into a single prompt: fifty exceptions in one context is
        fifty chances to attribute one merchant's shortfall to another line,
        and that failure reads as a fluent paragraph about the wrong money.
        """
        from concurrent.futures import ThreadPoolExecutor

        out: list[ReconVerdict] = []
        with ThreadPoolExecutor(max_workers=self._max_workers) as pool:
            for verdict in pool.map(self.judge, rows):
                out.append(verdict)
                if on_each is not None:
                    on_each(verdict)
        return out

    def _failed(self, row: ReconRow, message: str, started: float
                ) -> ReconVerdict:
        """A failed call falls back to what the join already established."""
        return ReconVerdict(
            key=_key(row), action=recommended_action(row),
            headline=FINDING_LABEL.get(row.finding, row.finding),
            reasoning=f"{row.detail} {failures.explain(message)}",
            model=self._model,
            latency_ms=int((time.monotonic() - started) * 1000),
            error=message)
