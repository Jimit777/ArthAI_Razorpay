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

import json
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
        def judge_all(self, rows, on_each=None, extra_tools_factory=None):
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


# --- a merchant's own three sources ---------------------------------------
#
# The agent was demo-only for exactly two lines: the ones that called
# generate(). Everything below them - the join, the arithmetic, the agent, the
# dashboard - never knew where the batch came from, which is what made adding
# real data a matter of loading rather than rewriting.

TALLY_INVOICES = (
    b'Voucher No,Party Name,Date,Amount,Status\n'
    b'INV-2026-0001,Sunrise Retail,05-07-2026,"1,20,000.00",Paid\n'
    b'INV-2026-0002,Meridian Traders,06/07/2026,85000,Paid\n'
    b'INV-2026-0003,Kavya Enterprises,07-07-2026,45000,Paid\n')

GATEWAY_REPORT = (
    b'Payment Id,Order Receipt,Amount,Fee,Tax,Settled At,UTR\n'
    b'pay_1,INV-2026-0001,120000,2400,432,07-07-2026,HDFCN0001234567\n'
    b'pay_2,,85000,1700,306,08-07-2026,HDFCN0007654321\n'
    b'pay_3,INV-2026-0003,45000,900,162,09-07-2026,HDFCN0009999999\n')

# HDFC shape: separate Withdrawal/Deposit columns, two-digit years, and one row
# whose reference column holds the BANK's own ref rather than the gateway's.
HDFC_STATEMENT = (
    b'Date,Narration,Chq/Ref No,Withdrawal Amt.,Deposit Amt.,Closing Balance\n'
    b'07/07/26,NEFT-RAZORPAY-SETTLEMENT-HDFCN0001234567,HDFCN0001234567,,117168.00,500000\n'
    b'08/07/26,MB-NEFT-HDFCN0007654321-RZPY,REF99887766,,82994.00,583000\n'
    b'09/07/26,RENT PAYMENT NEFT,,25000.00,,558000\n')


def _upload(shop, kind, field, name, data):
    return shop.post("/agents/three-way/upload",
                     data={"kind": kind},
                     files={field: (name, data, "text/csv")})


def _upload_all(shop):
    _upload(shop, "invoice", "invoices", "tally.csv", TALLY_INVOICES)
    _upload(shop, "settlement", "settlements", "rzp.csv", GATEWAY_REPORT)
    _upload(shop, "bank", "bank", "hdfc.csv", HDFC_STATEMENT)


