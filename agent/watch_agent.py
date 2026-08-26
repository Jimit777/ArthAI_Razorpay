"""
The agent that decides whether a change is worth interrupting somebody for.

## Why this is not a threshold

The calculator has already worked out exactly what moved and by how much. The
obvious next step is a rule - "raise anything over Rs 50,000" - and it is the
wrong step, for a reason worth stating on stage:

    Rs 40,000 with a supplier who has stopped filing and 30 days left to claim
    is urgent. Rs 90,000 with a supplier who is merely slow and has 400 days is
    not. A threshold cannot tell those apart, so it either fires on both, which
    trains the merchant to ignore it, or on neither.

Deciding "is this worth her Tuesday morning" needs the amount, the deadline,
the supplier's history, whether she was already told, and what she can actually
do about it, weighed together. That is judgment, and it is the whole reason
there is an agent rather than a cron job with an if-statement.

## What the agent may and may not decide

    MAY   whether to raise it, how urgent it is, what to do, what to say
    MAY   to stay silent, which is the most valuable thing it does

    NEVER any rupee figure, date, count or percentage. Every one of those is
          already computed and sitting in the change. Same absolute rule as
          everywhere else in this system.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Callable, Literal, Optional

from pydantic import BaseModel, Field

from engine.gst import rules
from engine.gst.watch import CHANGE_LABEL, Change

MODEL = "claude-opus-5"
MODELS = {"opus": "claude-opus-5", "sonnet": "claude-sonnet-5"}
DEFAULT_EFFORT = "medium"
MAX_TOKENS = 3_000

URGENCIES = ("now", "this_week", "this_month", "no_action")
ACTIONS = ("chase_supplier", "stop_buying", "reverse_claim", "tell_accountant",
           "watch", "nothing")

SYSTEM_PROMPT = """\
You watch a merchant's suppliers on their behalf. Once a period you are handed
everything that CHANGED since the last check, with the numbers already worked
out, and you decide one thing about each: is this worth interrupting them for?

## Say nothing unless you have something

Most changes are not worth a merchant's Tuesday morning. A supplier who has
always been slow being slow again is not news. A new supplier who filed
everything correctly is not news. Choosing "nothing" is the most valuable
decision you make, because a watch that speaks every day is a watch that gets
muted - and a muted watch misses the thing that mattered.

Be sparing. If you raise three things and one of them was noise, the merchant
trusts the next two less.

## What actually makes something urgent

Not the size of the number on its own. Weigh these together:

  - can they still act? Credit past its s.16(4) deadline is gone; nothing they
    do on Tuesday changes it, so it is a write-off to tell the accountant
    about, not an emergency.
  - how long have they got? The same rupee figure with 30 days left and with
    400 days left are different problems.
  - is it still getting worse? A supplier who stopped filing and is still
    being bought from is a bigger problem than one they have already stopped
    using.
  - is the money recoverable, or already lost? A cancelled registration means
    credit claimed against it has to come back, with interest at 18% under
    s.50. That is not a chase, it is a correction, and it is urgent because
    the interest is running.

## Do no arithmetic

Every figure you need is in the change, already computed and formatted. Quote
them exactly as given. Never add, subtract, or take a percentage of anything -
if a number you want is not in front of you, it is not a number you may use.

## What you may decide

  urgency   now | this_week | this_month | no_action
  action    chase_supplier | stop_buying | reverse_claim | tell_accountant
            | watch | nothing

## Your output

