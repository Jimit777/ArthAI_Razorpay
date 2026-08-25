"""
The agent that reads a supplier's record and says what to do about it.

## What it is and is not given

Given: a filing history already counted - compliance rate, coverage, observed
default frequency, delays, registration status, and the pattern arithmetic
assigned. Every one of those is a fact.

Not given: any opportunity to produce a number. The specification for this
feature asked the model to calculate the late-filing percentage and return a
risk probability as a float. Both are arithmetic, both live in
engine/gst/risk.py, and the schema below has nowhere a figure could hide.

What is genuinely left to judgment, and what a script cannot do:

    Two suppliers both at 70% compliance. One has been improving for a year;
    the other was perfect until March. Same number, opposite advice - hold
    payment on the second, keep watching the first. Weighing a trend against a
    level against how much money is on the table this month is the work.

## Concurrency

Suppliers are judged in parallel, one call each, never batched into a single
prompt. A hundred suppliers in one context is a hundred chances for the model
to blend two of them together, and the failure would be invisible - a fluent
paragraph about the wrong company.
"""

from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, Literal, Optional

from pydantic import BaseModel, Field

from engine.gst import rules
from engine.gst.risk import PATTERN_LABEL, RiskProfile

MODEL = "claude-opus-5"
MODELS = {"opus": "claude-opus-5", "sonnet": "claude-sonnet-5"}
DEFAULT_EFFORT = "medium"
MAX_TOKENS = 2_000
MAX_WORKERS = 6

PATTERNS = tuple(PATTERN_LABEL)
ACTIONS = ("safe_to_pay", "pay_but_watch", "hold_payment",
           "stop_buying", "get_it_in_writing")

ACTION_LABEL = {
    "safe_to_pay": "Safe to pay",
    "pay_but_watch": "Pay, but keep watching",
    "hold_payment": "Hold payment until GSTR-3B is filed",
    "stop_buying": "Stop buying from them",
    "get_it_in_writing": "Get an undertaking before paying",
}

SYSTEM_PROMPT = """\
You are advising an Indian business on whether a supplier's filing record puts
their input tax credit at risk.

Everything countable has already been counted and is in front of you. Your job
is the part arithmetic cannot do: read the shape of the record and say what the
merchant should do about it this month.

## Do no arithmetic

Every figure you need is in the evidence, already computed and formatted. Quote
them exactly. Never add, subtract or take a percentage of anything. If a number
you want is not in front of you, it is not a number you may use.

## What actually decides the advice

Not the compliance rate on its own. Weigh these together:

  - the DIRECTION. A supplier at 70% who has been improving for a year and one
    who was perfect until March are the same number and opposite advice.
  - what GSTR-3B says versus GSTR-1. A supplier who reports sales punctually
    and does not pay the tax is the dangerous case: the invoice appears in the
    merchant's GSTR-2B, everything reconciles, and under CGST s.16(2)(c) the
    credit does not exist. A reconciliation cannot see this. You can.
  - whether the registration is still alive. Credit claimed against a cancelled
    or suspended one comes back with interest at 18% under s.50.
  - how much is on the table this month. The same record justifies different
    caution on Rs 4,000 and on Rs 4,00,000.
  - how much history there is. Six periods is thin. Say so rather than
    projecting confidence the record cannot support.

## Be sparing with alarm

Most suppliers are fine, and telling a merchant to hold payment from a supplier
who has done nothing wrong costs them a relationship. "Safe to pay" is the
right answer more often than not, and saying it plainly is worth more than
hedging.

## Your output

Write to the merchant, not about them. Two or three sentences: what their
record shows, and why that means what you are recommending. Name figures
exactly as given.
"""


class RiskJudgment(BaseModel):
    """
    Note what is absent: any number. Not the risk, not the percentage, not the
    score. Those are computed, and the schema gives them nowhere to hide.
    """
    pattern: Literal[PATTERNS] = Field(  # type: ignore[valid-type]
        description="The shape of this supplier's record.")
    action: Literal[ACTIONS] = Field(  # type: ignore[valid-type]
        description="What the merchant should do about this supplier's "
                    "invoices this month.")
    headline: str = Field(
        description="One line under 90 characters, addressed to the merchant.")
    reasoning: str = Field(
        description="Two or three sentences. Quote figures exactly as the "
                    "evidence gives them.")
    watch_for: Optional[str] = Field(
        default=None,
        description="One thing that would change this advice, if there is "
                    "one. Leave null otherwise.")


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
    schema = _strip_unsupported(RiskJudgment.model_json_schema())
    schema["additionalProperties"] = False
    return schema


