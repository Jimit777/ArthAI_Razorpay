"""
Judging a payout timing batch. Mirrors agent/treasury_classifier.py: one
summary, one call, because there is only ever one pattern to judge - see
engine/payout_timing/detector.py's module docstring for why nothing here is
resolved per record.

## What is decided here and what is not

The PATTERN and the mechanical ACTION are the engine's, not the agent's - a
miss rate crossing the systemic threshold is a comparison between two
figures already computed. The agent may escalate further than the
mechanical action calls for; it may never soften it. What the agent
genuinely adds is the narrative, and - when escalating - the actual message.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Literal, Optional

from pydantic import BaseModel, Field

from agent.payout_timing_prompt import SYSTEM_PROMPT, render
from engine.payout_timing.taxonomy import ACTION_SEVERITY, Pattern, PayoutAction

MODEL = "claude-opus-5"
DEFAULT_EFFORT = "medium"
MAX_TOKENS = 1_200

PATTERNS = tuple(str(p) for p in Pattern)
ACTIONS = tuple(str(a) for a in PayoutAction)


class PayoutTimingJudgment(BaseModel):
    """
    Note what is absent: every rupee figure, every day count. The model may
    escalate further than the mechanical action; it may never invent a
    number the engine already computed.
    """
    pattern: Literal[PATTERNS] = Field(  # type: ignore[valid-type]
        description="The pattern, as the evidence describes it - copy it "
                    "exactly, do not compute it.")
    action: Literal[ACTIONS] = Field(  # type: ignore[valid-type]
        description="What the merchant should do. May be more urgent than "
                    "the mechanical action, never less.")
    confidence: float = Field(
        description="How sure you are of the recommendation, 0 to 1.")
    reasoning: str = Field(
        description="Two or three sentences to the merchant. Quote figures "
                    "exactly as the evidence gives them.")
    escalation_text: Optional[str] = Field(
        default=None,
        description="Only when action is 'escalate': a paragraph addressed "
                    "to Razorpay's settlement/support team, paste-ready. "
                    "Null otherwise.")


UNSUPPORTED_SCHEMA_KEYS = (
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
    "minLength", "maxLength", "pattern", "format", "minItems", "maxItems",
)


def _strip_unsupported(node, keys_are_field_names: bool = False):
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
    schema = _strip_unsupported(PayoutTimingJudgment.model_json_schema())
    schema["additionalProperties"] = False
    return schema


@dataclass
class PayoutTimingVerdict:
    pattern: str
    action: str
    confidence: float
    reasoning: str
    escalation_text: Optional[str] = None
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

    def as_dict(self) -> dict:
        return {
            "pattern": self.pattern, "action": self.action,
            "confidence": self.confidence, "reasoning": self.reasoning,
            "escalation_text": self.escalation_text,
            "agent_action": self.agent_action,
            "goes_further": self.goes_further,
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
        if found.isdigit() and len(found) <= 2:
            continue                    # "three days", "the 14th"
        out.append(found)
    return out


def review(summary, parsed: PayoutTimingJudgment, evidence: str,
          tool_output: str = "") -> PayoutTimingVerdict:
    """Check the model against the arithmetic, and correct it where it
    drifts."""
    corrections: list[str] = []
    invented = unverified_figures(parsed.reasoning,
                                  evidence + "\n" + (tool_output or ""))
    if invented:
        corrections.append("the reasoning carried figures from nowhere: "
                           + ", ".join(invented[:4]))

    # The mechanical action is never overridden by the model - only ever
    # noted as something it wanted to go further than. Same convention as
    # agent/treasury_classifier.py's review(): `action` in the returned
    # verdict always stays the calculator's own, so a merchant can never be
    # shown a softer verdict than the arithmetic concluded, however the
    # model phrased its answer.
    action = summary.action
    mine = ACTION_SEVERITY.get(action, 0)
    theirs = ACTION_SEVERITY.get(parsed.action, 0)
    if theirs < mine:
        corrections.append(
            f"the figures call for {action}; the agent would have said "
            f"{parsed.action}")

    if parsed.pattern != summary.pattern:
        corrections.append(
            f"the pattern is {summary.pattern}; the agent called it "
            f"{parsed.pattern}")

    escalation = parsed.escalation_text
    if action != str(PayoutAction.ESCALATE):
        if escalation:
            corrections.append(
                "escalation text was written for an action that isn't "
                "'escalate' - dropped")
        escalation = None
    elif not escalation:
        corrections.append(
            "action is 'escalate' but no escalation text was drafted")

    return PayoutTimingVerdict(
        pattern=summary.pattern, action=action,
        agent_action=parsed.action, goes_further=theirs > mine,
        escalation_text=escalation,
        confidence=0.0 if invented else float(parsed.confidence or 0),
        reasoning=summary.detail if invented else parsed.reasoning,
        corrections=corrections, invented_figures=invented)


class ClaudePayoutTimingAgent:
    """One summary, one call. There is only ever one pattern to judge."""

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

    def judge(self, summary, business: str = "") -> PayoutTimingVerdict:
        evidence = render(summary, business=business)
        started = time.monotonic()

        if self._unavailable:
            return self._failed(summary, self._unavailable, started)

        totals = {"input": 0, "output": 0, "cache_read": 0}
        try:
            runner = self._client.beta.messages.tool_runner(
                model=self._model, max_tokens=MAX_TOKENS,
                system=SYSTEM_PROMPT, tools=[],
                messages=[{"role": "user", "content": evidence}],
                output_format=PayoutTimingJudgment,
                thinking={"type": "adaptive"},
                output_config={"effort": self._effort},
                max_iterations=2,
                cache_control={"type": "ephemeral"})
            response = None
            for message in runner:
                usage = getattr(message, "usage", None)
                if usage is not None:
                    totals["input"] += getattr(usage, "input_tokens", 0) or 0
                    totals["output"] += getattr(usage, "output_tokens", 0) or 0
                    totals["cache_read"] += getattr(
                        usage, "cache_read_input_tokens", 0) or 0
                response = message
        except Exception as exc:                            # noqa: BLE001
            return self._failed(summary, f"{type(exc).__name__}: {exc}",
                                started)

        parsed = getattr(response, "parsed_output", None)
        if parsed is None:
            return self._failed(summary, "model returned no structured output",
                                started)

        verdict = review(summary, parsed, evidence)
        verdict.model = self._model
        verdict.latency_ms = int((time.monotonic() - started) * 1000)
        verdict.input_tokens = totals["input"]
        verdict.output_tokens = totals["output"]
        verdict.cache_read_tokens = totals["cache_read"]
        return verdict

    def _failed(self, summary, message: str, started: float
               ) -> PayoutTimingVerdict:
        """The arithmetic already produced a usable answer. Fall back to it."""
        return PayoutTimingVerdict(
            pattern=summary.pattern, action=summary.action, confidence=0.0,
            reasoning=f"{summary.detail} The agent could not be reached: "
                     f"{message}.",
            model=self._model,
            latency_ms=int((time.monotonic() - started) * 1000),
            error=message)
