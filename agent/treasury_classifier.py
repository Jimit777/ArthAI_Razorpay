"""
Judging a cash forecast: which payout to move, and what it costs.

## What is decided here and what is not

The ACTION CODE is not the agent's. `delay_payout` versus `draw_credit_line`
turns on whether the movable total covers the shortfall, and that is a
comparison between two figures the engine already produced. Letting a model
decide it would mean a merchant could refresh and be told to arrange credit
they do not need, or worse, told to shuffle payments when they needed the week
to arrange a facility.

Same ladder as the other three agents on this platform, for the same reason it
was introduced in the first one: on a borderline record the model's answer
changed between two runs on identical data.

What the agent decides is WHICH payout to move. That is the judgment - it
weighs a supplier relationship against an interest charge against a service
interruption, and no comparison of totals produces it.

## Two things are checked before a recommendation is shown

A figure the model introduced that was not in the evidence. Standard across
every agent here.

A payout id that was not in the movable list. "Delay V-9999 by three days" is
a made-up figure wearing an identifier, and it is worse than a made-up number
because it reads as specific and actionable. A controller could act on it
before noticing there is no such invoice.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Literal, Optional

from pydantic import BaseModel, Field

from agent import failures
from agent.treasury_prompt import SYSTEM_PROMPT, render
from engine.treasury.records import (ACT_CHASE_RECEIVABLES, ACT_DELAY_PAYOUT,
                                     ACT_DRAW_CREDIT_LINE, ACT_NONE, ACT_WATCH,
                                     ACTION_SEVERITY)

MODEL = "claude-opus-5"
DEFAULT_EFFORT = "medium"
MAX_TOKENS = 1_800
# How many times round the tool loop before we stop. Generous enough to check
# three or four candidate payouts, short enough that a model which starts
# asking the same question repeatedly costs a few cents rather than a bill.
MAX_ITERATIONS = 8

ACTIONS = (ACT_NONE, ACT_WATCH, ACT_DELAY_PAYOUT, ACT_CHASE_RECEIVABLES,
           ACT_DRAW_CREDIT_LINE)

FINDINGS = ("CASH_HEALTHY", "CASH_TIGHT", "CASH_CRUNCH_WARNING",
            "CASH_OVERDRAWN")


class TreasuryJudgment(BaseModel):
    """
    Note what is absent: every rupee figure, and the balance on any day.

    The model may name a payout and say how many days to move it. It may not
    say what that does to the balance, because that would be arithmetic and
    the engine has already done it.
    """
    exception_code: Literal[FINDINGS] = Field(  # type: ignore[valid-type]
        description="The state of the forecast, as the evidence describes it.")
    action: Literal[ACTIONS] = Field(  # type: ignore[valid-type]
        description="What the controller should do.")
    hold_payout_id: Optional[str] = Field(
        default=None,
        description="The id of ONE payout to move, copied exactly from the "
                    "movable list in the evidence. Null if nothing should be "
                    "moved or nothing movable would help.")
    hold_days: Optional[int] = Field(
        default=None,
        description="How many days to move it by, within the limit stated in "
                    "the evidence. Null if no payout is being moved.")
    confidence: float = Field(
        description="How sure you are of the recommendation, 0 to 1. This is "
                    "about the ADVICE, not a prediction of the balance.")
    reasoning: str = Field(
        description="Two or three sentences to the controller. Lead with what "
                    "to do. Quote figures exactly as the evidence gives them.")


UNSUPPORTED_SCHEMA_KEYS = (
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
    "minLength", "maxLength", "pattern", "format", "minItems", "maxItems",
)


def _strip_unsupported(node, keys_are_field_names: bool = False):
    """
    Drop constraint keywords strict mode rejects - and only those.

    Filtering by key name at every depth would delete a FIELD called `pattern`.
    That bug shipped once, in the supplier risk agent, and the fix is repeated
    here rather than shared because each agent's schema is its own.
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
    schema = _strip_unsupported(TreasuryJudgment.model_json_schema())
    schema["additionalProperties"] = False
    return schema


