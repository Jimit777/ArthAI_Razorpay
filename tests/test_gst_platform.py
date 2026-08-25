"""
Tests for the GST reconciler as a deployed agent rather than as an engine.

The engine tests prove it reaches the right conclusion on a generated batch.
These prove a merchant can actually feed it their own data - which is a
different claim, and the one that was missing when the agent was measurable but
not usable.
"""

import sys
import time
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent))

from engine.gst.detector import detect_batch  # noqa: E402
from engine.gst.taxonomy import ITCCode  # noqa: E402
from merchant.ledger import Ledger  # noqa: E402
from merchant.suppliers import (BEHAVIOUR_FINDS, SupplierBehaviour,  # noqa: E402
                                current_period, file_invoice, gstin_for)

PASSWORD = "a-good-password"


@pytest.fixture
def client(tmp_path, monkeypatch):
    import merchant.app as appmod

    monkeypatch.setattr(appmod, "DB", str(tmp_path / "app.db"))
    return TestClient(appmod.app)


@pytest.fixture
def shop(client):
    client.post("/signup", data={"email": "meera@x.in", "password": PASSWORD})
    client.post("/businesses", data={"name": "Meera's Boutique"})
    client.post("/sources/simulator")
    return client


def _buy(client, supplier="Anand Textiles", rupees="1200.00",
         behaviour="correct", category="", paid="yes", interstate="no"):
    """
    Put a purchase in the books, with the simulated suppliers set to
    `behaviour` first.

    Goes through the ledger rather than a form: the manual purchase screen was
    removed when the workflow became file-driven, and these tests are about
    what the engine concludes, not about how a row got there.
    """
    import merchant.app as appmod
    from merchant.suppliers import SupplierBehaviour

    with appmod.ledger() as led:
        led.business_id = led.businesses.all()[0]["business_id"]
        led.record_purchase(
            supplier_name=supplier,
            taxable_value=int(round(float(rupees) * 100)),
            rate_bps=1800, interstate=(interstate == "yes"),
            behaviour=SupplierBehaviour(behaviour),
            category=category or None, paid=(paid == "yes"))

def _led(tmp_path=None):
    import merchant.app as appmod

    return appmod.ledger()


def _findings(client):
    """Reconcile with the agent off, and return code -> supplier."""
    import merchant.app as appmod

    with appmod.ledger() as led:
        biz = led.businesses.all()[0]["business_id"]
        led.business_id = biz
        batch = led.build_itc_batch()
    return {v.exception_code: v for v in detect_batch(batch)}


# --- the supplier files at the moment the invoice is recorded ------------

def test_recording_a_purchase_makes_the_supplier_file(shop):
    _buy(shop)
    import merchant.app as appmod

    with appmod.ledger() as led:
        assert led.conn.execute(
            "SELECT COUNT(*) n FROM live_purchases").fetchone()["n"] == 1
        assert led.conn.execute(
            "SELECT COUNT(*) n FROM live_gstr2b").fetchone()["n"] == 1


def test_a_supplier_who_does_not_file_leaves_nothing_in_gstr2b(shop):
    _buy(shop, behaviour="not_filed")
    import merchant.app as appmod

    with appmod.ledger() as led:
        assert led.conn.execute(
            "SELECT COUNT(*) n FROM live_gstr2b").fetchone()["n"] == 0


def test_the_supplier_files_before_anything_audits_it(shop):
    """
    The counterparty's behaviour is fixed when the record is created, exactly
    as capture_payment fixes the gateway's fee. An auditor that could influence
    what it audits is not an auditor.
    """
    _buy(shop, behaviour="short_reported")
    import merchant.app as appmod

    with appmod.ledger() as led:
        filed = led.conn.execute("SELECT * FROM live_gstr2b").fetchone()
        booked = led.conn.execute("SELECT * FROM live_purchases").fetchone()
    filed_tax = filed["cgst"] + filed["sgst"] + filed["igst"]
    booked_tax = booked["cgst"] + booked["sgst"] + booked["igst"]
    assert filed_tax < booked_tax


# --- every behaviour produces the finding it promises --------------------

@pytest.mark.parametrize("behaviour,expected", [
    (SupplierBehaviour.CORRECT, ITCCode.CLAIM_CLEAN),
    (SupplierBehaviour.NOT_FILED, ITCCode.SUPPLIER_NOT_FILED),
])
def test_a_behaviour_the_calculator_settles_produces_its_finding(
        shop, behaviour, expected):
    _buy(shop, behaviour=str(behaviour))
    assert str(expected) in _findings(shop)


