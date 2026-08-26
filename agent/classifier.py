"""
The Claude agent. Checkpoint 6.

Takes a Variance the detector could not resolve, and decides what kind of
discrepancy it is - with a confidence, an explanation, and the rule it relied on.

## Why the Anthropic SDK's tool runner rather than the Claude Agent SDK

CLAUDE.md section 8 names the Claude Agent SDK. Two things argued against it
here, and the choice is worth stating rather than burying:

  1. The Claude Agent SDK is Claude Code packaged as a library. It ships with
     file read/write/edit, bash and web tools built in. For an agent whose
     first guardrail is "never writes to a ledger", starting from a toolset
     that can write anywhere and then switching parts off is the wrong
     direction. Here the agent has five tools, all read-only, and no ability to
     write ANYTHING - not because we told it not to, but because no such tool
     exists in its world. That is a structural guarantee, and it survives a
     prompt injection in a settlement file.

  2. It runs on Node and the Claude Code CLI, neither of which is installed on
     this machine. CLAUDE.md section 8 asks for the fewest moving parts.

What we use instead is the tool-call loop from the official Anthropic Python
SDK - `client.beta.messages.tool_runner` - which drives exactly the same
request/execute/loop cycle over tools we define, in one `pip install`.

The classifier sits behind a narrow interface, so swapping the backend later is
a contained change rather than a rewrite.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Callable, Literal, Optional, Protocol

from pydantic import BaseModel, Field

from agent.prompt import render_variance, system_prompt
from agent.tools import build_tools
from engine.detector import Variance
from engine.taxonomy import ACTION_FOR, RECOVERABLE, Action, ExceptionCode

MODEL = "claude-opus-5"
MAX_TOKENS = 16_000
MAX_ITERATIONS = 8

# How hard the agent thinks. Thinking bills as output tokens and output is 83%
# of what a run costs, so this is the main cost dial.
#
# Measured before changing it from "high": five independent 60-record batches at
# medium, 65 of 65 agent decisions correct, zero false accusations - identical
# accuracy to high, 19% cheaper and 25% faster. Raise it back to "high" or
# "xhigh" before believing a result on materially different data.
DEFAULT_EFFORT = "medium"

# A cheaper model for development iterations. Opus stays the demo model: the
# whole pitch is accuracy, and saving a few rupees on the run that gets
# measured on stage is the wrong trade. Use this while changing prompts or
# rules, then re-measure on Opus before quoting a number.
DEV_MODEL = "claude-sonnet-5"

MODELS = {"opus": MODEL, "sonnet": DEV_MODEL}

CODES = tuple(c.value for c in ExceptionCode)
ACTIONS = tuple(a.value for a in Action)

# Named here so the review step can tell "I consulted this tool" apart from
# "I relied on a signal that does not exist".
TOOL_NAMES = frozenset({"rate_card_lookup", "payment_detail", "refund_history",
                        "tds_code_map", "similar_past_cases"})


class Classification(BaseModel):
    """
    What we require back from the model, enforced by the API rather than hoped
    for. Note what is NOT in here: no rupee amounts. The agent chooses a
    category and explains it; every figure comes from the engine. If the model
    could return a number, the number could be wrong, and the product IS
    accuracy.
    """
    exception_code: Literal[CODES] = Field(  # type: ignore[valid-type]
        description="Which category of discrepancy this record is.")
    action: Literal[ACTIONS] = Field(  # type: ignore[valid-type]
        description="What the merchant should do about it.")
    confidence: float = Field(
        ge=0.0, le=1.0,
        description="How sure you are, calibrated as described in the instructions.")
    reasoning: str = Field(
        description="2-4 sentences addressed to the merchant, naming the rule "
                    "and quoting figures exactly as they appear in the evidence.")
    rule_cited: str = Field(
        description="The rule relied on and the statute, circular or contract "
                    "clause behind it.")
    evidence_used: list[str] = Field(
        default_factory=list,
        description="The `kind` of each signal actually relied on.")
    dispute_text: Optional[str] = Field(
        default=None,
        description="Only when the action is dispute or fix_books: the body of "
                    "a message the merchant can paste into a support ticket. "
                    "Leave null otherwise.")


# Schema keywords the API's strict json_schema mode does not accept. Pydantic
# emits them from Field(ge=..., le=...) and similar constraints.
UNSUPPORTED_SCHEMA_KEYS = (
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
    "minLength", "maxLength", "pattern", "format", "minItems", "maxItems",
)


def _strip_unsupported(node):
    """Recursively drop constraint keywords strict mode rejects."""
    if isinstance(node, dict):
        return {k: _strip_unsupported(v) for k, v in node.items()
                if k not in UNSUPPORTED_SCHEMA_KEYS}
    if isinstance(node, list):
        return [_strip_unsupported(v) for v in node]
    return node


def strict_schema() -> dict:
    """
    Classification's JSON schema, tightened for the API's strict mode.

    Two adjustments Pydantic does not make for us:

      `additionalProperties: false` is required and Pydantic omits it.

      Numeric bounds are rejected. `confidence: float = Field(ge=0, le=1)`
      becomes minimum/maximum, and the API answers "For 'number' type,
      properties maximum, minimum are not supported" - which fails the entire
      batch, every record, before a single token is generated.

    Dropping the bounds from the WIRE schema does not drop the constraint: the
    response is parsed back through the Pydantic model, which still rejects a
    confidence of 1.7. The check moves from the API to us; it does not vanish.

    The synchronous path never hit either problem because the SDK's
    `output_format=Model` helper builds the schema itself. Anything hand-rolling
    a request has to do this work.
    """
    schema = _strip_unsupported(Classification.model_json_schema())
    schema["additionalProperties"] = False
    return schema


# --- what the calculator will and will not accept from the agent ---------
#
# The model picks a category; the taxonomy says what that category means the
# merchant must do. Where CLAUDE.md section 5 genuinely offers a choice - GST
# can be a books problem or a dispute depending on the amount - the model may
# pick. Everywhere else, a disagreement with the taxonomy is a mistake, not an
# opinion, and gets corrected.
PERMITTED_ACTIONS: dict[str, set[str]] = {
    ExceptionCode.GST_MISMATCH: {Action.FIX_BOOKS, Action.DISPUTE, Action.ESCALATE},
    ExceptionCode.PERIOD_BOUNDARY: {Action.FIX_BOOKS, Action.DISMISS, Action.ESCALATE},
    ExceptionCode.UNEXPLAINED: {Action.ESCALATE},
}


def permitted_actions(code: str) -> set[str]:
    if code in PERMITTED_ACTIONS:
        return {str(a) for a in PERMITTED_ACTIONS[code]}
    return {str(ACTION_FOR[ExceptionCode(code)]), str(Action.ESCALATE)}


# --- the figure check ----------------------------------------------------

# What the agent CLAIMS: rupee amounts and percentages in its prose.
_FIGURE = re.compile(r"-?Rs\s*([\d,]+(?:\.\d{1,2})?)")
_PERCENT = re.compile(r"(\d+(?:\.\d+)?)\s*%")

# What it was ALLOWED to claim: every number we put in front of it. Tool output
# is JSON, where a rate arrives as `"gst_percent": 18.0` with no percent sign,
# so matching only on Rs and % missed figures the agent had legitimately been
# handed. The lookbehind and lookahead keep it from matching digits buried in
# an identifier like pay_ZMGz6JJ7OQSwbq.
_NUMBER = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?(?![\w])")


def _canonical(values, scale: int = 2) -> set[str]:
    out = set()
    for raw in values:
        try:
            out.add(f"{round(float(str(raw).replace(',', '')), scale):.{scale}f}")
        except ValueError:
            continue
    return out


def unverified_figures(text: str, evidence: str) -> list[str]:
    """
    Every rupee amount and every percentage the agent states must appear in the
    evidence it was given.

    This is the arithmetic ban turned into something checkable. The prompt tells
    the model not to compute; this proves it did not. A figure in the output
    that is not in the input was either derived or invented, and both are
    disqualifying for a number a merchant is about to put in front of their
    payment gateway.

    Returns the offending figures, empty when clean.
    """
    allowed = _canonical(_NUMBER.findall(evidence))
    claimed = _canonical(_FIGURE.findall(text)) | _canonical(_PERCENT.findall(text))
    return sorted(claimed - allowed)


# --- the result ----------------------------------------------------------

@dataclass
class Verdict:
    """One agent decision, with everything needed to replay it. Guardrail 5."""
    payment_id: str
    exception_code: str
    action: str
    confidence: float
    reasoning: str
    rule_cited: str
    evidence_used: list[str] = field(default_factory=list)
    dispute_text: Optional[str] = None

    # what the review found wrong with the model's answer
    corrections: list[str] = field(default_factory=list)
    invented_figures: list[str] = field(default_factory=list)

    # audit trail
    model: str = MODEL
    tool_calls: list[str] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    latency_ms: int = 0
    error: Optional[str] = None
    decided_at: int = field(default_factory=lambda: int(time.time()))

    @property
    def is_recoverable(self) -> bool:
        return self.exception_code in {str(c) for c in RECOVERABLE}


class Classifier(Protocol):
    """The seam. Anything that can label a Variance fits here."""
    def classify(self, variance: Variance) -> Verdict: ...


# --- review --------------------------------------------------------------

def review(variance: Variance, result: Classification, evidence: str,
           tool_output: str = "") -> Verdict:
    """
    Check the model's answer before anyone acts on it. Pure Python.

    Three things are checked, all of them things a language model can plausibly
    get wrong and none of them things it should be trusted to self-police.
    """
    corrections: list[str] = []

    code = result.exception_code
    action = result.action
    confidence = result.confidence

    # 1. The action must be one this category permits. A recoverable overcharge
    #    that comes back as "dismiss" is the single worst output this system
    #    could produce - the merchant loses the money and never knows.
    allowed = permitted_actions(code)
    if action not in allowed:
        default = str(ACTION_FOR[ExceptionCode(code)])
        corrections.append(
            f"action '{action}' is not permitted for {code}; corrected to '{default}'")
        action = default

    # 2. Evidence it claims to have used must actually have been presented.
    #
    #    Tool names are excluded. The model reasonably reads "evidence I used"
    #    as including the tools it consulted, and those are recorded separately
    #    in Verdict.tool_calls. Counting them as fabricated evidence fired on
    #    almost every record that used a tool and capped its confidence at 0.4 -
    #    which would have made the confidence score measure our bug rather than
    #    the model's certainty.
    presented = {s.kind for s in variance.signals}
    phantom = [k for k in result.evidence_used
               if k not in presented and k not in TOOL_NAMES]
    if phantom:
        corrections.append(
            f"cited evidence not present on this record: {', '.join(phantom)}")
        confidence = min(confidence, 0.4)

    # 3. Every figure quoted must be a figure we computed. This covers the
    #    dispute text as well as the reasoning - a wrong number in a message
    #    the merchant sends to their gateway is the worst place for one.
    #
    #    Tool output counts as evidence. The agent is supposed to call
    #    rate_card_lookup when it wants a contracted rate, and the figures that
    #    come back were computed by our Python, not by the model - which is
    #    exactly what this check is testing for. Checking against the prompt
    #    alone punished the model for using its tools properly and capped four
    #    correct answers at 0.3 confidence.
    checked = result.reasoning + "\n" + (result.dispute_text or "")
    invented = unverified_figures(checked, evidence + "\n" + tool_output)
    if invented:
        corrections.append(
            f"stated figures absent from the evidence: {', '.join(invented)}")
        confidence = min(confidence, 0.3)

    return Verdict(
        payment_id=variance.payment_id,
        exception_code=code,
        action=action,
        confidence=confidence,
        reasoning=result.reasoning,
        rule_cited=result.rule_cited,
        evidence_used=list(result.evidence_used),
        dispute_text=result.dispute_text,
        corrections=corrections,
        invented_figures=invented,
    )


# --- the real thing ------------------------------------------------------

class ClaudeClassifier:
    """Classifies one variance per call, with tools available for digging."""

    def __init__(self, batch, client=None, model: str = MODEL,
                 effort: str = DEFAULT_EFFORT,
                 max_iterations: int = MAX_ITERATIONS):
        import anthropic

        from agent.tools import load_memory

        self._client = client if client is not None else anthropic.Anthropic()
        memory = load_memory()
        self._tools = build_tools(batch, batch.rate_card, memory)
        self._system = system_prompt(has_memory=bool(memory))
        self._model = model
        self._effort = effort
        self._max_iterations = max_iterations

    def classify(self, variance: Variance,
                 on_event: Optional[Callable[[str, str], None]] = None) -> Verdict:
        """
        Classify one variance.

        `on_event(kind, detail)` fires DURING the call, not after it. A single
        classification takes fifteen to twenty seconds, and reporting the tool
        calls only once the whole thing returns leaves a watcher staring at a
        still screen for the entire time the interesting part is happening.
        """
        def report(kind: str, detail: str = "") -> None:
            if on_event is not None:
                on_event(kind, detail)

        evidence = render_variance(variance)
        started = time.monotonic()
        report("weighing")

        try:
            runner = self._client.beta.messages.tool_runner(
                model=self._model,
                max_tokens=MAX_TOKENS,
                system=self._system,
                tools=self._tools,
                messages=[{"role": "user", "content": evidence}],
                output_format=Classification,
                # Judgment work under ambiguity - exactly what thinking is for.
                thinking={"type": "adaptive"},
                output_config={"effort": self._effort},
                # The system prompt and five tool schemas are byte-identical on
                # every record in the batch, so the whole prefix caches and only
                # the evidence block is new. Sixty records, one prefix.
                cache_control={"type": "ephemeral"},
                max_iterations=self._max_iterations,
            )
            # Usage has to be summed across every turn of the loop. The final
            # message reports only its own turn, so reading usage off it alone
            # showed "2 input tokens" for a request that actually sent
            # thousands - and would have made the cost figure meaningless.
            tool_calls: list[str] = []
            tool_output: list[str] = []
            totals = {"input": 0, "output": 0, "cache_read": 0}
            for message in runner:
                for block in message.content:
                    if block.type == "tool_use":
                        tool_calls.append(block.name)
                        report("tool", block.name)
                # Capture what the tools handed back, so the figure check can
                # tell a number we supplied from a number the model made up.
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
                    totals["cache_read"] += getattr(usage, "cache_read_input_tokens", 0) or 0
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
            return self._failed(variance, "model returned no structured output", started)

        verdict = review(variance, parsed, evidence, "\n".join(tool_output))
        verdict.model = self._model
        verdict.tool_calls = tool_calls
        verdict.latency_ms = int((time.monotonic() - started) * 1000)
        usage = getattr(final, "usage", None)
        if usage is not None:
            verdict.input_tokens = getattr(usage, "input_tokens", 0) or 0
            verdict.output_tokens = getattr(usage, "output_tokens", 0) or 0
            verdict.cache_read_tokens = getattr(usage, "cache_read_input_tokens", 0) or 0
        return verdict

    def _failed(self, variance: Variance, message: str, started: float) -> Verdict:
        """
        A failed call is never a clean record.

        The safe default when the agent cannot answer is to escalate to a human,
        not to assume there was nothing wrong. Silence is not absolution.
        """
        return Verdict(
            payment_id=variance.payment_id,
            exception_code=str(ExceptionCode.UNEXPLAINED),
            action=str(Action.ESCALATE),
            confidence=0.0,
            reasoning=f"The agent could not classify this record: {message}. "
                      f"It has been escalated rather than assumed clean.",
            rule_cited="none - classification failed",
            model=self._model,
            latency_ms=int((time.monotonic() - started) * 1000),
            error=message,
        )


# Errors where retrying the rest of the batch is pointless. Every remaining
# record will fail the same way, and each failure costs a round trip and
# produces an escalation that reads like a judgement until you look closely.
FATAL_ERROR_MARKERS = ("credit balance", "authentication", "invalid x-api-key",
                       "permission")


def _is_fatal(error: Optional[str]) -> bool:
    return bool(error) and any(m in error.lower() for m in FATAL_ERROR_MARKERS)


def classify_batch(variances: list[Variance], classifier: Classifier,
                   progress: bool = False) -> list[Verdict]:
    """
    Label every variance the detector left open.

    Sequential on purpose: sixty records is a demo, not a workload, and a
    serial run keeps the audit log in a readable order. The first call pays for
    the cached prefix and the rest read it.

    Stops early on an error that will not fix itself. An exhausted credit
    balance or a bad key fails identically on every remaining record, and
    grinding through them turns one clear problem into a batch of escalations
    that look like results. Better to say what happened and stop.
    """
    verdicts = []
    for i, variance in enumerate(variances, 1):
        if progress:
            print(f"  [{i}/{len(variances)}] {variance.payment_id} ...", flush=True)
        verdict = classifier.classify(variance)
        verdicts.append(verdict)

        if _is_fatal(verdict.error):
            remaining = len(variances) - i
            print(f"\n  !! stopping after {i} of {len(variances)} records.")
            print(f"     {verdict.error}")
            if remaining:
                print(f"     {remaining} records not attempted - they would all "
                      f"fail the same way.")
            print("     Nothing here is a measurement of the system.\n", flush=True)
            break

    return verdicts
