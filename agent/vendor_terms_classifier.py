"""
Judging one supplier's overbilled batch. Mirrors agent/payout_timing_classifier.py:
one call per supplier that has at least one OVERBILLED line, because the
judgment here - is this worth pursuing, does it read as a pattern - is
genuinely a supplier-level question, not a per-line rule. See
engine/vendor_terms/detector.py's module docstring for why classification
itself is fully mechanical and never reaches this file.

## What is decided here and what is not

The action (REQUEST_CREDIT_NOTE) is the engine's, not the agent's - every
overbilled line the merchant sees regardless of what the agent says. What
the agent genuinely adds is the confidence read on the pattern and the
narrative that goes with it. A low-confidence read does not hide the
finding; engine/vendor_terms/gate.py routes it to a human queue instead.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Optional

from pydantic import BaseModel, Field

from agent.vendor_terms_prompt import SYSTEM_PROMPT, render
from engine.vendor_terms import rules

MODEL = "claude-opus-5"
DEFAULT_EFFORT = "medium"
MAX_TOKENS = 1_000


class VendorTermsJudgment(BaseModel):
    """Note what is absent: every rupee figure. The model reads the
    pattern; it never re-derives a number the engine already computed."""
    confidence: float = Field(
        description="How sure you are this is worth pursuing as a credit "
                    "note request, 0 to 1.")
    reasoning: str = Field(
        description="Two or three sentences to the merchant. Quote figures "
                    "exactly as the evidence gives them.")


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
    schema = _strip_unsupported(VendorTermsJudgment.model_json_schema())
    schema["additionalProperties"] = False
    return schema


@dataclass
class VendorTermsVerdict:
    confidence: float
    reasoning: str
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
            "corrections": list(self.corrections), "errored": bool(self.error),
        }


_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


def unverified_figures(text: str, supplied: str) -> list[str]:
    """Every number in the prose that we did not hand it. Same check as
    agent/payout_timing_classifier.py's own."""
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
            continue                    # "three items", "the 2nd invoice"
        out.append(found)
    return out


def review(group, parsed: VendorTermsJudgment, evidence: str
          ) -> VendorTermsVerdict:
    """Check the model against the arithmetic, and correct it where it
    drifts."""
    corrections: list[str] = []
    invented = unverified_figures(parsed.reasoning, evidence)
    if invented:
        corrections.append("the reasoning carried figures from nowhere: "
                           + ", ".join(invented[:4]))

    default_reasoning = (
        f"{len(group.overbilled)} line(s) from {group.supplier_name} "
        f"billed above the contracted price, "
        f"{rules.rupees(group.at_stake_paise)} at stake.")

    return VendorTermsVerdict(
        confidence=0.0 if invented else float(parsed.confidence or 0),
        reasoning=default_reasoning if invented else parsed.reasoning,
        corrections=corrections, invented_figures=invented)


class ClaudeVendorTermsAgent:
    """One call per supplier's overbilled batch."""

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

    def judge(self, group, business: str = "") -> VendorTermsVerdict:
        evidence = render(group, business=business)
        started = time.monotonic()

        if self._unavailable:
            return self._failed(group, self._unavailable, started)

        totals = {"input": 0, "output": 0, "cache_read": 0}
        try:
            runner = self._client.beta.messages.tool_runner(
                model=self._model, max_tokens=MAX_TOKENS,
                system=SYSTEM_PROMPT, tools=[],
                messages=[{"role": "user", "content": evidence}],
                output_format=VendorTermsJudgment,
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
            return self._failed(group, f"{type(exc).__name__}: {exc}", started)

        parsed = getattr(response, "parsed_output", None)
        if parsed is None:
            return self._failed(group, "model returned no structured output",
                                started)

        verdict = review(group, parsed, evidence)
        verdict.model = self._model
        verdict.latency_ms = int((time.monotonic() - started) * 1000)
        verdict.input_tokens = totals["input"]
        verdict.output_tokens = totals["output"]
        verdict.cache_read_tokens = totals["cache_read"]
        return verdict

    def _failed(self, group, message: str, started: float
               ) -> VendorTermsVerdict:
        """The arithmetic already produced a usable finding. Fall back to
        it - a merchant chasing an overcharge needs the line list far more
        than a well-turned sentence."""
        from engine.vendor_terms import rules

        return VendorTermsVerdict(
            confidence=0.0,
            reasoning=(
                f"{len(group.overbilled)} line(s) from {group.supplier_name} "
                f"billed above the contracted price, "
                f"{rules.rupees(group.at_stake_paise)} at stake. The agent "
                f"could not be reached: {message}."),
            model=self._model,
            latency_ms=int((time.monotonic() - started) * 1000),
            error=message)
