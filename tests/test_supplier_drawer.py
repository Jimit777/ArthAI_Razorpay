"""
Tests for the drawer, the statutory clocks and the two documents.

Two things run through all of it. Every figure a merchant reads was computed
before the model was asked - including the deadline countdowns, which are
statutory rules and must not have a second implementation living in a browser.
And a document is bound to the supplier whose row was clicked, so a notice can
never carry one company's name over another company's invoices.
"""

import json
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.vendor_documents import (CIRCULAR_183, drc01c_defence,  # noqa: E402
                                    vendor_notice, write_case)
from engine.gst.filing_history import Persona, history_for  # noqa: E402
from engine.gst.risk import (STATUS_LATE, STATUS_MISSED,  # noqa: E402
                             STATUS_ON_TIME, STATUS_SILENT,
                             monthly_compliance, statutory_clocks)
from merchant.purchase_import import (SAMPLE_REGISTER,  # noqa: E402
                                      sample_filing_history)

PASSWORD = "a-good-password"


# --- the 36-month grid ----------------------------------------------------

def test_the_grid_is_one_cell_per_period():
    assert len(monthly_compliance(history_for("27X"))) == 36


@pytest.mark.parametrize("persona,expected", [
    (Persona.HONEST, STATUS_ON_TIME),
    (Persona.LATE, STATUS_LATE),
    (Persona.DEFAULTER, STATUS_MISSED),
])
def test_each_persona_colours_the_way_it_behaves(persona, expected):
    grid = monthly_compliance(history_for("27X", persona=persona))
    statuses = [c["status"] for c in grid]
    assert statuses.count(expected) > len(grid) / 2, statuses[:5]


def test_filing_nothing_is_its_own_colour():
    """
    Distinct from "reported and did not pay". One means the invoice never
    reaches your GSTR-2B; the other means it does and the credit still is not
    there. A merchant needs to tell them apart.
    """
    grid = monthly_compliance(history_for("27X", persona=Persona.ERRATIC))
    assert STATUS_SILENT in {c["status"] for c in grid}


def test_every_cell_carries_words_for_its_colour():
    for cell in monthly_compliance(history_for("27X")):
        assert cell["label"] and cell["period"]


# --- the statutory clocks -------------------------------------------------

def test_rule_37_counts_180_days_from_the_invoice():
    today = date(2026, 8, 25)
    clocks = statutory_clocks(
        [{"invoice_number": "A/1", "invoice_date": "2026-01-10",
          "total_tax": 21_600}], today=today)
    row = clocks["invoices"][0]
    assert row["rule_37_due"] == "2026-07-09"
    assert row["rule_37_breached"] is True


def test_an_invoice_inside_the_window_is_not_breached():
    clocks = statutory_clocks(
        [{"invoice_number": "A/1", "invoice_date": "2026-08-01",
          "total_tax": 10_800}], today=date(2026, 8, 25))
    assert clocks["rule_37_breached_count"] == 0
    assert clocks["rule_37_days_left"] > 0


def test_the_section_16_4_deadline_follows_the_financial_year():
    """
    Two invoices a few days apart are nearly a year apart in urgency, which is
    exactly the trap this clock exists to show.
    """
    clocks = statutory_clocks([
        {"invoice_number": "A", "invoice_date": "2026-03-30", "total_tax": 1},
        {"invoice_number": "B", "invoice_date": "2026-04-02", "total_tax": 1},
    ], today=date(2026, 8, 25))
    by_number = {r["invoice_number"]: r for r in clocks["invoices"]}
    assert by_number["A"]["claim_deadline"] == "2026-11-30"
    assert by_number["B"]["claim_deadline"] == "2027-11-30"


def test_expired_credit_is_totalled_not_just_counted():
    clocks = statutory_clocks(
        [{"invoice_number": "OLD", "invoice_date": "2024-06-01",
          "total_tax": 5_000}], today=date(2026, 8, 25))
    assert clocks["claim_expired_count"] == 1
    assert clocks["claim_expired_tax"] == 5_000


def test_an_unreadable_date_is_skipped_not_guessed():
    clocks = statutory_clocks([{"invoice_number": "X", "invoice_date": "",
                                "total_tax": 100}])
    assert clocks["invoices"] == []


