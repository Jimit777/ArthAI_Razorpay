"""
The same agent, run through the Message Batches API. Half the price.

Batch processing costs 50% of standard rates in exchange for being
asynchronous - you submit the work, it completes within the hour, you collect
the results. For validating accuracy across several seeds that trade is free
money: nobody is waiting on a validation run, and the answers are identical.

## Why this is more than one API call

The Batches API sends a request and returns a response. It does not run a tool
loop. Our agent has tools, so a record whose answer needs one comes back with
stop_reason='tool_use' rather than a verdict.

So the loop is turned inside out. Instead of looping per record, we loop per
ROUND: submit every unfinished conversation as one batch, wait, execute
whatever tools were asked for, and submit the survivors again. Records that
finish drop out; the batch shrinks each round.

In practice most records now finish in a single round, because the evidence
already states the raw fields the agent used to fetch with payment_detail.

## What this is NOT for

The live demo. A batch takes minutes to come back, and standing in front of
judges watching a poll loop is not a demo. Use the synchronous classifier for
anything anyone is watching, and this for validation runs.
"""

from __future__ import annotations

import json
import time
from typing import Optional

from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
from anthropic.types.messages.batch_create_params import Request

from agent.classifier import (
    DEFAULT_EFFORT,
    MAX_TOKENS,
    MODEL,
    Classification,
    Verdict,
    review,
    strict_schema,
)
from agent.prompt import render_variance, system_prompt
from agent.tools import build_tools, load_memory
from engine.detector import Variance
from engine.taxonomy import Action, ExceptionCode

POLL_SECONDS = 20
MAX_ROUNDS = 4          # a record needing five rounds of tools is a bug, not a record