@pytest.mark.parametrize("behaviour", [
    SupplierBehaviour.WRONG_GSTIN,
    SupplierBehaviour.SHORT_REPORTED,
    SupplierBehaviour.FILED_LATE,
])
def test_a_behaviour_needing_judgment_reaches_the_agent(shop, behaviour):
    """
    These three do not get settled mechanically on purpose - each has more than
    one honest reading, which is the whole reason there is an agent.
    """
    _buy(shop, behaviour=str(behaviour))
    found = _findings(shop)
    assert None in found, f"{behaviour} should have needed judgment"


def test_the_promised_finding_for_each_behaviour_is_a_real_code():
    for behaviour, promise in BEHAVIOUR_FINDS.items():
        if behaviour is SupplierBehaviour.CORRECT:
            continue
        assert promise in {str(c) for c in ITCCode}, \
            f"{behaviour} promises {promise}, which is not a taxonomy code"


# --- the rules that are about the merchant, not the supplier -------------

def test_a_blocked_category_is_caught_even_when_filed_perfectly(shop):
    """The trap: a correctly filed restaurant bill still is not claimable."""
    _buy(shop, supplier="Le Cafe Catering", category="food_beverage",
         behaviour="correct")
    found = _findings(shop)
    assert str(ITCCode.BLOCKED_CREDIT) in found
    assert str(ITCCode.CLAIM_CLEAN) not in found


def test_an_unpaid_supplier_past_180_days_needs_a_reversal(tmp_path):
    led = Ledger(str(tmp_path / "r.db"))
    led.business_id = led.businesses.create("Shop")
    led.record_purchase(supplier_name="Slow Pay Ltd", taxable_value=50_000_00,
                        paid=False,
                        invoice_date=date.today() - timedelta(days=200))
    found = {v.exception_code for v in detect_batch(led.build_itc_batch())}
    assert str(ITCCode.RULE_37_REVERSAL) in found
    led.close()


def test_a_correctly_filed_invoice_is_not_reported_late(shop):
    """
    Regression. The detector hardcoded the synthetic generator's period, so on
    live data every correctly filed invoice looked like a late filing. The
    expected period belongs to the batch.
    """
    _buy(shop, behaviour="correct")
    found = _findings(shop)
    assert str(ITCCode.CLAIM_CLEAN) in found
    assert str(ITCCode.SUPPLIER_LATE_FILED) not in found


# --- scoping -------------------------------------------------------------

def test_a_purchase_belongs_to_one_business_only(tmp_path):
    led = Ledger(str(tmp_path / "two.db"))
    first = led.businesses.create("Shop One")
    second = led.businesses.create("Shop Two")

    led.business_id = first
    led.record_purchase(supplier_name="Anand", taxable_value=10_000_00)
    assert len(led.purchases()) == 1

    led.business_id = second
    assert led.purchases() == []
    assert led.build_itc_batch() is None
    led.close()


# --- the run ------------------------------------------------------------

def test_reconciling_with_nothing_to_reconcile_is_refused(shop):
    r = shop.post("/itc/run", follow_redirects=False)
    assert "error=" in r.headers["location"]


def test_reconciling_with_the_agent_switched_off_is_refused(shop):
    _buy(shop)
    shop.post("/agents/gst_itc/toggle")
    r = shop.post("/itc/run", follow_redirects=False)
    assert "error=" in r.headers["location"]


def test_a_reconciliation_runs_and_reports_what_it_found(shop):
    import merchant.app as appmod

    _buy(shop, supplier="Kaveri Silk", behaviour="not_filed")
    _buy(shop, supplier="Le Cafe", category="food_beverage")
    r = shop.post("/itc/run", data={"use_agent": "no"}, follow_redirects=False)
    key = r.headers["location"].rsplit("/", 1)[-1]

    deadline = time.time() + 20
    while time.time() < deadline:
        with appmod._lock:
            state = dict(appmod.RUNS.get(key) or {})
        if state.get("state") != "running":
            break
        time.sleep(0.05)

    assert state["state"] == "done"
    text = " ".join(l["text"] for l in state["lines"])
    assert "at risk" in text
    assert "should not be claimed" in text
    assert "proposal" in text


def test_a_reconciliation_marks_its_invoices_reconciled(shop):
    import merchant.app as appmod

    _buy(shop)
    r = shop.post("/itc/run", data={"use_agent": "no"}, follow_redirects=False)
    key = r.headers["location"].rsplit("/", 1)[-1]
    deadline = time.time() + 20
    while time.time() < deadline:
        with appmod._lock:
            if (appmod.RUNS.get(key) or {}).get("state") != "running":
                break
        time.sleep(0.05)

    with appmod.ledger() as led:
        led.business_id = led.businesses.all()[0]["business_id"]
        assert led.unreconciled_purchases() == []
        assert led.itc_runs()


