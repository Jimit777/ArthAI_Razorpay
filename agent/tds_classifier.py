"""
The TDS credit agent. Mirrors agent/gst_classifier.py.

Same two things carried across deliberately:

  the arithmetic ban is CHECKED, not just instructed. Every number in the
  model's reasoning is matched against the numbers supplied; anything else is
  recorded as an invented figure and the confidence is capped.

  a failed call is never a clean record. When the agent cannot answer, the
  record escalates to a person rather than quietly becoming CREDIT_CLEAN.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Callable, Literal, Optional, Protocol

from pydantic import BaseModel, Field

from agent.tds_prompt import render_variance, system_prompt
from agent.tds_tools import TOOL_NAMES, build_tools
from engine.tds.detector import TdsVariance
from engine.tds.taxonomy import ACTION_FOR, TdsAction, TdsCode

MODEL = "claude-opus-5"
MODELS = {"opus": "claude-opus-5", "sonnet": "claude-sonnet-5"}
DEFAULT_EFFORT = "medium"
MAX_TOKENS = 4_000
MAX_ITERATIONS = 8

CODES = tuple(str(c) for c in TdsCode)
ACTIONS = tuple(str(a) for a in TdsAction)


class TdsClassification(BaseModel):
    """
    What the API is required to return. No rupee amount anywhere - the agent
    picks a category and explains it; every figure comes from the engine.
    """
    exception_code: Literal[CODES] = Field(  # type: ignore[valid-type]
        description="Which category this payment falls into.")
    action: Literal[ACTIONS] = Field(  # type: ignore[valid-type]
        description="What the merchant should do about it.")
    confidence: float = Field(
        description="How sure you are, 0 to 1, calibrated per the instructions.")
    reasoning: str = Field(
        description="2-4 sentences addressed to the merchant, naming the rule "
                    "and quoting figures exactly as the evidence gives them.")
    rule_cited: str = Field(
        description="The rule relied on and the statutory source behind it.")
    evidence_used: list[str] = Field(
        default_factory=list,
        description="The `kind` of each signal actually relied on.")
    supplier_message: Optional[str] = Field(
        default=None,
        description="Only when the action is chase or fix_books: a paragraph "
                    "the merchant can send without editing it. Leave null "
                    "otherwise.")


UNSUPPORTED_SCHEMA_KEYS = (
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
    "minLength", "maxLength", "pattern", "format", "minItems", "maxItems",
)


def _strip_unsupported(node, keys_are_field_names: bool = False):
    """Drop constraint keywords strict mode rejects - and only those. See
    agent/gst_classifier.py's copy of this function for why the field-name
    guard exists."""
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
    schema = _strip_unsupported(TdsClassification.model_json_schema())
    schema["additionalProperties"] = False
    return schema


@dataclass
class TdsVerdict:
    """One agent decision, with everything needed to replay it. Guardrail 5."""
    payment_id: str
    exception_code: str
    action: str
    confidence: float
    reasoning: str
    rule_cited: str
    evidence_used: list[str] = field(default_factory=list)
    supplier_message: Optional[str] = None

    corrections: list[str] = field(default_factory=list)
    invented_figures: list[str] = field(default_factory=list)

    model: str = MODEL
    tool_calls: list[str] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    latency_ms: int = 0
    error: Optional[str] = None
    decided_at: int = field(default_factory=lambda: int(time.time()))


# Numbers, with or without separators and decimals.
_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


def unverified_figures(text: str, supplied: str) -> list[str]:
    """
    Every number in the model's prose that we did not hand it.

    This is the arithmetic ban made checkable rather than merely instructed.
    Bare years and small counts are ignored - "394 days" and "s.393" are
    citations, not computed money.
    """
    if not text:
        return []
    known = set(_NUMBER.findall(supplied or ""))
    known |= {n.replace(",", "") for n in known}
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


def review(variance: TdsVariance, parsed: TdsClassification, evidence: str,
          tool_output: str = "") -> TdsVerdict:
    """
    Check the model's answer against what it was given, before trusting it.
    """
    corrections: list[str] = []

    code = TdsCode(parsed.exception_code)
    expected_action = ACTION_FOR[code]
    action = parsed.action
    if action != str(expected_action):
        corrections.append(
            f"action was {action}, but {code} always means {expected_action}")
        action = str(expected_action)

    supplied_kinds = {s.kind for s in variance.signals}
    phantom = [e for e in parsed.evidence_used
              if e not in supplied_kinds and e not in TOOL_NAMES]
    if phantom:
        corrections.append(f"cited evidence that was not supplied: {phantom}")

    invented = unverified_figures(parsed.reasoning,
                                  evidence + "\n" + (tool_output or ""))
    confidence = max(0.0, min(1.0, parsed.confidence))
    if invented:
        corrections.append(f"figures appear in no input: {invented}")
        confidence = min(confidence, 0.4)
    if phantom:
        confidence = min(confidence, 0.4)

    return TdsVerdict(
        payment_id=variance.payment_id,
        exception_code=str(code),
        action=action,
        confidence=confidence,
        reasoning=parsed.reasoning,
        rule_cited=parsed.rule_cited,
        evidence_used=list(parsed.evidence_used),
        supplier_message=parsed.supplier_message,
        corrections=corrections,
        invented_figures=invented)


class Classifier(Protocol):
    def classify(self, variance: TdsVariance) -> TdsVerdict: ...


class ClaudeTdsClassifier:
    """One payment per call, with three read-only tools for digging."""

    def __init__(self, batch, client=None, model: str = MODEL,
                effort: str = DEFAULT_EFFORT,
                max_iterations: int = MAX_ITERATIONS):
        import anthropic

        self._client = client if client is not None else anthropic.Anthropic()
        self._tools = build_tools(batch)
        self._system = system_prompt()
        self._model = model
        self._effort = effort
        self._max_iterations = max_iterations

    def classify(self, variance: TdsVariance,
                on_event: Optional[Callable[[str, str], None]] = None
                ) -> TdsVerdict:
        def report(kind: str, detail: str = "") -> None:
            if on_event is not None:
                on_event(kind, detail)

        evidence = render_variance(variance)
        started = time.monotonic()
        report("weighing")

        tool_calls: list[str] = []
        tool_output: list[str] = []
        totals = {"input": 0, "output": 0, "cache_read": 0}

        try:
            runner = self._client.beta.messages.tool_runner(
                model=self._model,
                max_tokens=MAX_TOKENS,
                system=self._system,
                tools=self._tools,
                messages=[{"role": "user", "content": evidence}],
                output_format=TdsClassification,
                thinking={"type": "adaptive"},
                output_config={"effort": self._effort},
                cache_control={"type": "ephemeral"},
                max_iterations=self._max_iterations,
            )
            for message in runner:
                for block in message.content:
                    if block.type == "tool_use":
                        tool_calls.append(block.name)
                        report("tool", block.name)
                response = runner.generate_tool_call_response()
                if response is not None:
                    for block in response["content"]:
                        content = block.get("content") if isinstance(block, dict) else None
                        if isinstance(content, str):
                            tool_output.append(content)
                usage = getattr(message, "usage", None)
                if usage is not None:
                    totals["input"] += getattr(usage, "input_tokens", 0) or 0
                    totals["output"] += getattr(usage, "output_tokens", 0) or 0
                    totals["cache_read"] += getattr(
                        usage, "cache_read_input_tokens", 0) or 0
            final = runner.until_done()

        except Exception as exc:                            # noqa: BLE001
            return self._failed(variance, f"{type(exc).__name__}: {exc}", started)

        parsed = final.parsed_output
        if parsed is None:
            return self._failed(variance, "model returned no structured output",
                                started)

        verdict = review(variance, parsed, evidence, "\n".join(tool_output))
        verdict.model = self._model
        verdict.tool_calls = tool_calls
        verdict.latency_ms = int((time.monotonic() - started) * 1000)
        verdict.input_tokens = totals["input"]
        verdict.output_tokens = totals["output"]
        verdict.cache_read_tokens = totals["cache_read"]
        return verdict

    def _failed(self, variance: TdsVariance, message: str,
               started: float) -> TdsVerdict:
        """A failed call escalates. It never becomes a clean credit."""
        return TdsVerdict(
            payment_id=variance.payment_id,
            exception_code=str(TdsCode.UNEXPLAINED),
            action=str(TdsAction.ESCALATE),
            confidence=0.0,
            reasoning=f"The agent could not classify this payment: {message}. "
                     f"It has been escalated rather than assumed clean.",
            rule_cited="none - classification failed",
            model=self._model,
            latency_ms=int((time.monotonic() - started) * 1000),
            error=message)


def classify_batch(variances, classifier, progress: bool = False
                   ) -> list[TdsVerdict]:
    out = []
    for n, variance in enumerate(variances, 1):
        if progress:
            print(f"  [{n}/{len(variances)}] {variance.payment_id}", flush=True)
        out.append(classifier.classify(variance))
    return out
