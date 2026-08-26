"""
Tests for the three-way reconciliation agent.

Two things run through all of it.

The match rate is MEASURED. The generator returns an answer key, so every
number this feature puts on a screen is checked against what each record was
built to be - and a matcher that quietly stops finding the hard cases fails
here rather than producing a slightly worse figure nobody notices.

And no model touches the join. The specification called the second pass
"fuzzy / AI logic"; it is a bounded search over amounts and dates and it is
deterministic, because a match rate that changes between runs on identical
data is worth nothing. What the agent does is explain the leftovers.
"""

import sys
import time
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.recon_agent import (ReconJudgment, recommended_action,  # noqa: E402
                               render, review, strict_schema,
                               unverified_figures)
from engine.recon.generator import generate  # noqa: E402
from engine.recon.matcher import (TOLERANCE_PAISE, WINDOW_DAYS,  # noqa: E402
                                  reconcile)
from engine.recon.records import (ACTION_CHASE, ACTION_DISPUTE,  # noqa: E402
                                  ACTION_INVESTIGATE, ACTION_NONE,
                                  ACTION_WRITE_OFF, AMOUNT_MISMATCH, MATCHED,
                                  MATCHED_FUZZY, MISSING_IN_BANK,
                                  MISSING_IN_GATEWAY, ORPHAN_BANK_CREDIT,
                                  UNEXPLAINED_FEE, BankCredit, Invoice,
                                  ReconBatch, ReconRow, Settlement)
from merchant.recon_pipeline import run, score  # noqa: E402

PASSWORD = "a-good-password"


# --- the batch is big enough to mean something ----------------------------

def test_the_batch_clears_the_fifty_record_bar():
    batch, _truth = generate(55)
    assert len(batch.invoices) >= 50
    assert batch.total_records >= 150, "three sources, not one"


def test_every_discrepancy_kind_is_planted():
    """
    A batch that happens to contain no orphan credit would let the matcher
    silently lose the ability to find one. Dealt round-robin so every kind
    appears at any batch size.
    """
    _batch, truth = generate(55)
    planted = Counter(truth.values())
    for kind in (MATCHED, MATCHED_FUZZY, MISSING_IN_BANK, MISSING_IN_GATEWAY,
                 AMOUNT_MISMATCH, UNEXPLAINED_FEE, ORPHAN_BANK_CREDIT):
        assert planted[kind] >= 1, f"{kind} never planted: {dict(planted)}"


def test_most_records_are_clean():
    """
    Eighty per cent clean is the point. A demo where half the records are
    broken looks like a data-quality problem rather than a reconciliation.
    """
    _batch, truth = generate(55)
    planted = Counter(truth.values())
    assert planted[MATCHED] / sum(planted.values()) > 0.7


def test_the_batch_is_the_same_every_time():
    """A match rate that moved between runs would be worth nothing."""
    first, truth_a = generate(55)
    second, truth_b = generate(55)
    assert truth_a == truth_b
    assert [i.invoice_id for i in first.invoices] == \
        [i.invoice_id for i in second.invoices]
    assert [s.net_settled for s in first.settlements] == \
        [s.net_settled for s in second.settlements]


# --- the join reproduces the answer key -----------------------------------

def test_the_matcher_finds_exactly_what_was_planted():
    """
    THE test. Every line classified as the generator built it, or the match
    rate on the page is a claim rather than a measurement.
    """
    batch, truth = generate(55)
    rows, _stats = reconcile(batch)
    result = score(rows, truth)

    assert result["wrong"] == 0, result["misses"]
    assert result["accuracy_percentage"] == 100.0
    assert result["records_with_a_known_answer"] >= 50


def test_all_three_passes_do_work():
    """
    A pass that never fires is dead code every test walks past. Pass 3 in
    particular existed and did nothing until the generator started producing
    statements whose UTR column carries the bank's own reference.
    """
    batch, _truth = generate(55)
    _rows, stats = reconcile(batch)

    assert stats.exact > 0
    assert stats.windowed > 0, "no reference-less line was joined on amount"
    assert stats.narration > 0, "the narration parser never fired"


