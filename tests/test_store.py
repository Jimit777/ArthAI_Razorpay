"""
Tests for persistence.

Two things are being protected here. First, money: SQLite will happily accept a
float into an INTEGER column and round it, and Rs 1,627.00 becoming 1626.99
would be the quietest possible catastrophe in a project about rupees. Second,
guardrail 1: nothing in this codebase may mark a finding as reviewed, because
that is a person's decision and a column the system can flip itself is not a
guardrail.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.classifier import Verdict  # noqa: E402
from engine.detector import detect_batch  # noqa: E402
from engine.gate import gate_batch  # noqa: E402
from engine.store import Store, new_run_id  # noqa: E402
from generator.synthetic import generate_batch  # noqa: E402


@pytest.fixture
def stored(tmp_path):
    b, gt = generate_batch(60)
    variances = detect_batch(b)
    verdicts = [Verdict(payment_id=v.payment_id, exception_code=gt[v.payment_id],
                        action="dispute", confidence=0.9, reasoning="because",
                        rule_cited="rule 1", tool_calls=["rate_card_lookup"],
                        output_tokens=400)
                for v in variances if v.needs_agent]
    decisions = gate_batch(variances, verdicts, b.rate_card)

    store = Store(tmp_path / "test.db")
    run_id = store.save_run(b, model="claude-opus-5", effort="medium")
    store.save_findings(run_id, decisions, variances, verdicts)
    yield store, run_id, b, gt, variances, verdicts
    store.close()


# --- money ---------------------------------------------------------------

def test_every_money_column_comes_back_as_an_exact_integer(stored):
    """
    The whole reason this project uses paise. A REAL column would turn
    Rs 1,627.00 into 1626.9999999999998 and nobody would notice until a
    merchant queried a dispute.
    """
    store, run_id, b, *_ = stored
    rows = store.conn.execute(
        "SELECT amount FROM payments WHERE run_id = ?", (run_id,)).fetchall()
    assert rows
    for row in rows:
        assert isinstance(row["amount"], int)

    for table, col in [("settlement_lines", "fee"), ("settlement_lines", "tax"),
                       ("bank_credits", "amount"), ("variances", "delta"),
                       ("variances", "money_at_stake")]:
        for row in store.conn.execute(
                f"SELECT {col} AS v FROM {table} WHERE run_id = ?", (run_id,)):
            assert row["v"] is None or isinstance(row["v"], int), f"{table}.{col}"


def test_amounts_survive_the_round_trip_unchanged(stored):
    store, run_id, b, *_ = stored
    original = {r.record_id: r.payment.amount for r in b.records}
    for row in store.conn.execute(
            "SELECT payment_id, amount FROM payments WHERE run_id = ?", (run_id,)):
        assert row["amount"] == original[row["payment_id"]]


def test_the_bank_credits_still_tie_out_after_a_round_trip(stored):
    """The argument in CLAUDE.md 1.3 has to survive storage."""
    store, run_id, *_ = stored
    per_utr = {}
    for ln in store.conn.execute(
            "SELECT utr, amount, fee, tax FROM settlement_lines WHERE run_id = ?",
            (run_id,)):
        per_utr[ln["utr"]] = per_utr.get(ln["utr"], 0) + ln["amount"] - ln["fee"] - ln["tax"]
    for bc in store.conn.execute(
            "SELECT utr, amount FROM bank_credits WHERE run_id = ?", (run_id,)):
        assert bc["amount"] == per_utr[bc["utr"]]


# --- guardrail 1 ---------------------------------------------------------

def test_nothing_is_ever_stored_as_reviewed(stored):
    """
    Guardrail 1: the agent proposes, a human disposes. Every finding lands
    unreviewed and stays that way until a person says otherwise.
    """
    store, run_id, *_ = stored
    rows = store.conn.execute(
        "SELECT human_reviewed FROM variances WHERE run_id = ?", (run_id,)).fetchall()
    assert rows
    assert all(r["human_reviewed"] == 0 for r in rows)


def test_the_codebase_never_sets_human_reviewed():
    """
    Grep-level guarantee. A column the system can flip itself is not a
    guardrail, it is a default.
    """
    root = Path(__file__).parent.parent
    for path in list(root.glob("*.py")) + list(root.glob("engine/*.py")) \
            + list(root.glob("agent/*.py")) + list(root.glob("generator/*.py")):
        text = path.read_text()
        assert "human_reviewed = 1" not in text, path
        assert "human_reviewed=1" not in text, path


def test_dispute_text_starts_empty(stored):
    """Checkpoint 8 fills this. Nothing should be inventing one yet."""
    store, run_id, *_ = stored
    rows = store.conn.execute(
        "SELECT dispute_text FROM variances WHERE run_id = ?", (run_id,)).fetchall()
    assert all(r["dispute_text"] is None for r in rows)


# --- the audit trail -----------------------------------------------------

def test_every_agent_decision_is_logged(stored):
    """Guardrail 5: timestamped and replayable."""
    store, run_id, b, gt, variances, verdicts = stored
    logged = store.audit_trail(run_id)
    assert len(logged) == len(verdicts)
    for row in logged:
        assert row["decided_at"] > 0
        assert row["model"]
        assert row["reasoning"]


def test_the_audit_trail_keeps_the_evidence_the_agent_saw(stored):
    """
    Replayable means you can see what it was looking at, not just what it said.
    A reasoning trace with no record of the inputs cannot be audited.
    """
    store, run_id, b, gt, variances, verdicts = stored
    by_id = {v.payment_id: v for v in variances}
    for row in store.audit_trail(run_id):
        signals = json.loads(row["signals"])
        assert len(signals) == len(by_id[row["payment_id"]].signals)
        for s in signals:
            assert s["source"] and s["rule"]


def test_tool_calls_are_recorded(stored):
    store, run_id, *_ = stored
    for row in store.audit_trail(run_id):
        assert json.loads(row["tool_calls"]) == ["rate_card_lookup"]


def test_the_trail_can_be_pulled_for_one_payment(stored):
    store, run_id, b, gt, variances, verdicts = stored
    pid = verdicts[0].payment_id
    rows = store.audit_trail(run_id, payment_id=pid)
    assert len(rows) == 1
    assert rows[0]["payment_id"] == pid


# --- reading it back -----------------------------------------------------

def test_a_run_can_be_listed_and_found_again(stored):
    store, run_id, b, *_ = stored
    runs = store.list_runs()
    assert len(runs) == 1
    assert runs[0]["run_id"] == run_id
    assert runs[0]["seed"] == b.seed
    assert runs[0]["n_records"] == 60
    assert runs[0]["findings"] == 60
    assert store.latest_run_id() == run_id


def test_findings_come_back_biggest_money_first(stored):
    """The merchant should see the Rs 8,000 problem before the Rs 12 one."""
    store, run_id, *_ = stored
    stakes = [r["money_at_stake"] for r in store.findings(run_id)]
    assert stakes == sorted(stakes, reverse=True)


def test_the_human_queue_can_be_pulled_on_its_own(stored):
    store, run_id, *_ = stored
    queued = store.findings(run_id, queued_only=True)
    assert queued
    assert all(r["queued_for_human"] == 1 for r in queued)
    assert len(queued) < len(store.findings(run_id))


def test_queue_reasons_are_stored_not_just_the_flag(stored):
    """"Needs a human" without saying why is not a queue, it is a shrug."""
    store, run_id, *_ = stored
    for row in store.findings(run_id, queued_only=True):
        assert json.loads(row["queue_reasons"])


def test_totals_match_the_findings(stored):
    store, run_id, *_ = stored
    totals = store.totals(run_id)
    assert totals["n"] == 60
    assert totals["queued"] == len(store.findings(run_id, queued_only=True))
    assert totals["recoverable_paise"] > 0


def test_the_rate_card_is_stored_with_the_run(stored):
    """
    An audit has to stay reproducible after the contract is renegotiated. If
    the rate card lived only in a config file, last month's findings would
    silently re-interpret themselves against this month's rates.
    """
    store, run_id, b, *_ = stored
    rows = store.conn.execute(
        "SELECT * FROM rate_card WHERE run_id = ?", (run_id,)).fetchall()
    assert {r["instrument"] for r in rows} == set(b.rate_card["instruments"])
    assert all(r["source"] for r in rows)


# --- many runs in one file ----------------------------------------------

def test_two_runs_do_not_collide(tmp_path):
    store = Store(tmp_path / "many.db")
    a, _ = generate_batch(60, seed=1)
    b, _ = generate_batch(60, seed=2)
    run_a = store.save_run(a)
    run_b = store.save_run(b)

    assert run_a != run_b
    assert len(store.list_runs()) == 2
    for run in (run_a, run_b):
        n = store.conn.execute(
            "SELECT COUNT(*) AS n FROM payments WHERE run_id = ?", (run,)).fetchone()
        assert n["n"] == 60
    store.close()


def test_the_same_seed_can_be_stored_twice(tmp_path):
    """Re-running an audit is normal. It must not collide with its own history."""
    store = Store(tmp_path / "same.db")
    b, _ = generate_batch(60, seed=7)
    first = store.save_run(b)
    second = store.save_run(b)
    assert first != second
    assert len(store.list_runs()) == 2
    store.close()


def test_opening_an_existing_file_does_not_destroy_it(tmp_path):
    path = tmp_path / "reopen.db"
    store = Store(path)
    b, _ = generate_batch(60)
    run_id = store.save_run(b)
    store.close()

    again = Store(path)
    assert again.latest_run_id() == run_id
    again.close()


# --- resolution memory ---------------------------------------------------

def test_resolutions_survive_across_runs(tmp_path):
    """CLAUDE.md section 12: the thing that looks like the system learning."""
    path = tmp_path / "memory.db"
    store = Store(path)
    store.remember_resolution("ZERO_MDR_VIOLATION", "pay_x",
                              "gateway credited it back within a week")
    store.close()

    later = Store(path)
    hits = later.resolutions("ZERO_MDR_VIOLATION")
    assert len(hits) == 1
    assert "credited it back" in hits[0]["resolution"]
    assert later.resolutions("GST_MISMATCH") == []
    later.close()


def test_resolutions_scoped_to_one_business_do_not_leak(tmp_path):
    """
    The multi-tenant merchant app always passes business_id. Without this,
    one merchant's confirmed resolution note - which can name their own
    accountant, ticket numbers, anything - would be recalled for a different
    merchant's variance carrying the same exception code.
    """
    store = Store(tmp_path / "memory.db")
    store.remember_resolution("ZERO_MDR_VIOLATION", "pay_a",
                              "confirmed with Priya, our accountant",
                              business_id="biz_a")
    store.remember_resolution("ZERO_MDR_VIOLATION", "pay_b",
                              "this is our monthly AMC", business_id="biz_b")

    a_only = store.resolutions("ZERO_MDR_VIOLATION", business_id="biz_a")
    assert len(a_only) == 1
    assert a_only[0]["payment_id"] == "pay_a"

    b_only = store.resolutions("ZERO_MDR_VIOLATION", business_id="biz_b")
    assert len(b_only) == 1
    assert b_only[0]["payment_id"] == "pay_b"

    nobody = store.resolutions("ZERO_MDR_VIOLATION", business_id="biz_c")
    assert nobody == []

    # business_id=None (the default) is the original single-business tool's
    # behaviour: no filter, because there is nothing to filter against.
    everyone = store.resolutions("ZERO_MDR_VIOLATION")
    assert len(everyone) == 2
    store.close()


def test_an_unscoped_resolution_defaults_to_the_empty_business(tmp_path):
    """The CLI tool never passes business_id - it should not have to."""
    store = Store(tmp_path / "memory.db")
    store.remember_resolution("ZERO_MDR_VIOLATION", "pay_x", "noted")
    row = store.resolutions("ZERO_MDR_VIOLATION")[0]
    assert row["business_id"] == ""
    store.close()


def test_run_ids_are_unique_even_in_the_same_millisecond():
    """
    A bare millisecond timestamp collided the first time two runs were saved in
    a loop. My original version of this test asserted `>= 1`, which is true of
    any non-empty set and proves nothing - it passed while the bug was live.

    It then went intermittently red, roughly once in a hundred runs, and that
    was the test being right rather than flaky: a three-byte suffix carries
    about a one percent chance of a collision across 500 ids generated inside
    one millisecond. Widened to four. Ten thousand here rather than five
    hundred, so a suffix that is too short fails every time instead of
    occasionally.
    """
    assert len({new_run_id() for _ in range(10_000)}) == 10_000


def test_run_ids_sort_in_time_order():
    import time as _t
    first = new_run_id()
    _t.sleep(0.002)
    assert first < new_run_id()


def test_a_dispute_message_is_stored_with_its_finding(tmp_path):
    """Checkpoint 8 output has to survive the run that produced it."""
    from engine.gate import gate_batch

    b, gt = generate_batch(60)
    variances = detect_batch(b)
    verdicts = [Verdict(payment_id=v.payment_id, exception_code="ZERO_MDR_VIOLATION",
                        action="dispute", confidence=0.9, reasoning="because",
                        rule_cited="rule 1",
                        dispute_text="Please credit the difference.")
                for v in variances if v.needs_agent]
    decisions = gate_batch(variances, verdicts, b.rate_card)

    from agent.dispute import attach_disputes
    disputes = attach_disputes(variances, verdicts, decisions)
    assert disputes

    store = Store(tmp_path / "d.db")
    run_id = store.save_run(b)
    store.save_findings(run_id, decisions, variances, verdicts, disputes)

    stored = {r["payment_id"]: r["dispute_text"] for r in store.findings(run_id)}
    for pid, message in disputes.items():
        assert stored[pid] == message
        assert "--- Reference details ---" in stored[pid]

    # and nothing was invented for the records that had no message
    assert any(v is None for v in stored.values())
    store.close()