def test_the_clocks_are_computed_before_the_page_is_built():
    """
    Not in the browser. Date arithmetic on a tax deadline in JavaScript would
    be a second implementation of a statutory rule, untested and free to
    disagree with the one the findings were built from.
    """
    from merchant.purchase_import import parse
    from merchant.risk_pipeline import run

    payload = run(parse(SAMPLE_REGISTER.encode(), "s.csv"),
                  use_agent=False).as_dict()
    for supplier in payload["suppliers"]:
        assert supplier["clocks"]["invoices"]
        assert len(supplier["compliance_grid"]) == 36


# --- the documents --------------------------------------------------------

SUPPLIER = {
    "supplier_name": "Deepak Packaging", "gstin": "29NYOZN7564Z9ZV",
    "at_risk": 69_29_604,
    "invoices": [
        {"invoice_number": "DEE/1190", "invoice_date": "2026-08-02",
         "total_tax": 72_00_000},
        {"invoice_number": "DEE/1204", "invoice_date": "2026-08-21",
         "total_tax": 46_80_000}],
    "profile": {"periods": 36, "gstr1_filed": 36, "gstr3b_filed": 11,
                "sold_but_did_not_pay": 25, "compliance_pct": 30.6,
                "default_rate_pct": 69.4, "registration_status": "active"},
}


def test_the_vendor_notice_cites_the_provision_that_makes_it_the_buyers_problem():
    body = vendor_notice(SUPPLIER).body
    assert "16(2)(c)" in body
    assert "Supreme Court" in body


def test_the_notice_lists_the_actual_invoices():
    body = vendor_notice(SUPPLIER).body
    for invoice in SUPPLIER["invoices"]:
        assert invoice["invoice_number"] in body


def test_the_hold_is_the_credit_at_risk_not_the_whole_invoice():
    """
    Withholding more than the tax at stake turns a compliance conversation
    into a commercial dispute.
    """
    document = vendor_notice(SUPPLIER)
    assert document.amount == SUPPLIER["at_risk"]
    assert document.amount < sum(i["total_tax"] for i in SUPPLIER["invoices"])


def test_the_defence_cites_the_circular_written_for_this_argument():
    body = drc01c_defence(SUPPLIER).body
    assert "183/15/2022" in body
    assert "DRC-01C" in body


def test_the_defence_says_what_the_buyer_did_control():
    """
    The whole argument: every condition under s.16(2) within the recipient's
    control was met, and the one that was not depends on the supplier.
    """
    body = drc01c_defence(SUPPLIER).body
    assert "within our" in body and "control" in body


def test_both_documents_work_without_the_agent():
    """The facts are the useful part. A merchant chasing a defaulter needs the
    invoice list far more than a well-turned sentence."""
    for document in (vendor_notice(SUPPLIER), drc01c_defence(SUPPLIER)):
        assert document.body and document.written_by == "template"
        assert "DEE/1190" in document.body


def test_the_agent_may_not_introduce_a_figure():
    """Checked the same way as everywhere else in this product."""
    class Invents:
        class messages:
            @staticmethod
            def create(**kw):
                class Block:
                    type = "text"
                    text = "They owe us Rs 9,99,999.00 across many periods."

                class Response:
                    content = [Block()]
                return Response()

    text, error = write_case(SUPPLIER, "vendor_notice", client=Invents())
    assert text == ""
    assert "figures from nowhere" in error


# --- the page -------------------------------------------------------------

@pytest.fixture
def shop(tmp_path, monkeypatch):
    import merchant.app as appmod

    monkeypatch.setattr(appmod, "DB", str(tmp_path / "app.db"))
    client = TestClient(appmod.app)
    client.post("/signup", data={"email": "meera@x.in", "password": PASSWORD})
    client.post("/businesses", data={"name": "Meera's Boutique"})
    client.post("/sources/simulator")
    return client


def go_live(client):
    """
    Put this business on live data.

    Written straight into data_sources because the only public route that gets
    there validates real Razorpay credentials against Razorpay, which a test
    cannot and should not do. What is being simulated is the STATE - "this
    merchant is not on the built-in simulator" - which is exactly what the
    three-mode switch reads.
    """
    import merchant.app as appmod
    from merchant.sources import SourceKind, Sources

    with appmod.ledger(None) as led:
        business = led.conn.execute(
            "SELECT business_id FROM businesses LIMIT 1").fetchone()
        Sources(led.conn)._set(business["business_id"], SourceKind.RAZORPAY,
                               "rzp_test_x", "ok", "connected")
    return business["business_id"]