def test_the_match_rate_is_reported_honestly():
    batch, truth = generate(55)
    result = run(batch, truth=truth, use_agent=False)
    metrics = result.as_dict()["match_metrics"]

    assert 80 <= metrics["match_rate_percentage"] <= 95
    assert metrics["successful_matches_count"] + metrics["exception_count"] \
        == len(result.rows)


# --- the rules that keep the join from guessing ---------------------------

def test_two_identical_settlements_are_left_unresolved():
    """
    The rule that keeps Pass 2 honest.

    Two settlements of the same amount in the same week is completely ordinary
    for a merchant with a fixed-price product. Picking the nearer one would be
    a coin toss presented as a reconciliation, so ambiguity is reported.
    """
    when = date(2026, 7, 1)
    batch = ReconBatch(
        invoices=[Invoice("INV-1", "Sunrise Retail", 500000, when)],
        settlements=[
            Settlement("pay_a", 500000, 11800, 488200, when + timedelta(days=2)),
            Settlement("pay_b", 500000, 11800, 488200, when + timedelta(days=2)),
        ], bank=[])

    rows, stats = reconcile(batch)
    invoice_row = next(r for r in rows if r.invoice)
    assert invoice_row.finding == MISSING_IN_GATEWAY
    assert stats.windowed == 0


def test_a_credit_outside_the_window_is_not_claimed():
    when = date(2026, 7, 1)
    batch = ReconBatch(
        invoices=[Invoice("INV-1", "Sunrise Retail", 500000, when)],
        settlements=[Settlement("pay_a", 500000, 11800, 488200,
                                when + timedelta(days=2),
                                invoice_reference="INV-1")],
        bank=[BankCredit("HDFCN1", "NEFT-OTHER", 488200,
                         when + timedelta(days=2 + WINDOW_DAYS + 4))])

    rows, _stats = reconcile(batch)
    assert rows[0].finding == MISSING_IN_BANK
    # And the credit is reported rather than dropped.
    assert any(r.finding == ORPHAN_BANK_CREDIT for r in rows)


def test_a_short_fragment_does_not_link_a_credit():
    """
    A four-character overlap between two reference numbers in one statement is
    not evidence of anything. The narration pass refuses fragments that short
    rather than inventing a link.
    """
    from engine.recon.matcher import _narration_points_at

    assert _narration_points_at("NEFT-RAZORPAY-HDFCN1234567890",
                                "HDFCN1234567890")
    assert _narration_points_at("MB-NEFT-HDFCN12345678-RZPY",
                                "HDFCN1234567890")
    assert not _narration_points_at("NEFT-INWARD-MISC-ABCD", "HDFCN1234567890")


def test_a_sub_rupee_difference_is_not_an_exception():
    """Too tight a tolerance makes an exception list nobody reads."""
    when = date(2026, 7, 1)
    batch = ReconBatch(
        invoices=[Invoice("INV-1", "Sunrise Retail", 500000, when)],
        settlements=[Settlement("pay_a", 500000, 11800, 488200,
                                when + timedelta(days=2),
                                invoice_reference="INV-1", utr="HDFCN1")],
        bank=[BankCredit("HDFCN1", "NEFT", 488200 - TOLERANCE_PAISE,
                         when + timedelta(days=2))])

    rows, _ = reconcile(batch)
    assert rows[0].finding == MATCHED


def test_money_that_arrived_is_never_dropped():
    """
    A credit nothing accounts for is as likely to be somebody else's money as
    it is to be a windfall, and a matcher that ignores it silently loses it.
    """
    batch, truth = generate(55)
    rows, _ = reconcile(batch)
    orphans = [r for r in rows if r.finding == ORPHAN_BANK_CREDIT]
    assert orphans
    assert all(r.at_stake > 0 for r in orphans)


# --- the action is arithmetic, not the agent's ----------------------------

