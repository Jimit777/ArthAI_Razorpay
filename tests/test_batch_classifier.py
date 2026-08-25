"""
Tests for the Batches API path.

The batch path is the one that will run unattended, which means its failures
are the ones nobody is watching for. Every test here is about what happens when
something goes wrong mid-flight.

No network. A stub stands in for the batch endpoints and hands back the shapes
the real one does.
"""

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.batch_classifier import MAX_ROUNDS, BatchClassifier  # noqa: E402
from agent.classifier import Classification  # noqa: E402
from engine.detector import detect_batch  # noqa: E402
from engine.taxonomy import Action, ExceptionCode  # noqa: E402
from generator.synthetic import generate_batch  # noqa: E402


# --- a stand-in for the batch endpoints ---------------------------------

@dataclass
class _Block:
    type: str
    text: str = ""
    name: str = ""
    id: str = ""
    input: dict = field(default_factory=dict)


@dataclass
class _Usage:
    input_tokens: int = 100
    output_tokens: int = 200
    cache_read_input_tokens: int = 1000


@dataclass
class _Message:
    content: list
    usage: _Usage = field(default_factory=_Usage)


@dataclass
class _Result:
    type: str
    message: Any = None
    error: Any = None


@dataclass
class _Row:
    custom_id: str
    result: _Result


class _Counts:
    def __init__(self, n):
        self.succeeded, self.errored, self.processing = n, 0, 0


class _State:
    def __init__(self, n):
        self.processing_status = "ended"
        self.request_counts = _Counts(n)


def _verdict_json(code="ZERO_MDR_VIOLATION", action="dispute"):
    return json.dumps({
        "exception_code": code, "action": action, "confidence": 0.9,
        "reasoning": "A network MDR was charged where it is mandated to zero.",
        "rule_cited": "rule 1 - PSS Act s.10A", "evidence_used": [],
    })


class _FakeBatches:
    """Scripted rounds. Each entry maps custom_id -> the content blocks to return."""

    def __init__(self, rounds):
        self.rounds = rounds
        self.round = 0
        self.submitted = []
        self._current = {}

    def create(self, requests):
        self.submitted.append([r["custom_id"] for r in requests])
        script = self.rounds[min(self.round, len(self.rounds) - 1)]
        self._current = {r["custom_id"]: script for r in requests}
        self.round += 1
        return type("B", (), {"id": f"batch_{self.round}"})()

    def retrieve(self, batch_id):
        return _State(len(self._current))

    def results(self, batch_id):
        for pid, blocks in self._current.items():
            if blocks == "ERROR":
                yield _Row(pid, _Result("errored", error="server blew up"))
            else:
                yield _Row(pid, _Result("succeeded", message=_Message(list(blocks))))


class _FakeClient:
    def __init__(self, rounds):
        self.messages = type("M", (), {"batches": _FakeBatches(rounds)})()

    @property
    def batches(self):
        return self.messages.batches


@pytest.fixture(scope="module")
def setup():
    b, gt = generate_batch(60)
    variances = [v for v in detect_batch(b) if v.needs_agent]
    return b, gt, variances


def _classifier(batch, rounds):
    client = _FakeClient(rounds)
    return BatchClassifier(batch, client=client, poll_seconds=0, progress=False), client


# --- the happy path ------------------------------------------------------

def test_a_single_round_classifies_everything(setup):
    b, gt, variances = setup
    c, client = _classifier(b, [[_Block("text", text=_verdict_json())]])
    verdicts = c.classify_all(variances)

    assert len(verdicts) == len(variances)
    assert client.batches.round == 1, "one round should have been enough"
    assert all(v.exception_code == "ZERO_MDR_VIOLATION" for v in verdicts)
    assert all(v.error is None for v in verdicts)


def test_verdicts_come_back_in_the_order_they_went_in(setup):
    """
    Batch results arrive in any order. Keying by position instead of custom_id
    would silently attach every explanation to the wrong payment - a failure
    that produces a complete, plausible, entirely wrong report.
    """
    b, gt, variances = setup
    c, _ = _classifier(b, [[_Block("text", text=_verdict_json())]])
    verdicts = c.classify_all(variances)
    assert [v.payment_id for v in verdicts] == [v.payment_id for v in variances]