@dataclass
class TreasuryVerdict:
    exception_code: str
    action: str
    confidence: float
    reasoning: str
    hold_payout_id: Optional[str] = None
    hold_days: Optional[int] = None
    agent_action: str = ""
    goes_further: bool = False
    # What it looked up before deciding. Shown to the merchant, because an
    # agent that investigated and an agent that guessed produce identical
    # prose and only one of them should be trusted.
    tool_calls: list[str] = field(default_factory=list)
    corrections: list[str] = field(default_factory=list)
    invented_figures: list[str] = field(default_factory=list)

    model: str = MODEL
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    latency_ms: int = 0
    error: Optional[str] = None

    def as_dict(self) -> dict:
        return {
            "exception_code": self.exception_code, "action": self.action,
            "confidence": self.confidence, "reasoning": self.reasoning,
            "hold_payout_id": self.hold_payout_id,
            "hold_days": self.hold_days,
            "agent_action": self.agent_action,
            "goes_further": self.goes_further,
            "tool_calls": list(self.tool_calls),
            "corrections": list(self.corrections),
            "errored": bool(self.error),
        }


_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


def unverified_figures(text: str, supplied: str) -> list[str]:
    """Every number in the prose that we did not hand it."""
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
        # Small bare integers are prose - "three days", "the 14th".
        if found.isdigit() and len(found) <= 2:
            continue
        out.append(found)
    return out


def movable_ids(forecast) -> set:
    """
    Every payout id the agent is allowed to name.

    Deliberately NOT everything movable. A payout falling after the low point
    can be moved and will not raise it, so naming one is a specific
    instruction that reads as checked and accomplishes nothing - which is
    worse than saying nothing at all. Those live in movable_after_trough and
    are shown to the agent as context, never as an answer.
    """
    out = set()
    for row in forecast.movable_near_trough:
        out.add(row.get("payout_id") or f"recurring:{row.get('name')}")
    return {i for i in out if i}


def review(forecast, parsed: TreasuryJudgment,
           evidence: str) -> TreasuryVerdict:
    """Check the model against the arithmetic, and correct it where it drifts."""
    corrections: list[str] = []
    invented = unverified_figures(parsed.reasoning, evidence)
    if invented:
        corrections.append("the advice carried figures from nowhere: "
                           + ", ".join(invented[:4]))

    allowed = movable_ids(forecast)
    held = (parsed.hold_payout_id or "").strip() or None
    if held and held not in allowed:
        # A payout id nobody supplied is worse than an invented number: it
        # reads as specific, and a controller could act on it before noticing
        # there is no such invoice. Same treatment for one that exists and
        # would not help - a phone call that accomplishes nothing is still a
        # phone call somebody made on our say-so.
        after = {r.get("payout_id") or f"recurring:{r.get('name')}"
                 for r in getattr(forecast, "movable_after_trough", [])}
        corrections.append(
            f"the agent named {held}, which falls after the low point and "
            f"would not raise it"
            if held in after else
            f"the agent named a payout that is not in the movable list "
            f"({held})")
        held = None

    action = forecast.action
    mine = ACTION_SEVERITY.get(action, 0)
    theirs = ACTION_SEVERITY.get(parsed.action, 0)
    if theirs < mine:
        corrections.append(
            f"the figures call for {action}; the agent would have said "
            f"{parsed.action}")

    # The finding is the engine's too. It is a description of the balance, not
    # an opinion about it.
    if parsed.exception_code != forecast.finding:
        corrections.append(
            f"the forecast is {forecast.finding}; the agent called it "
            f"{parsed.exception_code}")

    return TreasuryVerdict(
        exception_code=forecast.finding, action=action,
        agent_action=parsed.action, goes_further=theirs > mine,
        hold_payout_id=held,
        hold_days=parsed.hold_days if held else None,
        confidence=0.0 if invented else float(parsed.confidence or 0),
        reasoning=forecast.detail if invented else parsed.reasoning,
        corrections=corrections, invented_figures=invented)


