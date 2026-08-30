"""
Judging a run's open-window GST corrections. One call per run - see
engine/gst_filing/timing.py's module docstring for why: the genuine
judgment ("which open period to file a GSTR-1A for first") is inherently
comparative across periods, so one call covers a run's whole set of open
periods together, not one per period. Structurally a copy of
agent/payout_timing_classifier.py.

## What is decided here and what is not

Every open period's mechanical action is already "file_1a" - the agent
cannot soften that for any period, only order them by priority and explain
why. There is no lever in this schema for "don't file" because none should
exist: see agent/gst_correction_prompt.py.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Literal, Optional

from pydantic import BaseModel, Field

from agent.gst_correction_prompt import SYSTEM_PROMPT, render

MODEL = "claude-opus-5"
DEFAULT_EFFORT = "medium"
MAX_TOKENS = 1_400

PRIORITIES = ("file_first", "file_next", "low_priority")


class PeriodPriority(BaseModel):
    period: str = Field(
        description="The period exactly as given, e.g. '2026-07'. Copy it, "
                    "do not compute or guess it.")
    priority: Literal[PRIORITIES] = Field(  # type: ignore[valid-type]
        description="Which to file first among this run's open periods. "
                    "Never a reason to skip filing - every period shown "
                    "here gets a GSTR-1A regardless of this label.")
    reasoning: str = Field(
        description="One or two sentences to the merchant. Quote figures "
                    "exactly as given.")


class GSTCorrectionJudgment(BaseModel):
    periods: list[PeriodPriority]
    overall_reasoning: str = Field(
        description="One short paragraph covering the whole run's open "
                    "periods together - why they were ordered this way.")


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
    schema = _strip_unsupported(GSTCorrectionJudgment.model_json_schema())
    schema["additionalProperties"] = False
    return schema


@dataclass
class PeriodVerdict:
    period: str
    priority: str
    reasoning: str
    confidence: float = 0.0
    error: Optional[str] = None
    invented_figures: list[str] = field(default_factory=list)


@dataclass
class GSTCorrectionVerdict:
    periods: dict[str, PeriodVerdict] = field(default_factory=dict)
    overall_reasoning: str = ""
    corrections: list[str] = field(default_factory=list)

    model: str = MODEL
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    latency_ms: int = 0
    error: Optional[str] = None

    def as_dict(self) -> dict:
        return {
            "periods": {p: v.__dict__ for p, v in self.periods.items()},
            "overall_reasoning": self.overall_reasoning,
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
            continue                    # "two periods", "the 11th"
        out.append(found)
    return out


def review(findings, parsed: GSTCorrectionJudgment, evidence: str
          ) -> GSTCorrectionVerdict:
    """Check the model against the arithmetic and each finding's own
    period, correcting where it drifts. Confidence is set per period, not
    once for the whole call, since a merchant may trust one period's
    priority and not another's."""
    corrections: list[str] = []
    known_periods = {f.period for f in findings}
    seen = {p.period for p in parsed.periods}

    missing = known_periods - seen
    if missing:
        corrections.append(
            f"periods with no priority given: {', '.join(sorted(missing))}")
    extra = seen - known_periods
    if extra:
        corrections.append(
            f"priorities given for periods not in this run: "
            f"{', '.join(sorted(extra))}")

    periods: dict[str, PeriodVerdict] = {}
    for p in parsed.periods:
        if p.period not in known_periods:
            continue
        invented = unverified_figures(p.reasoning, evidence)
        conf = 0.0 if invented else 0.85
        if invented:
            corrections.append(
                f"{p.period}: reasoning carried figures from nowhere: "
                + ", ".join(invented[:4]))
        periods[p.period] = PeriodVerdict(
            period=p.period, priority=p.priority, reasoning=p.reasoning,
            confidence=conf, invented_figures=invented)

    for period in missing:
        periods[period] = PeriodVerdict(
            period=period, priority="file_next",
            reasoning="No priority reached this period.", confidence=0.0,
            error="missing from the agent's response")

    return GSTCorrectionVerdict(
        periods=periods, overall_reasoning=parsed.overall_reasoning,
        corrections=corrections)


class ClaudeGSTCorrectionAgent:
    """One call per run, covering every open period together."""

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

    def judge(self, findings, business: str = "") -> GSTCorrectionVerdict:
        evidence = render(findings, business=business)
        started = time.monotonic()

        if self._unavailable:
            return self._failed(findings, self._unavailable, started)

        totals = {"input": 0, "output": 0, "cache_read": 0}
        try:
            runner = self._client.beta.messages.tool_runner(
                model=self._model, max_tokens=MAX_TOKENS,
                system=SYSTEM_PROMPT, tools=[],
                messages=[{"role": "user", "content": evidence}],
                output_format=GSTCorrectionJudgment,
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
            return self._failed(findings, f"{type(exc).__name__}: {exc}",
                                started)

        parsed = getattr(response, "parsed_output", None)
        if parsed is None:
            return self._failed(findings, "model returned no structured output",
                                started)

        verdict = review(findings, parsed, evidence)
        verdict.model = self._model
        verdict.latency_ms = int((time.monotonic() - started) * 1000)
        verdict.input_tokens = totals["input"]
        verdict.output_tokens = totals["output"]
        verdict.cache_read_tokens = totals["cache_read"]
        return verdict

    def _failed(self, findings, message: str, started: float
               ) -> GSTCorrectionVerdict:
        """The arithmetic already produced a usable answer for every period
        - fall back to it, with no priority beyond "file it"."""
        periods = {
            f.period: PeriodVerdict(
                period=f.period, priority="file_next",
                reasoning=f.reasoning, confidence=0.0, error=message)
            for f in findings}
        return GSTCorrectionVerdict(
            periods=periods,
            overall_reasoning=f"The agent could not be reached: {message}.",
            model=self._model,
            latency_ms=int((time.monotonic() - started) * 1000),
            error=message)