def _analyse(shop, timeout=30):
    import merchant.app as appmod

    # The Demo Mode tab: both halves generated. Uploading a register with no
    # history now refuses rather than falling back to generated records, so
    # this is the flow that exercises the dashboard without inventing data
    # about anyone.
    r = shop.post("/agents/input-credit/demo",
                  data={"use_agent": "no"}, follow_redirects=False)
    key = r.headers["location"].split("key=")[-1]
    deadline = time.time() + timeout
    while time.time() < deadline:
        with appmod._risk_lock:
            state = dict(appmod.RISK_RUNS.get(key) or {})
        if state.get("state") != "running":
            return key, state
        time.sleep(0.05)
    raise AssertionError("the analysis never finished")


def test_supplier_risk_is_the_landing_view(shop):
    """On the simulator the landing view is the demo button, not an upload."""
    page = shop.get("/agents/input-credit").text
    assert "Generate &amp; analyse demo data" in page
    assert "Supplier risk" in page
    # The three stacked upload boxes are gone.
    assert "Upload your purchase register" not in page
    assert "Import GSTR-2B" not in page
    assert "Step 1" not in page


def test_the_tabs_are_the_three_ways_history_arrives(shop):
    """
    The tabs name the one question that actually differs between them: where
    supplier filing history comes from. Everything downstream is identical.
    """
    from merchant.nav import AGENT_ROUTES

    labels = [t.label for t in AGENT_ROUTES["gst_itc"].tabs]
    assert labels == ["Demo Mode", "Without API", "With API"]
    for gone in ("Purchases", "Suppliers", "Setup", "Supplier risk"):
        assert gone not in labels


def test_every_row_opens_a_drawer(shop):
    key, state = _analyse(shop)
    page = shop.get(f"/agents/input-credit?key={key}").text
    suppliers = len(state["payload"]["suppliers"])
    assert page.count('class="clickable"') == suppliers
    assert page.count('class="drawer" id="dr-') == suppliers


def test_each_drawer_carries_a_full_grid(shop):
    """
    Counted by the title attribute, which only grid cells carry - the four
    legend swatches per drawer use the same classes and would otherwise be
    counted as months.
    """
    import re

    key, state = _analyse(shop)
    page = shop.get(f"/agents/input-credit?key={key}").text
    suppliers = len(state["payload"]["suppliers"])
    cells = re.findall(r'<i class="g-[a-z_]+" title=', page)
    assert len(cells) == 36 * suppliers
    assert page.count('<div class="grid36">') == suppliers


def test_the_drawer_shows_both_clocks(shop):
    key, _state = _analyse(shop)
    page = shop.get(f"/agents/input-credit?key={key}").text
    assert "Rule 37" in page
    assert "Section 16(4)" in page


def test_a_document_is_bound_to_the_supplier_whose_row_was_clicked(shop):
    """
    A notice carrying one company's name over another company's invoices would
    be the worst possible output of this feature.
    """
    key, state = _analyse(shop)
    for supplier in state["payload"]["suppliers"][:3]:
        page = shop.post("/agents/input-credit/notice",
                         data={"key": key, "gstin": supplier["gstin"]}).text
        assert supplier["supplier_name"] in page
        for invoice in supplier["invoices"]:
            assert invoice["invoice_number"] in page
        for other in state["payload"]["suppliers"]:
            if other["gstin"] == supplier["gstin"]:
                continue
            for invoice in other["invoices"]:
                assert invoice["invoice_number"] not in page


def test_a_document_for_an_expired_run_says_so_rather_than_guessing(shop):
    response = shop.post("/agents/input-credit/notice",
                         data={"key": "gone", "gstin": "29NYOZN7564Z9ZV"},
                         follow_redirects=False)
    assert "error=" in response.headers["location"]


def test_the_document_page_says_nothing_was_sent(shop):
    key, state = _analyse(shop)
    gstin = state["payload"]["suppliers"][0]["gstin"]
    page = shop.post("/agents/input-credit/defence",
                     data={"key": key, "gstin": gstin}).text
    assert "Nothing has been sent" in page
    assert CIRCULAR_183.split(" dated")[0] in page


# --- the drawer's buttons have to carry the run they belong to ------------