def test_usage_is_accumulated_for_costing(setup):
    b, gt, variances = setup
    c, _ = _classifier(b, [[_Block("text", text=_verdict_json())]])
    verdicts = c.classify_all(variances)
    assert all(v.output_tokens == 200 for v in verdicts)
    assert all(v.cache_read_tokens == 1000 for v in verdicts)


# --- tools across rounds -------------------------------------------------

def test_a_tool_request_triggers_another_round(setup):
    """
    The batch endpoint answers once and stops. A record that asks for a tool
    has to be executed locally and resubmitted, or its verdict never arrives.
    """
    b, gt, variances = setup
    rounds = [
        [_Block("tool_use", name="rate_card_lookup", id="tu_1",
                input={"instrument_key": "upi"})],
        [_Block("text", text=_verdict_json())],
    ]
    c, client = _classifier(b, rounds)
    verdicts = c.classify_all(variances)

    assert client.batches.round == 2
    assert all("rate_card_lookup" in v.tool_calls for v in verdicts)
    assert all(v.error is None for v in verdicts)


def test_tool_output_counts_as_verified_evidence(setup):
    """
    Same rule as the synchronous path: a figure the agent read from one of our
    tools is a figure we computed, not one it invented.
    """
    b, gt, variances = setup
    answer = json.dumps({
        "exception_code": "ZERO_MDR_VIOLATION", "action": "dispute",
        "confidence": 0.9,
        "reasoning": "The contracted platform fee is 0.40% and GST is 18.00%.",
        "rule_cited": "rule 1", "evidence_used": [],
    })
    rounds = [
        [_Block("tool_use", name="rate_card_lookup", id="tu_1",
                input={"instrument_key": "upi"})],
        [_Block("text", text=answer)],
    ]
    c, _ = _classifier(b, rounds)
    verdicts = c.classify_all(variances)
    assert all(v.invented_figures == [] for v in verdicts)


def test_a_record_that_never_stops_calling_tools_is_escalated(setup):
    """A record still asking for tools after four rounds is a bug, not a record."""
    b, gt, variances = setup
    forever = [[_Block("tool_use", name="rate_card_lookup", id="tu_1",
                       input={"instrument_key": "upi"})]]
    c, client = _classifier(b, forever)
    verdicts = c.classify_all(variances)

    assert client.batches.round == MAX_ROUNDS
    assert all(v.exception_code == ExceptionCode.UNEXPLAINED for v in verdicts)
    assert all(v.action == Action.ESCALATE for v in verdicts)
    assert all(v.error for v in verdicts)


def test_an_unknown_tool_name_does_not_crash_the_round(setup):
    b, gt, variances = setup
    rounds = [
        [_Block("tool_use", name="definitely_not_a_tool", id="tu_1", input={})],
        [_Block("text", text=_verdict_json())],
    ]
    c, _ = _classifier(b, rounds)
    verdicts = c.classify_all(variances)
    assert all(v.error is None for v in verdicts)


# --- failure ------------------------------------------------------------

def test_an_errored_result_escalates_rather_than_resolving(setup):
    b, gt, variances = setup
    c, _ = _classifier(b, ["ERROR"])
    verdicts = c.classify_all(variances)
    assert all(v.exception_code == ExceptionCode.UNEXPLAINED for v in verdicts)
    assert all("batch result errored" in (v.error or "") for v in verdicts)


def test_a_failed_submission_escalates_every_record(setup):
    b, gt, variances = setup

    class _Broken:
        def create(self, requests):
            raise RuntimeError("credit balance too low")

    client = type("C", (), {})()
    client.messages = type("M", (), {"batches": _Broken()})()
    c = BatchClassifier(b, client=client, poll_seconds=0, progress=False)
    verdicts = c.classify_all(variances)

    assert len(verdicts) == len(variances)
    assert all(v.confidence == 0.0 for v in verdicts)
    assert all("batch submission failed" in (v.error or "") for v in verdicts)


def test_unparseable_output_escalates(setup):
    """Never guess at what the model meant to say."""
    b, gt, variances = setup
    c, _ = _classifier(b, [[_Block("text", text="I think it's probably fine?")]])
    verdicts = c.classify_all(variances)
    assert all(v.exception_code == ExceptionCode.UNEXPLAINED for v in verdicts)
    assert all("could not parse" in (v.error or "") for v in verdicts)


