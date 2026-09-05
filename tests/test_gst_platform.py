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
    driven by what the business is connected to, and the old URL lands on it.
    """
    landing = shop.get("/agents/input-credit").text
    assert "Record a purchase invoice" not in landing
    assert 'name="behaviour"' not in landing
    # This fixture is on the simulator, so the landing view is the demo run.
    assert "Generate &amp; analyse demo data" in landing

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


# --- several supplier behaviours at once ----------------------------------
#
# The switch used to be one fault for the whole book, which produces a register
# where every supplier has the same problem. That demonstrates one finding well
# and the case the agent actually exists for - several kinds of problem side by
# side, needing to be told apart - not at all.

def test_the_control_is_a_multi_select(shop):
    page = shop.get("/data/simulator").text
    assert 'type="checkbox" name="behaviour"' in page
    # And no radio left behind, which would silently limit it to one.
    assert 'type="radio" name="behaviour"' not in page.split(
        "How your suppliers file")[1]


def test_several_behaviours_can_be_stored_at_once(shop):
    import merchant.app as appmod

    shop.post("/settings/suppliers",
              data={"behaviour": ["not_filed", "wrong_gstin", "filed_late"]})
    with appmod.ledger() as led:
        biz = led.businesses.all()[0]["business_id"]
        stored = led.businesses.supplier_behaviours(biz)
    assert {str(b) for b in stored} == {"not_filed", "wrong_gstin", "filed_late"}


def test_a_mixed_setting_produces_a_mixed_register(shop):
    """
    The point of the whole feature, asserted on what the detector finds.

    Ten suppliers, all five behaviours ticked: the register that comes out has
    to contain all five kinds of discrepancy, or the mix is decorative.
    """
    import merchant.app as appmod
    from engine.gst.detector import detect_batch

    shop.post("/settings/suppliers",
              data={"behaviour": ["correct", "not_filed", "wrong_gstin",
                                  "short_reported", "filed_late"]})

    names = ["Anand Textiles", "Kaveri Silk Mills", "Deepak Packaging",
             "Bright Print House", "Coimbatore Yarns", "Nashik Logistics",
             "Surat Fabrics", "Pune Threads", "Ludhiana Wool", "Jaipur Blocks"]
    with appmod.ledger() as led:
        led.business_id = led.businesses.all()[0]["business_id"]
        for name in names:
            led.record_purchase(supplier_name=name, taxable_value=100_000)
        variances = detect_batch(led.build_itc_batch())

    found = {v.signals[0].kind for v in variances if v.signals}
    assert found == {"matched_exactly", "absent_from_2b",
                     "absent_but_similar_elsewhere", "tax_short_in_2b",
                     "filed_in_later_period"}


def test_a_supplier_keeps_its_behaviour_across_invoices(shop):
    """
    The stickiness the mix depends on.

    Filing behaviour belongs to the supplier, not the invoice. A supplier who
    misfiles to another state does it every time, which is exactly why a
    cross-GSTIN search finds them - re-rolling per invoice would leave no
    consistent wrong registration to search for and quietly break the finding.
    """
    import merchant.app as appmod

    shop.post("/settings/suppliers",
              data={"behaviour": ["not_filed", "wrong_gstin",
                                  "short_reported", "filed_late"]})
    with appmod.ledger() as led:
        led.business_id = led.businesses.all()[0]["business_id"]
        for _ in range(4):
            for name in ("Anand Textiles", "Kaveri Silk Mills",
                         "Deepak Packaging"):
                led.record_purchase(supplier_name=name, taxable_value=50_000)

        for name in ("Anand Textiles", "Kaveri Silk Mills", "Deepak Packaging"):
            behaviours = {r["behaviour"] for r in led.conn.execute(
                "SELECT behaviour FROM live_purchases WHERE supplier_name = ?",
                (name,))}
            assert len(behaviours) == 1, f"{name} drifted: {behaviours}"


def test_one_ticked_behaviour_still_applies_to_everybody(shop):
    """The single-choice case is the one-element case, not a separate path."""
    import merchant.app as appmod

    shop.post("/settings/suppliers", data={"behaviour": ["not_filed"]})
    with appmod.ledger() as led:
        led.business_id = led.businesses.all()[0]["business_id"]
        for name in ("A Traders", "B Traders", "C Traders"):
            led.record_purchase(supplier_name=name, taxable_value=50_000)
        assert led.conn.execute(
            "SELECT COUNT(*) n FROM live_gstr2b").fetchone()["n"] == 0


def test_ticking_nothing_means_filing_correctly(shop):
    """A form with no faults selected reads as 'no faults', not as an error."""
    import merchant.app as appmod

    shop.post("/settings/suppliers", data={})
    with appmod.ledger() as led:
        biz = led.businesses.all()[0]["business_id"]
        assert led.businesses.supplier_behaviour(biz) == "correct"


def test_a_legacy_single_value_still_reads(shop):
    """
    Rows written before this feature hold a bare value.

    No migration was run, so this is the compatibility that keeps an existing
    database working - and it is one line of parsing rather than a rewrite of
    live rows.
    """
    import merchant.app as appmod

    with appmod.ledger() as led:
        biz = led.businesses.all()[0]["business_id"]
        led.conn.execute(
            "UPDATE businesses SET supplier_behaviour = 'wrong_gstin'"
            " WHERE business_id = ?", (biz,))
        led.conn.commit()
        assert [str(b) for b in led.businesses.supplier_behaviours(biz)] \
            == ["wrong_gstin"]


def _sold_and_audited(client, timeout=30):
    """A settlement, audited with the deterministic rate card alone, so its
    GST figures exist for gateway_fee_credit to find - no agent, no cost."""
    import time as _time

    import merchant.app as appmod

    client.post("/settings/gateway", data={"behaviour": "correct"})
    client.post("/sale", data={"rupees": "1000.00", "instrument": "upi"})

    with appmod.ledger() as led:
        led.conn.execute("UPDATE live_payments SET captured_at = 1749547200")
        led.conn.execute("UPDATE live_orders SET created_at = 1749547200")
        led.conn.commit()

    run_id = client.post("/settle", follow_redirects=False
                         ).headers["location"].rsplit("/", 1)[-1]
    client.post(f"/audit/{run_id}", data={})
    deadline = _time.time() + timeout
    while _time.time() < deadline:
        if client.get(f"/audit/{run_id}/status").json().get("state") != "running":
            return run_id
        _time.sleep(0.05)
    raise AssertionError("the settlement audit never finished")


def test_gateway_fee_credit_appears_on_the_reconciliation_page(shop):
    """
    The fourth cross-agent connection: a fact the settlement auditor already
    verified, surfaced on the page whose purchase register has never heard
    of it.
    """
    _sold_and_audited(shop)
    _buy(shop, "Anand Textiles")
    key, _state = _reconcile(shop)

    page = shop.get(f"/itc/{key}").text
    assert "Also claimable: GST paid to Razorpay" in page
    assert "correctly charged" in page
    assert 'href="/settlements"' in page


def test_no_settlement_means_no_gateway_fee_credit_card(shop):
    """The common case for a business that has only ever run the GST agent -
    nothing to claim from a settlement audit that has never run."""
    _buy(shop, "Anand Textiles")
    key, _state = _reconcile(shop)

    page = shop.get(f"/itc/{key}").text
    assert "Also claimable: GST paid to Razorpay" not in page


def test_dashboard_summary_matches_hand_computed_totals(shop):
    """
    The Home page's hero card and side panel read this one method - if the
    numbers here are wrong, every merchant's first screen is wrong. Checked
    against a real settlement (sold and audited) plus a clean and an
    at-risk ITC purchase, not a mocked total.
    """
    import merchant.app as appmod

    _sold_and_audited(shop)
    _buy(shop, "Anand Textiles", rupees="1200.00", behaviour="correct")
    _buy(shop, "Deepak Enterprises", rupees="800.00", behaviour="not_filed")
    _reconcile(shop)

    with appmod.ledger() as led:
        led.business_id = led.businesses.all()[0]["business_id"]
        summary = led.dashboard_summary()

        gross = led.conn.execute(
            "SELECT COALESCE(SUM(p.amount),0) p FROM payments p"
            " JOIN business_runs br ON br.run_id = p.run_id"
            " WHERE br.business_id = ?", (led.business_id,)).fetchone()["p"]
        claimed_clean = led.conn.execute(
            "SELECT COALESCE(SUM(claimed_tax),0) c FROM itc_findings"
            " WHERE business_id = ? AND exception_code = 'CLAIM_CLEAN'",
            (led.business_id,)).fetchone()["c"]
        at_risk = led.conn.execute(
            "SELECT COALESCE(SUM(money_at_stake),0) m FROM itc_findings"
            " WHERE business_id = ? AND exception_code = 'SUPPLIER_NOT_FILED'",
            (led.business_id,)).fetchone()["m"]

    assert summary["gross_paise"] == gross > 0
    assert summary["net_paise"] == (summary["gross_paise"] - summary["fee_paise"]
                                    - summary["tax_paise"])
    assert summary["itc_safe_paise"] == claimed_clean > 0
    assert summary["itc_at_risk_paise"] == at_risk > 0


def test_auditing_the_same_batch_twice_does_not_double_the_recoverable(shop):
    """
    A second audit is a second opinion on the same money, not more money.

    Every other figure on this card was deduplicated when repeated imports
    were found inflating gross; recoverable was left summing every variance
    row, so re-auditing one batch doubled the headline number the Insights
    card leads with.
    """
    import merchant.app as appmod

    _sold_and_audited(shop)

    with appmod.ledger() as led:
        led.business_id = led.businesses.all()[0]["business_id"]
        run_id = led.conn.execute(
            "SELECT run_id FROM variances LIMIT 1").fetchone()["run_id"]
        # Make this run's verdicts disputes, so the figure is non-zero and a
        # doubling would actually show.
        led.conn.execute("UPDATE variances SET action = 'dispute',"
                         " money_at_stake = 5000 WHERE run_id = ?", (run_id,))
        led.conn.commit()
        once = led.dashboard_summary()["recoverable_paise"]
        assert once > 0

        # Audit the same payments again - same verdicts, new rows.
        led.conn.execute(
            "INSERT INTO variances (run_id, payment_id, money_at_stake, action)"
            " SELECT run_id, payment_id, money_at_stake, action FROM variances"
            " WHERE run_id = ?", (run_id,))
        led.conn.commit()

        assert led.dashboard_summary()["recoverable_paise"] == once


def test_a_re_audit_can_take_money_off_the_recoverable_figure(shop):
    """
    The point of keeping only the latest verdict: a payment re-judged as a
    dismissal stops being counted. Summing every row could only ever add.
    """
    import merchant.app as appmod

    _sold_and_audited(shop)

    with appmod.ledger() as led:
        led.business_id = led.businesses.all()[0]["business_id"]
        row = led.conn.execute(
            "SELECT run_id, payment_id FROM variances LIMIT 1").fetchone()
        led.conn.execute("UPDATE variances SET action = 'dispute',"
                         " money_at_stake = 5000 WHERE payment_id = ?",
                         (row["payment_id"],))
        led.conn.commit()
        before = led.dashboard_summary()["recoverable_paise"]

        led.conn.execute(
            "INSERT INTO variances (run_id, payment_id, money_at_stake, action)"
            " VALUES (?, ?, 5000, 'dismiss')",
            (row["run_id"], row["payment_id"]))
        led.conn.commit()

        assert led.dashboard_summary()["recoverable_paise"] == before - 5000


def test_dashboard_summary_is_scoped_to_one_business(tmp_path):
    """A second business with no runs at all must see zeros, not the first
    business's totals - the same scoping guarantee every other cross-run
    query in this file carries."""
    from merchant.ledger import Ledger

    led = Ledger(str(tmp_path / "two.db"))
    first = led.businesses.create("Shop One")
    second = led.businesses.create("Shop Two")

    led.business_id = first
    summary_first_empty = led.dashboard_summary()
    led.business_id = second
    summary_second = led.dashboard_summary()
    led.close()

    assert summary_first_empty == summary_second == {
        "gross_paise": 0, "fee_paise": 0, "tax_paise": 0, "net_paise": 0,
        "bank_credited_paise": 0, "recoverable_paise": 0,
        "itc_safe_paise": 0, "itc_at_risk_paise": 0,
        # The counting cards' figures, zero for a business with no runs -
        # a real zero, which the card renders as "nothing here yet".
        "payment_count": 0, "method_count": 0, "method_mix": [],
        # Nothing imported and nothing simulated, so no split to draw.
        "by_source": {},
        "customer_count": 0, "customer_registered": 0,
        "vendor_count": 0, "vendor_overbilled_paise": 0,
    }