def test_the_drawer_forms_carry_the_run_key(shop):
    """
    Regression. The results view rendered the drawers without the run key, so
    every 'Draft vendor notice' button posted an empty key and came back
    'That analysis is no longer in memory.'

    The existing binding tests all posted the key directly, which is exactly
    why nobody noticed: the handler was fine and the FORM was empty. So this
    reads the key out of the rendered HTML rather than supplying one.
    """
    key, state = _analyse(shop)
    page = shop.get(f"/agents/input-credit?key={key}").text

    assert f'name="key" value="{key}"' in page
    assert 'name="key" value=""' not in page
    # One pair of buttons - notice and defence - for every supplier.
    assert page.count(f'value="{key}"') == 2 * len(state["payload"]["suppliers"])


def test_a_button_in_the_page_actually_works(shop):
    """End to end through the markup, not around it."""
    import re

    key, state = _analyse(shop)
    page = shop.get(f"/agents/input-credit?key={key}").text

    form = re.search(
        r'action="/agents/input-credit/notice".*?'
        r'name="gstin" value="([^"]+)".*?name="key" value="([^"]*)"',
        page, re.S)
    assert form, "no notice form in the page"
    gstin, found_key = form.groups()

    response = shop.post("/agents/input-credit/notice",
                         data={"key": found_key, "gstin": gstin})
    assert response.status_code == 200
    assert "no longer in memory" not in response.text


# --- the drawer is not a simulator privilege ------------------------------

def test_the_drawer_renders_the_same_from_an_uploaded_history(shop):
    """
    Every interactive piece has to work whatever produced the data.

    The grid, the clocks and both documents are built from the standard
    contract, so a mode that fills that contract gets all of them - this is
    the assertion that keeps it that way.
    """
    upload = shop.post(
        "/agents/input-credit/history",
        files={"history": ("history.csv", sample_filing_history().encode(),
                           "text/csv")}, follow_redirects=False)
    assert upload.status_code == 303
    assert "error=" not in upload.headers["location"]

    # The register flow, not the demo one: with history uploaded, a register
    # upload resolves to that history.
    import merchant.app as appmod

    r = shop.post("/agents/input-credit",
                  files={"register": ("r.csv", SAMPLE_REGISTER.encode(),
                                      "text/csv")},
                  data={"use_agent": "no"}, follow_redirects=False)
    key = r.headers["location"].split("key=")[-1]
    deadline = time.time() + 30
    while time.time() < deadline:
        with appmod._risk_lock:
            state = dict(appmod.RISK_RUNS.get(key) or {})
        if state.get("state") != "running":
            break
        time.sleep(0.05)
    assert state["state"] == "done", state
    page = shop.get(f"/agents/input-credit?key={key}").text

    assert state["payload"]["portfolio"]["history_source"] == "file"
    assert state["payload"]["portfolio"]["history_is_demo"] is False
    # The demo warning must be gone now that the data is the merchant's own.
    assert "Do not act on these against a real supplier" not in page

    for supplier in state["payload"]["suppliers"]:
        assert len(supplier["compliance_grid"]) == 36
        assert supplier["clocks"]["invoices"]

    gstin = state["payload"]["suppliers"][0]["gstin"]
    notice = shop.post("/agents/input-credit/notice",
                       data={"key": key, "gstin": gstin})
    assert notice.status_code == 200
    assert "no longer in memory" not in notice.text


def test_an_upload_completes_the_one_time_step(shop):
    """
    The Without API tab stops asking once the one-time effort is done.

    A page that keeps demanding three years of filing history after it has been
    given three years of filing history is a page nobody finishes.
    """
    before = shop.get("/agents/input-credit/without-api").text
    assert "Step 1" in before
    assert "No GST API is configured" in before

    shop.post("/agents/input-credit/history",
              files={"history": ("history.csv", sample_filing_history().encode(),
                                 "text/csv")})
    after = shop.get("/agents/input-credit/without-api").text
    assert "Supplier history on file" in after
    assert "Step 1" not in after
    assert "Upload your purchase register" in after

    shop.post("/agents/input-credit/history/forget")
    assert "Step 1" in shop.get("/agents/input-credit/without-api").text


def test_a_history_upload_survives_a_page_load(shop):
    """Stored, not held for one run. A merchant who assembled three years of
    filing dates is not asked for them again."""
    shop.post("/agents/input-credit/history",
              files={"history": ("history.csv", sample_filing_history().encode(),
                                 "text/csv")})
    page = shop.get("/agents/input-credit/without-api").text
    assert "tax periods" in page
    assert "2023-" in page or "2024-" in page