class BatchClassifier:
    """Classifies a whole batch of variances at 50% of the standard price."""

    def __init__(self, batch, client=None, model: str = MODEL,
                 effort: str = DEFAULT_EFFORT, poll_seconds: int = POLL_SECONDS,
                 progress: bool = True):
        import anthropic

        self._client = client if client is not None else anthropic.Anthropic()
        memory = load_memory()
        self._tools = {t.to_dict()["name"]: t for t in build_tools(batch, batch.rate_card, memory)}
        self._schemas = [t.to_dict() for t in build_tools(batch, batch.rate_card, memory)]
        self._system = system_prompt(has_memory=bool(memory))
        self._model = model
        self._effort = effort
        self._poll = poll_seconds
        self._progress = progress

    # --- request construction -------------------------------------------

    def _params(self, messages: list) -> MessageCreateParamsNonStreaming:
        return MessageCreateParamsNonStreaming(
            model=self._model,
            max_tokens=MAX_TOKENS,
            # The cache breakpoint, and it is not optional here.
            #
            # Requests render as tools -> system -> messages, so marking the end
            # of the system block caches the tool schemas with it. Without this
            # the whole prefix is re-sent for every record: the first batch run
            # billed 86,805 new input tokens and read ZERO from cache, which
            # cost more than the 50% batch discount saved. A half-price request
            # that carries ten times the tokens is not a saving.
            system=[{
                "type": "text",
                "text": self._system,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=messages,
            tools=self._schemas,
            thinking={"type": "adaptive"},
            output_config={
                "effort": self._effort,
                "format": {"type": "json_schema", "schema": strict_schema()},
            },
        )

    def _say(self, message: str) -> None:
        if self._progress:
            print(message, flush=True)

    # --- the round loop ---------------------------------------------------

    def classify_all(self, variances: list[Variance]) -> list[Verdict]:
        evidence = {v.payment_id: render_variance(v) for v in variances}
        by_id = {v.payment_id: v for v in variances}

        # live conversations, keyed by payment id
        threads: dict[str, list] = {
            v.payment_id: [{"role": "user", "content": evidence[v.payment_id]}]
            for v in variances
        }
        tool_output: dict[str, list[str]] = {v.payment_id: [] for v in variances}
        tool_calls: dict[str, list[str]] = {v.payment_id: [] for v in variances}
        usage: dict[str, dict] = {v.payment_id: {"in": 0, "out": 0, "cache": 0}
                                  for v in variances}
        verdicts: dict[str, Verdict] = {}
        started = time.monotonic()

        for round_no in range(1, MAX_ROUNDS + 1):
            if not threads:
                break
            self._say(f"  round {round_no}: submitting {len(threads)} records")

            try:
                submitted = self._client.messages.batches.create(requests=[
                    Request(custom_id=pid, params=self._params(msgs))
                    for pid, msgs in threads.items()
                ])
            except Exception as exc:                       # noqa: BLE001
                for pid in list(threads):
                    verdicts[pid] = _failed(by_id[pid], f"batch submission failed: {exc}",
                                            self._model, started)
                threads.clear()
                break

            self._await(submitted.id)

            next_threads: dict[str, list] = {}
            for result in self._client.messages.batches.results(submitted.id):
                pid = result.custom_id
                variance = by_id[pid]

                if result.result.type != "succeeded":
                    detail = getattr(result.result, "error", result.result.type)
                    verdicts[pid] = _failed(variance, f"batch result {result.result.type}: "
                                                      f"{detail}", self._model, started)
                    continue

                message = result.result.message
                u = getattr(message, "usage", None)
                if u is not None:
                    usage[pid]["in"] += getattr(u, "input_tokens", 0) or 0
                    usage[pid]["out"] += getattr(u, "output_tokens", 0) or 0
                    usage[pid]["cache"] += getattr(u, "cache_read_input_tokens", 0) or 0

                requested = [b for b in message.content if b.type == "tool_use"]
                if requested and round_no < MAX_ROUNDS:
                    # Run what it asked for and send the conversation back round.
                    results_block = []
                    for call in requested:
                        tool_calls[pid].append(call.name)
                        answer = self._run_tool(call.name, call.input)
                        tool_output[pid].append(answer)
                        results_block.append({
                            "type": "tool_result",
                            "tool_use_id": call.id,
                            "content": answer,
                        })
                    thread = threads[pid] + [
                        {"role": "assistant", "content": message.content},
                        {"role": "user", "content": results_block},
                    ]
                    next_threads[pid] = thread
                    continue

                verdicts[pid] = self._finish(variance, message, evidence[pid],
                                             tool_output[pid], tool_calls[pid],
                                             usage[pid], started)

            threads = next_threads

        # Anything still unfinished ran out of rounds.
        for pid, _ in threads.items():
            verdicts[pid] = _failed(by_id[pid],
                                    f"still calling tools after {MAX_ROUNDS} rounds",
                                    self._model, started)

        return [verdicts[v.payment_id] for v in variances]

    # --- helpers ----------------------------------------------------------

    def _await(self, batch_id: str) -> None:
        while True:
            state = self._client.messages.batches.retrieve(batch_id)
            if state.processing_status == "ended":
                counts = state.request_counts
                self._say(f"    done: {counts.succeeded} succeeded, "
                          f"{counts.errored} errored")
                return
            self._say(f"    {state.processing_status}, "
                      f"{state.request_counts.processing} still processing")
            time.sleep(self._poll)

    def _run_tool(self, name: str, arguments) -> str:
        tool = self._tools.get(name)
        if tool is None:
            return json.dumps({"error": f"no tool named '{name}'"})
        try:
            # Tool inputs are parsed JSON, never string-matched. Models vary in
            # how they escape strings inside tool arguments.
            return tool.call(dict(arguments))
        except Exception as exc:                            # noqa: BLE001
            return json.dumps({"error": f"{name} failed: {exc}"})

    def _finish(self, variance, message, evidence, tool_output, tool_calls,
                usage, started) -> Verdict:
        text = next((b.text for b in message.content if b.type == "text"), None)
        if not text:
            return _failed(variance, "no structured output in the response",
                           self._model, started)
        try:
            parsed = Classification(**json.loads(text))
        except Exception as exc:                            # noqa: BLE001
            return _failed(variance, f"could not parse the verdict: {exc}",
                           self._model, started)

        verdict = review(variance, parsed, evidence, "\n".join(tool_output))
        verdict.model = self._model
        verdict.tool_calls = list(tool_calls)
        verdict.input_tokens = usage["in"]
        verdict.output_tokens = usage["out"]
        verdict.cache_read_tokens = usage["cache"]
        verdict.latency_ms = int((time.monotonic() - started) * 1000)
        return verdict


def _failed(variance: Variance, message: str, model: str, started: float) -> Verdict:
    """Same rule as the synchronous path: a failure escalates, never resolves."""
    return Verdict(
        payment_id=variance.payment_id,
        exception_code=str(ExceptionCode.UNEXPLAINED),
        action=str(Action.ESCALATE),
        confidence=0.0,
        reasoning=f"The agent could not classify this record: {message}. "
                  f"It has been escalated rather than assumed clean.",
        rule_cited="none - classification failed",
        model=model,
        latency_ms=int((time.monotonic() - started) * 1000),
        error=message,
    )