@pytest.mark.parametrize("finding,expected", [
    (MATCHED, ACTION_NONE),
    (MATCHED_FUZZY, ACTION_NONE),
    (MISSING_IN_BANK, ACTION_CHASE),
    (AMOUNT_MISMATCH, ACTION_DISPUTE),
    (MISSING_IN_GATEWAY, ACTION_INVESTIGATE),
    (ORPHAN_BANK_CREDIT, ACTION_INVESTIGATE),
])
def test_each_finding_has_one_settled_action(finding, expected):
    assert recommended_action(ReconRow(finding=finding, variance=50_000)) \
        == expected


def test_a_small_shortfall_is_written_off_and_a_large_one_disputed():
    """
    Disputing a Rs 12 bank charge costs more than it recovers, and saying so
    is more useful than flagging it. The threshold is stated where it can be
    argued with.
    """
    assert recommended_action(
        ReconRow(finding=UNEXPLAINED_FEE, variance=-1_180)) == ACTION_WRITE_OFF
    assert recommended_action(
        ReconRow(finding=UNEXPLAINED_FEE, variance=-90_000)) == ACTION_DISPUTE


def test_the_action_does_not_change_between_runs():
    batch, _ = generate(55)
    first = [(r.finding, recommended_action(r)) for r in reconcile(batch)[0]]
    for _ in range(4):
        again = [(r.finding, recommended_action(r)) for r in reconcile(batch)[0]]
        assert again == first


# --- the agent may explain, never compute ---------------------------------

def test_the_schema_has_nowhere_to_put_a_number():
    fields = strict_schema()["properties"]
    assert set(fields) == {"action", "headline", "reasoning",
                           "likeliest_cause"}
    for spec in fields.values():
        assert spec.get("type") != "number"
        assert spec.get("type") != "integer"


def test_an_invented_figure_discards_the_explanation():
    """
    The one number a merchant must never read is one the model made up. The
    arithmetic's own wording is substituted rather than the prose being
    printed with a warning next to it.
    """
    row = ReconRow(finding=AMOUNT_MISMATCH, variance=-88_000,
                   detail="The gateway settled Rs 12,522.33 and the bank "
                          "credited Rs 11,642.33 - Rs 880.00 short.")
    judged = ReconJudgment(action="dispute", headline="short",
                           reasoning="They kept Rs 4,321.00 as a hidden fee.")

    verdict = review(row, judged, render(row))
    assert verdict.invented_figures
    assert verdict.reasoning == row.detail
    assert not verdict.headline


def test_the_agent_may_not_relax_the_action():
    """It proposes; the figures dispose. The severity ladder is one-way."""
    row = ReconRow(finding=MISSING_IN_BANK, variance=488_200,
                   detail="settled and never credited")
    relaxed = ReconJudgment(action="write_off", headline="probably fine",
                            reasoning="settled and never credited")

    verdict = review(row, relaxed, render(row))
    assert verdict.action == ACTION_CHASE
    assert verdict.agent_action == "write_off"
    assert verdict.corrections


def test_the_agent_going_further_is_recorded_not_discarded():
    row = ReconRow(finding=UNEXPLAINED_FEE, variance=-1_180,
                   detail="short by a small amount")
    stricter = ReconJudgment(action="dispute", headline="worth a look",
                             reasoning="short by a small amount")

    verdict = review(row, stricter, render(row))
    assert verdict.action == ACTION_WRITE_OFF
    assert verdict.goes_further is True


def test_whole_rupee_formatting_is_not_an_invention():
    supplied = "Rs 27,000.00 settled"
    assert unverified_figures("They settled Rs 27,000.", supplied) == []


def test_the_evidence_names_all_three_sources():
    batch, _ = generate(55)
    rows, _ = reconcile(batch)
    exception = next(r for r in rows if not r.resolved)
    evidence = render(exception)

    assert "ERP INVOICE" in evidence
    assert "GATEWAY" in evidence
    assert "BANK" in evidence


# --- the run works without a model at all ---------------------------------

