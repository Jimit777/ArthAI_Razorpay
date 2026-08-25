"""
Tests for the agent layer. Checkpoint 6.

There is no API key on this machine, so these tests do not call Claude. That is
not purely a limitation - almost everything that can go wrong here is on our
side of the boundary, and those parts SHOULD be tested without a network call:

  the tools return what we think they return, and cannot write anything
  the prompt contains the evidence and not the answer key
  the review step catches a model that misbehaves
  a failed call escalates rather than silently passing

What is genuinely untested until a key exists is the live request itself.
Said plainly rather than papered over.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.classifier import (  # noqa: E402
    Classification,
    ClaudeClassifier,
    Verdict,
    classify_batch,
    permitted_actions,
    review,
    unverified_figures,
)
from agent.prompt import render_variance, system_prompt  # noqa: E402
from agent.tools import build_tools, load_memory  # noqa: E402
from engine.detector import detect_batch  # noqa: E402
from engine.taxonomy import ACTION_FOR, Action, ExceptionCode  # noqa: E402
from generator.synthetic import generate_batch  # noqa: E402


@pytest.fixture(scope="module")
def audited():
    b, gt = generate_batch(60)
    variances = detect_batch(b)
    return b, gt, variances


@pytest.fixture(scope="module")
def open_variances(audited):
    _, _, variances = audited
    return [v for v in variances if v.needs_agent]


# A stub history, so the full five-tool surface is exercised. The empty-memory
# case is tested separately below.
STUB_MEMORY = [{"exception_code": "ZERO_MDR_VIOLATION",
                "resolution": "gateway credited it back"}]


@pytest.fixture(scope="module")
def tools(audited):
    b, _, _ = audited
    return {t.to_dict()["name"]: t for t in build_tools(b, b.rate_card, STUB_MEMORY)}


class ScriptedClassifier:
    """
    A stand-in for the model, so the pipeline around it can be tested.

    Deliberately NOT shipped in the agent package. A rules-based classifier
    living next to the real one is the kind of thing that quietly becomes the
    demo, and then the pitch is a lie.
    """

    def __init__(self, answer: Classification, evidence_hook=None):
        self.answer = answer
        self.calls: list[str] = []
        self.evidence_hook = evidence_hook

    def classify(self, variance) -> Verdict:
        self.calls.append(variance.payment_id)
        evidence = render_variance(variance)
        if self.evidence_hook:
            self.evidence_hook(evidence)
        return review(variance, self.answer, evidence)


def _answer(**kw) -> Classification:
    base = dict(
        exception_code="ZERO_MDR_VIOLATION",
        action="dispute",
        confidence=0.9,
        reasoning="A network MDR was charged on a rail where it is mandated to zero.",
        rule_cited="rule 1 - PSS Act s.10A",
        evidence_used=["ZERO_MDR_RAIL_OVERCHARGED"],
    )
    return Classification(**{**base, **kw})


# --- the tools -----------------------------------------------------------

def test_no_tool_can_write_anything(tools):
    """
    Guardrail 1, structurally. The agent proposes and a human disposes - which
    holds because there is no tool in its world that changes anything.

    Four tools with an empty resolution memory, five with history. Never any
    that write.
    """
    assert len(tools) == 5
    forbidden = ("write", "update", "insert", "delete", "edit", "set", "create",
                 "bash", "exec", "file")
    for name in tools:
        assert not any(word in name for word in forbidden), name


def test_every_tool_returns_valid_json(tools, audited):
    b, _, _ = audited
    pid = b.records[0].record_id
    args = {
        "rate_card_lookup": {"instrument_key": "upi"},
        "payment_detail": {"payment_id": pid},
        "refund_history": {"payment_id": pid},
        "tds_code_map": {"deducted_on": "2026-06-10"},
        "similar_past_cases": {"exception_code": "ZERO_MDR_VIOLATION"},
    }
    for name, tool in tools.items():
        json.loads(tool.call(args[name]))


def test_tools_do_not_mutate_the_batch(tools, audited):
    """A read-only tool that quietly edits the data would be the worst kind of bug."""
    b, _, _ = audited
    before = repr(b.records)
    pid = b.records[0].record_id
    tools["payment_detail"].call({"payment_id": pid})
    tools["refund_history"].call({"payment_id": pid})
    tools["rate_card_lookup"].call({"instrument_key": "credit_card"})
    assert repr(b.records) == before


def test_tools_handle_an_unknown_id_without_raising(tools):
    """
    A model will ask for things that do not exist. The tool must answer, not
    crash - an exception mid-loop loses the whole record.
    """
    out = json.loads(tools["payment_detail"].call({"payment_id": "pay_nonexistent"}))
    assert "error" in out
    out = json.loads(tools["rate_card_lookup"].call({"instrument_key": "crypto"}))
    assert "error" in out and out["available"]
    out = json.loads(tools["tds_code_map"].call({"deducted_on": "not-a-date"}))
    assert "error" in out


def test_money_from_tools_always_carries_a_formatted_string(tools, audited):
    """
    The agent must never divide by 100. Every money value crosses the boundary
    with its display form already attached.
    """
    b, _, _ = audited
    out = json.loads(tools["payment_detail"].call({"payment_id": b.records[0].record_id}))
    for key in ("amount", "expected_fee", "expected_gst"):
        assert set(out[key]) == {"paise", "display"}
        assert out[key]["display"].startswith("Rs ") or out[key]["display"].startswith("-Rs ")


def test_tds_tool_knows_which_side_of_the_regime_change_a_date_is_on(tools):
    after = json.loads(tools["tds_code_map"].call({"deducted_on": "2026-06-10"}))
    assert after["correct_code"] == "1035"
    assert after["reported_in"] == "Form 168"
    before = json.loads(tools["tds_code_map"].call({"deducted_on": "2026-03-10"}))
    assert before["correct_code"] == "194O"
    assert before["reported_in"] == "Form 26AS"


def test_resolution_memory_returns_what_it_has(tools):
    """CLAUDE.md section 12. Recalled cases are offered; nothing is invented."""
    out = json.loads(tools["similar_past_cases"].call(
        {"exception_code": "ZERO_MDR_VIOLATION"}))
    assert out["cases_found"] == 1
    assert "confirmed by the merchant" in out["note"]

    none = json.loads(tools["similar_past_cases"].call(
        {"exception_code": "GST_MISMATCH"}))
    assert none["cases_found"] == 0
    assert "judge this record on its own evidence" in none["note"]


def test_missing_memory_file_returns_no_cases():
    assert load_memory(Path("/nonexistent/resolution_memory.json")) == []


# --- the prompt ----------------------------------------------------------

def test_system_prompt_is_byte_stable(audited):
    """
    The cached prefix is the system prompt plus the tool schemas. A timestamp
    or a record id anywhere in it would invalidate the cache on every call and
    quietly multiply the cost of a run.
    """
    assert system_prompt() == system_prompt()


def test_system_prompt_is_long_enough_to_cache():
    """Below roughly 1024 tokens a prefix silently will not cache at all."""
    assert len(system_prompt()) > 4096


def test_system_prompt_states_the_arithmetic_ban():
    prompt = system_prompt()
    assert "DO NOT DO ARITHMETIC" in prompt
    assert "NEVER invent a balancing entry" in prompt


def test_system_prompt_lists_every_taxonomy_code():
    prompt = system_prompt()
    for code in ExceptionCode:
        assert code.value in prompt


def test_evidence_contains_the_numbers_the_agent_will_need(open_variances):
    for v in open_variances:
        evidence = render_variance(v)
        for signal in v.signals:
            assert signal.detail in evidence
            assert signal.source in evidence


def test_evidence_never_leaks_the_answer_key(audited, open_variances):
    """
    The generator's planted code must not reach the prompt by any route. If it
    did, the accuracy number would measure nothing at all.
    """
    _, gt, _ = audited
    for v in open_variances:
        evidence = render_variance(v)
        assert "planted" not in evidence.lower()
        assert "ground_truth" not in evidence.lower()
        assert "answer key" not in evidence.lower()
        # the taxonomy code appears as a candidate, which is fine - but never
        # labelled as the truth
        assert "correct answer" not in evidence.lower()


def test_evidence_for_a_missing_record_explains_there_is_nothing_to_compare(audited):
    _, gt, variances = audited
    missing = [v for v in variances if not v.settlement_present]
    assert missing
    evidence = render_variance(missing[0])
    assert "No settlement line exists" in evidence
    assert "no money arrived" in evidence


# --- the review step -----------------------------------------------------

def test_review_passes_a_well_formed_answer_through(open_variances):
    v = next(v for v in open_variances
             if any(s.kind == "ZERO_MDR_RAIL_OVERCHARGED" for s in v.signals))
    verdict = review(v, _answer(), render_variance(v))
    assert verdict.corrections == []
    assert verdict.confidence == 0.9
    assert verdict.action == "dispute"
    assert verdict.is_recoverable


def test_review_refuses_to_dismiss_a_recoverable_overcharge(open_variances):
    """
    The worst output this system could produce: the merchant is told to ignore
    money they were entitled to get back, and never learns otherwise.
    """
    v = next(v for v in open_variances
             if any(s.kind == "ZERO_MDR_RAIL_OVERCHARGED" for s in v.signals))
    verdict = review(v, _answer(action="dismiss"), render_variance(v))
    assert verdict.action == "dispute"
    assert any("not permitted" in c for c in verdict.corrections)


def test_review_allows_the_choice_the_taxonomy_genuinely_offers(open_variances):
    """GST is 'fix books OR dispute' in CLAUDE.md section 5. Both must pass."""
    v = next(v for v in open_variances
             if any(s.kind == "GST_NOT_EIGHTEEN_PERCENT_OF_FEE" for s in v.signals))
    for action in ("fix_books", "dispute"):
        verdict = review(v, _answer(exception_code="GST_MISMATCH", action=action,
                                    evidence_used=["GST_NOT_EIGHTEEN_PERCENT_OF_FEE"]),
                         render_variance(v))
        assert verdict.action == action
        assert verdict.corrections == []


def test_review_catches_evidence_the_agent_never_saw(open_variances):
    v = next(v for v in open_variances
             if any(s.kind == "ZERO_MDR_RAIL_OVERCHARGED" for s in v.signals))
    verdict = review(v, _answer(evidence_used=["A_SIGNAL_THAT_DOES_NOT_EXIST"]),
                     render_variance(v))
    assert any("not present" in c for c in verdict.corrections)
    assert verdict.confidence <= 0.4


def test_review_catches_an_invented_rupee_figure(open_variances):
    """
    The arithmetic ban, enforced rather than requested. A figure in the output
    that is not in the input was derived or invented, and either way it is not
    something a merchant should put in front of their gateway.
    """
    v = next(v for v in open_variances
             if any(s.kind == "ZERO_MDR_RAIL_OVERCHARGED" for s in v.signals))
    bogus = _answer(reasoning="You were overcharged by Rs 4,271.19 on this payment.")
    verdict = review(v, bogus, render_variance(v))
    assert verdict.invented_figures == ["4271.19"]
    assert verdict.confidence <= 0.3
    assert any("absent from the evidence" in c for c in verdict.corrections)


def test_review_accepts_figures_that_came_from_the_evidence(open_variances):
    from engine.expected_value import rupees
    v = next(v for v in open_variances
             if any(s.kind == "ZERO_MDR_RAIL_OVERCHARGED" for s in v.signals))
    quoted = _answer(reasoning=f"You were overcharged by {rupees(v.fee_delta)} here.")
    verdict = review(v, quoted, render_variance(v))
    assert verdict.invented_figures == []
    assert verdict.confidence == 0.9


def test_unverified_figures_ignores_formatting_differences():
    assert unverified_figures("Rs 1,234.00", "the gap was Rs 1234") == []
    assert unverified_figures("charged 2.40%", "rate charged 2.40%") == []
    assert unverified_figures("charged 9.90%", "rate charged 2.40%") == ["9.90"]


def test_permitted_actions_never_lets_a_recoverable_code_be_dismissed():
    from engine.taxonomy import RECOVERABLE
    for code in RECOVERABLE:
        assert "dismiss" not in permitted_actions(str(code))


def test_every_taxonomy_code_has_at_least_its_default_action_permitted():
    for code in ExceptionCode:
        assert str(ACTION_FOR[code]) in permitted_actions(str(code)) or code in (
            ExceptionCode.UNEXPLAINED,)


# --- failure behaviour ---------------------------------------------------

class _Exploding:
    """A client that fails the way a real one does."""
    class beta:  # noqa: N801
        class messages:  # noqa: N801
            @staticmethod
            def tool_runner(**kwargs):
                import anthropic
                raise anthropic.APIConnectionError(request=None)


def test_a_failed_call_escalates_and_is_never_called_clean(audited, open_variances):
    """
    Silence is not absolution. If the agent cannot answer, the record goes to a
    human - it does not quietly become a clean record and vanish from the report.
    """
    b, _, _ = audited
    classifier = ClaudeClassifier(b, client=_Exploding())
    verdict = classifier.classify(open_variances[0])
    assert verdict.exception_code == ExceptionCode.UNEXPLAINED
    assert verdict.action == Action.ESCALATE
    assert verdict.confidence == 0.0
    assert verdict.error
    assert "escalated rather than assumed clean" in verdict.reasoning


def test_classify_batch_labels_every_open_variance(open_variances):
    scripted = ScriptedClassifier(_answer())
    verdicts = classify_batch(open_variances, scripted)
    assert len(verdicts) == len(open_variances)
    assert scripted.calls == [v.payment_id for v in open_variances]
    assert all(isinstance(v, Verdict) for v in verdicts)


def test_the_agent_only_ever_sees_records_the_detector_could_not_resolve(audited):
    """
    Checkpoint 5 resolves the easy majority. Sending them to the model anyway
    would cost money and add a hallucination surface for no gain.
    """
    _, _, variances = audited
    open_ones = [v for v in variances if v.needs_agent]
    assert 0 < len(open_ones) < len(variances) * 0.3


# --- the output contract -------------------------------------------------

def test_classification_rejects_a_code_outside_the_taxonomy():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        _answer(exception_code="DEFINITELY_FRAUD")


def test_classification_rejects_an_impossible_confidence():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        _answer(confidence=1.7)


def test_classification_carries_no_money_field():
    """
    Structural. If the model could return a rupee amount, the rupee amount
    could be wrong - and the product IS accuracy.
    """
    schema = Classification.model_json_schema()["properties"]
    for field_name, spec in schema.items():
        assert spec.get("type") != "integer", field_name
        assert "paise" not in field_name
        assert "amount" not in field_name


def test_verdict_is_serialisable_for_the_audit_log(open_variances):
    """Guardrail 5: every decision timestamped and replayable."""
    from dataclasses import asdict
    verdict = review(open_variances[0], _answer(), render_variance(open_variances[0]))
    blob = json.dumps(asdict(verdict))
    assert "decided_at" in blob and "rule_cited" in blob


# --- the request itself --------------------------------------------------

def test_the_request_shape_is_accepted_by_the_real_sdk(audited, open_variances):
    """
    The closest we can get to testing the live call without a key.

    A real Anthropic client, pointed at a dead port. Every keyword argument is
    validated by the SDK before a socket is opened, so a misspelled parameter
    or a shape the SDK does not accept raises TypeError here rather than in
    front of a judge. What reaches the transport instead is a connection error,
    which exercises the escalate-on-failure path with a genuine client.

    This is the test that catches SDK drift - the day a parameter is renamed,
    this goes red.
    """
    import anthropic

    b, _, _ = audited
    client = anthropic.Anthropic(api_key="not-a-real-key",
                                 base_url="http://127.0.0.1:1", max_retries=0)
    verdict = ClaudeClassifier(b, client=client).classify(open_variances[0])

    assert verdict.exception_code == ExceptionCode.UNEXPLAINED
    assert verdict.action == Action.ESCALATE
    assert "connection failed" in (verdict.error or "")


def test_a_tool_name_in_evidence_used_is_not_treated_as_fabricated(open_variances):
    """
    Caught on the first live call.

    The model listed the tools it had consulted under `evidence_used`, which is
    a fair reading of "evidence I used". The review step counted them as
    invented evidence and capped confidence at 0.4 - and would have done so on
    nearly every record that used a tool, making the confidence score a measure
    of our bug rather than the model's certainty.
    """
    v = next(v for v in open_variances
             if any(s.kind == "ZERO_MDR_RAIL_OVERCHARGED" for s in v.signals))
    answer = _answer(evidence_used=["ZERO_MDR_RAIL_OVERCHARGED", "rate_card_lookup",
                                    "payment_detail"])
    verdict = review(v, answer, render_variance(v))
    assert verdict.corrections == []
    assert verdict.confidence == 0.9


def test_genuinely_fabricated_evidence_is_still_caught(open_variances):
    """The fix above must not have opened the door it was guarding."""
    v = next(v for v in open_variances
             if any(s.kind == "ZERO_MDR_RAIL_OVERCHARGED" for s in v.signals))
    verdict = review(v, _answer(evidence_used=["CHARGEBACK_DETECTED"]),
                     render_variance(v))
    assert any("not present" in c for c in verdict.corrections)
    assert verdict.confidence <= 0.4


def test_a_figure_fetched_from_a_tool_is_not_treated_as_invented(open_variances):
    """
    Caught on the first full run.

    The agent called rate_card_lookup, correctly quoted the 0.40% UPI platform
    fee it got back, and the review step called it an invented figure because
    that number was not in the prompt. Four correct answers were capped at 0.3
    confidence, which then tripped the guardrail gate and queued them for a
    human who had nothing to fix.

    Numbers our own tools return were computed in Python. That is the whole
    thing the check is testing for.
    """
    v = open_variances[0]
    answer = _answer(evidence_used=[],
                     reasoning="The contracted platform fee is 0.40% and GST is 18.00%.")
    tool_output = '{"platform_fee_percent": 0.4, "gst_percent": 18.0}'

    without = review(v, answer, render_variance(v))
    assert without.invented_figures, "the check should notice figures absent from the prompt"

    with_tools = review(v, answer, render_variance(v), tool_output)
    assert with_tools.invented_figures == []
    assert with_tools.confidence == 0.9


def test_a_fabricated_figure_is_still_caught_when_tools_were_used(open_variances):
    """The fix must not turn the check off whenever a tool happens to run."""
    v = open_variances[0]
    answer = _answer(evidence_used=[],
                     reasoning="You were overcharged Rs 9,999.99 on this payment.")
    verdict = review(v, answer, render_variance(v),
                     '{"platform_fee_percent": 0.4, "gst_percent": 18.0}')
    assert verdict.invented_figures == ["9999.99"]
    assert verdict.confidence <= 0.3


# --- not spending money we do not need to spend -------------------------

def test_the_memory_tool_is_not_offered_when_there_is_no_memory(audited):
    """
    Measured on a real run: with an empty store the agent called
    similar_past_cases on 100% of records and was told "no history" every time.
    Each call is a full round trip, and output tokens are 83% of what a run
    costs. A tool that can only answer "nothing here" does not earn a turn.
    """
    b, _, _ = audited
    names = {t.to_dict()["name"] for t in build_tools(b, b.rate_card, [])}
    assert "similar_past_cases" not in names
    assert len(names) == 4


def test_the_memory_tool_comes_back_when_there_is_history(audited):
    b, _, _ = audited
    cases = [{"exception_code": "ZERO_MDR_VIOLATION", "resolution": "credited back"}]
    names = {t.to_dict()["name"] for t in build_tools(b, b.rate_card, cases)}
    assert "similar_past_cases" in names
    assert len(names) == 5


def test_the_prompt_describes_only_the_tools_that_exist():
    """
    A prompt advertising a tool the agent does not have invites it to try, fail,
    and burn a turn finding out.
    """
    assert "similar_past_cases" not in system_prompt(has_memory=False)
    assert "similar_past_cases" in system_prompt(has_memory=True)


def test_both_prompt_variants_are_still_byte_stable():
    """Whichever variant is in use, the cached prefix must not move."""
    assert system_prompt(False) == system_prompt(False)
    assert system_prompt(True) == system_prompt(True)
    assert system_prompt(False) != system_prompt(True)


def test_a_batch_stops_early_on_an_unrecoverable_error(open_variances):
    """
    Twice in one session a dead credit balance produced a full batch of
    escalations that read like data. Every remaining call fails identically,
    so continuing costs money and manufactures noise.
    """
    class _Dead:
        def __init__(self):
            self.calls = 0

        def classify(self, variance):
            self.calls += 1
            return Verdict(payment_id=variance.payment_id,
                           exception_code="UNEXPLAINED", action="escalate",
                           confidence=0.0, reasoning="failed",
                           rule_cited="none",
                           error="BadRequestError: Your credit balance is too low")

    dead = _Dead()
    verdicts = classify_batch(open_variances, dead)
    assert dead.calls == 1, "it kept going after an unrecoverable error"
    assert len(verdicts) == 1


def test_a_one_off_error_does_not_stop_the_batch(open_variances):
    """A single timeout is not a reason to abandon the other twelve records."""
    class _Flaky:
        def __init__(self):
            self.calls = 0

        def classify(self, variance):
            self.calls += 1
            err = "connection failed: timeout" if self.calls == 1 else None
            return Verdict(payment_id=variance.payment_id,
                           exception_code="ZERO_MDR_VIOLATION", action="dispute",
                           confidence=0.9, reasoning="because", rule_cited="rule 1",
                           error=err)

    flaky = _Flaky()
    verdicts = classify_batch(open_variances, flaky)
    assert flaky.calls == len(open_variances)
    assert len(verdicts) == len(open_variances)


def test_the_default_model_is_the_one_the_number_gets_quoted_on(audited):
    """
    A cheaper model is available for iterating on prompts and rules. It must
    never become the default by accident - the pitch is accuracy, and saving a
    few rupees on the run that gets measured on stage is the wrong trade.
    """
    from agent.classifier import DEV_MODEL, MODEL, MODELS

    b, _, _ = audited
    assert MODEL == "claude-opus-5"
    assert MODELS["opus"] == MODEL
    assert MODELS["sonnet"] == DEV_MODEL
    assert DEV_MODEL != MODEL

    import anthropic
    client = anthropic.Anthropic(api_key="x", base_url="http://127.0.0.1:1",
                                 max_retries=0)
    assert ClaudeClassifier(b, client=client)._model == MODEL


def test_a_cheaper_model_can_be_selected_explicitly(audited):
    from agent.classifier import DEV_MODEL

    import anthropic
    b, _, _ = audited
    client = anthropic.Anthropic(api_key="x", base_url="http://127.0.0.1:1",
                                 max_retries=0)
    c = ClaudeClassifier(b, client=client, model=DEV_MODEL)
    assert c._model == DEV_MODEL


def test_the_evidence_answers_what_the_agent_used_a_tool_to_ask(open_variances):
    """
    Measured: the agent called payment_detail on 100% of records, purely to
    read fields we already had. A tool round trip is billed in output tokens,
    which are 83% of the cost; the same facts inline are input tokens, a tenth
    the price. Answer the obvious question before it gets asked.
    """
    for v in open_variances:
        evidence = render_variance(v)
        assert "raw fields as the gateway recorded them" in evidence
        assert f"method          {v.raw['method']}" in evidence
        assert v.order_id in evidence


def test_the_mislabel_signature_is_visible_without_a_tool_call(open_variances):
    """The one field that makes the mislabel findable must be stated outright."""
    mislabels = [v for v in open_variances if v.raw.get("upi_reference")]
    assert mislabels
    for v in mislabels:
        assert v.raw["upi_reference"] in render_variance(v)


def test_the_default_effort_is_the_one_that_was_measured(audited):
    """
    medium was not chosen to be cheap. It was chosen after five independent
    60-record batches came back 65/65 with zero false accusations - identical
    accuracy to high, 19% cheaper, 25% faster. If someone lowers it further,
    that number has to be re-earned.
    """
    from agent.classifier import DEFAULT_EFFORT

    import anthropic
    b, _, _ = audited
    assert DEFAULT_EFFORT == "medium"
    client = anthropic.Anthropic(api_key="x", base_url="http://127.0.0.1:1",
                                 max_retries=0)
    assert ClaudeClassifier(b, client=client)._effort == DEFAULT_EFFORT