@dataclass
class RiskVerdict:
    gstin: str
    supplier_name: str
    pattern: str
    action: str
    headline: str
    reasoning: str
    watch_for: Optional[str] = None
    # What the arithmetic decided, and what the agent would have done. The
    # first is what the merchant is told; the second is surfaced only when it
    # is MORE cautious, because "the agent would go further" is worth knowing
    # and "the agent would relax this" is not something to act on.
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
    decided_at: int = field(default_factory=lambda: int(time.time()))


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
            continue                    # section numbers, month counts, years
        loose.append(raw)
    return loose


def render(profile: RiskProfile, supplier_name: str, exposure_paise: int,
           at_risk_paise: int, recent_rows: list[dict]) -> str:
    """One supplier as evidence, with every figure already worked out."""
    lines = [
        f"SUPPLIER  {supplier_name} ({profile.gstin})",
        f"  registration        {profile.registration_status}",
        f"  history             {profile.periods} periods",
        f"  reported sales in   {profile.gstr1_filed} of them"
        f"  ({profile.coverage_bps / 100:.1f}% coverage)",
        f"  paid the tax in     {profile.gstr3b_filed}"
        f"  ({profile.compliance_bps / 100:.1f}% of what they reported)",
        f"  reported and did NOT pay  {profile.sold_but_did_not_pay} times"
        f"  ({profile.default_rate_bps / 100:.1f}%)",
        f"  same, last 12 months      {profile.recent_sold_but_did_not_pay}"
        f" of {profile.recent_periods}"
        f"  ({profile.recent_default_rate_bps / 100:.1f}%)",
        f"  average GSTR-3B delay     {profile.avg_gstr3b_delay_days} days"
        f"  (worst {profile.worst_gstr3b_delay_days})",
        f"  trust score               {profile.trust_score}/100",
        f"  arithmetic calls it       {profile.pattern}",
        "",
        f"THIS MONTH",
        f"  credit claimed from them  {rules.rupees(exposure_paise)}",
        f"  their record puts at risk {rules.rupees(at_risk_paise)}",
    ]
    if profile.suspensions:
        lines.append(f"  registration suspended in {', '.join(profile.suspensions)}")
    if not profile.enough_history:
        lines.append("  NOTE: fewer than six filed periods. Thin evidence.")

    lines.append("")
    lines.append("LAST 12 PERIODS (filed dates, blank means never filed)")
    for row in recent_rows:
        lines.append(
            f"  {row['period']}  GSTR-1 {row['gstr1_filed'] or '-':<12}"
            f"GSTR-3B {row['gstr3b_filed'] or '-':<12}"
            + ("  <- reported the sale, never paid the tax"
               if row["sold_but_did_not_pay"] else ""))
    return "\n".join(lines)


def _recommended(profile: RiskProfile) -> str:
    from engine.gst.risk import recommended_action

    return recommended_action(profile)


def _readable(text: Optional[str]) -> str:
    """
    Undo a stray escape sequence the model wrote as literal text.

    Seen in the wild: one supplier's headline came back containing the six
    characters \\u2014 rather than an em dash, while every other response in
    the same batch used the character. Cheap to repair and it only ever affects
    text that was already broken.
    """
    if not text:
        return ""
    if "\\u" not in text:
        return text
    try:
        return text.encode("utf-8").decode("unicode_escape")
    except (UnicodeDecodeError, UnicodeEncodeError):
        return text


def review(profile: RiskProfile, supplier_name: str,
           parsed: RiskJudgment, evidence: str) -> RiskVerdict:
    """Check the answer before trusting it."""
    corrections: list[str] = []

    pattern = parsed.pattern
    # The model may disagree with the arithmetic - that is allowed and is
    # sometimes right, since it can see a trend the pattern rules cannot. But
    # a disagreement is recorded rather than silently accepted.
    if pattern != profile.pattern:
        corrections.append(
            f"the record reads as {profile.pattern}; the agent called it "
            f"{pattern}")

    invented = unverified_figures(
        f"{parsed.headline} {parsed.reasoning} {parsed.watch_for or ''}",
        evidence)
    if invented:
        corrections.append(f"figures appear in no input: {invented}")

    # The recommendation is the arithmetic's, not the agent's.
    #
    # It used to be whatever the model returned, and on a borderline record it
    # changed between runs - the same supplier came back "pay, but keep
    # watching" one afternoon and "safe to pay" the next, with sound reasoning
    # both times. A merchant who refreshes and gets different advice on
    # unchanged data stops believing the advice.
    #
    # The agent's own view is kept. When it is more cautious than the ladder
    # that is worth showing; when it is more relaxed it is noted as a
    # disagreement and not acted on.
    from engine.gst.risk import ACTION_SEVERITY, recommended_action

    action = recommended_action(profile)
    agent_action = parsed.action
    mine = ACTION_SEVERITY.get(action, 0)
    theirs = ACTION_SEVERITY.get(agent_action, 0)
    goes_further = theirs > mine
    if theirs < mine:
        corrections.append(
            f"the record calls for {action}; the agent would have said "
            f"{agent_action}")

    return RiskVerdict(
        gstin=profile.gstin, supplier_name=supplier_name,
        pattern=pattern, action=action,
        agent_action=agent_action, goes_further=goes_further,
        headline=_readable(parsed.headline),
        reasoning=_readable(parsed.reasoning),
        watch_for=_readable(parsed.watch_for) or None,
        corrections=corrections, invented_figures=invented)


