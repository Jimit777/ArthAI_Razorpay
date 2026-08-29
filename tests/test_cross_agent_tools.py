"""
Tests for the two tools that connect the cash forecaster to the OTHER live
agents' own findings.

Eliminates a specific criticism: this platform ran four agents that shared a
database and nothing else. A merchant could be told "hold this payout,
relief arrives Tuesday" by the forecaster while the settlement auditor
already knew that Tuesday's money was under an open dispute - and the two
would never compare notes. settlement_status is that comparison.

at_risk_input_credit is the second one, symmetric in spirit and opposite in
direction: before calling a month comfortable, check whether the GST
reconciler found claimed credit the merchant may have to pay back.

No agent calls in this file - it tests the lookups and the wiring around
them, not the model's judgment.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from merchant.cross_agent_tools import build_tools  # noqa: E402
from merchant.ledger import Ledger  # noqa: E402


@pytest.fixture
def led(tmp_path, monkeypatch):
    """A ledger already pointed at one business, and the tool pointed at it."""
    import merchant.cross_agent_tools as cat

    db = tmp_path / "cross.db"
    monkeypatch.setattr(cat, "DB", str(db))

    bootstrap = Ledger(db)
    business_id = bootstrap.businesses.create("Test Boutique")
    bootstrap.close()
    ledger = Ledger(db, business_id)
    yield ledger
    ledger.close()


def _plant(led, business_id, payment_id, run_id="run_1", **overrides):
    """Insert one settlement-audit finding directly - the auditor is tested
    on its own elsewhere; this just needs a row to be found."""
    row = {
        "action": "dispute", "exception_code": "ZERO_MDR_VIOLATION",
        "money_at_stake": 7198, "reasoning": "network MDR charged on UPI",
        "rule_cited": "PSS Act s.10A", "human_reviewed": 0,
        "queued_for_human": 0, "created_at": 0,
        **overrides,
    }
    led.conn.execute(
        "INSERT OR IGNORE INTO business_runs (run_id, business_id, created_at)"
        " VALUES (?,?,0)", (run_id, business_id))
    led.conn.execute(
        "INSERT INTO variances (payment_id, run_id, expected_fee, actual_fee,"
        " expected_tax, actual_tax, delta, money_at_stake, exception_code,"
        " confidence, reasoning, rule_cited, action, human_reviewed,"
        " queued_for_human, created_at)"
        " VALUES (?,?,0,0,0,0,0,?,?,0.9,?,?,?,?,?,?)",
        (payment_id, run_id, row["money_at_stake"], row["exception_code"],
         row["reasoning"], row["rule_cited"], row["action"],
         row["human_reviewed"], row["queued_for_human"], row["created_at"]))
    led.conn.commit()


def _call(business_id, payment_id):
    tools = {t.name: t for t in build_tools(business_id)}
    return json.loads(tools["settlement_status"].call({"payment_id": payment_id}))


def test_no_business_id_no_tool():
    """A demo forecast has no real settlement history behind its ids - the
    tool would only ever say 'nothing found', which reads as a checked fact
    and is really an artifact of two disconnected demo generators."""
    assert build_tools("") == []
    assert build_tools(None) == []


def test_offered_for_a_real_business():
    assert {t.name for t in build_tools("biz_123")} == {
        "settlement_status", "at_risk_input_credit", "recon_status"}


def test_an_unaudited_payment_says_so(led):
    result = _call(led.business_id, "pay_never_audited")
    assert result["audited"] is False
    assert result["payment_id"] == "pay_never_audited"


def test_finds_an_open_dispute(led):
    _plant(led, led.business_id, "pay_abc123")

    result = _call(led.business_id, "pay_abc123")

    assert result["audited"] is True
    assert result["has_unresolved_dispute"] is True
    assert result["at_risk"]["paise"] == 7198
    assert result["findings"][0]["exception_code"] == "ZERO_MDR_VIOLATION"
    assert result["findings"][0]["rule_cited"] == "PSS Act s.10A"


def test_a_resolved_finding_is_not_a_live_dispute(led):
    """A human already looked at this one. It should not read as open."""
    _plant(led, led.business_id, "pay_resolved", human_reviewed=1)

    result = _call(led.business_id, "pay_resolved")

    assert result["audited"] is True
    assert result["has_unresolved_dispute"] is False
    assert result["at_risk"]["paise"] == 0


def test_a_clean_finding_is_not_a_dispute(led):
    """CLEAN and ROUNDING findings exist too - they should not be mistaken
    for money at risk just because the payment was looked at."""
    _plant(led, led.business_id, "pay_clean", action="dismiss",
          exception_code="ROUNDING", money_at_stake=0)

    result = _call(led.business_id, "pay_clean")

    assert result["audited"] is True
    assert result["has_unresolved_dispute"] is False


def test_a_business_cannot_see_another_businesss_dispute(led, tmp_path):
    """The whole reason this tool opens its own scoped connection rather than
    querying variances directly - the same guardrail merchant/ledger.py
    states as its purpose."""
    other_id = led.businesses.create("Someone Else")
    _plant(led, other_id, "pay_shared_id", run_id="run_other")

    result = _call(led.business_id, "pay_shared_id")

    assert result["audited"] is False, (
        "a payment id that only exists under another business leaked through")


def test_a_bad_business_id_fails_closed(monkeypatch, tmp_path):
    """No such business at all - not even a database to open a table in."""
    import merchant.cross_agent_tools as cat

    monkeypatch.setattr(cat, "DB", str(tmp_path / "nothing.db"))
    result = _call("biz_does_not_exist", "pay_x")
    assert result["audited"] is False


def test_the_tool_is_read_only(led):
    """Same guardrail every other tool on this platform proves the same way:
    an actual before/after check, not a name that sounds safe."""
    _plant(led, led.business_id, "pay_readonly_check")
    before = led.conn.execute(
        "SELECT COUNT(*) n FROM variances").fetchone()["n"]

    _call(led.business_id, "pay_readonly_check")

    after = led.conn.execute("SELECT COUNT(*) n FROM variances").fetchone()["n"]
    assert before == after


def test_multiple_findings_on_the_same_payment_all_come_back(led):
    _plant(led, led.business_id, "pay_multi", run_id="run_1", created_at=1)
    _plant(led, led.business_id, "pay_multi", run_id="run_2", created_at=2,
          action="dispute", exception_code="RATE_MISMATCH",
          money_at_stake=500)

    result = _call(led.business_id, "pay_multi")

    assert len(result["findings"]) == 2
    assert result["at_risk"]["paise"] == 7198 + 500


# --- the `found` accumulator: what makes the connection clickable ----------
#
# The tool's JSON answer is for the model. `found` is for the merchant layer -
# it is how a link to the actual settlement finding reaches the cash forecast
# page, instead of the dispute only ever existing as a sentence in the
# agent's prose. See merchant/treasury_pipeline.py and views.py's `_cash_alert`.

def _call_with_found(business_id, payment_id):
    found = []
    tools = {t.name: t for t in build_tools(business_id, found=found)}
    result = json.loads(tools["settlement_status"].call({"payment_id": payment_id}))
    return result, found


def test_an_open_dispute_is_recorded_in_found(led):
    _plant(led, led.business_id, "pay_abc123", run_id="run_x")

    _result, found = _call_with_found(led.business_id, "pay_abc123")

    assert len(found) == 1
    assert found[0]["payment_id"] == "pay_abc123"
    assert found[0]["run_id"] == "run_x"
    assert found[0]["exception_code"] == "ZERO_MDR_VIOLATION"
    assert found[0]["money_at_stake"] == 7198
    assert "7,198" in found[0]["money_at_stake_display"] \
        or "71.98" in found[0]["money_at_stake_display"]


def test_a_resolved_finding_is_not_recorded_in_found(led):
    """Already looked at by a person - nothing left to link to."""
    _plant(led, led.business_id, "pay_resolved", human_reviewed=1)

    _result, found = _call_with_found(led.business_id, "pay_resolved")

    assert found == []


def test_an_unaudited_payment_adds_nothing_to_found(led):
    _result, found = _call_with_found(led.business_id, "pay_never_audited")
    assert found == []


def test_found_is_optional_and_defaults_to_no_tracking(led):
    """Every existing caller that never passed `found` must keep working
    exactly as before - the tool still answers, it just does not report back."""
    _plant(led, led.business_id, "pay_abc123")
    result = _call(led.business_id, "pay_abc123")
    assert result["has_unresolved_dispute"] is True


# --- at_risk_input_credit --------------------------------------------------

def _plant_itc(led, business_id, run_id="itc_run_1", **overrides):
    """Insert one GST reconciliation finding directly - the reconciler is
    tested on its own elsewhere; this just needs a row to be found."""
    row = {
        "supplier_name": "Sundaram Packaging", "invoice_number": "INV-1001",
        "exception_code": "BLOCKED_CREDIT", "money_at_stake": 5000,
        "claim_deadline": "2026-11-30", "created_at": 0,
    }
    row.update(overrides)

    led.conn.execute(
        "INSERT OR IGNORE INTO business_itc_runs (run_id, business_id,"
        " period, n_invoices, created_at) VALUES (?,?,?,1,?)",
        (run_id, business_id, "2026-08", row["created_at"]))
    led.conn.execute(
        "INSERT INTO itc_findings (run_id, business_id, invoice_id,"
        " supplier_name, supplier_gstin, invoice_number, invoice_date,"
        " taxable_value, cgst, sgst, igst, claimed_tax, available_tax, delta,"
        " tolerance, exception_code, action, confidence, reasoning,"
        " rule_cited, decided_by, money_at_stake, queued_for_human,"
        " claim_deadline, days_to_deadline, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,0,0,?,?,?,0,0,?,?,0.9,?,?,?,?,0,?,30,?)",
        (run_id, business_id, "inv_1", row["supplier_name"],
         "29ABCDE1234F1Z5", row["invoice_number"], "2026-08-01",
         row["money_at_stake"], row["money_at_stake"], row["money_at_stake"],
         0, row["exception_code"], "do_not_claim", "blocked credit",
         "s.17(5)", "calculator", row["money_at_stake"],
         row["claim_deadline"], row["created_at"]))
    led.conn.commit()


def _call_credit(business_id):
    tools = {t.name: t for t in build_tools(business_id)}
    return json.loads(tools["at_risk_input_credit"].call({}))


def _call_credit_with_found(business_id):
    found = []
    tools = {t.name: t for t in build_tools(business_id, credit_found=found)}
    result = json.loads(tools["at_risk_input_credit"].call({}))
    return result, found


def test_offered_alongside_settlement_status():
    names = {t.name for t in build_tools("biz_123")}
    assert "at_risk_input_credit" in names


def test_no_itc_run_at_all_reads_clean(led):
    result = _call_credit(led.business_id)
    assert result["checked"] is True
    assert result["at_risk_paise"] == 0


def test_finds_overclaimed_credit(led):
    _plant_itc(led, led.business_id)

    result = _call_credit(led.business_id)

    assert result["checked"] is True
    assert result["at_risk_paise"] == 5000
    assert result["claims"][0]["supplier"] == "Sundaram Packaging"
    assert result["claims"][0]["reason"] == "BLOCKED_CREDIT"


def test_at_risk_credit_that_is_not_yet_claimed_is_not_counted(led):
    """AT_RISK (SUPPLIER_NOT_FILED etc.) is money not arriving, not money
    owed back. Only OVERCLAIMED codes belong on a cash forecast."""
    _plant_itc(led, led.business_id, exception_code="SUPPLIER_NOT_FILED")

    result = _call_credit(led.business_id)

    assert result["at_risk_paise"] == 0


def test_a_clean_claim_is_not_at_risk(led):
    _plant_itc(led, led.business_id, exception_code="CLAIM_CLEAN")
    result = _call_credit(led.business_id)
    assert result["at_risk_paise"] == 0


def test_only_the_latest_itc_run_counts(led):
    """An older run's findings should not still be counted once a newer
    reconciliation exists - otherwise the same invoice's risk could be
    double-counted run after run."""
    _plant_itc(led, led.business_id, run_id="itc_old", created_at=1,
              money_at_stake=9000)
    _plant_itc(led, led.business_id, run_id="itc_new", created_at=2,
              money_at_stake=5000)

    result = _call_credit(led.business_id)

    assert result["at_risk_paise"] == 5000


def test_found_records_the_at_risk_credit(led):
    _plant_itc(led, led.business_id)

    _result, found = _call_credit_with_found(led.business_id)

    assert len(found) == 1
    assert found[0]["at_risk_paise"] == 5000
    assert found[0]["count"] == 1


def test_a_business_cannot_see_another_businesss_credit_risk(led):
    other_id = led.businesses.create("Someone Else")
    _plant_itc(led, other_id, run_id="itc_other")

    result = _call_credit(led.business_id)

    assert result["at_risk_paise"] == 0


def test_the_credit_tool_is_read_only(led):
    _plant_itc(led, led.business_id)
    before = led.conn.execute(
        "SELECT COUNT(*) n FROM itc_findings").fetchone()["n"]

    _call_credit(led.business_id)

    after = led.conn.execute("SELECT COUNT(*) n FROM itc_findings").fetchone()["n"]
    assert before == after


# --- recon_status -----------------------------------------------------------

def _plant_recon(led, business_id, payment_id, run_id="recon_run_1", **overrides):
    """Insert one reconciliation finding directly - the reconciler is tested
    on its own elsewhere; this just needs a row to be found."""
    row = {
        "finding": "MISSING_IN_BANK", "action": "chase", "at_stake": 488200,
        "reasoning": "The gateway settled this and the bank has no record "
                    "of it.",
        "detail": "Settled Rs 4,882.00 on 2026-07-03, no matching credit.",
        "created_at": 0,
    }
    row.update(overrides)

    led.conn.execute(
        "INSERT OR IGNORE INTO business_recon_runs (run_id, business_id,"
        " source, n_records, created_at) VALUES (?,?,'connected',1,?)",
        (run_id, business_id, row["created_at"]))
    led.conn.execute(
        "INSERT INTO recon_findings (run_id, business_id, invoice_id,"
        " txn_id, utr_number, finding, variance, at_stake, action,"
        " reasoning, detail, created_at)"
        " VALUES (?,?,?,?,NULL,?,?,?,?,?,?,?)",
        (run_id, business_id, "INV-9001", payment_id, row["finding"],
         row["at_stake"], row["at_stake"], row["action"], row["reasoning"],
         row["detail"], row["created_at"]))
    led.conn.commit()


def _call_recon(business_id, payment_id):
    tools = {t.name: t for t in build_tools(business_id)}
    return json.loads(tools["recon_status"].call({"payment_id": payment_id}))


def _call_recon_with_found(business_id, payment_id):
    found = []
    tools = {t.name: t for t in build_tools(business_id, recon_found=found)}
    result = json.loads(tools["recon_status"].call({"payment_id": payment_id}))
    return result, found


def test_offered_alongside_the_other_two():
    names = {t.name for t in build_tools("biz_123")}
    assert "recon_status" in names


def test_no_recon_run_at_all_reads_unflagged(led):
    result = _call_recon(led.business_id, "pay_never_reconciled")
    assert result["checked"] is True
    assert result["flagged"] is False


def test_finds_a_flagged_settlement(led):
    _plant_recon(led, led.business_id, "pay_missing123")

    result = _call_recon(led.business_id, "pay_missing123")

    assert result["checked"] is True
    assert result["flagged"] is True
    assert result["findings"][0]["finding"] == "MISSING_IN_BANK"
    assert result["at_risk"]["paise"] == 488200


def test_a_payment_not_in_the_latest_run_is_unflagged(led):
    """Only matched lines are absent from recon_findings, but a payment the
    reconciler never saw at all should read the same way - nothing to flag,
    not an error."""
    _plant_recon(led, led.business_id, "pay_missing123")
    result = _call_recon(led.business_id, "pay_some_other_payment")
    assert result["flagged"] is False


def test_only_the_latest_recon_run_counts(led):
    """An older run's finding should not still be counted once a newer
    reconciliation exists, mirroring the same rule for GST credit risk."""
    _plant_recon(led, led.business_id, "pay_x", run_id="recon_old",
                created_at=1, at_stake=999999)
    _plant_recon(led, led.business_id, "pay_x", run_id="recon_new",
                created_at=2, at_stake=488200)

    result = _call_recon(led.business_id, "pay_x")

    assert result["at_risk"]["paise"] == 488200


def test_found_records_the_recon_flag(led):
    _plant_recon(led, led.business_id, "pay_missing123", run_id="recon_abc")

    _result, found = _call_recon_with_found(led.business_id, "pay_missing123")

    assert len(found) == 1
    assert found[0]["run_id"] == "recon_abc"
    assert found[0]["finding"] == "MISSING_IN_BANK"
    assert found[0]["at_stake"] == 488200


def test_a_business_cannot_see_another_businesss_recon_flags(led):
    other_id = led.businesses.create("Someone Else")
    _plant_recon(led, other_id, "pay_shared", run_id="recon_other")

    result = _call_recon(led.business_id, "pay_shared")

    assert result["flagged"] is False


def test_the_recon_tool_is_read_only(led):
    _plant_recon(led, led.business_id, "pay_readonly")
    before = led.conn.execute(
        "SELECT COUNT(*) n FROM recon_findings").fetchone()["n"]

    _call_recon(led.business_id, "pay_readonly")

    after = led.conn.execute("SELECT COUNT(*) n FROM recon_findings").fetchone()["n"]
    assert before == after