def test_the_exception_list_is_actionable_without_the_agent():
    """
    The agent adds an explanation to a complete answer; it is never a
    precondition for one.
    """
    batch, truth = generate(55)
    payload = run(batch, truth=truth, use_agent=False).as_dict()

    assert payload["metadata"]["usage"]["usd"] == 0
    assert payload["exception_list"]
    for row in payload["exception_list"]:
        assert row["action"]
        assert row["detail"]
        assert row["at_stake"] >= 0


def test_the_agent_is_only_asked_about_exceptions():
    """
    Fifty calls to be told fifty times that three numbers agree is money spent
    to learn nothing. The clean lines cost nothing.
    """
    asked = []

    class Counting:
        def judge_all(self, rows, on_each=None):
            asked.extend(rows)
            return []

    batch, truth = generate(55)
    result = run(batch, truth=truth, use_agent=True, agent=Counting())

    assert len(asked) == len(result.exceptions)
    assert len(asked) < len(result.rows) / 2


# --- the payload the spec asked for ---------------------------------------

def test_the_payload_carries_every_section():
    batch, truth = generate(55)
    payload = run(batch, truth=truth, use_agent=False).as_dict()

    assert set(payload) == {"metadata", "match_metrics", "matched_records",
                            "exception_list", "vocabulary"}
    assert payload["metadata"]["total_records_processed"] >= 150
    assert payload["metadata"]["processing_time_ms"] >= 0

    metrics = payload["match_metrics"]
    for field in ("successful_matches_count", "exception_count",
                  "match_rate_percentage"):
        assert field in metrics


def test_every_matched_record_joins_three_ids():
    """The artefact somebody would actually hand an auditor."""
    batch, truth = generate(55)
    payload = run(batch, truth=truth, use_agent=False).as_dict()

    assert payload["matched_records"]
    for row in payload["matched_records"]:
        assert row["txn_id"]
        assert row["utr_number"]
        # A fuzzy match may have no invoice behind it; that is the finding.
        assert row["finding_type"] in (MATCHED, MATCHED_FUZZY)
        if row["finding_type"] == MATCHED:
            assert row["invoice_id"]


def test_every_exception_is_categorised_and_priced():
    batch, truth = generate(55)
    payload = run(batch, truth=truth, use_agent=False).as_dict()

    for row in payload["exception_list"]:
        assert row["finding_type"] in {
            MISSING_IN_BANK, MISSING_IN_GATEWAY, AMOUNT_MISMATCH,
            UNEXPLAINED_FEE, ORPHAN_BANK_CREDIT}
        assert row["finding_label"]
        assert row["action_label"]


# --- the page -------------------------------------------------------------

@pytest.fixture
def shop(tmp_path, monkeypatch):
    import merchant.app as appmod

    monkeypatch.setattr(appmod, "DB", str(tmp_path / "app.db"))
    client = TestClient(appmod.app)
    client.post("/signup", data={"email": "meera@x.in", "password": PASSWORD})
    client.post("/businesses", data={"name": "Meera's Boutique"})
    return client


def _run(shop, timeout=30):
    import merchant.app as appmod

    response = shop.post("/agents/three-way/run", data={"use_agent": "no"},
                         follow_redirects=False)
    key = response.headers["location"].split("key=")[-1]
    deadline = time.time() + timeout
    while time.time() < deadline:
        with appmod._recon_lock:
            state = dict(appmod.RECON_RUNS.get(key) or {})
        if state.get("state") != "running":
            return key, state
        time.sleep(0.05)
    raise AssertionError("the run never finished")


def test_the_landing_page_offers_one_button(shop):
    page = shop.get("/agents/three-way").text
    assert "Run reconciliation" in page
    assert "ERP invoices" in page and "Bank credits" in page


def test_a_run_reaches_the_dashboard(shop):
    key, state = _run(shop)
    assert state["state"] == "done", state

    page = shop.get(f"/agents/three-way?key={key}").text
    assert "auto-reconciled" in page
    assert "records audited" in page
    assert "Needs your decision" in page