class ClaudeRiskAgent:
    """One supplier per call, several calls at a time."""

    def __init__(self, client=None, model: str = MODEL,
                 effort: str = DEFAULT_EFFORT, max_workers: int = MAX_WORKERS):
        import anthropic

        self._client = client if client is not None else anthropic.Anthropic()
        self._model = model
        self._effort = effort
        self._max_workers = max_workers

    def judge(self, profile: RiskProfile, supplier_name: str,
              exposure_paise: int, at_risk_paise: int,
              recent_rows: list[dict]) -> RiskVerdict:
        import anthropic

        evidence = render(profile, supplier_name, exposure_paise,
                          at_risk_paise, recent_rows)
        started = time.monotonic()

        try:
            runner = self._client.beta.messages.tool_runner(
                model=self._model,
                max_tokens=MAX_TOKENS,
                system=SYSTEM_PROMPT,
                tools=[],
                messages=[{"role": "user", "content": evidence}],
                output_format=RiskJudgment,
                thinking={"type": "adaptive"},
                output_config={"effort": self._effort},
                # Identical prompt on every supplier, so the prefix caches.
                cache_control={"type": "ephemeral"},
            )
            response = runner.until_done()
        except (anthropic.APIStatusError, anthropic.APIConnectionError) as exc:
            return self._failed(profile, supplier_name,
                                f"{type(exc).__name__}: {exc}", started)

        parsed = getattr(response, "parsed_output", None)
        if parsed is None:
            return self._failed(profile, supplier_name,
                                "model returned no structured output", started)

        verdict = review(profile, supplier_name, parsed, evidence)
        verdict.model = self._model
        verdict.latency_ms = int((time.monotonic() - started) * 1000)
        usage = getattr(response, "usage", None)
        if usage is not None:
            verdict.input_tokens = getattr(usage, "input_tokens", 0) or 0
            verdict.output_tokens = getattr(usage, "output_tokens", 0) or 0
            verdict.cache_read_tokens = getattr(
                usage, "cache_read_input_tokens", 0) or 0
        return verdict

    def judge_all(self, jobs: list[tuple], on_each: Optional[Callable] = None
                  ) -> list[RiskVerdict]:
        """
        Judge every supplier, several at a time, one call each.

        Never batched into a single prompt: a hundred suppliers in one context
        is a hundred chances to blend two of them, and that failure looks like
        a fluent paragraph about the wrong company.
        """
        out: list[RiskVerdict] = []
        with ThreadPoolExecutor(max_workers=self._max_workers) as pool:
            for verdict in pool.map(lambda job: self.judge(*job), jobs):
                out.append(verdict)
                if on_each is not None:
                    on_each(verdict)
        return out

    def _failed(self, profile: RiskProfile, supplier_name: str,
                message: str, started: float) -> RiskVerdict:
        """
        A failed call falls back to what the arithmetic already knows.

        Not silence, and not a guess: the pattern and the score were computed
        before the model was asked, so the merchant still gets a usable row
        with the failure stated on it.
        """
        return RiskVerdict(
            gstin=profile.gstin, supplier_name=supplier_name,
            pattern=profile.pattern,
            action=_recommended(profile),
            headline=f"{supplier_name}: scored from their record only",
            reasoning=(f"Their filing record reads as "
                       f"{PATTERN_LABEL.get(profile.pattern, profile.pattern)} "
                       f"and scores {profile.trust_score}/100. The agent could "
                       f"not be reached ({message}), so this row is the "
                       f"arithmetic without the judgment on top."),
            model=self._model,
            latency_ms=int((time.monotonic() - started) * 1000),
            error=message)