def test_a_verdict_outside_the_taxonomy_escalates(setup):
    b, gt, variances = setup
    bogus = json.dumps({"exception_code": "DEFINITELY_FRAUD", "action": "dispute",
                        "confidence": 0.9, "reasoning": "x", "rule_cited": "y",
                        "evidence_used": []})
    c, _ = _classifier(b, [[_Block("text", text=bogus)]])
    verdicts = c.classify_all(variances)
    assert all(v.error for v in verdicts)


# --- the request itself --------------------------------------------------

def test_the_request_carries_the_schema_and_the_tools(setup):
    b, gt, variances = setup
    c, _ = _classifier(b, [[_Block("text", text=_verdict_json())]])
    params = c._params([{"role": "user", "content": "x"}])

    assert params["output_config"]["format"]["type"] == "json_schema"
    assert params["output_config"]["format"]["schema"]["additionalProperties"] is False
    assert params["thinking"] == {"type": "adaptive"}
    assert {t["name"] for t in params["tools"]}


def test_the_batch_request_sets_a_cache_breakpoint(setup):
    """
    Measured the expensive way. The first batch run read ZERO tokens from cache
    and billed 86,805 new input tokens - re-sending the system prompt and all
    four tool schemas for every single record. It cost 87% MORE than the
    synchronous path, not 50% less. A half-price request carrying ten times the
    tokens is not a saving.

    Requests render tools -> system -> messages, so a breakpoint at the end of
    system caches the tool schemas along with it.
    """
    b, gt, variances = setup
    c, _ = _classifier(b, [[_Block("text", text=_verdict_json())]])
    params = c._params([{"role": "user", "content": "x"}])

    system = params["system"]
    assert isinstance(system, list), "a plain string cannot carry a breakpoint"
    assert system[-1]["cache_control"] == {"type": "ephemeral"}
    assert system[-1]["text"] == c._system


def test_review_is_applied_on_the_batch_path_too(setup):
    """
    The guardrails are not a property of the synchronous path. A recoverable
    overcharge must not come back as 'dismiss' however it was classified.
    """
    b, gt, variances = setup
    c, _ = _classifier(b, [[_Block("text", text=_verdict_json(action="dismiss"))]])
    verdicts = c.classify_all(variances)
    assert all(v.action == "dispute" for v in verdicts)
    assert all(v.corrections for v in verdicts)


def test_the_schema_sent_to_the_api_forbids_extra_properties():
    """
    Pydantic does not emit `additionalProperties: false` and strict json_schema
    output requires it. The synchronous path never hit this because the
    output_format helper builds the schema itself; anything hand-rolling the
    request has to add it, or the API rejects the call.
    """
    from agent.classifier import strict_schema

    schema = strict_schema()
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) >= {"exception_code", "action", "confidence",
                                       "reasoning", "rule_cited"}


def test_the_schema_carries_no_numeric_bounds():
    """
    Cost a whole batch run to find. `confidence: float = Field(ge=0, le=1)`
    becomes minimum/maximum, and strict mode answers "For 'number' type,
    properties maximum, minimum are not supported" - rejecting all 13 records
    before a single token is generated.
    """
    import json

    from agent.classifier import UNSUPPORTED_SCHEMA_KEYS, strict_schema

    blob = json.dumps(strict_schema())
    for key in UNSUPPORTED_SCHEMA_KEYS:
        assert f'"{key}"' not in blob, f"{key} will be rejected by the API"


def test_dropping_the_bounds_does_not_drop_the_constraint():
    """
    The range check moves from the API to us. It does not disappear - a
    confidence of 1.7 still has to be impossible.
    """
    from pydantic import ValidationError

    from agent.classifier import Classification

    with pytest.raises(ValidationError):
        Classification(exception_code="CLEAN", action="dismiss", confidence=1.7,
                       reasoning="x", rule_cited="y")
    with pytest.raises(ValidationError):
        Classification(exception_code="CLEAN", action="dismiss", confidence=-0.2,
                       reasoning="x", rule_cited="y")
