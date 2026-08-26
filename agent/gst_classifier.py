"""
The input tax credit agent. Mirrors agent/classifier.py.

Two things carried across deliberately rather than reinvented:

  the arithmetic ban is CHECKED, not just instructed. A model told not to
  compute figures will mostly obey, and "mostly" is not a property you can put
  on a slide. Every number in the model's reasoning is matched against the
  numbers we supplied; anything else is recorded as an invented figure and the
  confidence is capped.

  a failed call is never a clean record. When the agent cannot answer, the
  record escalates to a person. Silence is not absolution - a network error
  that quietly became CLAIM_CLEAN would be the worst bug this system could have.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Callable, Literal, Optional, Protocol

from pydantic import BaseModel, Field

from agent.gst_prompt import render_variance, system_prompt
from agent.gst_tools import TOOL_NAMES, build_tools
from engine.gst.detector import ITCVariance
from engine.gst.taxonomy import (ACTION_FOR, AT_RISK, ITCAction, ITCCode,
                                 OVERCLAIMED)

MODEL = "claude-opus-5"
MODELS = {"opus": "claude-opus-5", "sonnet": "claude-sonnet-5"}
DEFAULT_EFFORT = "medium"
MAX_TOKENS = 4_000
MAX_ITERATIONS = 8

CODES = tuple(str(c) for c in ITCCode)
ACTIONS = tuple(str(a) for a in ITCAction)


class ITCClassification(BaseModel):
    """
    What the API is required to return. Note what is absent: any rupee amount.
    The agent picks a category and explains it; every figure comes from the
    engine. If the model could return a number, the number could be wrong.
    """
    exception_code: Literal[CODES] = Field(  # type: ignore[valid-type]
        description="Which category this invoice falls into.")
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
        description="Only when the action is chase_supplier or fix_books: a "
                    "paragraph the merchant can send without editing it. "
                    "Leave null otherwise.")


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
    """
    Pydantic's schema, made acceptable to strict json_schema mode.

    Two adjustments the API requires and Pydantic does not emit: constraint
    keywords are dropped, and additionalProperties must be present and false.
    """
    schema = _strip_unsupported(ITCClassification.model_json_schema())
    schema["additionalProperties"] = False
    return schema


@dataclass
class ITCVerdict:
    """One agent decision, with everything needed to replay it. Guardrail 5."""
    invoice_id: str
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

    @property
    def is_at_risk(self) -> bool:
        return self.exception_code in {str(c) for c in AT_RISK}

    @property
    def is_overclaimed(self) -> bool:
        return self.exception_code in {str(c) for c in OVERCLAIMED}


# Numbers, with or without separators and decimals.
_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


def unverified_figures(text: str, supplied: str) -> list[str]:
    """
    Every number in the model's prose that we did not hand it.

    This is the arithmetic ban made checkable rather than merely instructed.
    Bare years and small counts are ignored - "s.16(4)" and "180 days" are
    citations, not computed money.
    """
    if not text:
        return []
    known = set(_NUMBER.findall(supplied or ""))
    # Normalised forms too, so "5,344.70" matches "5344.70".
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


def review(variance: ITCVariance, parsed: ITCClassification, evidence: str,
           tool_output: str = "") -> ITCVerdict:
    """
    Check the model's answer against what it was given, before trusting it.

    Three checks, each of which has caught something real in the settlement
    agent: an action that does not match the code, evidence cited that was
    never supplied, and figures that appear in no input.
    """
    corrections: list[str] = []

    code = ITCCode(parsed.exception_code)
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

    return ITCVerdict(
        invoice_id=variance.invoice_id,
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
    def classify(self, variance: ITCVariance) -> ITCVerdict: ...


class ClaudeITCClassifier:
    """One invoice per call, with four read-only tools for digging."""

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

    def classify(self, variance: ITCVariance,
                 on_event: Optional[Callable[[str, str], None]] = None
                 ) -> ITCVerdict:
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
                output_format=ITCClassification,
                thinking={"type": "adaptive"},
                output_config={"effort": self._effort},
                # System prompt and four tool schemas are byte-identical across
                # the batch, so the whole prefix caches and only the evidence
                # block is new on each record.
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
            # Broad on purpose: the contract is that a failed judgment falls
            # back to the arithmetic, and that must hold for every way a call
            # can fail. A missing API key surfaces as a TypeError raised at
            # REQUEST time, not when the client is built, so catching only API
            # errors let it escape and crash a run whose figures were already
            # computed.
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

    def _failed(self, variance: ITCVariance, message: str,
                started: float) -> ITCVerdict:
        """
        A failed call escalates. It never becomes a clean claim.

        The safe default when the agent cannot answer is a person looking at
        it, not an assumption that there was nothing wrong.
        """
        return ITCVerdict(
            invoice_id=variance.invoice_id,
            exception_code=str(ITCCode.UNEXPLAINED),
            action=str(ITCAction.ESCALATE),
            confidence=0.0,
            reasoning=f"The agent could not classify this invoice: {message}. "
                      f"It has been escalated rather than assumed clean.",
            rule_cited="none - classification failed",
            model=self._model,
            latency_ms=int((time.monotonic() - started) * 1000),
            error=message)


def classify_batch(variances, classifier, progress: bool = False
                   ) -> list[ITCVerdict]:
    out = []
    for n, variance in enumerate(variances, 1):
        if progress:
            print(f"  [{n}/{len(variances)}] {variance.invoice_id}", flush=True)
        out.append(classifier.classify(variance))
    return out
