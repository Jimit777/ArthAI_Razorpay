"""
Tests for the measured-accuracy page.

The page exists because the track asks for a match rate and an honest list of
what could not be resolved. Most of what is worth testing here is not that the
number appears, but the circumstances in which the page must refuse to show
one: a recording that belongs to a different batch, and a run where the API
calls failed.
"""

import json
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent))

from merchant.accesslog import Action  # noqa: E402
from merchant.benchmark import (Benchmarks, DEFAULT_N, DEFAULT_SEED,  # noqa: E402
                                FREE_MODES, GST, MODE_LIVE, MODE_REPLAY,
                                SETTLEMENT, run_benchmark)

PASSWORD = "a-good-password"
RECORDING = str(Path(__file__).parent.parent / "demo_run.json")


@pytest.fixture
def client(tmp_path, monkeypatch):
    import merchant.app as appmod

    monkeypatch.setattr(appmod, "DB", str(tmp_path / "app.db"))
    return TestClient(appmod.app)


@pytest.fixture
def op(client):
    client.post("/signup", data={"email": "op@x.in", "password": PASSWORD})
    return client


def _settle(client, timeout=20):
    """Wait for the background benchmark thread."""
    import merchant.app as appmod

    deadline = time.time() + timeout
    while time.time() < deadline:
        if appmod.BENCH and all(v.state != "running"
                                for v in appmod.BENCH.values()):
            return
        time.sleep(0.05)
    raise AssertionError("benchmark did not finish")


def _rows():
    import merchant.app as appmod

    with appmod.ledger() as led:
        return Benchmarks(led.conn).history(50)


# --- the measurement itself ----------------------------------------------

def test_replaying_the_recording_scores_the_batch():
    card, ms = run_benchmark(mode=MODE_REPLAY, recording=RECORDING)
    assert card.total == DEFAULT_N
    assert card.correct == card.total
    assert card.by_calculator + card.by_agent == card.total
    assert not card.false_accusations


def test_a_replay_costs_nothing_and_is_marked_as_such():
    assert MODE_REPLAY in FREE_MODES
    assert MODE_LIVE not in FREE_MODES


def test_the_calculator_and_the_agent_are_reported_separately():
    """
    A headline that quietly includes records the model never saw is flattering
    unless the split is stated.
    """
    card, _ = run_benchmark(mode=MODE_REPLAY, recording=RECORDING)
    assert card.by_calculator > 0
    assert card.by_agent > 0


def test_planted_anomalies_and_decoys_are_both_counted():
    card, _ = run_benchmark(mode=MODE_REPLAY, recording=RECORDING)
    assert card.anomalies > 0
    assert card.decoys > 0


# --- when it must refuse to produce a number -----------------------------

def test_a_recording_from_a_different_batch_is_refused(tmp_path):
    """
    Scoring one set of answers against another set of questions would still
    produce a percentage. That is the worst possible failure for this page.
    """
    with open(RECORDING) as f:
        verdicts = json.load(f)
    short = tmp_path / "wrong.json"
    short.write_text(json.dumps(verdicts[:5]))

    with pytest.raises(ValueError) as caught:
        run_benchmark(mode=MODE_REPLAY, recording=str(short))
    assert "different batch" in str(caught.value)


def test_a_recording_that_does_not_exist_is_refused(tmp_path):
    with pytest.raises(ValueError):
        run_benchmark(mode=MODE_REPLAY, recording=str(tmp_path / "nope.json"))


def test_an_unknown_mode_is_refused():
    with pytest.raises(ValueError):
        run_benchmark(mode="rules")


def test_a_run_with_failed_calls_is_labelled_an_outage(op):
    """
    Failed calls mean those records were escalated rather than judged. The page
    must say the number measures an outage instead of printing it plainly.
    """
    import merchant.app as appmod

    card, _ = run_benchmark(mode=MODE_REPLAY, recording=RECORDING)
    card.failed_calls = 4
    with appmod.ledger() as led:
        Benchmarks(led.conn).record(
            card, mode=MODE_REPLAY, n=card.total, seed=DEFAULT_SEED,
            model="opus", effort="medium", duration_ms=10, ran_by="op@x.in")

    page = op.get("/admin/accuracy").text
    assert "do not quote" in page
    assert "failed to run" in page


# --- the page ------------------------------------------------------------