def test_the_exception_table_prices_and_offers_actions(shop):
    key, state = _run(shop)
    page = shop.get(f"/agents/three-way?key={key}").text

    for label in ("Write off", "Dispute", "Investigate"):
        assert label in page
    assert "At stake" in page
    # The run key reaches the buttons, or every one of them fails.
    assert f'name="key" value="{key}"' in page
    assert 'name="key" value=""' not in page


def test_a_decision_is_recorded_and_nothing_is_posted(shop):
    """
    The guardrail. A button that quietly wrote something off would break the
    thing the whole platform rests on.
    """
    import merchant.app as appmod

    key, state = _run(shop)
    line = state["payload"]["exception_list"][0]
    target = line["invoice_id"] or line["txn_id"] or line["utr_number"]

    response = shop.post("/agents/three-way/decide",
                         data={"key": key, "line": target,
                               "decision": "write_off"},
                         follow_redirects=False)
    assert response.status_code == 303
    assert "ok=" in response.headers["location"]
    assert appmod.RECON_DECISIONS[key][target] == "write_off"

    page = shop.get(response.headers["location"]).text
    assert "Nothing was posted anywhere" in page


def test_the_matched_tab_shows_the_three_ids(shop):
    key, _state = _run(shop)
    page = shop.get(f"/agents/three-way/matched?key={key}").text

    assert "Matched lines" in page
    for header in ("Invoice", "Gateway txn", "Bank UTR"):
        assert header in page


def test_the_json_payload_is_available_on_its_own(shop):
    key, _state = _run(shop)
    payload = shop.get(f"/agents/three-way/{key}.json").json()

    assert payload["match_metrics"]["match_rate_percentage"] > 0
    assert payload["match_metrics"]["accuracy"]["wrong"] == 0


def test_an_unknown_run_is_a_404_not_an_empty_dashboard(shop):
    assert shop.get("/agents/three-way/nope.json").status_code == 404


def test_the_agent_is_registered_as_the_third_live_one(shop):
    from merchant.catalog import live_agents

    ids = {a.id for a in live_agents()}
    assert "three_way_recon" in ids
    page = shop.get("/agents").text
    assert "Three-Way Reconciliation" in page


def test_the_progress_screen_names_the_right_agent(shop):
    """
    Regression. The progress screen was reused verbatim from the supplier risk
    agent, so a three-way run told the merchant it was "working through your
    suppliers" and lit up the wrong agent in the rail. A page that lies about
    which agent is running is worse than no progress screen.
    """
    import merchant.app as appmod

    head = "<h1>x</h1>"
    shell = {"business": None, "businesses": []}
    page = appmod._risk_running(
        {"phase": "Joining", "done": 1, "total": 6}, head, shell,
        title="Three-way reconciliation", active="agent:three_way_recon",
        doing="Joining your invoices, settlements and bank credits")

    assert "Joining your invoices" in page
    assert "Working through your suppliers" not in page
    assert "Three-way reconciliation" in page


def test_switching_tabs_does_not_lose_the_run(shop):
    """
    Regression. The tab links are generic and carry no run key, so clicking
    Matched showed "run a reconciliation first" over results that were sitting
    in memory. A merchant who navigates away and back should find their work.
    """
    key, _state = _run(shop)

    # No key in the URL at all - exactly what the tab link produces.
    matched = shop.get("/agents/three-way/matched").text
    assert "Matched lines" in matched
    assert "Run a reconciliation first" not in matched

    back = shop.get("/agents/three-way").text
    assert "Needs your decision" in back
    assert "Run reconciliation" not in back


def test_one_merchants_run_never_falls_back_into_anothers(shop, tmp_path,
                                                          monkeypatch):
    """
    The fallback is scoped by business, because these runs are held in one
    process-wide dict and the numbers are somebody's money.
    """
    import merchant.app as appmod

    key, _state = _run(shop)

    other = TestClient(appmod.app)
    other.post("/signup", data={"email": "other@x.in", "password": PASSWORD})
    other.post("/businesses", data={"name": "Someone Else Ltd"})

    page = other.get("/agents/three-way").text
    assert "Run reconciliation" in page
    assert "Needs your decision" not in page