Write to the merchant, not about them. One or two sentences saying what
changed and why it matters now. Name the supplier. Quote the figures exactly.
Where the action is chasing a supplier, include a short message they can send
without editing it.
"""


class WatchJudgment(BaseModel):
    """
    Note what is absent: any number. The agent decides whether and how urgently,
    never how much.
    """
    raise_it: bool = Field(
        description="Whether to put this in front of the merchant at all.")
    urgency: Literal[URGENCIES] = Field(  # type: ignore[valid-type]
        description="How soon it needs attention. Use no_action when raise_it "
                    "is false.")
    action: Literal[ACTIONS] = Field(  # type: ignore[valid-type]
        description="What the merchant should actually do.")
    headline: str = Field(
        description="One line, under 90 characters, addressed to the merchant.")
    reasoning: str = Field(
        description="One or two sentences on what changed and why it matters "
                    "now. Quote figures exactly as the change gives them.")
    supplier_message: Optional[str] = Field(
        default=None,
        description="Only when the action is chase_supplier: a paragraph they "
                    "can send unedited. Leave null otherwise.")


@dataclass
class Raised:
    """One decision about one change, with everything needed to replay it."""
    kind: str
    gstin: str
    name: str
    raise_it: bool
    urgency: str
    action: str
    headline: str
    reasoning: str
    supplier_message: Optional[str] = None
    exposed_paise: int = 0

    corrections: list[str] = field(default_factory=list)
    invented_figures: list[str] = field(default_factory=list)

    model: str = MODEL
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    latency_ms: int = 0
    error: Optional[str] = None
    decided_at: int = field(default_factory=lambda: int(time.time()))


UNSUPPORTED_SCHEMA_KEYS = (
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
    "minLength", "maxLength", "pattern", "format", "minItems", "maxItems",
)


def _strip_unsupported(node, keys_are_field_names: bool = False):
    """
    Drop constraint keywords strict mode rejects - and only those.

    The first version filtered by key name at every depth, which meant a FIELD
    called `pattern` was deleted from the schema because `pattern` is also a
    JSON Schema keyword. The model was then asked for an object whose most
    important property did not exist. Inside a `properties` object the keys are
    field names, not keywords, and nothing there is filtered.
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
    schema = _strip_unsupported(WatchJudgment.model_json_schema())
    schema["additionalProperties"] = False
    return schema


_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


def unverified_figures(text: str, supplied: str) -> list[str]:
    """Every number in the model's prose that we did not hand it."""
    if not text:
        return []
    known = set(_NUMBER.findall(supplied or ""))
    known |= {n.replace(",", "") for n in known}
    # "Rs 27,000" for a supplied "Rs 27,000.00" is the same number said
    # normally, and flagging it as invented was a false positive that cost the
    # model confidence on a correct answer. Whole-rupee forms of every supplied
    # figure count as known.
    for number in list(known):
        if number.endswith(".00"):
            known.add(number[:-3])
        elif "." in number:
            known.add(number.split(".")[0])
    loose = []
    for raw in _NUMBER.findall(text):
        if raw in known or raw.replace(",", "") in known:
            continue
        plain = raw.replace(",", "")
        if "." not in plain and len(plain) <= 4:
            continue                    # section numbers, day counts, years
        loose.append(raw)
    return loose


def render_change(change: Change) -> str:
    lines = [
        f"CHANGE  {CHANGE_LABEL.get(change.kind, change.kind)}",
        f"  supplier      {change.name} ({change.gstin})",
        f"  what happened {change.detail}",
        f"  exposed       {rules.rupees(change.exposed_paise)}",
    ]
    if change.was or change.now:
        lines.append(f"  was / now     {change.was or '-'}  ->  {change.now or '-'}")
    if change.days_to_deadline is not None:
        lines.append(f"  deadline      {change.days_to_deadline} days away"
                     + (" - ALREADY PASSED" if change.days_to_deadline < 0 else ""))
    return "\n".join(lines)