def test_the_page_is_operator_only(client):
    client.post("/signup", data={"email": "first@x.in", "password": PASSWORD})
    client.post("/signup", data={"email": "second@x.in", "password": PASSWORD})
    client.post("/login", data={"email": "second@x.in", "password": PASSWORD})
    assert client.get("/admin/accuracy").status_code == 403


def test_a_signed_out_visitor_cannot_run_a_benchmark(client):
    client.post("/signup", data={"email": "op@x.in", "password": PASSWORD})
    client.get("/logout")
    client.post("/admin/accuracy/run", data={"mode": "replay"})
    assert _rows() == []


def test_the_page_says_nothing_measured_before_the_first_run(op):
    assert "Nothing measured yet" in op.get("/admin/accuracy").text


def test_running_it_stores_a_result_and_shows_the_match_rate(op):
    op.post("/admin/accuracy/run",
            data={"mode": "replay", "agent_id": SETTLEMENT})
    _settle(op)
    rows = _rows()
    assert len(rows) == 1
    assert rows[0]["correct"] == rows[0]["total"] == DEFAULT_N
    assert rows[0]["agent_id"] == SETTLEMENT
    page = op.get("/admin/accuracy").text
    assert "100.0%" in page
    # Scoped to this agent's block. The other agent has not been run, and its
    # block correctly still says so - a page-wide assertion would call that a
    # failure when it is the honest state.
    block = page.split("Settlement Deduction Auditor")[1]
    block = block.split("GST Input Credit Reconciler")[0]
    assert "Nothing measured yet" not in block


def test_each_agent_is_measured_separately(op):
    """
    Two agents measured on two different kinds of batch do not average into a
    meaningful number, so they are never pooled.
    """
    page = op.get("/admin/accuracy").text
    assert "Settlement Deduction Auditor" in page
    assert "GST Input Credit Reconciler" in page


def test_a_result_for_one_agent_is_not_shown_against_the_other(op):
    op.post("/admin/accuracy/run",
            data={"mode": "replay", "agent_id": SETTLEMENT})
    _settle(op)
    page = op.get("/admin/accuracy").text
    gst_block = page.split("GST Input Credit Reconciler")[1]
    assert "Nothing measured yet" in gst_block


def test_replaying_an_agent_with_no_recording_is_refused(op, tmp_path,
                                                         monkeypatch):
    """
    Points the agent at a path that does not exist rather than assuming none of
    them have been recorded. This test failed the moment the GST agent was run
    for real - which is the test depending on the state of the repo instead of
    on the behaviour it is meant to check.
    """
    from merchant.benchmark import BENCHMARK_AGENTS

    monkeypatch.setitem(BENCHMARK_AGENTS[GST], "recording",
                        str(tmp_path / "nothing-here.json"))
    r = op.post("/admin/accuracy/run",
                data={"mode": "replay", "agent_id": GST},
                follow_redirects=False)
    assert "error=" in r.headers["location"]
    assert _rows() == []


def test_an_unknown_agent_is_refused(op):
    op.post("/admin/accuracy/run",
            data={"mode": "replay", "agent_id": "not_an_agent"})
    assert _rows() == []


def test_every_run_is_kept_including_a_bad_one(op):
    """A benchmark you can re-run until it flatters you is not a measurement."""
    for _ in range(3):
        op.post("/admin/accuracy/run", data={"mode": "replay"})
        _settle(op)
    assert len(_rows()) == 3
    assert "Every run" in op.get("/admin/accuracy").text


def test_running_live_without_an_api_key_is_refused_not_attempted(op, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    r = op.post("/admin/accuracy/run", data={"mode": "live"},
                follow_redirects=False)
    assert "error=" in r.headers["location"]
    assert _rows() == []


def test_a_benchmark_run_is_recorded_in_the_access_log(op):
    import merchant.app as appmod

    op.post("/admin/accuracy/run", data={"mode": "replay"})
    _settle(op)
    with appmod.ledger() as led:
        rows = led.conn.execute(
            "SELECT * FROM access_log WHERE action = ?",
            (str(Action.RUN_BENCHMARK),)).fetchall()
    assert len(rows) == 1
    assert rows[0]["email"] == "op@x.in"


def test_the_page_explains_why_merchants_do_not_see_a_match_rate(op):
    """
    The reason is the product's whole thesis: real settlements have no answer
    key. If that explanation ever disappears, the number looks arbitrary.
    """
    page = op.get("/admin/accuracy").text
    assert "no answer key" in page or "do not" in page
    assert "invented" in page
