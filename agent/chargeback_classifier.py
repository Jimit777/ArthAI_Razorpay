"""
Judging one dispute's evidence. Mirrors agent/vendor_terms_classifier.py:
one call per dispute that has EVIDENCE_COMPLETE or EVIDENCE_PARTIAL - see
engine/chargeback/detector.py's module docstring for why classification
itself is fully mechanical and never reaches this file.

## What is decided here and what is not

The action (DRAFT_EVIDENCE_PACK) is the engine's, not the agent's - every
dispute with something on file gets a drafted pack regardless of what the
agent says. What the agent genuinely adds is the confidence read on the
case and the `summary` text for the real Contest API's own field. A
low-confidence read does not hide the finding; engine/chargeback/gate.py
routes it to a human queue instead, same as the deadline trigger.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Optional

from pydantic import BaseModel, Field

from agent.chargeback_prompt import SYSTEM_PROMPT, render

MODEL = "claude-opus-5"
DEFAULT_EFFORT = "medium"
MAX_TOKENS = 1_000
SUMMARY_MAX_CHARS = 1_000     # the real Contest API's own limit


class ChargebackJudgment(BaseModel):
    """Note what is absent: every rupee figure, every evidence detail not
    already supplied. The model reads the case; it never re-derives a
    number the engine already computed."""
    confidence: float = Field(
        description="How sure you are this case is worth contesting, 0 to 1.")
    reasoning: str = Field(
        description="Two or three sentences to the merchant. Quote figures "
                    "and details exactly as the evidence gives them.")
    summary: str = Field(
        description="The dispute submission's own summary text. Plain, "
                    "factual. Maximum 1000 characters. No invented facts.")


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
    schema = _strip_unsupported(ChargebackJudgment.model_json_schema())
    schema["additionalProperties"] = False
    return schema


@dataclass
class ChargebackVerdict:
    confidence: float
    reasoning: str
    summary: str
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
            "confidence": self.confidence, "reasoning": self.reasoning,
            "summary": self.summary, "corrections": list(self.corrections),
            "errored": bool(self.error),
        }


_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


def unverified_figures(text: str, supplied: str) -> list[str]:
    """Every number in the prose that we did not hand it. Same check as
    agent/vendor_terms_classifier.py's own."""
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
            continue                    # "two items", "day 3"
        out.append(found)
    return out


def review(classified, parsed: ChargebackJudgment, evidence: str
          ) -> ChargebackVerdict:
    """Check the model against the arithmetic, and correct it where it
    drifts."""
    corrections: list[str] = []
    invented = (unverified_figures(parsed.reasoning, evidence)
               + unverified_figures(parsed.summary, evidence))
    if invented:
        corrections.append("the reasoning carried figures from nowhere: "
                           + ", ".join(invented[:4]))

    summary = parsed.summary or ""
    if len(summary) > SUMMARY_MAX_CHARS:
        corrections.append(
            f"the summary was {len(summary)} characters, over the real "
            f"API's {SUMMARY_MAX_CHARS}-character limit - truncated")
        summary = summary[:SUMMARY_MAX_CHARS]

    default_reasoning = (
        f"{len(classified.present)} of {len(classified.required)} required "
        f"evidence type(s) on file for reason code "
        f'"{classified.reason_code}".')

    return ChargebackVerdict(
        confidence=0.0 if invented else float(parsed.confidence or 0),
        reasoning=default_reasoning if invented else parsed.reasoning,
        summary="" if invented else summary,
        corrections=corrections, invented_figures=invented)


class ClaudeChargebackAgent:
    """One call per dispute with something on file to judge."""

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

    def judge(self, classified, evidence_detail: dict[str, str],
             business: str = "") -> ChargebackVerdict:
        evidence = render(classified, evidence_detail, business=business)
        started = time.monotonic()

        if self._unavailable:
            return self._failed(classified, self._unavailable, started)

        totals = {"input": 0, "output": 0, "cache_read": 0}
        try:
            runner = self._client.beta.messages.tool_runner(
                model=self._model, max_tokens=MAX_TOKENS,
                system=SYSTEM_PROMPT, tools=[],
                messages=[{"role": "user", "content": evidence}],
                output_format=ChargebackJudgment,
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
            return self._failed(classified, f"{type(exc).__name__}: {exc}",
                                started)

        parsed = getattr(response, "parsed_output", None)
        if parsed is None:
            return self._failed(classified, "model returned no structured "
                                "output", started)

        verdict = review(classified, parsed, evidence)
        verdict.model = self._model
        verdict.latency_ms = int((time.monotonic() - started) * 1000)
        verdict.input_tokens = totals["input"]
        verdict.output_tokens = totals["output"]
        verdict.cache_read_tokens = totals["cache_read"]
        return verdict

    def _failed(self, classified, message: str, started: float
               ) -> ChargebackVerdict:
        """The arithmetic already produced a usable checklist. Fall back to
        it - a merchant racing a deadline needs the checklist far more than
        a well-turned sentence."""
        return ChargebackVerdict(
            confidence=0.0,
            reasoning=(
                f"{len(classified.present)} of {len(classified.required)} "
                f"required evidence type(s) on file. The agent could not "
                f"be reached: {message}."),
            summary="",
            model=self._model,
            latency_ms=int((time.monotonic() - started) * 1000),
            error=message)