def review(change: Change, parsed: WatchJudgment, evidence: str) -> Raised:
    """Check the answer before trusting it. Same three checks as elsewhere."""
    corrections: list[str] = []

    urgency = parsed.urgency
    action = parsed.action

    # A decision not to raise something cannot carry an urgency, and a decision
    # to raise something cannot be "nothing". Either would put a contradiction
    # in front of the merchant.
    if not parsed.raise_it and urgency != "no_action":
        corrections.append(
            f"urgency was {urgency} on something it chose not to raise")
        urgency = "no_action"
    if not parsed.raise_it and action != "nothing":
        corrections.append(f"action was {action} on something not raised")
        action = "nothing"
    if parsed.raise_it and action == "nothing":
        corrections.append("raised it but chose no action")

    invented = unverified_figures(
        f"{parsed.headline} {parsed.reasoning} {parsed.supplier_message or ''}",
        evidence)
    if invented:
        corrections.append(f"figures appear in no input: {invented}")

    return Raised(
        kind=change.kind, gstin=change.gstin, name=change.name,
        raise_it=parsed.raise_it, urgency=urgency, action=action,
        headline=parsed.headline, reasoning=parsed.reasoning,
        supplier_message=parsed.supplier_message,
        exposed_paise=change.exposed_paise,
        corrections=corrections, invented_figures=invented)


class ClaudeWatchAgent:
    """Judges one change per call."""

    def __init__(self, client=None, model: str = MODEL,
                 effort: str = DEFAULT_EFFORT):
        import anthropic

        self._client = client if client is not None else anthropic.Anthropic()
        self._model = model
        self._effort = effort

    def judge(self, change: Change,
              on_event: Optional[Callable[[str, str], None]] = None) -> Raised:
        evidence = render_change(change)
        started = time.monotonic()
        if on_event:
            on_event("weighing", change.name)

        try:
            # tool_runner with no tools. Structured output lives there rather
            # than on messages.create, and this agent has nothing to look up -
            # everything it needs was computed before it was called.
            runner = self._client.beta.messages.tool_runner(
                model=self._model,
                max_tokens=MAX_TOKENS,
                system=SYSTEM_PROMPT,
                tools=[],
                messages=[{"role": "user", "content": evidence}],
                output_format=WatchJudgment,
                thinking={"type": "adaptive"},
                output_config={"effort": self._effort},
                # The prompt is byte-identical on every change in the batch.
                cache_control={"type": "ephemeral"},
            )
            response = runner.until_done()
        except Exception as exc:                            # noqa: BLE001
            # Broad on purpose: the contract is that a failed judgment falls
            # back to the arithmetic, and that must hold for every way a call
            # can fail. A missing API key surfaces as a TypeError raised at
            # REQUEST time, not when the client is built, so catching only API
            # errors let it escape and crash a run whose figures were already
            # computed.
            return self._failed(change, f"{type(exc).__name__}: {exc}", started)

        parsed = getattr(response, "parsed_output", None)
        if parsed is None:
            return self._failed(change, "model returned no structured output",
                                started)

        raised = review(change, parsed, evidence)
        raised.model = self._model
        raised.latency_ms = int((time.monotonic() - started) * 1000)
        usage = getattr(response, "usage", None)
        if usage is not None:
            raised.input_tokens = getattr(usage, "input_tokens", 0) or 0
            raised.output_tokens = getattr(usage, "output_tokens", 0) or 0
            raised.cache_read_tokens = getattr(
                usage, "cache_read_input_tokens", 0) or 0
        return raised

    def _failed(self, change: Change, message: str, started: float) -> Raised:
        """
        A failed call RAISES rather than staying silent.

        The opposite default from the classifier, and deliberately so. There,
        failing safe meant escalating instead of calling a record clean. Here,
        failing safe means telling the merchant something changed and we could
        not work out whether it mattered - because silence is the outcome a
        broken watch produces naturally, and it is indistinguishable from
        working correctly.
        """
        return Raised(
            kind=change.kind, gstin=change.gstin, name=change.name,
            raise_it=True, urgency="this_week", action="watch",
            headline=f"Something changed with {change.name}",
            reasoning=(f"{change.detail} The agent could not judge how urgent "
                       f"this is ({message}), so it is being shown rather than "
                       f"filtered out."),
            exposed_paise=change.exposed_paise,
            model=self._model,
            latency_ms=int((time.monotonic() - started) * 1000),
            error=message)


def judge_all(changes, agent, on_event=None) -> list[Raised]:
    return [agent.judge(c, on_event=on_event) for c in changes]
