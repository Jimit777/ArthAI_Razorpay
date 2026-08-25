"""
Tests for the agent's terminal.

The guarantee worth protecting: a line in this terminal cannot claim something
the audit trail does not contain, because there is nowhere else for it to come
from. It is rebuilt from `variances` and `audit_log` rather than captured
separately while the audit runs - two records of the same events eventually
disagree, and then the prettier one gets believed.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.classifier import Verdict  # noqa: E402
from engine.detector import detect_batch  # noqa: E402
from engine.gate import gate_batch  # noqa: E402
from merchant.gateway import Behaviour  # noqa: E402
from merchant.ledger import Ledger  # noqa: E402
from merchant.trace import build  # noqa: E402
from merchant.views import terminal  # noqa: E402


@pytest.fixture
def audited(tmp_path):
    boot = Ledger(tmp_path / "t.db")
    business_id = boot.businesses.create("Meera's Boutique")
    boot.close()

    led = Ledger(tmp_path / "t.db", business_id)
    led.set_behaviour(Behaviour.CARD_RATE_ON_UPI)
    led.capture_payment(led.create_order(162_700, "Scarf"), "upi")
    led.capture_payment(led.create_order(350_000, "Kurta"), "card", "visa", "credit")

    batch = led.build_settlement(led.rate_card())
    run_id = led.commit_settlement(batch)
    variances = detect_batch(batch)
    verdicts = [Verdict(payment_id=v.payment_id,
                        exception_code="ZERO_MDR_VIOLATION", action="dispute",
                        confidence=0.95, reasoning="Zero-MDR rail overcharged.",
                        rule_cited="rule 1 - PSS Act s.10A",
                        tool_calls=["rate_card_lookup", "payment_detail"],
                        output_tokens=420, latency_ms=11_000,
                        dispute_text="Please issue a credit note.")
                for v in variances if v.needs_agent]
    decisions = gate_batch(variances, verdicts, led.rate_card())
    from agent.dispute import attach_disputes
    led.store.save_findings(run_id, decisions, variances, verdicts,
                            attach_disputes(variances, verdicts, decisions))

    yield led, run_id
    led.close()


def _text(lines):
    return "\n".join(line.text for line in lines)


# --- it tells the story in the order it happened -------------------------

def test_the_trace_follows_the_pipeline(audited):
    led, run_id = audited
    lines = build(led.store, run_id, led.rate_card())
    text = _text(lines)

    order = ["Opening settlement", "reached the bank", "contract",
             "Checking every one", "settled by the rate card alone",
             "need judgement", "Applying the guardrails"]
    positions = [text.index(phrase) for phrase in order]
    assert positions == sorted(positions), "the trace is out of order"


def test_it_makes_the_split_visible(audited):
    """
    The headline claim is that arithmetic resolves most records and the model is
    spent only on judgment. That is invisible in a findings table, where a
    record settled by a rule looks exactly like one a model reasoned about.
    """
    led, run_id = audited
    text = _text(build(led.store, run_id, led.rate_card()))
    assert "never reach a language model" in text
    assert "need judgement" in text


def test_the_tools_the_agent_called_are_shown(audited):
    led, run_id = audited
    lines = build(led.store, run_id, led.rate_card())
    tools = [line.text for line in lines if line.kind == "tool"]
    assert any(t.startswith("rate_card_lookup()") for t in tools)
    assert any(t.startswith("payment_detail()") for t in tools)


def test_the_review_step_is_reported(audited):
    led, run_id = audited
    text = _text(build(led.store, run_id, led.rate_card()))
    assert "traces back to the engine" in text
    assert "I did not compute any of them" in text


def test_a_correction_is_surfaced_as_a_warning(tmp_path):
    """A run the review step had to correct must not look like a clean one."""
    boot = Ledger(tmp_path / "c.db")
    biz = boot.businesses.create("Corrected Co")
    boot.close()

    led = Ledger(tmp_path / "c.db", biz)
    led.set_behaviour(Behaviour.CARD_RATE_ON_UPI)
    led.capture_payment(led.create_order(162_700, "Scarf"), "upi")
    batch = led.build_settlement(led.rate_card())
    run_id = led.commit_settlement(batch)
    variances = detect_batch(batch)
    verdicts = [Verdict(payment_id=v.payment_id,
                        exception_code="ZERO_MDR_VIOLATION", action="dispute",
                        confidence=0.3, reasoning="x", rule_cited="rule 1",
                        invented_figures=["9999.99"],
                        corrections=["stated figures absent from the evidence"])
                for v in variances if v.needs_agent]
    led.store.save_findings(run_id, gate_batch(variances, verdicts,
                                               led.rate_card()),
                            variances, verdicts)

    lines = build(led.store, run_id, led.rate_card())
    notes = [line.text for line in lines if line.kind == "note"]
    assert any("9999.99" in n for n in notes)
    assert any("confidence has been capped" in n for n in notes)
    led.close()


# --- it cannot invent anything -------------------------------------------

def test_every_figure_appears_in_the_stored_record(audited):
    """
    Rebuilt, not narrated. If the trace could state a number the audit trail
    does not hold, it would be a second record of the same events - and two
    records eventually disagree.
    """
    led, run_id = audited
    text = _text(build(led.store, run_id, led.rate_card()))

    stored = led.store.audit_trail(run_id)[0]
    assert str(stored["output_tokens"]) in text
    assert stored["exception_code"] in text
    for tool in json.loads(stored["tool_calls"]):
        assert tool in text


def test_an_unaudited_settlement_says_so(audited):
    led, run_id = audited
    led.capture_payment(led.create_order(100_000, "Later"), "upi")
    fresh = led.commit_settlement(led.build_settlement(led.rate_card()))

    lines = build(led.store, fresh, led.rate_card())
    assert len(lines) == 1
    assert "not been audited yet" in lines[0].text


def test_a_missing_settlement_fails_loudly(audited):
    led, _ = audited
    lines = build(led.store, "run_does_not_exist", led.rate_card())
    assert lines[0].kind == "fail"


# --- rendering -----------------------------------------------------------

def test_the_terminal_escapes_what_it_renders(audited):
    """Payment ids and reasons are data. Data is not markup."""
    from merchant.trace import Line

    html = terminal([Line("note", "<script>alert(1)</script>")])
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_each_line_carries_its_kind_as_a_class(audited):
    led, run_id = audited
    html = terminal(build(led.store, run_id, led.rate_card()))
    for kind in ("say", "do", "think", "fact", "ok", "tool"):
        assert f'class="at-ln {kind}"' in html


def test_every_line_is_timestamped(audited):
    """The reference this was modelled on stamps every line, and it is what
    makes a transcript readable as a sequence rather than a list."""
    import re

    led, run_id = audited
    for entry in build(led.store, run_id, led.rate_card()):
        assert re.match(r"^\d\d:\d\d:\d\d$", entry.at), entry.text


def test_the_terminal_has_window_chrome_and_a_status(audited):
    led, run_id = audited
    lines = build(led.store, run_id, led.rate_card())

    complete = terminal(lines, status="complete")
    assert 'class="at-dot r"' in complete and 'class="at-dot g"' in complete
    assert "COMPLETE" in complete

    running = terminal(lines, status="running", live=True)
    assert "RUNNING" in running
    assert "at-cursor" in running, "a live terminal should show it is still going"
    assert "at-cursor" not in complete


def test_the_terminal_uses_no_class_that_exists_elsewhere():
    """
    The first version reused `.mark` and `.step` - the rail's logo chip and the
    first-run checklist - and rendered with blue chips and separator lines
    between every line. A shared stylesheet makes generic class names a
    collision waiting to happen.
    """
    from merchant.views import CSS

    for generic in ('class="mark"', 'class="ln ', 'class="step"'):
        assert generic not in terminal([], status="complete")

    # and every terminal rule is namespaced
    terminal_block = CSS[CSS.index("the agent's terminal"):CSS.index("first run")]
    for rule in ("\n.mark ", "\n.step ", "\n.ln "):
        assert rule not in terminal_block


def test_live_narration_and_the_replay_use_the_same_builders():
    """
    The guarantee that keeps them honest. If the runner wrote its own live
    narration and `build` wrote another for the replay, they would drift - and
    then a person would watch one story and read a different one afterwards.

    Both call the functions in merchant.trace, so drift is not possible without
    changing the shared builder.
    """
    import inspect

    import merchant.agents.settlement as runner

    source = inspect.getsource(runner)
    for builder in ("trace.opening", "trace.loaded", "trace.contract",
                    "trace.comparing", "trace.rules_settled",
                    "trace.needs_judgment", "trace.looking_at", "trace.the_gap",
                    "trace.evidence", "trace.tool_call", "trace.verdict",
                    "trace.reviewed_clean", "trace.gate", "trace.drafted",
                    "trace.finished"):
        assert builder in source, f"the runner does not use {builder}"

    # and the runner defines no narration of its own
    assert 'line(' not in source.replace("trace.", "")


def test_the_runner_narrates_nothing_it_did_not_do(audited):
    """
    No tools called means no tool lines. A transcript that invents activity is
    worse than one that is quiet.
    """
    led, run_id = audited
    entry = led.store.audit_trail(run_id)[0]
    import json as _json

    called = _json.loads(entry["tool_calls"])
    lines = build(led.store, run_id, led.rate_card())
    tool_lines = [l for l in lines if l.kind == "tool"]
    assert len(tool_lines) == len(called)


def test_tool_calls_are_narrated_while_the_model_is_still_thinking():
    """
    A classification takes fifteen to twenty seconds. Reporting its tool calls
    only once it returns leaves a watcher staring at a still screen for the
    entire time the interesting part is happening - which is what "it is not
    dynamic" means in practice.

    The classifier now fires on_event DURING the call, so the terminal moves.
    """
    import inspect

    from agent.classifier import ClaudeClassifier
    import merchant.agents.settlement as runner

    signature = inspect.signature(ClaudeClassifier.classify)
    assert "on_event" in signature.parameters

    source = inspect.getsource(ClaudeClassifier.classify)
    # the report happens inside the iteration, not after it
    loop = source.index("for message in runner:")
    assert source.index('report("tool"') > loop
    assert source.index('report("weighing")') < loop

    # and the runner wires it up rather than reading tool_calls afterwards
    wiring = inspect.getsource(runner)
    assert "on_event=live" in wiring
    assert "for tool in verdict.tool_calls" not in wiring


# --- replay --------------------------------------------------------------

def test_pacing_comes_from_the_latency_that_was_measured(audited):
    """
    The pauses in a replay are real pauses. Stored timestamps are per RECORD,
    so replaying straight off them would jump in lumps; the offsets are derived
    from the `latency_ms` the model actually took, which is why watching a
    replay is watching the run rather than an animation of it.
    """
    led, run_id = audited
    lines = build(led.store, run_id, led.rate_card(), paced=True)

    offsets = [l.offset for l in lines]
    assert offsets == sorted(offsets), "playback offsets go backwards"
    assert offsets[0] == 0

    entry = led.store.audit_trail(run_id)[0]
    weighing = next(i for i, l in enumerate(lines) if l.kind == "do"
                    and "Weighing" in l.text)
    verdict_at = next(i for i, l in enumerate(lines) if l.kind == "ok"
                      and "Confidence" in l.text)
    thinking = lines[verdict_at].offset - lines[weighing].offset
    # the gap across the model call reflects the recorded latency
    assert thinking >= entry["latency_ms"] * 0.5


def test_pacing_is_off_unless_asked_for(audited):
    """A static render should not carry playback timing it will never use."""
    led, run_id = audited
    assert all(l.offset == 0
               for l in build(led.store, run_id, led.rate_card()))


def test_a_replay_costs_nothing(audited):
    """
    The whole reason for building it. A replay must not touch the API - not the
    classifier, not the ask endpoint, nothing.
    """
    import inspect

    import merchant.app as appmod

    source = inspect.getsource(appmod.settlement_page)
    replay_block = source[source.index("rp-go"):]
    for forbidden in ("ClaudeClassifier", "anthropic", "/audit/", "classify"):
        assert forbidden not in replay_block, f"replay reaches {forbidden}"


def test_the_replay_renders_the_same_lines_it_would_show_statically(audited):
    """
    Playback is the same transcript, scheduled. If the replay drew from a
    different source it would be a third narration, and three narrations of the
    same events is two too many.
    """
    import json as _json

    led, run_id = audited
    static = build(led.store, run_id, led.rate_card())
    paced = build(led.store, run_id, led.rate_card(), paced=True)

    assert [(l.kind, l.text) for l in static] == [(l.kind, l.text) for l in paced]
    # and the payload the page ships is exactly those lines
    payload = _json.loads(_json.dumps([l.as_dict() for l in paced]))
    assert [p["text"] for p in payload] == [l.text for l in static]


def test_the_narration_names_the_leg_that_actually_moved():
    """
    Caught on a real recording. `delta` is fee plus GST, so describing a GST
    error in terms of the fee produced: "Charged Rs 17.98 where the contract
    allows Rs 17.98. That is Rs 158.58 more than it should be."

    Gibberish - and in the one place the product is meant to be clearer than a
    settlement report.
    """
    from merchant.trace import the_gap

    # GST wrong, fee correct
    gst = the_gap(1_798, 1_798, 15_858, actual_tax=16_182, expected_tax=324)
    assert "fee of Rs 17.98 is correct" in gst.text
    assert "GST is not" in gst.text
    assert "Rs 158.58 too much" in gst.text

    # fee wrong, GST follows it up
    both = the_gap(6_126, 1_885, 5_005, actual_tax=1_103, expected_tax=339)
    assert "the GST follows the inflated fee" in both.text
    assert "Rs 50.05 too much in total" in both.text

    # fee wrong on its own
    fee_only = the_gap(6_126, 1_885, 4_241, actual_tax=339, expected_tax=339)
    assert "Rs 42.41 more than it should be" in fee_only.text

    # nothing moved
    clean = the_gap(35_742, 35_742, 0, actual_tax=6_434, expected_tax=6_434)
    assert "Every number matches" in clean.text