# --- the simulator is not the auditor ------------------------------------

def test_the_supplier_simulator_imports_nothing_from_the_engine():
    """
    Same guard the gateway simulator has. If the thing being audited and the
    thing auditing shared code, a demo would only ever be the auditor grading
    its own homework.
    """
    import ast

    source = (Path(__file__).parent.parent / "merchant" / "suppliers.py").read_text()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.module:
            assert not node.module.startswith("engine"), \
                f"suppliers.py imports {node.module}"
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("engine")


def test_a_gstin_is_stable_for_the_same_supplier():
    """Two invoices from one supplier must land under one registration, or a
    duplicate check and a cross-GSTIN search both stop meaning anything."""
    assert gstin_for("Anand Textiles", "27") == gstin_for("Anand Textiles", "27")
    assert gstin_for("Anand Textiles", "27") != gstin_for("Kaveri Silk", "27")


# --- the reconciliation page ---------------------------------------------

def _reconcile(client, timeout=30):
    import merchant.app as appmod

    r = client.post("/itc/run", data={"use_agent": "no"},
                    follow_redirects=False)
    key = r.headers["location"].rsplit("/", 1)[-1]
    deadline = time.time() + timeout
    while time.time() < deadline:
        with appmod._lock:
            state = dict(appmod.RUNS.get(key) or {})
        if state.get("state") != "running":
            return key, state
        time.sleep(0.05)
    raise AssertionError("the reconciliation never finished")


def test_a_reconciliation_stores_what_it_concluded(shop):
    """
    It used to keep nothing: the run narrated its findings into an in-memory
    list of sentences and dropped them. With no structured record the page had
    only the sentences to render, which is why it read as a log.
    """
    import merchant.app as appmod

    _buy(shop, "Kaveri Silk", behaviour="not_filed")
    _buy(shop, "Anand Textiles")
    _reconcile(shop)

    with appmod.ledger() as led:
        led.business_id = led.businesses.all()[0]["business_id"]
        run = led.latest_itc_run()
        assert run is not None
        findings = led.itc_findings(run["run_id"])
    assert len(findings) == 2
    assert all(f["evidence"] for f in findings), "no working to show"


def test_the_page_shows_no_agent_trace(shop):
    """
    "looking up invoice_detail" and "Asking the agent" are interesting to
    whoever built this and noise to whoever owns the money.
    """
    _buy(shop, "Kaveri Silk", behaviour="not_filed")
    key, _state = _reconcile(shop)
    page = shop.get(f"/itc/{key}").text
    for trace in ("looking up", "Asking the agent", "invoice_detail",
                  "find_invoice_number", "supplier_filing_history"):
        assert trace not in page, f"the page still shows: {trace}"


def test_the_page_leads_with_the_numbers(shop):
    _buy(shop, "Kaveri Silk", behaviour="not_filed")
    key, _state = _reconcile(shop)
    page = shop.get(f"/itc/{key}").text
    assert "claimed in your books" in page
    assert "safe to claim" in page
    assert "needs your attention" in page


def test_the_three_headline_figures_add_up(shop):
    """
    Claimed, safe and at-risk are one number split two ways. They used to be
    computed independently and could disagree; a summary whose own figures
    contradict each other is worse than no summary.
    """
    import merchant.app as appmod

    _buy(shop, "Kaveri Silk", "80000", behaviour="not_filed")
    _buy(shop, "Anand Textiles", "50000")
    _buy(shop, "Le Cafe", "6000")
    _reconcile(shop)

    with appmod.ledger() as led:
        led.business_id = led.businesses.all()[0]["business_id"]
        findings = led.itc_findings(led.latest_itc_run()["run_id"])

    claimed = sum(f["claimed_tax"] for f in findings)
    at_risk = sum(abs(f["money_at_stake"] or 0) for f in findings
                  if f["exception_code"] not in ("CLAIM_CLEAN", "ROUNDING"))
    assert 0 <= at_risk <= claimed