def _run_real(shop, source="upload", timeout=30):
    import merchant.app as appmod

    response = shop.post("/agents/three-way/run",
                         data={"source": source, "use_agent": "no"},
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


def test_real_files_reach_the_same_dashboard(shop):
    _upload_all(shop)
    key, state = _run_real(shop)
    assert state["state"] == "done", state

    payload = state["payload"]
    assert payload["match_metrics"]["successful_matches_count"] == 2
    assert payload["match_metrics"]["exception_count"] == 1
    # The one line that was settled and never arrived.
    assert payload["exception_list"][0]["finding_type"] == MISSING_IN_BANK

    page = shop.get(f"/agents/three-way/upload?key={key}").text
    assert "auto-reconciled" in page
    assert "Needs your decision" in page


def test_real_data_never_claims_a_measured_accuracy(shop):
    """
    The honesty requirement, and the reason it needed enforcing.

    The demo can state an accuracy because the generator returns an answer
    key. Real data has no key, so there is nothing to measure against -
    printing a percentage there would be exactly the dishonesty this project
    keeps refusing.
    """
    _upload_all(shop)
    key, state = _run_real(shop)

    assert state["payload"]["match_metrics"]["accuracy"] == {}
    page = shop.get(f"/agents/three-way/upload?key={key}").text
    assert "Checked against the answer key" not in page
    # And the demo still does say it.
    demo_key, _ = _run(shop)
    demo_page = shop.get(f"/agents/three-way?key={demo_key}").text
    assert "Checked against the answer key" in demo_page


def test_all_three_sources_are_required(shop):
    """
    A two-way join between books and bank tells a merchant money is missing
    and nothing about where it went, which is the whole reason the gateway is
    in the middle of this. Running one and calling it a three-way
    reconciliation would be the dishonest option.
    """
    _upload(shop, "invoice", "invoices", "tally.csv", TALLY_INVOICES)
    _upload(shop, "bank", "bank", "hdfc.csv", HDFC_STATEMENT)

    page = shop.get("/agents/three-way/upload").text
    assert "Not ready yet" in page
    assert "Gateway settlements" in page

    _key, state = _run_real(shop)
    assert state["state"] == "failed"
    assert "at least one is missing" in state["phase"]


def test_uploads_survive_a_page_load(shop):
    """Assembling three exports is real work. Nobody should be asked twice."""
    _upload_all(shop)
    page = shop.get("/agents/three-way/connected").text

    assert "Ready to reconcile" in page
    assert "3 invoice" in page or "3 records" in page
    # The two sources this tab still asks you for. Settlements are named by
    # the settlement auditor now, not by an uploaded file.
    for name in ("tally.csv", "hdfc.csv"):
        assert name in page


def test_the_upload_tab_is_gone(shop):
    """
    Connected absorbed it: same screen, one more thing to supply.

    Kept as a redirect rather than a 404 so an old bookmark still lands
    somewhere useful.
    """
    hop = shop.get("/agents/three-way/upload", follow_redirects=False)
    assert hop.headers["location"] == "/agents/three-way/connected"
    assert "Upload" not in shop.get("/agents/three-way/connected").text.split(
        "<main")[0]


def test_clearing_removes_all_three(shop):
    _upload_all(shop)
    shop.post("/agents/three-way/forget")
    page = shop.get("/agents/three-way/connected").text
    assert "Not ready yet" in page


# --- the bank statement, which is the awkward source ----------------------

def test_debits_are_ignored_not_reconciled(shop):
    """
    A current account is mostly money going OUT. Keeping those rows would
    flood the exception list with lines that were never meant to match
    anything, which is the fastest way to build a list nobody reads.
    """
    from merchant.recon_import import parse_bank

    result = parse_bank(HDFC_STATEMENT, "hdfc.csv")
    assert len(result.credits) == 2
    assert result.debits_ignored == 1


def test_the_other_common_statement_shape_also_reads():
    """One Amount column with a Dr/Cr flag, and no reference column at all."""
    from merchant.recon_import import parse_bank

    icici = (b"Value Date,Transaction Remarks,Type,Amount(INR)\n"
             b"07-07-2026,MB-NEFT-HDFCN0001234567-RZPY,CR,117168.00\n"
             b"08-07-2026,ATM WDL,DR,2000.00\n")
    result = parse_bank(icici, "icici.csv")

    assert len(result.credits) == 1
    assert result.debits_ignored == 1
    # The UTR was pulled out of the narration, because there is no column.
    assert result.credits[0].utr_number == "HDFCN0001234567"


def test_an_unreadable_date_is_reported_not_replaced_with_today():
    """
    Regression, and it would have been a bad one.

    Two-digit years were missing from the format list, so every HDFC statement
    row fell back to today - which put every credit outside its three-day
    window and had the PARSER manufacture an exception list of its own.
    """
    from merchant.recon_import import parse_bank, parse_invoices

    result = parse_invoices(b"Invoice No,Amount,Date\nINV-1,1000,not-a-date\n",
                            "x.csv")
    assert result.invoices == []
    assert "unreadable date" in result.rows_skipped[0]

    # And the format that caused it now parses.
    dated = parse_bank(HDFC_STATEMENT, "hdfc.csv")
    assert dated.credits[0].transaction_date == date(2026, 7, 7)


def test_a_file_with_the_wrong_columns_is_refused_by_name(shop):
    response = _upload(shop, "invoice", "invoices", "wrong.csv",
                       b"Colour,Size\nred,large\n")
    assert "Missing columns" in response.text


# --- the connected path ---------------------------------------------------

def test_razorpay_recon_rows_become_settlements():
    """
    order_receipt is the merchant's OWN reference for the order, which is what
    makes Pass 1 exact rather than a search.
    """
    from merchant.recon_import import settlements_from_razorpay

    rows = [
        {"type": "payment", "payment_id": "pay_1", "amount": 12000000,
         "fee": 240000, "tax": 43200, "settled_at": 1783000000,
         "settlement_utr": "HDFCN0001234567", "order_receipt": "INV-2026-0001"},
        # Refunds and adjustments belong in a settlement audit, not a
        # three-way match against sales invoices.
        {"type": "refund", "payment_id": "pay_2", "amount": 500000},
        {"type": "adjustment", "entity_id": "adj_1", "amount": 100000},
    ]
    out = settlements_from_razorpay(rows)

    assert len(out) == 1
    assert out[0].txn_id == "pay_1"
    assert out[0].invoice_reference == "INV-2026-0001"
    assert out[0].net_settled == 12000000 - 240000 - 43200


def test_the_connected_tab_takes_settlements_from_the_auditor(shop):
    """
    The settlement side is whatever this platform already holds, however it
    got here - so the tab no longer demands a gateway connection, which used
    to refuse it to businesses that had settlements ready to match.
    """
    page = shop.get("/agents/three-way/connected").text
    assert "settlement auditor" in page
    assert "Use my settlements" in page
    # Still honest that only one of the three is provided for you.
    assert "invoices" in page and "statement" in page


def test_using_settlements_with_none_settled_is_refused(shop):
    response = shop.post("/agents/three-way/use-settlements",
                         follow_redirects=False)
    assert "No settlements yet" in response.headers["location"].replace(
        "%20", " ")


def test_a_pull_without_an_account_is_refused(shop):
    response = shop.post("/agents/three-way/pull",
                         data={"year": "2026", "month": "07"},
                         follow_redirects=False)
    assert "error=" in response.headers["location"]


# --- the agent can widen the search now -----------------------------------
#
# It used to see a page of evidence for one exception and nothing else. Two
# widening searches let it check a hypothesis before recommending it, the
# same shape as the cash forecaster's what_if_delayed.

def test_two_settlements_tied_are_told_apart_from_zero_found():
    """
    THE bug this closes. The matcher correctly refuses to guess between two
    equally-good candidates, and the old detail sentence said "the gateway
    has no settlement for it" - which was false. Something matched; nothing
    was chosen.
    """
    when = date(2026, 7, 1)
    batch = ReconBatch(
        invoices=[Invoice("INV-1", "Sunrise Retail", 500000, when)],
        settlements=[
            Settlement("pay_a", 500000, 11800, 488200, when, utr="U1"),
            Settlement("pay_b", 500000, 11800, 488200, when, utr="U2")],
        bank=[])

    row = reconcile(batch)[0][0]
    assert row.finding == MISSING_IN_GATEWAY
    assert "has no settlement" not in row.detail
    assert "2 settlements match" in row.detail
    assert len(row.tied_candidates) == 2
    assert {c["txn_id"] for c in row.tied_candidates} == {"pay_a", "pay_b"}


def test_a_genuine_zero_still_says_nothing_exists():
    """The other half - a real gap must not start reading as ambiguous."""
    when = date(2026, 7, 1)
    batch = ReconBatch(
        invoices=[Invoice("INV-1", "Sunrise Retail", 500000, when)],
        settlements=[], bank=[])
    row = reconcile(batch)[0][0]
    assert row.tied_candidates == []
    assert "has no settlement" in row.detail


def test_nearby_settlements_finds_what_the_matcher_would_not_auto_pick():
    """
    A settlement nine days out is too far for the automatic window and close
    enough that a person investigating would want to see it.
    """
    from agent.recon_tools import build_tools

    when = date(2026, 7, 1)
    batch = ReconBatch(
        invoices=[Invoice("INV-1", "Sunrise Retail", 500000, when)],
        settlements=[Settlement("pay_far", 500000, 11800, 488200,
                                when + timedelta(days=9))],
        bank=[])
    rows, _ = reconcile(batch)
    row = rows[0]
    assert row.finding == MISSING_IN_GATEWAY

    tools = {t.name: t for t in build_tools(rows, exclude=row)}
    out = json.loads(tools["nearby_settlements"].call(
        {"around_amount_paise": 500000, "around_date": str(when)}))
    assert out["count"] == 1
    assert out["results"][0]["txn_id"] == "pay_far"
    assert out["results"][0]["days_away"] == 9


def test_the_search_tools_are_read_only():
    """
    No claim, no link, no write - a search that returns priced, dated
    possibilities and nothing that could act on what it finds.
    """
    from agent.recon_tools import build_tools

    batch, truth = generate(55)
    rows, _stats = reconcile(batch)
    tools = build_tools(rows)
    names = {t.name for t in tools}
    assert names == {"nearby_settlements", "nearby_bank_credits"}

    before = [(r.finding, r.matched_by, r.detail) for r in rows]
    for t in tools:
        for row in rows[:3]:
            if row.settlement:
                t.call({"around_amount_paise": row.settlement.gross_amount,
                       "around_date": str(row.settlement.settlement_date)})
    after = [(r.finding, r.matched_by, r.detail) for r in rows]
    assert before == after


def test_results_are_sorted_by_closeness_not_by_order():
    from agent.recon_tools import build_tools

    when = date(2026, 7, 1)
    batch = ReconBatch(
        invoices=[Invoice("INV-1", "Sunrise", 500000, when)],
        settlements=[
            Settlement("pay_close", 501000, 0, 501000, when + timedelta(days=2)),
            Settlement("pay_far", 520000, 0, 520000, when + timedelta(days=1)),
        ], bank=[])
    rows, _ = reconcile(batch)
    tools = {t.name: t for t in build_tools(rows, exclude=rows[0])}
    out = json.loads(tools["nearby_settlements"].call(
        {"around_amount_paise": 500000, "around_date": str(when)}))
    assert [r["txn_id"] for r in out["results"]] == ["pay_close", "pay_far"]


def test_an_unparseable_date_is_refused_not_guessed():
    from agent.recon_tools import build_tools

    batch, _ = generate(55)
    rows, _ = reconcile(batch)
    tools = {t.name: t for t in build_tools(rows)}
    out = json.loads(tools["nearby_bank_credits"].call(
        {"around_amount_paise": 1000, "around_date": "not-a-date"}))
    assert "error" in out


def test_the_agent_still_answers_without_a_pool():
    """Tools are an addition to a complete answer, never a precondition."""
    from agent.recon_agent import ClaudeReconAgent

    class Refuses:
        class beta:
            class messages:
                @staticmethod
                def tool_runner(**kw):
                    assert kw["tools"] == [], "no pool means no tools"
                    raise ConnectionError("down")

    batch, _ = generate(55)
    row = next(r for r in reconcile(batch)[0] if not r.resolved)
    verdict = ClaudeReconAgent(client=Refuses()).judge(row)
    assert verdict.action == recommended_action(row)
    assert verdict.tool_calls == []


def test_a_tool_run_reports_its_full_cost():
    """
    Same class of bug the cash forecaster and the settlement agent both had:
    reading usage off only the last turn of a multi-turn call. Fixed here from
    the start rather than shipped and found later.
    """
    from agent.recon_agent import ClaudeReconAgent, ReconJudgment

    def _judgment():
        return ReconJudgment(action="investigate", headline="h",
                             reasoning="r")

    class Turn:
        def __init__(self, i, o, last=False):
            self.usage = type("U", (), {"input_tokens": i, "output_tokens": o,
                                        "cache_read_input_tokens": 0})()
            self.content = []
            self.parsed_output = _judgment() if last else None

    turns = [Turn(2000, 100), Turn(2200, 150, last=True)]

    class Runner:
        def __iter__(self):
            return iter(turns)

        def generate_tool_call_response(self):
            return None

    class Client:
        class beta:
            class messages:
                @staticmethod
                def tool_runner(**_kw):
                    return Runner()

    batch, _ = generate(55)
    row = next(r for r in reconcile(batch)[0] if not r.resolved)
    verdict = ClaudeReconAgent(client=Client()).judge(row, pool=[row])
    assert verdict.input_tokens == 4200
    assert verdict.output_tokens == 250


# --- the settlement cross-check --------------------------------------------

def test_extra_tools_reach_the_model_alongside_the_search_tools():
    """
    settlement_status is built outside this module (it needs database access
    this layer deliberately does not have - see merchant/cross_agent_tools.py)
    and handed in through a factory. Confirm it actually reaches the
    tool_runner call rather than being dropped.
    """
    from agent.recon_agent import ClaudeReconAgent

    seen = {}

    class Captures:
        class beta:
            class messages:
                @staticmethod
                def tool_runner(**kw):
                    seen["tools"] = kw["tools"]
                    raise ConnectionError("down")

    def fake_settlement_status():
        pass
    fake_settlement_status.name = "settlement_status"

    def factory():
        return [fake_settlement_status], []

    batch, _ = generate(55)
    row = next(r for r in reconcile(batch)[0] if not r.resolved)
    ClaudeReconAgent(client=Captures()).judge(
        row, extra_tools_factory=factory)

    names = [getattr(t, "name", t) for t in seen["tools"]]
    assert "settlement_status" in names


def test_the_factory_is_called_fresh_for_every_row():
    """
    judge_all runs several rows in parallel. If they shared one accumulator,
    one payment's dispute could end up attached to a different row's verdict
    - the exact bug a per-row factory exists to prevent.
    """
    from agent.recon_agent import ClaudeReconAgent

    calls = []

    class Refuses:
        class beta:
            class messages:
                @staticmethod
                def tool_runner(**_kw):
                    raise ConnectionError("down")

    def factory():
        calls.append(object())
        return [], []

    batch, _ = generate(55)
    rows = [r for r in reconcile(batch)[0] if not r.resolved][:3]
    ClaudeReconAgent(client=Refuses()).judge_all(
        rows, extra_tools_factory=factory)

    assert len(calls) == len(rows)


def test_a_disputed_finding_is_recorded_and_not_flagged_as_invented():
    """
    Two things at once, same as the treasury regression: the tool's finding
    reaches the verdict for the UI to link to, AND a figure the tool supplied
    is not wrongly treated as a number the model invented - checking only the
    static evidence text would flag it, exactly as it did live before this
    fix.
    """
    from agent.recon_agent import ClaudeReconAgent, ReconJudgment, recommended_action

    tool_json = ('{"payment_id": "pay_x", "audited": true,'
                ' "has_unresolved_dispute": true,'
                ' "at_risk": {"paise": 500000, "display": "Rs 5,000.00"}}')

    def _judgment(row):
        return ReconJudgment(
            action=recommended_action(row), headline="Already flagged elsewhere",
            reasoning="The settlement auditor already has this payment "
                      "under an open dispute worth Rs 5,000.00, so this is "
                      "not a new problem.")

    class ToolUseBlock:
        type = "tool_use"
        name = "settlement_status"

    batch, _ = generate(55)
    row = next(r for r in reconcile(batch)[0] if not r.resolved)

    class Turn:
        def __init__(self, content=None, last=False):
            self.usage = type("U", (), {"input_tokens": 10, "output_tokens": 10,
                                        "cache_read_input_tokens": 0})()
            self.content = content or []
            self.parsed_output = _judgment(row) if last else None

    turns = [Turn([ToolUseBlock()]), Turn([], last=True)]

    class Runner:
        def __init__(self):
            self._served = False

        def __iter__(self):
            return iter(turns)

        def generate_tool_call_response(self):
            if not self._served:
                self._served = True
                return {"content": [{"type": "tool_result",
                                    "content": tool_json}]}
            return None

    class Client:
        class beta:
            class messages:
                @staticmethod
                def tool_runner(**_kw):
                    return Runner()

    def factory():
        return [], [{"payment_id": "pay_x", "run_id": "run_abc",
                     "exception_code": "ZERO_MDR_VIOLATION",
                     "money_at_stake": 500000,
                     "money_at_stake_display": "Rs 5,000.00"}]

    verdict = ClaudeReconAgent(client=Client()).judge(
        row, extra_tools_factory=factory)

    assert verdict.corrections == [], (
        "a figure the tool supplied was wrongly treated as invented")
    assert len(verdict.disputed_findings) == 1
    assert verdict.disputed_findings[0]["run_id"] == "run_abc"


def _fake_recon_agent(seen):
    """A stand-in agent whose judge_all just records what it was given -
    for pipeline-level tests that only care about the wiring, not the
    model's judgment."""
    from agent.recon_agent import ReconVerdict, _key

    class FakeAgent:
        def judge_all(self, rows, on_each=None, extra_tools_factory=None):
            seen["extra_tools_factory"] = extra_tools_factory
            out = []
            for row in rows:
                out.append(ReconVerdict(key=_key(row), action=row.action,
                                        headline="", reasoning=row.detail))
            return out

    return FakeAgent()


def test_a_demo_run_never_gets_the_cross_agent_tool():
    """
    A demo run's txn_ids were generated fresh for that scenario - nothing in
    the settlement tables has ever heard of them. See
    cross_agent_tools.build_tools().
    """
    seen = {}
    batch, _ = generate(55)
    run(batch, use_agent=True, agent=_fake_recon_agent(seen),
       source="demo", business_id="biz_1")
    assert seen["extra_tools_factory"] is None


def test_a_connected_run_is_offered_the_cross_agent_tool():
    seen = {}
    batch, _ = generate(55)
    run(batch, use_agent=True, agent=_fake_recon_agent(seen),
       source="connected", business_id="biz_1")

    factory = seen["extra_tools_factory"]
    assert factory is not None
    tools, found = factory()
    assert {t.name for t in tools} == {"settlement_status"}
    assert found == []


def test_a_connected_run_with_no_business_gets_no_extra_tool():
    seen = {}
    batch, _ = generate(55)
    run(batch, use_agent=True, agent=_fake_recon_agent(seen),
       source="connected", business_id="")
    assert seen["extra_tools_factory"] is None


def test_disputed_findings_are_attached_to_the_row(tmp_path, monkeypatch):
    """
    The whole reason the factory exists: after judge_all() calls the tool and
    returns, the pipeline has to read back what each row's own call found and
    attach it to that row - or the merchant never sees a link to the actual
    settlement finding, only a sentence in the agent's prose.
    """
    import merchant.cross_agent_tools as cat
    from agent.recon_agent import ReconVerdict, _key
    from merchant.ledger import Ledger

    db = tmp_path / "recon_found.db"
    monkeypatch.setattr(cat, "DB", str(db))

    bootstrap = Ledger(db)
    business_id = bootstrap.businesses.create("Test Co")
    bootstrap.close()

    batch, _ = generate(55)
    rows, _stats = reconcile(batch)
    exceptions = [r for r in rows if not r.resolved]
    probe = exceptions[0]
    payment_id = probe.settlement.txn_id if probe.settlement else "pay_none"

    led = Ledger(db, business_id)
    led.conn.execute(
        "INSERT INTO business_runs (run_id, business_id, created_at)"
        " VALUES ('run_recon_x', ?, 0)", (business_id,))
    led.conn.execute(
        "INSERT INTO variances (payment_id, run_id, expected_fee, actual_fee,"
        " expected_tax, actual_tax, delta, money_at_stake, exception_code,"
        " confidence, reasoning, rule_cited, action, human_reviewed,"
        " queued_for_human, created_at)"
        " VALUES (?,'run_recon_x',0,0,0,0,0,300000,'ZERO_MDR_VIOLATION',0.9,"
        " 'network MDR on UPI','PSS Act s.10A','dispute',0,0,0)",
        (payment_id,))
    led.conn.commit()
    led.close()

    class RealToolAgent:
        """Actually calls the offered tool, without a live model."""
        def judge_all(self, rows, on_each=None, extra_tools_factory=None):
            out = []
            for row in rows:
                row_payment_id = (row.settlement.txn_id if row.settlement
                                  else None)
                if row_payment_id == payment_id and extra_tools_factory is not None:
                    tools, found = extra_tools_factory()
                    tools[0].call({"payment_id": payment_id})
                    out.append(ReconVerdict(key=_key(row), action=row.action,
                                            headline="", reasoning=row.detail,
                                            disputed_findings=found))
                else:
                    out.append(ReconVerdict(key=_key(row), action=row.action,
                                            headline="", reasoning=row.detail))
            return out

    result = run(batch, use_agent=True, agent=RealToolAgent(),
                source="connected", business_id=business_id)

    found_row = next(r for r in result.exceptions
                     if r.settlement and r.settlement.txn_id == payment_id)
    assert len(found_row.disputed_findings) == 1
    assert found_row.disputed_findings[0]["run_id"] == "run_recon_x"

    payload = result.as_dict()
    matching = [r for r in payload["exception_list"]
               if r["txn_id"] == payment_id]
    assert matching[0]["disputed_findings"][0]["run_id"] == "run_recon_x"


def test_what_it_searched_reaches_the_row_and_the_payload(shop):
    """
    An agent that widened the search and one that guessed produce identical
    prose. The page has to be able to tell them apart.
    """
    import merchant.app as appmod

    key, state = _run(shop)
    payload = state["payload"]
    if payload["exception_list"]:
        payload["exception_list"][0]["tool_calls"] = [
            "nearby_settlements", "nearby_settlements"]
        with appmod._recon_lock:
            appmod.RECON_RUNS[key]["payload"] = payload

        page = shop.get(f"/agents/three-way?key={key}").text
        assert "searched for a settlement" in page
        assert "&times;2" in page


def test_a_disputed_finding_becomes_a_clickable_link(shop):
    """
    The third live cross-agent connection: before saying a settlement line
    is missing or wrong, the reconciler can point at the settlement audit's
    own explanation for it - surfaced as a link, same treatment as the
    other two connections.
    """
    import merchant.app as appmod

    key, state = _run(shop)
    payload = state["payload"]
    if not payload["exception_list"]:
        pytest.skip("this generated batch produced no exceptions to attach to")

    payload["exception_list"][0]["disputed_findings"] = [{
        "payment_id": "pay_9K3fL2xQ1z", "run_id": "run_abcd1234",
        "exception_code": "ZERO_MDR_VIOLATION", "money_at_stake": 500000,
        "money_at_stake_display": "Rs 5,000.00",
    }]
    with appmod._recon_lock:
        appmod.RECON_RUNS[key]["payload"] = payload

    page = shop.get(f"/agents/three-way?key={key}").text
    assert "Already under dispute in your settlement audit" in page
    assert "Rs 5,000.00" in page
    assert 'href="/agents/settlement/run/run_abcd1234"' in page


def test_no_disputed_finding_means_no_extra_note(shop):
    key, state = _run(shop)
    page = shop.get(f"/agents/three-way?key={key}").text
    assert "Already under dispute in your settlement audit" not in page


# --- persisting findings, so the cash forecaster can check them later -----
#
# Settlement and GST findings both survive a restart already. Recon's used to
# live only in the run-state dict - fine for the page a person is looking at,
# not enough for a different agent to check later. See
# merchant/cross_agent_tools.py's recon_status and Ledger.record_recon_findings.

def test_recon_findings_persist_across_a_restart(tmp_path):
    from merchant.ledger import Ledger

    bootstrap = Ledger(tmp_path / "recon_store.db")
    business_id = bootstrap.businesses.create("Test Boutique")
    bootstrap.close()

    led = Ledger(tmp_path / "recon_store.db", business_id)
    batch, _truth = generate(55)
    rows, _stats = reconcile(batch)
    exception_count = len([r for r in rows if not r.resolved])

    run_id = led.commit_recon_run("connected", len(rows))
    led.record_recon_findings(run_id, rows)
    led.close()

    later = Ledger(tmp_path / "recon_store.db", business_id)
    latest = later.latest_recon_run()
    assert latest is not None
    assert latest["run_id"] == run_id

    stored = later.conn.execute(
        "SELECT * FROM recon_findings WHERE run_id = ?", (run_id,)).fetchall()
    assert len(stored) == exception_count
    later.close()


def test_matched_lines_are_not_stored(tmp_path):
    """Nothing downstream ever needs to ask 'was this payment ever clean' -
    only whether the reconciler flagged it."""
    from merchant.ledger import Ledger

    bootstrap = Ledger(tmp_path / "recon_store2.db")
    business_id = bootstrap.businesses.create("Test Boutique")
    bootstrap.close()

    led = Ledger(tmp_path / "recon_store2.db", business_id)
    batch, _truth = generate(55)
    rows, _stats = reconcile(batch)
    matched_txn_ids = {r.settlement.txn_id for r in rows
                       if r.resolved and r.settlement}

    run_id = led.commit_recon_run("connected", len(rows))
    led.record_recon_findings(run_id, rows)

    stored_txn_ids = {r["txn_id"] for r in led.conn.execute(
        "SELECT txn_id FROM recon_findings WHERE run_id = ?", (run_id,))}
    assert not (stored_txn_ids & matched_txn_ids), \
        "a matched line was stored as though it were an exception"
    led.close()


def test_recon_findings_are_scoped_to_one_business(tmp_path):
    from merchant.ledger import Ledger

    db = tmp_path / "recon_store3.db"
    bootstrap = Ledger(db)
    a = bootstrap.businesses.create("A Co")
    b = bootstrap.businesses.create("B Co")
    bootstrap.close()

    batch, _truth = generate(55)
    rows, _stats = reconcile(batch)

    la = Ledger(db, a)
    run_id = la.commit_recon_run("connected", len(rows))
    la.record_recon_findings(run_id, rows)
    la.close()

    lb = Ledger(db, b)
    assert lb.latest_recon_run() is None
    seen = lb.conn.execute(
        "SELECT COUNT(*) n FROM recon_findings WHERE business_id = ?",
        (b,)).fetchone()["n"]
    assert seen == 0
    lb.close()


def test_a_reconciliation_stores_what_it_concluded(shop):
    """End to end, through the real route: a run over connected data leaves
    a queryable record, not just an entry in the in-memory run dict."""
    import merchant.app as appmod

    key, state = _run(shop)
    n_exceptions = len(state["payload"]["exception_list"])

    with appmod.ledger(None) as led:
        biz = led.businesses.all()[0]["business_id"]
        led.business_id = biz
        latest = led.latest_recon_run()
        assert latest is not None
        stored = led.conn.execute(
            "SELECT COUNT(*) n FROM recon_findings WHERE run_id = ?",
            (latest["run_id"],)).fetchone()["n"]
    assert stored == n_exceptions