class ClaudeTreasuryAgent:
    """One forecast, one call. There is only ever one thing to judge."""

    def __init__(self, client=None, model: str = MODEL,
                 effort: str = DEFAULT_EFFORT):
        import anthropic

        self._model = model
        self._effort = effort
        self._unavailable: Optional[str] = None
        if client is not None:
            self._client = client
            return
        try:
            self._client = anthropic.Anthropic()
        except Exception as exc:                            # noqa: BLE001
            self._client = None
            self._unavailable = str(exc)

    def judge(self, forecast, business: str = "",
              inputs=None) -> TreasuryVerdict:
        """
        Judge one forecast, with tools when the inputs are available.

        `inputs` is what the tools close over. Without it the agent still
        works and still answers - it just cannot check anything, which is
        what it did before the tools existed. Degrading to that rather than
        refusing keeps the tools an addition to a complete answer.
        """
        from agent.treasury_tools import build_tools

        evidence = render(forecast, business=business)
        started = time.monotonic()

        if self._unavailable:
            return self._failed(forecast, self._unavailable, started)

        tools = build_tools(inputs, forecast) if inputs is not None else []
        called: list[str] = []
        totals = {"input": 0, "output": 0, "cache_read": 0}

        try:
            runner = self._client.beta.messages.tool_runner(
                model=self._model, max_tokens=MAX_TOKENS,
                system=SYSTEM_PROMPT, tools=tools,
                messages=[{"role": "user", "content": evidence}],
                output_format=TreasuryJudgment,
                thinking={"type": "adaptive"},
                output_config={"effort": self._effort},
                max_iterations=MAX_ITERATIONS,
                cache_control={"type": "ephemeral"})
            # Stepping the runner rather than calling until_done, so the tool
            # calls can be recorded as they happen - the same reason the
            # settlement classifier does it.
            response = None
            for message in runner:
                for block in getattr(message, "content", []) or []:
                    if getattr(block, "type", "") == "tool_use":
                        called.append(block.name)
                # Each message reports only its own turn. Reading usage off
                # the last one showed "2 input tokens" for a request that had
                # actually run four turns and sent the evidence every time.
                usage = getattr(message, "usage", None)
                if usage is not None:
                    totals["input"] += getattr(usage, "input_tokens", 0) or 0
                    totals["output"] += getattr(usage, "output_tokens", 0) or 0
                    totals["cache_read"] += getattr(
                        usage, "cache_read_input_tokens", 0) or 0
                response = message
        except Exception as exc:                            # noqa: BLE001
            # Broad, like every other agent here: the contract is that a
            # failed judgment degrades to the arithmetic, and that has to hold
            # for every way a call can fail.
            return self._failed(forecast, f"{type(exc).__name__}: {exc}",
                                started)

        parsed = getattr(response, "parsed_output", None)
        if parsed is None:
            return self._failed(forecast, "model returned no structured output",
                                started)

        verdict = review(forecast, parsed, evidence)
        verdict.tool_calls = called
        verdict.model = self._model
        verdict.latency_ms = int((time.monotonic() - started) * 1000)
        verdict.input_tokens = totals["input"]
        verdict.output_tokens = totals["output"]
        verdict.cache_read_tokens = totals["cache_read"]
        return verdict

    def _failed(self, forecast, message: str, started: float
                ) -> TreasuryVerdict:
        """The arithmetic already produced a usable answer. Fall back to it."""
        return TreasuryVerdict(
            exception_code=forecast.finding, action=forecast.action,
            confidence=0.0,
            reasoning=f"{forecast.detail} {failures.explain(message)}",
            model=self._model,
            latency_ms=int((time.monotonic() - started) * 1000),
            error=message)