def test_a_partial_shortfall_risks_only_the_unsupported_part(shop):
    """
    Claim Rs 216, GSTR-2B supports Rs 108, and you will receive the 108. Only
    108 is at risk. Counting the whole claim overstated the headline by exactly
    the amount the supplier DID report.

    Goes at the gate directly: classifying a partial mismatch needs the agent,
    and this is arithmetic that must hold whether or not the agent ran.
    """
    import merchant.app as appmod
    from engine.gst.detector import detect_batch
    from engine.gst.gate import money_at_stake
    from engine.gst.taxonomy import ITCCode

    _buy(shop, "Anand Textiles", "1200", behaviour="short_reported")
    with appmod.ledger() as led:
        led.business_id = led.businesses.all()[0]["business_id"]
        variance = detect_batch(led.build_itc_batch())[0]

    assert variance.available_tax > 0, "this test needs a partial match"
    stake = money_at_stake(variance, str(ITCCode.AMOUNT_MISMATCH))
    assert stake == abs(variance.delta)
    assert stake < variance.claimed_tax


def test_a_supplier_who_filed_nothing_risks_the_whole_claim(shop):
    """The other side of the same rule: nothing reported, everything at risk."""
    import merchant.app as appmod
    from engine.gst.detector import detect_batch
    from engine.gst.gate import money_at_stake
    from engine.gst.taxonomy import ITCCode

    _buy(shop, "Kaveri Silk", "80000", behaviour="not_filed")
    with appmod.ledger() as led:
        led.business_id = led.businesses.all()[0]["business_id"]
        variance = detect_batch(led.build_itc_batch())[0]

    assert money_at_stake(variance, str(ITCCode.SUPPLIER_NOT_FILED)) \
        == variance.claimed_tax


def test_every_finding_can_show_its_working(shop):
    _buy(shop, "Kaveri Silk", behaviour="not_filed")
    key, _state = _reconcile(shop)
    page = shop.get(f"/itc/{key}").text
    assert "Show the working" in page
    assert "Tolerance before it counts" in page


def test_the_proposal_disclaimer_is_a_banner(shop):
    _buy(shop, "Kaveri Silk", behaviour="not_filed")
    key, _state = _reconcile(shop)
    page = shop.get(f"/itc/{key}").text
    assert 'class="banner brand"' in page
    assert "has been filed, amended or claimed" in page


def test_a_clean_batch_says_so_rather_than_showing_an_empty_table(shop):
    _buy(shop, "Anand Textiles")
    key, _state = _reconcile(shop)
    page = shop.get(f"/itc/{key}").text
    assert "Everything reconciled" in page
    assert "Needs review" not in page


def test_there_is_no_manual_purchase_form_at_all(shop):
    """
    It used to ask a merchant how their supplier files - a demo control in a
    real data-entry screen. The whole form is gone now: the workflow is
    file-driven, and the old URL lands on the upload.
    """
    landing = shop.get("/agents/input-credit").text
    assert "Record a purchase invoice" not in landing
    assert 'name="behaviour"' not in landing
    assert "Upload your purchase register" in landing

    moved = shop.get("/agents/input-credit/purchases", follow_redirects=False)
    assert moved.status_code == 307
    assert moved.headers["location"] == "/agents/input-credit"

def test_the_simulator_is_where_supplier_filing_is_set(shop):
    page = shop.get("/data/simulator").text
    assert "How your suppliers file" in page
    assert 'action="/settings/suppliers"' in page
    assert "belongs to" in page and "simulator" in page


def test_the_simulator_setting_decides_what_suppliers_do(shop):
    """Still the simulator's switch - it just feeds the ledger now, not a form."""
    import merchant.app as appmod
    from merchant.suppliers import SupplierBehaviour

    shop.post("/settings/suppliers", data={"behaviour": "not_filed"})
    with appmod.ledger() as led:
        biz = led.businesses.all()[0]["business_id"]
        led.business_id = biz
        assert led.businesses.supplier_behaviour(biz) == "not_filed"
        led.record_purchase(supplier_name="Kaveri Silk",
                            taxable_value=50_000_00)
        assert led.conn.execute(
            "SELECT COUNT(*) n FROM live_gstr2b").fetchone()["n"] == 0

    shop.post("/settings/suppliers", data={"behaviour": "correct"})
    with appmod.ledger() as led:
        led.business_id = biz
        led.record_purchase(supplier_name="Anand Textiles",
                            taxable_value=50_000_00)
        assert led.conn.execute(
            "SELECT COUNT(*) n FROM live_gstr2b").fetchone()["n"] == 1

def test_an_unknown_behaviour_falls_back_rather_than_erroring(shop):
    shop.post("/settings/suppliers", data={"behaviour": "nonsense"})
    import merchant.app as appmod

    with appmod.ledger() as led:
        biz = led.businesses.all()[0]["business_id"]
        assert led.businesses.supplier_behaviour(biz) == "correct"
